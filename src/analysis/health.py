import json
from pathlib import Path
import duckdb

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
RAW_PARQUET = PROCESSED_DIR / "raw_transactions.parquet"
CLEAN_PARQUET = PROCESSED_DIR / "clean_transactions.parquet"
LEDGER_PATH = PROCESSED_DIR / "exclusions_ledger.json"
HEALTH_REPORT_PATH = PROCESSED_DIR / "health_report.json"

NON_PRODUCT_CODES = (
    "'POST', 'D', 'M', 'BANK CHARGES', 'PADS', 'DOT', 'CRUK', "
    "'ADJUST', 'ADJUST2', 'C2', 'SP1002'"
)


def run_health_and_cleaning() -> dict:
    con = duckdb.connect(database=":memory:")
    raw_posix = RAW_PARQUET.as_posix()
    clean_posix = CLEAN_PARQUET.as_posix()

    con.execute(f"CREATE VIEW raw_data AS SELECT * FROM read_parquet('{raw_posix}')")

    classification_sql = f"""
    CREATE TABLE classified_rows AS
    WITH dedup_flagged AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY invoice, stock_code, description, quantity, invoice_date, price, COALESCE(CAST(customer_id AS VARCHAR), 'NULL'), country
                ORDER BY raw_row_id
            ) AS dup_rank
        FROM raw_data
    )
    SELECT
        raw_row_id,
        invoice,
        stock_code,
        description,
        quantity,
        invoice_date,
        price,
        CAST(customer_id AS INTEGER) AS customer_id,
        country,
        source_sheet,
        CASE
            WHEN dup_rank > 1 THEN '1_EXACT_DUPLICATE'
            WHEN customer_id IS NULL OR isnan(customer_id) THEN '2_MISSING_CUSTOMER_ID'
            WHEN UPPER(invoice) LIKE 'C%' THEN '3_CANCELLATION_INVOICE'
            WHEN UPPER(TRIM(stock_code)) IN ({NON_PRODUCT_CODES})
                 OR UPPER(TRIM(stock_code)) LIKE 'TEST%' THEN '4_NON_PRODUCT_STOCK_CODE'
            WHEN quantity <= 0 OR price <= 0.0 OR isnan(price) THEN '5_INVALID_QTY_OR_PRICE'
            ELSE '0_VALID_ANALYSIS_ROW'
        END AS disposition
    FROM dedup_flagged;
    """
    con.execute(classification_sql)

    total_raw = con.execute("SELECT COUNT(*) FROM classified_rows").fetchone()[0]
    counts = dict(
        con.execute(
            "SELECT disposition, COUNT(*) FROM classified_rows GROUP BY disposition ORDER BY disposition"
        ).fetchall()
    )

    valid_rows = counts.get("0_VALID_ANALYSIS_ROW", 0)
    exclusions = {k: int(v) for k, v in counts.items() if k != "0_VALID_ANALYSIS_ROW"}
    total_excluded = sum(exclusions.values())

    assert total_raw - total_excluded == valid_rows, (
        f"Reconciliation failure! {total_raw} - {total_excluded} != {valid_rows}"
    )

    ledger = {
        "total_raw_rows": int(total_raw),
        "exclusions_by_rule": exclusions,
        "total_excluded_rows": int(total_excluded),
        "final_analysis_rows": int(valid_rows),
        "reconciled_exactly": (total_raw - total_excluded == valid_rows),
    }

    with open(LEDGER_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(ledger, f, indent=2, sort_keys=True)
        f.write("\n")

    rules_spec = [
        (
            "RULE_01_EXACT_DUPLICATE",
            "WARNING",
            "EXCLUDE",
            "Identical transaction lines (including overlapping Dec 2010 sheet boundary rows).",
            "disposition = '1_EXACT_DUPLICATE'",
        ),
        (
            "RULE_02_MISSING_CUSTOMER_ID",
            "HIGH",
            "EXCLUDE_FROM_COHORTS",
            "Rows missing Customer ID; cannot be tracked longitudinally across months.",
            "customer_id IS NULL",
        ),
        (
            "RULE_03_CANCELLATION_INVOICE",
            "INFO",
            "EXCLUDE_AS_RETURN",
            "Invoices prefixed with 'C' indicating order cancellations/returns.",
            "UPPER(invoice) LIKE 'C%'",
        ),
        (
            "RULE_04_NON_PRODUCT_CODE",
            "WARNING",
            "EXCLUDE",
            "Postage, bank charges, manual adjustments, and test stock codes.",
            f"UPPER(TRIM(stock_code)) IN ({NON_PRODUCT_CODES}) OR UPPER(TRIM(stock_code)) LIKE 'TEST%'",
        ),
        (
            "RULE_05_NON_POSITIVE_PRICE_OR_QTY",
            "WARNING",
            "EXCLUDE",
            "Non-cancellation rows with Quantity <= 0 or Price <= 0.",
            "UPPER(invoice) NOT LIKE 'C%' AND (quantity <= 0 OR price <= 0.0)",
        ),
    ]

    health_rules = []
    for rule_id, severity, action, desc, where_clause in rules_spec:
        raw_fail_count = con.execute(
            f"SELECT COUNT(*) FROM classified_rows WHERE {where_clause}"
        ).fetchone()[0]
        samples = [
            r[0]
            for r in con.execute(
                f"SELECT raw_row_id FROM classified_rows WHERE {where_clause} ORDER BY raw_row_id LIMIT 5"
            ).fetchall()
        ]
        health_rules.append(
            {
                "rule_id": rule_id,
                "severity": severity,
                "decision": action,
                "description": desc,
                "raw_failing_count": int(raw_fail_count),
                "sample_raw_row_ids": samples,
            }
        )

    health_report = {
        "status": "HEALTHY" if valid_rows > 0 and ledger["reconciled_exactly"] else "CRITICAL",
        "reconciliation": ledger,
        "rules": health_rules,
    }

    with open(HEALTH_REPORT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(health_report, f, indent=2, sort_keys=True)
        f.write("\n")

    con.execute(f"""
        COPY (
            SELECT
                raw_row_id,
                invoice,
                stock_code,
                description,
                quantity,
                invoice_date,
                price,
                ROUND(quantity * price, 4) AS line_revenue,
                customer_id,
                country
            FROM classified_rows
            WHERE disposition = '0_VALID_ANALYSIS_ROW'
            ORDER BY invoice_date, invoice, raw_row_id
        ) TO '{clean_posix}' (FORMAT PARQUET);
    """)
    print(f"Health check complete. Clean analysis rows: {valid_rows:,}")
    return health_report


if __name__ == "__main__":
    run_health_and_cleaning()