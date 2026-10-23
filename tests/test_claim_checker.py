"""
Unit, integration, and mutation tests for the deterministic Claim Checker & Manifest Update:
  1. Path resolver and number normalizer (including rounding tolerance rules)
  2. Verifiers for value, difference, and ci_overlap on real churn/at-risk outputs + METHOD_RULES
  3. Deterministic coverage check with allow-list
  4. Extractor verification against hand-labeled claims
  5. Mutation tests and the pipeline revision gate
  6. Extended Manifest provenance fields (git_dirty, lockfile_sha256, agent_model_id, checker_version,
     run_timestamp_utc, shopify_response_sha256, shopify_query_timestamp_utc)
  7. `save_report(body)` MCP tool & `evaluate_agent_run` (final tool call & saved file check)
  8. The 3 Manifest Checker Rules (every 64-hex string in build_manifest(), byte-for-byte equality,
     and missing manifest block failure)
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
    evaluate_agent_run,
    extract_claims,
    grade_report,
    matches_within_tolerance,
    normalize_number,
    normalize_range,
    resolve_path,
    verify_manifest_rules,
    verify_single_claim,
)
import agent.weekly_report as wr
from analysis.manifest import build_manifest, extract_all_manifest_hashes
from server.mcp_server import save_report

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


def build_reference_report_md(
    tool_outputs: dict, manifest: dict | None = None
) -> tuple[str, dict]:
    """Builds the deterministic reference report and manifest matching `tool_outputs`."""
    mf = manifest or build_manifest()
    narrative = wr._build_deterministic_narrative(
        tool_outputs["health"],
        tool_outputs["churn_summary"],
        tool_outputs["at_risk_summary"],
        tool_outputs["shopify_snapshot"],
        tool_outputs["shopify_health"],
    )
    full_md = wr.append_manifest_in_harness(
        f"# Reproducible Retention Report\n\n{narrative}", mf
    )
    return full_md, mf


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
    bounds, is_pct, dec = normalize_range("61.38–63.92%")
    assert bounds == (61.38, 63.92)
    assert is_pct is True
    assert dec == 2

    val, is_pct_curr, dec_curr = normalize_number("£1,390,289.84")
    assert val == 1390289.84
    assert is_pct_curr is False
    assert dec_curr == 2

    passed, aligned, err, tol = matches_within_tolerance(
        stated_val=62.6, tool_val=0.62641, decimals=1, is_percentage=True
    )
    assert passed is True
    assert round(aligned, 3) == 62.641
    assert err <= 0.05

    passed_bad, _, _, _ = matches_within_tolerance(
        stated_val=62.5, tool_val=0.62641, decimals=1, is_percentage=True
    )
    assert passed_bad is False


# ============================================================================
# 2. VERIFIERS (value, difference, ci_overlap) & METHOD_RULES ON REAL CHURN DATA
# ============================================================================
def test_verifiers_on_real_churn_and_at_risk_output(real_tool_outputs):
    c_val = ExtractedClaim(
        claim_id="C1",
        claim_type="value",
        span="60-day churn is 62.6%",
        sentence="At the 60-day threshold, 60-day churn is 62.6%.",
        stated_value="62.6%",
        paths=["churn_summary.full_sensitivity_table[inactivity_threshold_days=60].inferred_churn_rate"],
    )
    assert verify_single_claim(c_val, real_tool_outputs)["verdict"] == "PASS"

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
    assert verify_single_claim(c_range, real_tool_outputs)["verdict"] == "PASS"

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
    assert verify_single_claim(c_diff, real_tool_outputs)["verdict"] == "PASS"

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
    assert res_overlap["arithmetic_verdict"] == "PASS"
    assert any(m["severity"] == "WARN" for m in res_overlap["method_findings"])
    assert res_overlap["verdict"] == "FAIL"


def test_method_validity_hedged_vs_unhedged(real_tool_outputs):
    c_hedged = ExtractedClaim(
        claim_id="C5",
        claim_type="value",
        span="324 repeat customers",
        sentence="Inactivity does not prove permanent defection across the 324 repeat customers.",
        stated_value="324",
        paths=["at_risk_summary.total_at_risk_count"],
    )
    assert verify_single_claim(c_hedged, real_tool_outputs)["verdict"] == "PASS"

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
    report_md, _ = build_reference_report_md(real_tool_outputs)
    all_claims = extract_claims(report_md, real_tool_outputs, use_llm=False)

    full_cov = check_coverage(report_md, all_claims)
    assert full_cov["passed"] is True
    assert full_cov["recall"] == 1.0

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
    report_md, _ = build_reference_report_md(real_tool_outputs)
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
    valid_report, manifest = build_reference_report_md(real_tool_outputs)

    baseline_grade = grade_report(
        valid_report, real_tool_outputs, expected_manifest=manifest, use_llm_extractor=False
    )
    assert baseline_grade["passed"] is True
    assert baseline_grade["summary"]["fail_count"] == 0

    mutated_num_report = valid_report.replace("`56.45%`", "`62.64%`")
    mut1_grade = grade_report(
        mutated_num_report, real_tool_outputs, expected_manifest=manifest, use_llm_extractor=False
    )
    assert mut1_grade["passed"] is False
    assert mut1_grade["summary"]["fail_count"] >= 1

    mutated_causal_report = valid_report.replace(
        "inactivity does not prove permanent defection.",
        "inactivity proves permanent defection because of price sensitivity.",
    )
    mut2_grade = grade_report(
        mutated_causal_report, real_tool_outputs, expected_manifest=manifest, use_llm_extractor=False
    )
    assert mut2_grade["passed"] is False

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
    assert gate_res["audit"]["agent_eval"]["passed"] is True


# ============================================================================
# 6. EXTENDED MANIFEST PROVENANCE FIELDS
# ============================================================================
def test_extended_manifest_fields():
    manifest = build_manifest()
    required_new_fields = [
        "git_dirty",
        "lockfile_sha256",
        "lockfile_source",
        "agent_model_id",
        "checker_version",
        "run_timestamp_utc",
        "shopify_response_sha256",
        "shopify_query_timestamp_utc",
    ]
    for field in required_new_fields:
        assert field in manifest, f"Missing required manifest field: {field}"

    assert isinstance(manifest["git_dirty"], bool)
    assert len(manifest["lockfile_sha256"]) == 64
    assert len(manifest["shopify_response_sha256"]) == 64
    assert len(extract_all_manifest_hashes(manifest)) >= 7


# ============================================================================
# 7. `save_report(body)` TOOL & AGENT EVAL
# ============================================================================
def test_save_report_strips_model_manifest_and_agent_eval(real_tool_outputs):
    narrative = wr._build_deterministic_narrative(
        real_tool_outputs["health"],
        real_tool_outputs["churn_summary"],
        real_tool_outputs["at_risk_summary"],
        real_tool_outputs["shopify_snapshot"],
        real_tool_outputs["shopify_health"],
    )
    # Simulate a model-written Section (f) with a hallucinated hash
    body_with_model_stamp = (
        f"# Reproducible Retention Report\n\n{narrative}\n\n"
        "## (f) Run manifest stamp\n```json\n"
        '{"raw_xlsx_sha256": "' + ("f" * 64) + '"}\n```\n'
    )

    res = save_report(body_with_model_stamp)
    saved_path = Path(res["path"])
    assert saved_path.exists()
    saved_text = saved_path.read_text(encoding="utf-8")

    # Model-written fake hash ("f"*64) must be stripped and replaced by canonical build_manifest()
    assert ("f" * 64) not in saved_text
    assert "## (f) Run manifest stamp" in saved_text

    # Agent eval passes when save_report is the final call and file exists
    good_log = [
        {"step": 1, "tool_name": "run_data_health_check"},
        {"step": 2, "tool_name": "save_report"},
    ]
    assert evaluate_agent_run(good_log, saved_report_path=saved_path)["passed"] is True

    # Agent eval fails when save_report is skipped or not last
    bad_log = [
        {"step": 1, "tool_name": "run_data_health_check"},
        {"step": 2, "tool_name": "get_churn_summary"},
    ]
    eval_skipped = evaluate_agent_run(bad_log, saved_report_path=saved_path)
    assert eval_skipped["passed"] is False
    assert any("save_report" in e for e in eval_skipped["errors"])


# ============================================================================
# 8. THE 3 MANIFEST CHECKER RULES
# ============================================================================
def test_three_manifest_checker_rules(real_tool_outputs):
    valid_report, manifest = build_reference_report_md(real_tool_outputs)

    # Valid report passes all 3 manifest rules
    ok_check = verify_manifest_rules(valid_report, expected_manifest=manifest)
    assert ok_check["passed"] is True

    # Rule 1 Failure: A 64-hex string in the report that does not appear in build_manifest()
    rogue_hash = "a" * 64
    rogue_report = f"Verified against commit hash `{rogue_hash}`.\n\n" + valid_report
    r1_check = verify_manifest_rules(rogue_report, expected_manifest=manifest)
    assert r1_check["passed"] is False
    assert r1_check["all_64hex_in_manifest"] is False
    assert rogue_hash in r1_check["unknown_64hex_strings"]

    # Rule 2 Failure: Manifest block altered by 1 whitespace/character (fails byte-for-byte check)
    altered_stamp_report = valid_report.replace(
        '"metrics_version": "1.0.0"', '"metrics_version":  "1.0.0"'
    )
    r2_check = verify_manifest_rules(altered_stamp_report, expected_manifest=manifest)
    assert r2_check["passed"] is False
    assert r2_check["byte_for_byte_match"] is False

    # Rule 3 Failure: Report with no manifest block fails
    no_manifest_report = valid_report.split("## (f) Run manifest stamp")[0]
    r3_check = verify_manifest_rules(no_manifest_report, expected_manifest=manifest)
    assert r3_check["passed"] is False
    assert r3_check["has_manifest_block"] is False
    assert grade_report(
        no_manifest_report, real_tool_outputs, expected_manifest=manifest, use_llm_extractor=False
    )["passed"] is False


if __name__ == "__main__":
    bundle = build_real_tool_outputs()
    test_path_resolver_indices_and_selectors(bundle)
    test_number_and_range_normalizer_and_tolerance()
    test_verifiers_on_real_churn_and_at_risk_output(bundle)
    test_method_validity_hedged_vs_unhedged(bundle)
    test_coverage_check_catches_skipped_numeric_sentence(bundle)
    test_extractor_against_hand_labeled_claims(bundle)
    test_mutation_corrupted_numbers_and_pipeline_revision_gate(bundle)
    test_extended_manifest_fields()
    test_save_report_strips_model_manifest_and_agent_eval(bundle)
    test_three_manifest_checker_rules(bundle)
    print("ALL 8 CLAIM & MANIFEST CHECKER TEST SUITES PASSED!")