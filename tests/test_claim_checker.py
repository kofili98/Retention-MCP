"""
Unit, integration, and mutation tests for the deterministic Claim Checker
following the 5-stage build order:
  1. Path resolver and number normalizer (including rounding tolerance rules)
  2. Verifiers for value, difference, and ci_overlap on real churn/at-risk outputs + METHOD_RULES
  3. Deterministic coverage check with allow-list
  4. Extractor verification against hand-labeled claims
  5. Mutation tests and the pipeline revision gate
"""

import json
from pathlib import Path

try:
    import pytest
except ImportError:
    pytest = None

from agent.claim_checker import (
    ExtractedClaim,
    check_coverage,
    extract_claims,
    grade_report,
    matches_within_tolerance,
    normalize_number,
    normalize_range,
    resolve_path,
    verify_single_claim,
)
import agent.weekly_report as wr

REPO_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
REPORTS_DIR = REPO_ROOT / "reports"


def build_real_tool_outputs() -> dict:
    """Loads the real verified JSON artifacts from data/processed/ into the evidence_bundle shape."""
    health = json.loads((PROCESSED_DIR / "health_report.json").read_text(encoding="utf-8"))
    churn_raw = json.loads((PROCESSED_DIR / "churn_summary.json").read_text(encoding="utf-8"))
    cohorts_raw = json.loads((PROCESSED_DIR / "cohort_retention.json").read_text(encoding="utf-8"))
    at_risk_raw = json.loads((PROCESSED_DIR / "at_risk_customers.json").read_text(encoding="utf-8"))

    sel_90 = [
        r for r in churn_raw["sensitivity_table"] if r["inactivity_threshold_days"] == 90
    ][0]
    return {
        "health": health,
        "churn_summary": {
            "metrics_version": churn_raw["metrics_version"],
            "anchor_date_t_max": churn_raw["anchor_date_t_max"],
            "requested_inactivity_days": 90,
            "selected_threshold_result": sel_90,
            "inter_purchase_gap_distribution_days": churn_raw[
                "inter_purchase_gap_distribution_days"
            ],
            "full_sensitivity_table": churn_raw["sensitivity_table"],
        },
        "cohort_retention_sample": cohorts_raw["groupings"]["buyer_type"][:30],
        "at_risk_summary": {
            "total_at_risk_count": at_risk_raw["total_at_risk_count"],
            "total_at_risk_lifetime_revenue": at_risk_raw["total_at_risk_lifetime_revenue"],
        },
        "shopify_snapshot": {
            "data_source": "shopify_dev_store_sanitized_fixture",
            "total_contracts": 8,
            "status_breakdown": {"ACTIVE": 4, "PAUSED": 2, "CANCELLED": 2},
        },
        "shopify_health": {
            "status": "WARNING",
            "checks": [
                {"rule_id": "SHOPIFY_01_ORPHAN_SELLING_PLAN_ORDER", "failing_count": 1},
                {"rule_id": "SHOPIFY_02_ACTIVE_MISSING_NEXT_BILLING", "failing_count": 1},
            ],
        },
    }


def build_reference_report_md(tool_outputs: dict) -> str:
    """Builds the deterministic reference report matching `tool_outputs` (independent of whether disk report used live 0-contract Shopify store or fixture)."""
    manifest = json.loads((PROCESSED_DIR / "run_manifest.json").read_text(encoding="utf-8"))
    narrative = wr._build_deterministic_narrative(
        tool_outputs["health"],
        tool_outputs["churn_summary"],
        tool_outputs["at_risk_summary"],
        tool_outputs["shopify_snapshot"],
        tool_outputs["shopify_health"],
    )
    manifest_stamp = (
        "\n\n---\n### Cryptographic Run Manifest Stamp\n"
        f"```json\n{json.dumps(manifest, indent=2)}\n```\n"
    )
    return f"# Reproducible Retention Report\n\n{narrative}{manifest_stamp}"


if pytest is not None:
    real_tool_outputs = pytest.fixture(scope="module")(build_real_tool_outputs)


