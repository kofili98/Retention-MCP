# validations/validate_phase5.py
from server.mcp_server import (
    get_at_risk_customers,
    get_churn_summary,
    get_cohort_retention,
    get_run_manifest,
    run_data_health_check,
)

print("Validating Phase 5: MCP Server Tools...")

r1 = get_cohort_retention("buyer_type", 6)
assert len(r1["records"]) > 0 and "non_monotonic_warning" in r1

r2 = get_churn_summary(90)
assert r2["anchor_date_t_max"] == "2011-12-09"
assert r2["selected_threshold_result"]["inactivity_threshold_days"] == 90

r3 = get_at_risk_customers(5)
assert len(r3["top_at_risk_customers"]) == 5
assert isinstance(r3["top_at_risk_customers"][0]["customer_id"], int)

r4 = run_data_health_check()
assert r4["reconciliation"]["reconciled_exactly"] is True

r5 = get_run_manifest()
assert "artifact_sha256_hashes" in r5

print("PHASE 5 PASSED: All 5 warehouse MCP tools verified successfully!")