# validations/validate_phase4.py
import json
from pathlib import Path

processed_dir = Path("data/processed")
golden_path = Path("tests/golden/golden_hashes.json")

print("Validating Phase 4: Deterministic Analysis & Invariants...")


def load(name):
    with open(processed_dir / name, "r", encoding="utf-8") as f:
        return json.load(f)


cohorts = load("cohort_retention.json")["groupings"]["all"]
churn = load("churn_summary.json")
manifest = load("run_manifest.json")

# Invariant 1: Month-0 retention equals 1.0
month_0 = [r for r in cohorts if r["period_n"] == 0]
assert len(month_0) > 0
for r in month_0:
    assert r["retention_rate"] == 1.0
    assert r["active_customers"] == r["cohort_size"]

# Invariant 2: Retention bounded in [0, 1]
for r in cohorts:
    assert 0.0 <= r["retention_rate"] <= 1.0

# Invariant 3: Churn sensitivity math
for row in churn["sensitivity_table"]:
    assert (
        row["right_censored_customers"] + row["eligible_uncensored_customers"]
        == row["total_customers"]
    )
    assert (
        row["inferred_churned_customers"] + row["active_retained_customers"]
        == row["eligible_uncensored_customers"]
    )
    assert 0.0 <= row["ci_95_lower"] <= row["inferred_churn_rate"] <= row["ci_95_upper"] <= 1.0

# Invariant 4: Golden hash verification
if golden_path.exists():
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert manifest["artifact_sha256_hashes"] == golden, "Golden hash drift detected!"

print("PHASE 4 PASSED: All retention, churn, and invariant assertions verified!")