# ============================================================================
# 1. PATH RESOLVER & NUMBER NORMALIZER UNIT TESTS
# ============================================================================
def test_path_resolver_indices_and_selectors(real_tool_outputs):
    assert (
        resolve_path(real_tool_outputs, "health.reconciliation.total_raw_rows") == 1067371
    )
    assert (
        resolve_path(
            real_tool_outputs,
            "churn_summary.full_sensitivity_table[0].inferred_churn_rate",
        )
        == 0.62641
    )
    assert (
        resolve_path(
            real_tool_outputs,
            "churn_summary.full_sensitivity_table[inactivity_threshold_days=60].ci_95_lower",
        )
        == 0.613764
    )
    try:
        resolve_path(real_tool_outputs, "health.reconciliation.non_existent_key")
        assert False, "Expected KeyError for non_existent_key"
    except KeyError:
        pass


def test_number_and_range_normalizer_and_tolerance():
    # "61.38–63.92%" -> ((61.38, 63.92), True, 2)
    bounds, is_pct, dec = normalize_range("61.38–63.92%")
    assert bounds == (61.38, 63.92)
    assert is_pct is True
    assert dec == 2

    # "£1,390,289.84" -> (1390289.84, False, 2)
    val, is_pct_curr, dec_curr = normalize_number("£1,390,289.84")
    assert val == 1390289.84
    assert is_pct_curr is False
    assert dec_curr == 2

    # Tolerance rule: stated 62.6% matches 62.64% (0.62641) within 0.05 percentage points
    passed, aligned, err, tol = matches_within_tolerance(
        stated_val=62.6, tool_val=0.62641, decimals=1, is_percentage=True
    )
    assert passed is True
    assert round(aligned, 3) == 62.641
    assert err <= 0.05

    # Stated 62.5% should fail against 62.641% (error = 0.141 > 0.05)
    passed_bad, _, _, _ = matches_within_tolerance(
        stated_val=62.5, tool_val=0.62641, decimals=1, is_percentage=True
    )
    assert passed_bad is False


# ============================================================================
# 2. VERIFIERS (value, difference, ci_overlap) & METHOD_RULES ON REAL CHURN DATA
# ============================================================================
def test_verifiers_on_real_churn_and_at_risk_output(real_tool_outputs):
    # Value claim: 60-day churn rate stated as 62.6% vs tool 0.62641
    c_val = ExtractedClaim(
        claim_id="C1",
        claim_type="value",
        span="60-day churn is 62.6%",
        sentence="At the 60-day threshold, 60-day churn is 62.6%.",
        stated_value="62.6%",
        paths=["churn_summary.full_sensitivity_table[inactivity_threshold_days=60].inferred_churn_rate"],
    )
    res_val = verify_single_claim(c_val, real_tool_outputs)
    assert res_val["verdict"] == "PASS"

    # Value range claim: 60-day 95% CI "61.38–63.92%" vs (0.613764, 0.639237)
    c_range = ExtractedClaim(
        claim_id="C2",
        claim_type="value",
        span="95% CI of 61.38–63.92%",
        sentence="The 60-day threshold has a 95% CI of 61.38–63.92%.",
        stated_value="61.38–63.92%",
        paths=[
            "churn_summary.full_sensitivity_table[0].ci_95_lower",
            "churn_summary.full_sensitivity_table[0].ci_95_upper",
        ],
    )
    res_range = verify_single_claim(c_range, real_tool_outputs)
    assert res_range["verdict"] == "PASS"

    # Difference claim: 60-day churn (62.641%) minus 90-day churn (56.4498%) = 6.19%
    c_diff = ExtractedClaim(
        claim_id="C3",
        claim_type="difference",
        span="drops by 6.19%",
        sentence="Moving from a 60-day to a 90-day threshold drops by 6.19%.",
        stated_value="6.19%",
        paths=[
            "churn_summary.full_sensitivity_table[inactivity_threshold_days=60].inferred_churn_rate",
            "churn_summary.full_sensitivity_table[inactivity_threshold_days=90].inferred_churn_rate",
        ],
    )
    res_diff = verify_single_claim(c_diff, real_tool_outputs)
    assert res_diff["verdict"] == "PASS"

    # CI overlap claim + METHOD_RULES warn: 60d CI [0.6138, 0.6392] vs 90d CI [0.5504, 0.5767] do not overlap
    c_overlap = ExtractedClaim(
        claim_id="C4",
        claim_type="ci_overlap",
        span="the 95% CIs do not overlap, indicating a significant difference",
        sentence="Between 60d and 90d, the 95% CIs do not overlap, indicating a significant difference.",
        stated_value="do not overlap",
        paths=[
            "churn_summary.full_sensitivity_table[inactivity_threshold_days=60].ci_95_lower",
            "churn_summary.full_sensitivity_table[inactivity_threshold_days=60].ci_95_upper",
            "churn_summary.full_sensitivity_table[inactivity_threshold_days=90].ci_95_lower",
            "churn_summary.full_sensitivity_table[inactivity_threshold_days=90].ci_95_upper",
        ],
        overlaps_claimed=False,
    )
    res_overlap = verify_single_claim(c_overlap, real_tool_outputs)
    # Arithmetic CI check passes (they truly are disjoint), but METHOD_RULES triggers WARN and FAIL_IF_NO_TEST!
    assert res_overlap["arithmetic_verdict"] == "PASS"
    assert any(m["severity"] == "WARN" for m in res_overlap["method_findings"])
    assert res_overlap["verdict"] == "FAIL"


