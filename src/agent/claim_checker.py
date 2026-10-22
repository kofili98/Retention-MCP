"""
Deterministic Claim Checker for Reproducible Retention Reports.

Principle:
1. Extract (LLM with structured output schema, or deterministic fallback) -> claims.json
2. Coverage check (regex + allow-list, no LLM) -> verifies every numeric/statistical sentence is claimed
3. Verify (deterministic Python per claim type + method-validity rules) -> PASS / FAIL / UNVERIFIABLE
4. Grade (thresholds) -> gates the report and triggers at most one revision pass
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = Path(__file__).resolve().parent / "coverage_allowlist.json"

PINNED_EXTRACTOR_MODEL = os.getenv("CLAIM_EXTRACTOR_MODEL", "claude-sonnet-4-5-20250929")

# ============================================================================
# CENTRALIZED ROUNDING & TOLERANCE RULE (Single Source of Truth)
# ============================================================================
ROUNDING_TOLERANCE_RULE = {
    "description": (
        "Half-ULP rounding tolerance based on stated decimal places (0.5 * 10^-d), "
        "with a minimum tolerance of 0.05 percentage points for percentages so a "
        "1-decimal stated percentage like 62.6% matches 62.64% (|62.6 - 62.64| = 0.04 <= 0.05)."
    ),
    "min_percentage_point_tolerance": 0.05,
    "integer_exact_tolerance": 1e-6,
    "default_float_tolerance": 0.005,
}


# ============================================================================
# STEP 1: CLAIM SCHEMA & STRUCTURED EXTRACTION
# ============================================================================
ClaimType = Literal["value", "difference", "ci_overlap", "other"]
VerdictType = Literal["PASS", "FAIL", "UNVERIFIABLE"]


class ExtractedClaim(BaseModel):
    claim_id: str = Field(description="Unique sequential identifier such as C1, C2, ...")
    claim_type: ClaimType = Field(
        description="One of 'value', 'difference', 'ci_overlap', or 'other'. Anything that cannot be mapped to tool output paths MUST be 'other'."
    )
    span: str = Field(
        description="Exact verbatim substring from the report markdown containing the claim."
    )
    sentence: str = Field(
        description="Full verbatim sentence or bullet line from the report where the span appears."
    )
    stated_value: str = Field(
        description="The verbatim number, percentage, currency amount, interval (e.g. '61.38-63.92%'), or boolean/status string stated in the span."
    )
    paths: list[str] = Field(
        default_factory=list,
        description=(
            "JSON path(s) into tool_outputs for every number referenced by the claim. "
            "For 'value': [single_path] or [ci_low_path, ci_high_path]. "
            "For 'difference': [path_a, path_b]. "
            "For 'ci_overlap': [ci_low_a, ci_high_a, ci_low_b, ci_high_b]. "
            "For 'other': []."
        ),
    )
    overlaps_claimed: bool | None = Field(
        default=None,
        description="For 'ci_overlap' claims only: True if the report claims the intervals overlap, False if it claims they do not overlap.",
    )

    def to_dict(self) -> dict[str, Any]:
        if hasattr(self, "model_dump"):
            return self.model_dump()
        return self.dict()


class ClaimExtractionOutput(BaseModel):
    claims: list[ExtractedClaim]


EXTRACTION_SYSTEM_PROMPT = """You are a strict, non-judgmental Claim Extractor.
Your job is ONLY to extract quantitative, statistical, and methodological claims from a report into structured JSON.

