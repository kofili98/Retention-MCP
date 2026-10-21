# validations/validate_phase9_10.py
import json
from pathlib import Path

print("Validating Phases 9 & 10: Reproducibility Hashes & README...")

manifest = json.loads(Path("data/processed/run_manifest.json").read_text(encoding="utf-8"))
golden = json.loads(Path("tests/golden/golden_hashes.json").read_text(encoding="utf-8"))
assert manifest["artifact_sha256_hashes"] == golden, "Artifact hash drift from golden snapshot!"

readme = Path("README.md").read_text(encoding="utf-8")
required = [
    "Daqing Chen",
    "CC BY 4.0",
    "1,067,371",
    "Exclusions Ledger",
    "T_MAX",
    "Right-Censoring",
    "Shopify",
]
missing = [item for item in required if item not in readme]
assert not missing, f"README missing required items: {missing}"

print("PHASES 9 & 10 PASSED: Output hashes and README attributions verified!")