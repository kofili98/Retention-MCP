# validations/validate_phase3.py
from pathlib import Path

metrics_path = Path("docs/METRICS.md")
print("Validating Phase 3: Metric Definitions...")
assert metrics_path.exists(), f"Missing metrics doc: {metrics_path}"

text = metrics_path.read_text(encoding="utf-8")
required = [
    "T_MAX",
    "Acquisition Cohort",
    "Non-Monotonicity",
    "Right-Censoring",
    "At-Risk",
    "Wholesale_Proxy",
]
missing = [t for t in required if t not in text]
assert not missing, f"Missing definitions in docs/METRICS.md: {missing}"

print("PHASE 3 PASSED: All pre-registered metric definitions verified in docs/METRICS.md.")