CRITICAL RULES:
1. List claims and DO NOT judge them. Never decide whether a claim is true, false, accurate, or well-reasoned.
2. Quote the exact verbatim `span` and `sentence` from the report markdown. Do not paraphrase.
3. Provide the exact JSON `paths` into `tool_outputs` for every number in the claim (for example: `health.reconciliation.total_raw_rows`, `churn_summary.selected_threshold_result.inferred_churn_rate`, or `churn_summary.full_sensitivity_table[inactivity_threshold_days=60].inferred_churn_rate`).
4. Anything you cannot map to a concrete path in `tool_outputs` MUST be given `claim_type: "other"` with `paths: []`.
"""


# ============================================================================
# STEP 3A: PATH RESOLVER & NUMBER NORMALIZER
# ============================================================================
_PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[([^\[\]]+)\]")


def resolve_path(data: Any, path: str) -> Any:
    """
    Resolves a dotted/bracketed path against nested dicts and lists.
    Supports:
      - Dict keys: "health.reconciliation.total_raw_rows"
      - List indices: "churn_summary.full_sensitivity_table[0].inferred_churn_rate"
      - Key=Value list selectors: "churn_summary.full_sensitivity_table[inactivity_threshold_days=90].inferred_churn_rate"
    Raises KeyError or IndexError if the path cannot be resolved.
    """
    if not path or not path.strip():
        raise KeyError("Empty path cannot be resolved.")

    current = data
    tokens = _PATH_TOKEN_RE.findall(path.strip())
    if not tokens:
        raise KeyError(f"Malformed path: {path}")

    for key_tok, bracket_tok in tokens:
        if key_tok:
            if isinstance(current, dict) and key_tok in current:
                current = current[key_tok]
            else:
                raise KeyError(f"Key '{key_tok}' not found while resolving '{path}'")
        elif bracket_tok is not None:
            if "=" in bracket_tok:
                field_name, raw_val = [p.strip() for p in bracket_tok.split("=", 1)]
                raw_val_clean = raw_val.strip("'\"")
                if not isinstance(current, list):
                    raise KeyError(f"Selector [{bracket_tok}] applied to non-list in '{path}'")
                matched = None
                for item in current:
                    if isinstance(item, dict) and field_name in item:
                        item_val = item[field_name]
                        if str(item_val) == raw_val_clean:
                            matched = item
                            break
                if matched is None:
                    raise KeyError(f"No list element matched [{bracket_tok}] in '{path}'")
                current = matched
            else:
                idx = int(bracket_tok)
                if not isinstance(current, list) or idx < 0 or idx >= len(current):
                    raise IndexError(f"Index [{idx}] out of bounds while resolving '{path}'")
                current = current[idx]

    return current


_RANGE_RE = re.compile(
    r"^\s*[\[\(\s]*([+-]?\d[\d,]*(?:\.\d+)?)\s*%?\s*(?:[–—\-]|to|,)\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*(%?)\s*[\]\)\s]*$",
    re.IGNORECASE,
)

_NUMBER_RE = re.compile(
    r"^\s*[£$€]?\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*(%?)\s*$"
)


def _count_decimals(num_str: str) -> int:
    if "." not in num_str:
        return 0
    return len(num_str.split(".", 1)[1])


def normalize_number(raw: str | int | float) -> tuple[float, bool, int]:
    """
    Normalizes a formatted number string into (float_value, is_percentage, decimal_places).
    Examples:
      "£1,390,289.84" -> (1390289.84, False, 2)
      "62.6%"         -> (62.6, True, 1)
      "1,067,371"     -> (1067371.0, False, 0)
    """
    if isinstance(raw, bool):
        raise ValueError("Boolean is not a numeric value.")
    if isinstance(raw, int):
        return (float(raw), False, 0)
    if isinstance(raw, float):
        s = f"{raw}"
        return (raw, False, _count_decimals(s))

    text = str(raw).strip().strip("`")
    match = _NUMBER_RE.match(text)
    if not match:
        raise ValueError(f"Cannot normalize '{raw}' as a single number.")

    num_part, pct_sign = match.group(1), match.group(2)
    clean_num = num_part.replace(",", "")
    decimals = _count_decimals(clean_num)
    return (float(clean_num), bool(pct_sign), decimals)


def normalize_range(raw: str) -> tuple[tuple[float, float], bool, int]:
    """
    Normalizes a range/interval string into ((low, high), is_percentage, min_decimal_places).
    Examples:
      "61.38–63.92%"     -> ((61.38, 63.92), True, 2)
      "[55.04%, 57.67%]" -> ((55.04, 57.67), True, 2)
    """
    text = str(raw).strip().strip("`")
    match = _RANGE_RE.match(text)
    if not match:
        raise ValueError(f"Cannot normalize '{raw}' as a numeric range.")

    low_str = match.group(1).replace(",", "")
    high_str = match.group(2).replace(",", "")
    is_pct = bool(match.group(3)) or ("%" in text)
    decimals = min(_count_decimals(low_str), _count_decimals(high_str))
    return ((float(low_str), float(high_str)), is_pct, decimals)


def compute_tolerance(decimals: int, is_percentage: bool) -> float:
    """
    Centralized tolerance calculation from ROUNDING_TOLERANCE_RULE.
    - For integers (decimals == 0 and not percentage): 1e-6 (exact integer match)
    - For percentages: max(0.5 * 10^(-decimals), 0.05) percentage points
    - For standard floats: 0.5 * 10^(-decimals)
    """
    if decimals == 0 and not is_percentage:
        return float(ROUNDING_TOLERANCE_RULE["integer_exact_tolerance"])
    half_ulp = 0.5 * (10.0 ** (-decimals))
    if is_percentage:
        return max(half_ulp, float(ROUNDING_TOLERANCE_RULE["min_percentage_point_tolerance"]))
    return half_ulp


def align_tool_value(tool_val: float, is_percentage: bool, stated_val: float) -> float:
    """
    If the claim states a percentage (e.g. 62.6%) and the tool stores a proportion in [0, 1]
    (e.g. 0.62641), converts the tool proportion to percentage points (62.641).
    """
    if is_percentage and 0.0 <= abs(tool_val) <= 1.0 and abs(stated_val) > 1.0:
        return tool_val * 100.0
    return float(tool_val)


def matches_within_tolerance(
    stated_val: float,
    tool_val: float,
    decimals: int,
    is_percentage: bool,
) -> tuple[bool, float, float, float]:
    """
    Returns (passed, aligned_tool_val, abs_error, tolerance).
    """
    aligned = align_tool_value(float(tool_val), is_percentage, stated_val)
    tol = compute_tolerance(decimals, is_percentage)
    err = abs(stated_val - aligned)
    return (err <= tol + 1e-9, aligned, err, tol)


# ============================================================================
# STEP 3B: METHOD-VALIDITY RULES (Separate from Arithmetic)
# ============================================================================
METHOD_RULES: list[tuple[str, str, str]] = [
    # (pattern on the claim span/sentence, severity, message)
    (
        r"(non-?overlap|don't overlap|do not overlap).*(significan|differ)",
        "WARN",
        "CI overlap is not a valid test of difference, especially for nested samples",
    ),
    (
        r"\b(caus(e|es|ed)|effect of|proves?|because of)\b",
        "FAIL_IF_UNHEDGED",
        "Causal wording on observational data",
    ),
    (
        r"\bsignificant(ly)?\b",
        "FAIL_IF_NO_TEST",
        "Significance claimed but no test result in tool outputs",
    ),
]

CAUSAL_HEDGE_PATTERNS = re.compile(
    r"\b(not|no|never|can't|cannot|doesn't|don't|association|correlat\w*)\b",
    re.IGNORECASE,
)

SIGNIFICANCE_NEGATION_PATTERNS = re.compile(
    r"\b(not\s+(?:statistically\s+)?significant\w*|no\s+(?:statistically\s+)?significant\w*|"
    r"insignificant\w*|without\s+(?:a\s+)?significance)\b",
    re.IGNORECASE,
)

TEST_RESULT_KEYS = {"p_value", "pvalue", "t_stat", "z_stat", "chi2", "test_statistic"}


def _has_statistical_test_in_outputs(tool_outputs: dict, paths: list[str]) -> bool:
    for p in paths:
        if any(k in p.lower() for k in TEST_RESULT_KEYS):
            try:
                val = resolve_path(tool_outputs, p)
                if val is not None:
                    return True
            except (KeyError, IndexError, ValueError):
                pass
    return False


def evaluate_method_rules(
    claim: ExtractedClaim, tool_outputs: dict
) -> list[dict[str, str]]:
    """
    Checks each sentence in a claim's span/sentence against METHOD_RULES.
    Returns a list of triggered findings: [{"severity": "WARN"|"FAIL", "rule_type": ..., "message": ...}]
    """
    candidate_sentences = [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", claim.sentence or claim.span)
        if s.strip()
    ]
    if not candidate_sentences:
        candidate_sentences = [f"{claim.span} {claim.sentence}".strip()]

    findings: list[dict[str, str]] = []

    for sent in candidate_sentences:
        for pattern, rule_severity, message in METHOD_RULES:
            match = re.search(pattern, sent, flags=re.IGNORECASE)
            if not match:
                continue

            if rule_severity == "WARN":
                findings.append(
                    {"severity": "WARN", "rule_type": rule_severity, "message": message}
                )
            elif rule_severity == "FAIL_IF_UNHEDGED":
                if CAUSAL_HEDGE_PATTERNS.search(sent):
                    continue
                findings.append(
                    {"severity": "FAIL", "rule_type": rule_severity, "message": message}
                )
            elif rule_severity == "FAIL_IF_NO_TEST":
                if SIGNIFICANCE_NEGATION_PATTERNS.search(
                    sent
                ) or _has_statistical_test_in_outputs(tool_outputs, claim.paths):
                    continue
                findings.append(
                    {"severity": "FAIL", "rule_type": rule_severity, "message": message}
                )

    return findings


# ============================================================================
# STEP 3C: PER-CLAIM-TYPE VERIFIER REGISTRY
# ============================================================================
def verify_value_claim(claim: ExtractedClaim, tool_outputs: dict) -> dict[str, Any]:
    if not claim.paths:
        return {
            "verdict": "UNVERIFIABLE",
            "reason": "Value claim has no tool_outputs path.",
        }

    if len(claim.paths) == 2:
        try:
            (stated_low, stated_high), is_pct, decimals = normalize_range(claim.stated_value)
            raw_low = float(resolve_path(tool_outputs, claim.paths[0]))
            raw_high = float(resolve_path(tool_outputs, claim.paths[1]))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            return {"verdict": "FAIL", "reason": f"Range resolution/parse error: {exc}"}

        ok_low, aligned_low, err_low, tol = matches_within_tolerance(
            stated_low, raw_low, decimals, is_pct
        )
        ok_high, aligned_high, err_high, _ = matches_within_tolerance(
            stated_high, raw_high, decimals, is_pct
        )
        passed = ok_low and ok_high
        return {
            "verdict": "PASS" if passed else "FAIL",
            "stated": [stated_low, stated_high],
            "expected": [round(aligned_low, 6), round(aligned_high, 6)],
            "max_abs_error": round(max(err_low, err_high), 6),
            "tolerance": tol,
            "reason": (
                f"Interval [{stated_low}, {stated_high}] matched [{aligned_low:.4f}, {aligned_high:.4f}] within ±{tol}."
                if passed
                else f"Interval mismatch: stated [{stated_low}, {stated_high}] vs tool [{aligned_low:.4f}, {aligned_high:.4f}] (tol ±{tol})."
            ),
        }

    try:
        raw_tool_val = resolve_path(tool_outputs, claim.paths[0])
    except (KeyError, IndexError) as exc:
        return {"verdict": "FAIL", "reason": f"Path resolution failed: {exc}"}

    try:
        stated_num, is_pct, decimals = normalize_number(claim.stated_value)
        tool_num = float(raw_tool_val)
        passed, aligned_tool, err, tol = matches_within_tolerance(
            stated_num, tool_num, decimals, is_pct
        )
        return {
            "verdict": "PASS" if passed else "FAIL",
            "stated": stated_num,
            "expected": round(aligned_tool, 6),
            "abs_error": round(err, 6),
            "tolerance": tol,
            "reason": (
                f"Stated {stated_num} matched tool {aligned_tool:.4f} within ±{tol}."
                if passed
                else f"Value mismatch: stated {stated_num} vs tool {aligned_tool:.4f} (abs error {err:.4f} > tol {tol})."
            ),
        }
    except (ValueError, TypeError):
        stated_clean = str(claim.stated_value).strip().strip("`'\"")
        tool_clean = str(raw_tool_val).strip()
        passed = stated_clean.lower() == tool_clean.lower()
        return {
            "verdict": "PASS" if passed else "FAIL",
            "stated": stated_clean,
            "expected": tool_clean,
            "reason": (
                f"Exact status match ('{stated_clean}')."
                if passed
                else f"Status mismatch: stated '{stated_clean}' vs tool '{tool_clean}'."
            ),
        }


def verify_difference_claim(claim: ExtractedClaim, tool_outputs: dict) -> dict[str, Any]:
    if len(claim.paths) != 2:
        return {
            "verdict": "UNVERIFIABLE",
            "reason": "Difference claim requires exactly 2 paths [path_a, path_b].",
        }
    try:
        stated_diff, is_pct, decimals = normalize_number(claim.stated_value)
        raw_a = float(resolve_path(tool_outputs, claim.paths[0]))
        raw_b = float(resolve_path(tool_outputs, claim.paths[1]))
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return {"verdict": "FAIL", "reason": f"Difference resolution error: {exc}"}

    if is_pct and 0.0 <= abs(raw_a) <= 1.0 and 0.0 <= abs(raw_b) <= 1.0:
        val_a, val_b = raw_a * 100.0, raw_b * 100.0
    else:
        val_a, val_b = raw_a, raw_b

    true_diff = val_a - val_b
    comparison_target = abs(true_diff) if stated_diff >= 0 else true_diff
    tol = compute_tolerance(decimals, is_pct)
    err = abs(stated_diff - comparison_target)
    passed = err <= tol + 1e-9

    return {
        "verdict": "PASS" if passed else "FAIL",
        "stated": stated_diff,
        "expected": round(comparison_target, 6),
        "components": [round(val_a, 6), round(val_b, 6)],
        "abs_error": round(err, 6),
        "tolerance": tol,
        "reason": (
            f"Stated difference {stated_diff} matched |{val_a:.4f} - {val_b:.4f}| = {comparison_target:.4f} within ±{tol}."
            if passed
            else f"Difference mismatch: stated {stated_diff} vs |{val_a:.4f} - {val_b:.4f}| = {comparison_target:.4f} (tol ±{tol})."
        ),
    }


def verify_ci_overlap_claim(claim: ExtractedClaim, tool_outputs: dict) -> dict[str, Any]:
    if len(claim.paths) != 4:
        return {
            "verdict": "UNVERIFIABLE",
            "reason": "ci_overlap claim requires 4 paths: [low_1, high_1, low_2, high_2].",
        }
    try:
        low_1 = float(resolve_path(tool_outputs, claim.paths[0]))
        high_1 = float(resolve_path(tool_outputs, claim.paths[1]))
        low_2 = float(resolve_path(tool_outputs, claim.paths[2]))
        high_2 = float(resolve_path(tool_outputs, claim.paths[3]))
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return {"verdict": "FAIL", "reason": f"CI path resolution error: {exc}"}

    if low_1 > high_1 or low_2 > high_2:
        return {"verdict": "FAIL", "reason": "Inverted CI bounds in tool outputs."}

    actual_overlaps = max(low_1, low_2) <= min(high_1, high_2)

    if claim.overlaps_claimed is not None:
        claimed_overlaps = claim.overlaps_claimed
    else:
        text_lower = f"{claim.stated_value} {claim.span}".lower()
        if any(w in text_lower for w in ["non-overlap", "do not overlap", "don't overlap", "disjoint", "false"]):
            claimed_overlaps = False
        else:
            claimed_overlaps = True

    passed = actual_overlaps == claimed_overlaps
    return {
        "verdict": "PASS" if passed else "FAIL",
        "stated_overlaps": claimed_overlaps,
        "actual_overlaps": actual_overlaps,
        "interval_1": [low_1, high_1],
        "interval_2": [low_2, high_2],
        "reason": (
            f"CI overlap status ({actual_overlaps}) matches claim for [{low_1:.4f}, {high_1:.4f}] vs [{low_2:.4f}, {high_2:.4f}]."
            if passed
            else f"CI overlap mismatch: claimed overlap={claimed_overlaps}, but [{low_1:.4f}, {high_1:.4f}] and [{low_2:.4f}, {high_2:.4f}] have overlap={actual_overlaps}."
        ),
    }


def verify_other_claim(claim: ExtractedClaim, tool_outputs: dict) -> dict[str, Any]:
    return {
        "verdict": "UNVERIFIABLE",
        "reason": "Claim is classified as 'other' (unmapped to a deterministic tool output path).",
    }


VERIFIER_REGISTRY: dict[
    str, Callable[[ExtractedClaim, dict], dict[str, Any]]
] = {
    "value": verify_value_claim,
    "difference": verify_difference_claim,
    "ci_overlap": verify_ci_overlap_claim,
    "other": verify_other_claim,
}


def verify_single_claim(claim: ExtractedClaim, tool_outputs: dict) -> dict[str, Any]:
    verifier = VERIFIER_REGISTRY.get(claim.claim_type, verify_other_claim)
    arith_result = verifier(claim, tool_outputs)
    method_findings = evaluate_method_rules(claim, tool_outputs)

    final_verdict: VerdictType = arith_result["verdict"]
    reasons = [arith_result.get("reason", "")]

    for finding in method_findings:
        if finding["severity"] == "FAIL":
            final_verdict = "FAIL"
            reasons.append(f"[METHOD_FAIL: {finding['rule_type']}] {finding['message']}")
        elif finding["severity"] == "WARN":
            reasons.append(f"[METHOD_WARN] {finding['message']}")

    return {
        "claim_id": claim.claim_id,
        "claim_type": claim.claim_type,
        "span": claim.span,
        "sentence": claim.sentence,
        "stated_value": claim.stated_value,
        "paths": claim.paths,
        "arithmetic_verdict": arith_result["verdict"],
        "method_findings": method_findings,
        "verdict": final_verdict,
        "reason": " | ".join(r for r in reasons if r),
        "details": arith_result,
    }


# ============================================================================
# STEP 2: DETERMINISTIC COVERAGE CHECK (Regex + Allow-List, No LLM)
# ============================================================================
def load_coverage_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check_coverage(
    report_md: str,
    claims: list[ExtractedClaim],
    allowlist_path: Path = ALLOWLIST_PATH,
) -> dict[str, Any]:
    """
    Splits report_md into candidate lines/sentences, strips allow-listed tokens/headers,
    and checks that every remaining sentence containing a number or statistical claim
    is covered by at least one extracted claim.
    """
    allowlist = load_coverage_allowlist(allowlist_path)

    cleaned_md = report_md
    for sec_header in allowlist.get("ignore_sections", []):
        if sec_header in cleaned_md:
            cleaned_md = cleaned_md.split(sec_header, 1)[0]

    ignore_regexes = [re.compile(pat) for pat in allowlist.get("ignore_line_regexes", [])]
    allowed_tokens = allowlist.get("allowed_unclaimed_tokens", [])

    required_sentences: list[str] = []
    covered_sentences: list[str] = []
    uncovered_sentences: list[str] = []

    for raw_line in cleaned_md.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(rx.search(line) for rx in ignore_regexes):
            continue

        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z*`])", line) if s.strip()]
        for sent in sentences:
            if any(rx.search(sent) for rx in ignore_regexes):
                continue

            scrubbed = sent
            for tok in allowed_tokens:
                scrubbed = scrubbed.replace(tok, " ")

            has_number = bool(re.search(r"\d", scrubbed))
            has_stat_keyword = bool(
                re.search(r"\b(significant\w*|caus\w*|proves?|effect of)\b", scrubbed, re.IGNORECASE)
            )
            if not (has_number or has_stat_keyword):
                continue

            required_sentences.append(sent)

            is_covered = any(
                (c.span and c.span in sent)
                or (c.sentence and (sent in c.sentence or c.sentence in sent))
                for c in claims
            )
            if is_covered:
                covered_sentences.append(sent)
            else:
                uncovered_sentences.append(sent)

    total_req = len(required_sentences)
    recall = round(len(covered_sentences) / total_req, 6) if total_req > 0 else 1.0

    return {
        "total_numeric_or_stat_sentences": total_req,
        "covered_count": len(covered_sentences),
        "uncovered_count": len(uncovered_sentences),
        "recall": recall,
        "uncovered_sentences": uncovered_sentences,
        "passed": len(uncovered_sentences) == 0,
    }


# ============================================================================
# STEP 1 EXECUTION: LLM STRUCTURED EXTRACTOR + DETERMINISTIC FALLBACK
# ============================================================================
def extract_claims_deterministic_fallback(
    report_md: str, tool_outputs: dict
) -> list[ExtractedClaim]:
    """
    Deterministic regex/rule-based claim extractor used when ANTHROPIC_API_KEY is absent
    (e.g. offline CI/pytest) or as a backup if the API is unreachable.
    Strictly extracts spans and maps them to paths without judging truth.
    """
    cleaned_md = report_md.split("### Cryptographic Run Manifest Stamp", 1)[0]
    claims: list[ExtractedClaim] = []
    cid = 1

    def _add(
        c_type: ClaimType,
        span: str,
        sentence: str,
        val: str,
        paths: list[str],
        overlaps: bool | None = None,
    ) -> None:
        nonlocal cid
        claims.append(
            ExtractedClaim(
                claim_id=f"C{cid}",
                claim_type=c_type,
                span=span,
                sentence=sentence,
                stated_value=val,
                paths=paths,
                overlaps_claimed=overlaps,
            )
        )
        cid += 1

    for raw_line in cleaned_md.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue

        # 1. Health & Reconciliation claims
        m_status = re.search(r"\*\*Status:\*\*\s*`([^`]+)`", line)
        if m_status:
            _add("value", m_status.group(0), line, m_status.group(1), ["health.status"])

        m_rec = re.search(r"Exact Reconciliation:\s*`([^`]+)`", line)
        if m_rec:
            _add(
                "value",
                m_rec.group(0),
                line,
                m_rec.group(1),
                ["health.reconciliation.reconciled_exactly"],
            )

        for label, path in [
            (r"\*\*Raw Rows:\*\*\s*`([\d,]+)`", "health.reconciliation.total_raw_rows"),
            (r"\*\*Excluded Rows:\*\*\s*`([\d,]+)`", "health.reconciliation.total_excluded_rows"),
            (r"\*\*Clean Analysis Rows:\*\*\s*`([\d,]+)`", "health.reconciliation.final_analysis_rows"),
            (r"Missing Customer ID\s*\(`([\d,]+)`\)", "health.reconciliation.exclusions_by_rule.2_MISSING_CUSTOMER_ID"),
            (r"Exact Duplicates\s*\(`([\d,]+)`\)", "health.reconciliation.exclusions_by_rule.1_EXACT_DUPLICATE"),
            (r"Cancellations\s*\(`([\d,]+)`\)", "health.reconciliation.exclusions_by_rule.3_CANCELLATION_INVOICE"),
        ]:
            m = re.search(label, line)
            if m:
                _add("value", m.group(0), line, m.group(1), [path])

        # 2. Churn & CI claims
        m_churn = re.search(r"\*\*90-Day Inferred Churn Rate:\*\*\s*`([\d\.]+%)`", line)
        if m_churn:
            _add(
                "value",
                m_churn.group(0),
                line,
                m_churn.group(1),
                ["churn_summary.selected_threshold_result.inferred_churn_rate"],
            )

        m_ci = re.search(r"95%\s*CI:\s*`(\[[\d\.]+%,\s*[\d\.]+%\])`", line)
        if m_ci:
            _add(
                "value",
                m_ci.group(0),
                line,
                m_ci.group(1),
                [
                    "churn_summary.selected_threshold_result.ci_95_lower",
                    "churn_summary.selected_threshold_result.ci_95_upper",
                ],
            )

        m_uncens = re.search(r"across\s*`([\d,]+)`\s*uncensored customers", line)
        if m_uncens:
            _add(
                "value",
                m_uncens.group(0),
                line,
                m_uncens.group(1),
                ["churn_summary.selected_threshold_result.eligible_uncensored_customers"],
            )

        m_cens = re.search(r"\(`([\d,]+)`\s*right-censored\)", line)
        if m_cens:
            _add(
                "value",
                m_cens.group(0),
                line,
                m_cens.group(1),
                ["churn_summary.selected_threshold_result.right_censored_customers"],
            )

        m_med = re.search(r"Median\s*`(\d+)`\s*days", line)
        if m_med:
            _add(
                "value",
                m_med.group(0),
                line,
                m_med.group(1),
                ["churn_summary.inter_purchase_gap_distribution_days.p50_median"],
            )

        m_p75 = re.search(r"75th percentile\s*`(\d+)`\s*days", line)
        if m_p75:
            _add(
                "value",
                m_p75.group(0),
                line,
                m_p75.group(1),
                ["churn_summary.inter_purchase_gap_distribution_days.p75"],
            )

        # 3. At-Risk claims
        m_ar_cnt = re.search(r"\*\*At-Risk Population:\*\*\s*`([\d,]+)`\s*repeat customers", line)
        if m_ar_cnt:
            _add(
                "value",
                m_ar_cnt.group(0),
                line,
                m_ar_cnt.group(1),
                ["at_risk_summary.total_at_risk_count"],
            )

        m_ar_rev = re.search(r"representing\s*`(£?[\d,]+\.\d{2})`\s*in historical revenue", line)
        if m_ar_rev:
            _add(
                "value",
                m_ar_rev.group(0),
                line,
                m_ar_rev.group(1),
                ["at_risk_summary.total_at_risk_lifetime_revenue"],
            )

        # 4. Shopify claims
        m_shop_tot = re.search(r"`(\d+)`\s*total contracts", line)
        if m_shop_tot:
            _add(
                "value",
                m_shop_tot.group(0),
                line,
                m_shop_tot.group(1),
                ["shopify_snapshot.total_contracts"],
            )

        m_shop_stat = re.search(r"\*\*Integrity Checks\s*\(`([^`]+)`\):\*\*", line)
        if m_shop_stat:
            _add(
                "value",
                m_shop_stat.group(0),
                line,
                m_shop_stat.group(1),
                ["shopify_health.status"],
            )

        m_shop_c0 = re.search(r"Flagged\s*`(\d+)`\s*selling-plan order", line)
        if m_shop_c0:
            _add(
                "value",
                m_shop_c0.group(0),
                line,
                m_shop_c0.group(1),
                ["shopify_health.checks[0].failing_count"],
            )

        m_shop_c1 = re.search(r"and\s*`(\d+)`\s*ACTIVE contract missing", line)
        if m_shop_c1:
            _add(
                "value",
                m_shop_c1.group(0),
                line,
                m_shop_c1.group(1),
                ["shopify_health.checks[1].failing_count"],
            )

        # 5. Methodological / causal / significance claims per individual sentence
        for sent in [s.strip() for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]:
            for pat, _, _ in METHOD_RULES:
                if re.search(pat, sent, re.IGNORECASE):
                    already_on_sent = any(c.sentence == sent or sent in c.sentence for c in claims)
                    if not already_on_sent:
                        _add("other", sent, sent, sent, [])

    return claims


def extract_claims(
    report_md: str,
    tool_outputs: dict,
    use_llm: bool = True,
) -> list[ExtractedClaim]:
    """
    Extracts structured claims from `report_md` referencing paths in `tool_outputs`.
    Uses Anthropic structured outputs (`messages.parse` / `output_config`) at `temperature=0.0`
    with a pinned model when `ANTHROPIC_API_KEY` is configured and `use_llm=True`;
    otherwise falls back to `extract_claims_deterministic_fallback`.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not use_llm or not api_key or api_key.startswith("sk-ant-..."):
        return extract_claims_deterministic_fallback(report_md, tool_outputs)

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        cleaned_report = report_md.split("### Cryptographic Run Manifest Stamp", 1)[0]
        user_prompt = (
            "Extract every numeric, statistical, interval, difference, and methodological claim "
            "from the REPORT MARKDOWN below and map each number to its exact path in TOOL OUTPUTS.\n\n"
            f"REPORT MARKDOWN:\n{cleaned_report}\n\n"
            f"TOOL OUTPUTS:\n{json.dumps(tool_outputs, indent=2)}"
        )

        if hasattr(client.messages, "parse"):
            response = client.messages.parse(
                model=PINNED_EXTRACTOR_MODEL,
                max_tokens=4096,
                temperature=0.0,
                system=EXTRACTION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
                output_format=ClaimExtractionOutput,
            )
            return response.parsed_output.claims

        schema = (
            ClaimExtractionOutput.model_json_schema()
            if hasattr(ClaimExtractionOutput, "model_json_schema")
            else ClaimExtractionOutput.schema()
        )
        response = client.messages.create(
            model=PINNED_EXTRACTOR_MODEL,
            max_tokens=4096,
            temperature=0.0,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[
                {
                    "name": "record_extracted_claims",
                    "description": "Record all extracted claims in structured form.",
                    "input_schema": schema,
                }
            ],
            tool_choice={"type": "tool", "name": "record_extracted_claims"},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                if hasattr(ClaimExtractionOutput, "model_validate"):
                    parsed = ClaimExtractionOutput.model_validate(block.input)
                else:
                    parsed = ClaimExtractionOutput.parse_obj(block.input)
                return parsed.claims
    except Exception as exc:
        print(f"[WARNING] LLM claim extraction failed ({exc}); using deterministic extractor.")

    return extract_claims_deterministic_fallback(report_md, tool_outputs)


# ============================================================================
# STEP 4: GRADER & PIPELINE GATE
# ============================================================================
def grade_report(
    report_md: str,
    tool_outputs: dict,
    claims: list[ExtractedClaim] | None = None,
    use_llm_extractor: bool = True,
    min_coverage_recall: float = 1.0,
    max_unverifiable_count: int = 2,
    allowlist_path: Path = ALLOWLIST_PATH,
) -> dict[str, Any]:
    """
    Runs the full 4-step Claim Checker pipeline:
      1. Extract claims -> list[ExtractedClaim]
      2. Coverage check -> verify every numeric/statistical sentence is claimed
      3. Verify claims -> run VERIFIER_REGISTRY + METHOD_RULES on each claim
      4. Grade against thresholds -> gate the report
    """
    extracted = (
        claims
        if claims is not None
        else extract_claims(report_md, tool_outputs, use_llm=use_llm_extractor)
    )
    coverage = check_coverage(report_md, extracted, allowlist_path=allowlist_path)
    verifications = [verify_single_claim(c, tool_outputs) for c in extracted]

    pass_count = sum(1 for v in verifications if v["verdict"] == "PASS")
    fail_count = sum(1 for v in verifications if v["verdict"] == "FAIL")
    unverifiable_count = sum(1 for v in verifications if v["verdict"] == "UNVERIFIABLE")
    warn_count = sum(
        1
        for v in verifications
        for mf in v.get("method_findings", [])
        if mf.get("severity") == "WARN"
    )

    failures = [v for v in verifications if v["verdict"] == "FAIL"]
    unverifiables = [v for v in verifications if v["verdict"] == "UNVERIFIABLE"]

    passed = (
        fail_count == 0
        and coverage["recall"] >= min_coverage_recall
        and unverifiable_count <= max_unverifiable_count
    )

    return {
        "passed": passed,
        "extractor_model": PINNED_EXTRACTOR_MODEL,
        "rounding_tolerance_rule": ROUNDING_TOLERANCE_RULE,
        "summary": {
            "total_claims": len(verifications),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "unverifiable_count": unverifiable_count,
            "method_warning_count": warn_count,
            "coverage_recall": coverage["recall"],
            "min_coverage_recall_threshold": min_coverage_recall,
            "max_unverifiable_threshold": max_unverifiable_count,
        },
        "coverage": coverage,
        "failures": failures,
        "unverifiables": unverifiables,
        "verifications": verifications,
        "claims": [c.to_dict() for c in extracted],
    }

