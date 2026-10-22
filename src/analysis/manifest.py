import hashlib
import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
MANIFEST_PATH = PROCESSED_DIR / "run_manifest.json"
TRACKED_ARTIFACTS = [
    "exclusions_ledger.json",
    "health_report.json",
    "cohort_retention.json",
    "churn_summary.json",
    "at_risk_customers.json",
]
TRACKED_PACKAGES = ["duckdb", "pandas", "pyarrow", "openpyxl", "mcp", "anthropic", "pydantic"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_git_commit() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return "uncommitted"


def build_manifest() -> dict:
    with open(PROCESSED_DIR / "ingestion_metadata.json", "r", encoding="utf-8") as f:
        ingest_meta = json.load(f)

    artifact_hashes = {
        name: sha256_file(PROCESSED_DIR / name)
        for name in TRACKED_ARTIFACTS
        if (PROCESSED_DIR / name).exists()
    }

    pkg_versions = {}
    for pkg in TRACKED_PACKAGES:
        try:
            pkg_versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            pkg_versions[pkg] = "not_installed"

    manifest = {
        "metrics_version": "1.0.0",
        "git_commit": get_git_commit(),
        "python_version": platform.python_version(),
        "raw_xlsx_sha256": ingest_meta["xlsx_sha256"],
        "artifact_sha256_hashes": artifact_hashes,
        "parameters": {
            "anchor_date_rule": "MAX(invoice_date)",
            "bootstrap_seed": 42,
            "sensitivity_thresholds_days": [60, 90, 120, 180],
            "at_risk_min_purchase_days": 3,
        },
        "package_versions": pkg_versions,
    }

    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Run manifest written to {MANIFEST_PATH}")
    return manifest


if __name__ == "__main__":
    build_manifest()