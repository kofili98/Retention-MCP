import json
import duckdb

ledger_path = "data/processed/exclusions_ledger.json"
clean_parquet = "data/processed/clean_transactions.parquet"

with open(ledger_path, "r", encoding="utf-8") as f:
    ledger = json.load(f)

print(json.dumps(ledger, indent=2))
assert ledger["reconciled_exactly"] is True, "Exclusions ledger did not reconcile exactly!"
assert ledger["total_raw_rows"] - ledger["total_excluded_rows"] == ledger["final_analysis_rows"], "Row count math mismatch!"

clean_cnt = duckdb.sql(f"SELECT COUNT(*) FROM '{clean_parquet}'").fetchone()[0]
assert clean_cnt == ledger["final_analysis_rows"], f"Clean parquet count {clean_cnt} != ledger count {ledger['final_analysis_rows']}"

bad_rows = duckdb.sql(f"""
    SELECT COUNT(*) FROM '{clean_parquet}'
    WHERE customer_id IS NULL OR UPPER(invoice) LIKE 'C%' OR quantity <= 0 OR price <= 0.0
""").fetchone()[0]
assert bad_rows == 0, f"Found {bad_rows} dirty rows in clean parquet!"

print(f"PHASE 2 PASSED: {clean_cnt:,} clean rows reconciled with 0 dirty rows!")
' | Out-File -Encoding utf8 validate_phase2.py'