def test_method_validity_hedged_vs_unhedged(real_tool_outputs):
    # Hedged negation ("inactivity does not prove permanent defection") must NOT fail
    c_hedged = ExtractedClaim(
        claim_id="C5",
        claim_type="value",
        span="324 repeat customers",
        sentence="Inactivity does not prove permanent defection across the 324 repeat customers.",
        stated_value="324",
        paths=["at_risk_summary.total_at_risk_count"],
    )
    assert verify_single_claim(c_hedged, real_tool_outputs)["verdict"] == "PASS"

    # Unhedged causal claim ("proves permanent defection") MUST fail
    c_unhedged = ExtractedClaim(
        claim_id="C6",
        claim_type="value",
        span="324 repeat customers",
        sentence="Crossing the 90-day threshold proves permanent defection for 324 repeat customers.",
        stated_value="324",
        paths=["at_risk_summary.total_at_risk_count"],
    )
    res_unhedged = verify_single_claim(c_unhedged, real_tool_outputs)
    assert res_unhedged["arithmetic_verdict"] == "PASS"
    assert res_unhedged["verdict"] == "FAIL"
    assert "FAIL_IF_UNHEDGED" in res_unhedged["reason"]


# ============================================================================
# 3. COVERAGE CHECK TESTS
# ============================================================================
def test_coverage_check_catches_skipped_numeric_sentence(real_tool_outputs):
    report_md = build_reference_report_md(real_tool_outputs)
    all_claims = extract_claims(report_md, real_tool_outputs, use_llm=False)

    full_cov = check_coverage(report_md, all_claims)
    assert full_cov["passed"] is True
    assert full_cov["recall"] == 1.0

    # Drop the At-Risk sentence claims and verify coverage check flags the missing sentence
    partial_claims = [
        c for c in all_claims if "324" not in c.sentence and "1,390,289.84" not in c.sentence
    ]
    partial_cov = check_coverage(report_md, partial_claims)
    assert partial_cov["passed"] is False
    assert partial_cov["recall"] < 1.0
    assert any("324" in s for s in partial_cov["uncovered_sentences"])


