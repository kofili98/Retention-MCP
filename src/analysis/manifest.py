import datetime
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
MANIFEST_PATH = PROCESSED_DIR / "run_manifest.json"
SHOPIFY_PROVENANCE_PATH = PROCESSED_DIR / "shopify_query_provenance.json"
DEFAULT_SHOPIFY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "shopify_sanitized_fixture.json"

CLAIM_CHECKER_VERSION = "1.0.0"

TRACKED_ARTIFACTS = [
    "exclusions_ledger.json",
    "health_report.json",
    "cohort_retention.json",
    "churn_summary.json",
    "at_risk_customers.json",
]
TRACKED_PACKAGES = [
    "duckdb",
    "pandas",
    "pyarrow",
    "openpyxl",
    "mcp",
    "anthropic",
    "pydantic",
]
LOCKFILE_CANDIDATES = [
    "uv.lock",
    "poetry.lock",
    "requirements.lock",
    "Pipfile.lock",
    "pyproject.toml",
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_git_commit() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
            )
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return "uncommitted"


def get_git_dirty_flag() -> bool:
    """
    Returns True if the working tree has uncommitted modifications or untracked files,
    or if git status cannot be verified (since a commit hash is meaningless if modified after).
    """
    try:
        status_out = (
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL
            )
            .decode("utf-8")
            .strip()
        )
        return bool(status_out)
    except Exception:
        return True


def get_lockfile_provenance() -> tuple[str, str]:
    """
    Computes the SHA-256 hash of the project's lockfile (or pyproject.toml if no lockfile exists).
    Returns (lockfile_filename, sha256_hex).
    """
    for candidate in LOCKFILE_CANDIDATES:
        cand_path = REPO_ROOT / candidate
        if cand_path.exists():
            return candidate, sha256_file(cand_path)
    return "none", "0" * 64


def get_shopify_provenance() -> dict:
    """
    Reads the most recent Shopify response SHA-256 hash and query timestamp recorded
    by `shopify/tools.py`, or computes a fallback from the sanitized fixture if not yet queried.
    """
    if SHOPIFY_PROVENANCE_PATH.exists():
        try:
            with open(SHOPIFY_PROVENANCE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "shopify_response_sha256" in data and "shopify_query_timestamp_utc" in data:
                return data
        except Exception:
            pass

    if DEFAULT_SHOPIFY_FIXTURE.exists():
        raw_bytes = DEFAULT_SHOPIFY_FIXTURE.read_bytes()
        fixture_obj = json.loads(raw_bytes.decode("utf-8"))
        canonical_bytes = json.dumps(fixture_obj, sort_keys=True).encode("utf-8")
        return {
            "shopify_data_source": fixture_obj.get("source", "shopify_dev_store_sanitized_fixture"),
            "shopify_response_sha256": sha256_bytes(canonical_bytes),
            "shopify_query_timestamp_utc": fixture_obj.get(
                "captured_at", "2026-10-18T14:00:00Z"
            ),
        }

    return {
        "shopify_data_source": "unqueried",
        "shopify_response_sha256": "0" * 64,
        "shopify_query_timestamp_utc": "unqueried",
    }


def build_manifest(
    run_timestamp_utc: str | None = None,
    shopify_provenance: dict | None = None,
) -> dict:
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

    lockfile_name, lockfile_hash = get_lockfile_provenance()
    shop_prov = shopify_provenance or get_shopify_provenance()
    ts_utc = run_timestamp_utc or datetime.datetime.now(datetime.timezone.utc).isoformat()
    agent_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929").strip()

    manifest = {
        "metrics_version": "1.0.0",
        "checker_version": CLAIM_CHECKER_VERSION,
        "agent_model_id": agent_model,
        "run_timestamp_utc": ts_utc,
        "git_commit": get_git_commit(),
        "git_dirty": get_git_dirty_flag(),
        "lockfile_source": lockfile_name,
        "lockfile_sha256": lockfile_hash,
        "python_version": platform.python_version(),
        "raw_xlsx_sha256": ingest_meta["xlsx_sha256"],
        "artifact_sha256_hashes": artifact_hashes,
        "shopify_response_sha256": shop_prov["shopify_response_sha256"],
        "shopify_query_timestamp_utc": shop_prov["shopify_query_timestamp_utc"],
        "parameters": {
            "anchor_date_rule": "MAX(invoice_date)",
            "bootstrap_seed": 42,
            "sensitivity_thresholds_days": [60, 90, 120, 180],
            "at_risk_min_purchase_days": 3,
        },
        "package_versions": pkg_versions,
    }

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    return manifest


def extract_all_manifest_hashes(manifest: dict) -> set[str]:
    """Returns the set of all legitimate 64-character hex SHA-256 hashes present in `manifest`."""
    hashes: set[str] = set()
    for key in ("raw_xlsx_sha256", "lockfile_sha256", "shopify_response_sha256"):
        val = manifest.get(key)
        if isinstance(val, str) and len(val) == 64:
            hashes.add(val.lower())
    for val in manifest.get("artifact_sha256_hashes", {}).values():
        if isinstance(val, str) and len(val) == 64:
            hashes.add(val.lower())
    return hashes


if __name__ == "__main__":
    m = build_manifest()
    print(f"Run manifest written to {MANIFEST_PATH}")