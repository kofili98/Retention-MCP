import json
from pathlib import Path

PROCESSED_DIR = Path("data/processed")
GOLDEN_HASH_PATH = Path("tests/golden/golden_hashes.json")


def load_json(name: str) -> dict:
    with open(PROCESSED_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def test_exclusions_ledger_reconciles_exactly():
    ledger = load_json("exclusions_ledger.json")
    raw_total = ledger["total_raw_rows"]
    excluded_sum = sum(ledger["exclusions_by_rule"].values())
    final_rows = ledger["final_analysis_rows"]

    assert raw_total == 1067371
    assert raw_total - excluded_sum == final_rows
    assert ledger["reconciled_exactly"] is True


def test_cohort_retention_invariants():
    cohorts = load_json("cohort_retention.json")["groupings"]["all"]
    churn = load_json("churn_summary.json")
    total_customers = churn["sensitivity_table"][0]["total_customers"]

    month_0_rows = [r for r in cohorts if r["period_n"] == 0]
    assert len(month_0_rows) > 0
    for r in month_0_rows:
        assert r["retention_rate"] == 1.0
        assert r["active_customers"] == r["cohort_size"]

    assert sum(r["cohort_size"] for r in month_0_rows) == total_customers

    for r in cohorts:
        assert 0.0 <= r["retention_rate"] <= 1.0


def test_churn_sensitivity_invariants():
    table = load_json("churn_summary.json")["sensitivity_table"]
    for row in table:
        assert (
            row["right_censored_customers"] + row["eligible_uncensored_customers"]
            == row["total_customers"]
        )
        assert (
            row["inferred_churned_customers"] + row["active_retained_customers"]
            == row["eligible_uncensored_customers"]
        )
        assert 0.0 <= row["ci_95_lower"] <= row["inferred_churn_rate"] <= row["ci_95_upper"] <= 1.0


def test_golden_output_hashes_match():
    manifest = load_json("run_manifest.json")
    current_hashes = manifest["artifact_sha256_hashes"]

    if not GOLDEN_HASH_PATH.exists():
        GOLDEN_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(GOLDEN_HASH_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(current_hashes, f, indent=2, sort_keys=True)
            f.write("\n")
        return

    with open(GOLDEN_HASH_PATH, "r", encoding="utf-8") as f:
        golden_hashes = json.load(f)

    assert current_hashes == golden_hashes, (
        f"REPRODUCIBILITY FAILURE: Output hashes drifted from golden snapshot!\n"
        f"Current: {current_hashes}\nGolden:  {golden_hashes}"
    )