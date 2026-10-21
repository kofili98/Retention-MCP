import json
from pathlib import Path
import duckdb
import numpy as np

PROCESSED_DIR = Path("data/processed")
CLEAN_PARQUET = PROCESSED_DIR / "clean_transactions.parquet"
COHORT_OUT = PROCESSED_DIR / "cohort_retention.json"
CHURN_OUT = PROCESSED_DIR / "churn_summary.json"
AT_RISK_OUT = PROCESSED_DIR / "at_risk_customers.json"

SENSITIVITY_THRESHOLDS = [60, 90, 120, 180]
BOOTSTRAP_SEED = 42
METRICS_VERSION = "1.0.0"


def _write_deterministic_json(path: Path, payload: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")


def get_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(database=":memory:")
    clean_posix = CLEAN_PARQUET.as_posix()
    con.execute(f"CREATE VIEW tx AS SELECT * FROM read_parquet('{clean_posix}')")
    con.execute("""
    CREATE TABLE customer_summary AS
    WITH invoice_totals AS (
        SELECT
            customer_id,
            invoice,
            CAST(invoice_date AS DATE) AS purchase_date,
            DATE_TRUNC('month', invoice_date) AS purchase_month,
            MAX(country) AS country,
            SUM(line_revenue) AS invoice_revenue,
            SUM(quantity) AS invoice_qty
        FROM tx
        GROUP BY customer_id, invoice, CAST(invoice_date AS DATE), DATE_TRUNC('month', invoice_date)
    ),
    cust_agg AS (
        SELECT
            customer_id,
            MIN(purchase_date) AS first_purchase_date,
            MAX(purchase_date) AS last_purchase_date,
            STRFTIME(MIN(purchase_month), '%Y-%m') AS cohort_month,
            COUNT(DISTINCT invoice) AS total_invoices,
            COUNT(DISTINCT purchase_date) AS distinct_purchase_days,
            ROUND(SUM(invoice_revenue), 2) AS lifetime_revenue,
            ROUND(AVG(invoice_revenue), 2) AS avg_invoice_revenue,
            SUM(invoice_qty) AS lifetime_qty,
            CASE WHEN MAX(country) = 'United Kingdom' THEN 'UK' ELSE 'International' END AS geo_segment
        FROM invoice_totals
        GROUP BY customer_id
    )
    SELECT
        *,
        CASE
            WHEN avg_invoice_revenue >= 300.0 OR lifetime_qty >= 1000 THEN 'Wholesale_Proxy'
            ELSE 'Retail_Proxy'
        END AS buyer_type
    FROM cust_agg
    ORDER BY customer_id;
    """)
    return con


def compute_cohort_retention(con: duckdb.DuckDBPyConnection) -> dict:
    results = {"metrics_version": METRICS_VERSION, "groupings": {}}

    for group_name, group_col in [
        ("all", "'ALL'"),
        ("buyer_type", "c.buyer_type"),
        ("geo_segment", "c.geo_segment"),
    ]:
        query = f"""
        WITH monthly_active AS (
            SELECT DISTINCT
                t.customer_id,
                c.cohort_month,
                {group_col} AS segment,
                STRFTIME(DATE_TRUNC('month', t.invoice_date), '%Y-%m') AS activity_month,
                (EXTRACT('year' FROM DATE_TRUNC('month', t.invoice_date)) - EXTRACT('year' FROM CAST(c.cohort_month || '-01' AS DATE))) * 12 +
                (EXTRACT('month' FROM DATE_TRUNC('month', t.invoice_date)) - EXTRACT('month' FROM CAST(c.cohort_month || '-01' AS DATE))) AS period_n
            FROM tx t
            JOIN customer_summary c ON t.customer_id = c.customer_id
        ),
        cohort_sizes AS (
            SELECT
                cohort_month,
                {group_col} AS segment,
                COUNT(DISTINCT customer_id) AS cohort_size
            FROM customer_summary c
            GROUP BY cohort_month, {group_col}
        )
        SELECT
            cs.segment,
            cs.cohort_month,
            cs.cohort_size,
            CAST(ma.period_n AS INTEGER) AS period_n,
            COUNT(DISTINCT ma.customer_id) AS active_customers,
            ROUND(COUNT(DISTINCT ma.customer_id) * 1.0 / cs.cohort_size, 6) AS retention_rate
        FROM cohort_sizes cs
        JOIN monthly_active ma
          ON cs.cohort_month = ma.cohort_month AND cs.segment = ma.segment
        GROUP BY cs.segment, cs.cohort_month, cs.cohort_size, ma.period_n
        ORDER BY cs.segment, cs.cohort_month, ma.period_n;
        """
        rows = con.execute(query).fetchall()
        records = [
            {
                "segment": r[0],
                "cohort_month": r[1],
                "cohort_size": int(r[2]),
                "period_n": int(r[3]),
                "active_customers": int(r[4]),
                "retention_rate": float(r[5]),
            }
            for r in rows
        ]
        results["groupings"][group_name] = records

    _write_deterministic_json(COHORT_OUT, results)
    return results


def compute_churn_and_at_risk(con: duckdb.DuckDBPyConnection) -> tuple[dict, dict]:
    t_max = con.execute("SELECT CAST(MAX(invoice_date) AS DATE) FROM tx").fetchone()[0]
    t_max_str = str(t_max)

    gaps_sql = """
    WITH daily_purchases AS (
        SELECT DISTINCT customer_id, CAST(invoice_date AS DATE) AS p_date
        FROM tx
    ),
    lagged AS (
        SELECT
            customer_id,
            p_date,
            LAG(p_date) OVER (PARTITION BY customer_id ORDER BY p_date) AS prev_date
        FROM daily_purchases
    )
    SELECT
        customer_id,
        DATEDIFF('day', prev_date, p_date) AS gap_days
    FROM lagged
    WHERE prev_date IS NOT NULL
    ORDER BY customer_id, p_date;
    """
    gap_rows = con.execute(gaps_sql).fetchall()
    all_gaps = np.array([r[1] for r in gap_rows], dtype=np.float64)

    gap_percentiles = {
        "p25": int(np.percentile(all_gaps, 25)),
        "p50_median": int(np.percentile(all_gaps, 50)),
        "p75": int(np.percentile(all_gaps, 75)),
        "p90": int(np.percentile(all_gaps, 90)),
        "mean": round(float(np.mean(all_gaps)), 2),
        "total_observed_gaps": int(len(all_gaps)),
    }

    eval_thresholds = sorted(list(set(SENSITIVITY_THRESHOLDS + [gap_percentiles["p75"]])))
    rng = np.random.default_rng(BOOTSTRAP_SEED)

    sensitivity_table = []
    for t_days in eval_thresholds:
        row_data = con.execute(f"""
        SELECT
            customer_id,
            buyer_type,
            geo_segment,
            DATEDIFF('day', first_purchase_date, DATE '{t_max_str}') AS tenure_days,
            DATEDIFF('day', last_purchase_date, DATE '{t_max_str}') AS days_since_last
        FROM customer_summary
        ORDER BY customer_id
        """).fetchall()

        total_cust = len(row_data)
        right_censored = sum(1 for r in row_data if r[3] < t_days)
        uncensored_flags = np.array(
            [1 if r[4] > t_days else 0 for r in row_data if r[3] >= t_days],
            dtype=np.int32,
        )
        uncensored_count = len(uncensored_flags)
        churned_count = int(np.sum(uncensored_flags))
        churn_rate = round(float(churned_count / uncensored_count), 6) if uncensored_count else 0.0

        boot_means = [
            float(np.mean(rng.choice(uncensored_flags, size=uncensored_count, replace=True)))
            for _ in range(500)
        ]
        ci_low, ci_high = np.percentile(boot_means, [2.5, 97.5])

        sensitivity_table.append(
            {
                "inactivity_threshold_days": int(t_days),
                "total_customers": int(total_cust),
                "right_censored_customers": int(right_censored),
                "eligible_uncensored_customers": int(uncensored_count),
                "inferred_churned_customers": int(churned_count),
                "active_retained_customers": int(uncensored_count - churned_count),
                "inferred_churn_rate": churn_rate,
                "ci_95_lower": round(float(ci_low), 6),
                "ci_95_upper": round(float(ci_high), 6),
            }
        )

    churn_summary = {
        "metrics_version": METRICS_VERSION,
        "anchor_date_t_max": t_max_str,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "inter_purchase_gap_distribution_days": gap_percentiles,
        "recommended_threshold_days": gap_percentiles["p75"],
        "sensitivity_table": sensitivity_table,
    }

    _write_deterministic_json(CHURN_OUT, churn_summary)

    at_risk_sql = f"""
    WITH daily_p AS (
        SELECT DISTINCT customer_id, CAST(invoice_date AS DATE) AS p_date FROM tx
    ),
    gaps AS (
        SELECT
            customer_id,
            DATEDIFF('day', LAG(p_date) OVER (PARTITION BY customer_id ORDER BY p_date), p_date) AS gap_days
        FROM daily_p
    ),
    cust_gaps AS (
        SELECT
            customer_id,
            ROUND(MEDIAN(gap_days), 2) AS median_gap_days,
            ROUND(AVG(gap_days), 2) AS mean_gap_days
        FROM gaps
        WHERE gap_days IS NOT NULL
        GROUP BY customer_id
    )
    SELECT
        c.customer_id,
        c.buyer_type,
        c.geo_segment,
        c.distinct_purchase_days,
        c.total_invoices,
        c.lifetime_revenue,
        CAST(c.last_purchase_date AS VARCHAR) AS last_purchase_date,
        DATEDIFF('day', c.last_purchase_date, DATE '{t_max_str}') AS days_since_last,
        g.median_gap_days,
        g.mean_gap_days,
        ROUND(DATEDIFF('day', c.last_purchase_date, DATE '{t_max_str}') / NULLIF(g.median_gap_days, 0), 2) AS overdue_ratio
    FROM customer_summary c
    JOIN cust_gaps g ON c.customer_id = g.customer_id
    WHERE c.distinct_purchase_days >= 3
      AND DATEDIFF('day', c.last_purchase_date, DATE '{t_max_str}') <= 180
      AND DATEDIFF('day', c.last_purchase_date, DATE '{t_max_str}') > GREATEST(1.5 * g.median_gap_days, g.median_gap_days + 14)
    ORDER BY c.lifetime_revenue DESC, c.customer_id ASC;
    """
    at_risk_rows = con.execute(at_risk_sql).fetchall()
    at_risk_list = [
        {
            "customer_id": int(r[0]),
            "buyer_type": r[1],
            "geo_segment": r[2],
            "distinct_purchase_days": int(r[3]),
            "total_invoices": int(r[4]),
            "lifetime_revenue": float(r[5]),
            "last_purchase_date": r[6],
            "days_since_last_purchase": int(r[7]),
            "typical_median_gap_days": float(r[8]),
            "typical_mean_gap_days": float(r[9]),
            "overdue_ratio": float(r[10]),
        }
        for r in at_risk_rows
    ]

    at_risk_payload = {
        "metrics_version": METRICS_VERSION,
        "anchor_date_t_max": t_max_str,
        "eligibility_criteria": "distinct_purchase_days >= 3, days_since_last <= 180, days_since_last > max(1.5 * median_gap, median_gap + 14)",
        "total_at_risk_count": len(at_risk_list),
        "total_at_risk_lifetime_revenue": round(sum(x["lifetime_revenue"] for x in at_risk_list), 2),
        "customers": at_risk_list,
    }

    _write_deterministic_json(AT_RISK_OUT, at_risk_payload)
    return churn_summary, at_risk_payload


if __name__ == "__main__":
    conn = get_con()
    compute_cohort_retention(conn)
    compute_churn_and_at_risk(conn)
    print("Deterministic retention & churn metrics generated.")