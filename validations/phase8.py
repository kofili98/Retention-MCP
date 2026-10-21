# validations/validate_phase8.py
import json
from pathlib import Path
import agent.weekly_report as wr

print("Validating Phase 8: Weekly Report Agent & Circuit-Breaker...")

log_path = Path("reports/weekly_report_tool_log.json")
report_path = Path("reports/weekly_retention_report.md")

assert log_path.exists(), f"Missing tool log: {log_path}"
assert report_path.exists(), f"Missing report: {report_path}"

log = json.loads(log_path.read_text(encoding="utf-8"))
seq = [entry["tool_name"] for entry in log]
expected = [
    "run_data_health_check",
    "get_run_manifest",
    "get_churn_summary",
    "get_cohort_retention",
    "get_at_risk_customers",
    "shopify_subscription_snapshot",
    "shopify_data_health_check",
]
assert seq == expected, f"Unexpected tool sequence: {seq}"

report_text = report_path.read_text(encoding="utf-8")
assert "Cryptographic Run Manifest Stamp" in report_text

# Test health circuit-breaker
orig_fn = wr.READ_ONLY_TOOLS["run_data_health_check"]
wr.READ_ONLY_TOOLS["run_data_health_check"] = lambda: {
    "status": "CRITICAL",
    "reconciliation": {"reconciled_exactly": False},
}
try:
    wr.generate_weekly_report()
    aborted_text = report_path.read_text(encoding="utf-8")
    assert "CRITICAL DATA HEALTH FAILURE" in aborted_text
finally:
    wr.READ_ONLY_TOOLS["run_data_health_check"] = orig_fn
    wr.generate_weekly_report()  # Restore valid report

print("PHASE 8 PASSED: Weekly report agent and circuit-breaker verified!")