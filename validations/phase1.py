# validations/validate_phase1.py
import json
from pathlib import Path
import duckdb

metadata_path = Path("data/processed/ingestion_metadata.json")
parquet_path = Path("data/processed/raw_transactions.parquet")

print("Validating Phase 1: Data Acquisition & Provenance...")
assert metadata_path.exists(), f"Missing metadata file: {metadata_path}"
assert parquet_path.exists(), f"Missing parquet file: {parquet_path}"

with open(metadata_path, "r", encoding="utf-8") as f:
    meta = json.load(f)

print("Sheet counts:", meta["sheet_row_counts"])
assert meta["sheet_row_counts"]["Year 2009-2010"] == 525461, "Sheet 1 row count mismatch!"
assert meta["sheet_row_counts"]["Year 2010-2011"] == 541910, "Sheet 2 row count mismatch!"
assert meta["total_raw_rows"] == 1067371, "Total raw rows must equal 1,067,371!"

parquet_cnt = duckdb.sql(f"SELECT COUNT(*) FROM '{parquet_path.as_posix()}'").fetchone()[0]
assert parquet_cnt == 1067371, f"Parquet row count {parquet_cnt} != 1,067,371!"

print("PHASE 1 PASSED: 1,067,371 raw rows verified in Parquet and metadata!")