# ============================================================================
# 4. EXTRACTOR CHECKED AGAINST HAND-LABELED CLAIMS
# ============================================================================
def test_extractor_against_hand_labeled_claims(real_tool_outputs):
    report_md = build_reference_report_md(real_tool_outputs)
    extracted = extract_claims(report_md, real_tool_outputs, use_llm=False)

    hand_labeled_expectations = {
        "health.reconciliation.total_raw_rows": "1,067,371",
        "health.reconciliation.total_excluded_rows": "290,794",
        "health.reconciliation.final_analysis_rows": "776,577",
        "churn_summary.selected_threshold_result.inferred_churn_rate": "56.45%",
        "at_risk_summary.total_at_risk_count": "324",
        "at_risk_summary.total_at_risk_lifetime_revenue": "£1,390,289.84",
        "shopify_snapshot.total_contracts": "8",
    }

    extracted_by_path = {
        c.paths[0]: c.stated_value for c in extracted if len(c.paths) == 1
    }
    for exp_path, exp_val in hand_labeled_expectations.items():
        assert exp_path in extracted_by_path, f"Missing expected path: {exp_path}"
        assert extracted_by_path[exp_path] == exp_val


# ============================================================================
# 5. MUTATION TESTS & PIPELINE REVISION GATE
# ============================================================================
def test_mutation_corrupted_numbers_and_pipeline_revision_gate(real_tool_outputs):
    valid_report = build_reference_report_md(real_tool_outputs)

    # Baseline valid report passes grade_report
    baseline_grade = grade_report(valid_report, real_tool_outputs, use_llm_extractor=False)
    assert baseline_grade["passed"] is True
    assert baseline_grade["summary"]["fail_count"] == 0

    # Mutation 1: Corrupt 90-day churn rate from 56.45% to 62.64%
    mutated_num_report = valid_report.replace("`56.45%`", "`62.64%`")
    mut1_grade = grade_report(mutated_num_report, real_tool_outputs, use_llm_extractor=False)
    assert mut1_grade["passed"] is False
    assert mut1_grade["summary"]["fail_count"] >= 1

    # Mutation 2: Inject unhedged causal claim
    mutated_causal_report = valid_report.replace(
        "inactivity does not prove permanent defection.",
        "inactivity proves permanent defection because of price sensitivity.",
    )
    mut2_grade = grade_report(mutated_causal_report, real_tool_outputs, use_llm_extractor=False)
    assert mut2_grade["passed"] is False

    # Pipeline Gate Test: Inject a corrupted v1 draft into generate_weekly_report()
    # Verify that attempt 1 fails, triggers revision_triggered=True, and attempt 2 repairs and publishes!
    def bad_draft_fn(bundle: dict) -> str:
        good = wr._build_deterministic_narrative(
            bundle["health"],
            bundle["churn_summary"],
            bundle["at_risk_summary"],
            bundle["shopify_snapshot"],
            bundle["shopify_health"],
        )
        return good.replace("`56.45%`", "`99.99%`")

    def clean_revision_fn(draft: str, grade: dict, bundle: dict) -> str:
        return wr._build_deterministic_narrative(
            bundle["health"],
            bundle["churn_summary"],
            bundle["at_risk_summary"],
            bundle["shopify_snapshot"],
            bundle["shopify_health"],
        )

    gate_res = wr.generate_weekly_report(
        use_fixture=True,
        draft_override_fn=bad_draft_fn,
        revision_override_fn=clean_revision_fn,
    )
    assert gate_res["passed"] is True
    assert gate_res["audit"]["attempt_1"]["passed"] is False
    assert gate_res["audit"]["revision_triggered"] is True
    assert gate_res["audit"]["attempt_2"]["passed"] is True


if __name__ == "__main__":
    bundle = build_real_tool_outputs()
    test_path_resolver_indices_and_selectors(bundle)
    test_number_and_range_normalizer_and_tolerance()
    test_verifiers_on_real_churn_and_at_risk_output(bundle)
    test_method_validity_hedged_vs_unhedged(bundle)
    test_coverage_check_catches_skipped_numeric_sentence(bundle)
    test_extractor_against_hand_labeled_claims(bundle)
    test_mutation_corrupted_numbers_and_pipeline_revision_gate(bundle)
    print("ALL 7 CLAIM CHECKER TEST SUITES PASSED!")