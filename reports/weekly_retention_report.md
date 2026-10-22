# Weekly Reproducible Retention Report

## 1. Data Health & Exclusions Ledger
- **Status:** `HEALTHY` (Exact Reconciliation: `True`)
- **Raw Rows:** `1,067,371` | **Excluded Rows:** `290,794` | **Clean Analysis Rows:** `776,577`
- **Primary Exclusions:** Missing Customer ID (`235,151`), Exact Duplicates (`34,335`), Cancellations (`18,390`).

## 2. Inferred Churn & Sensitivity Context (Anchored to 2011-12-09)
- **90-Day Inferred Churn Rate:** `56.45%` (95% CI: `[55.04%, 57.67%]`) across `5,256` uncensored customers (`596` right-censored).
- **Empirical Inter-Purchase Gap:** Median `32` days, 75th percentile `70` days.
- **Caveat:** Because UCI Online Retail II is non-contractual gift-ware data, inactivity does not prove permanent defection. What the data does not support: we cannot infer why customers paused purchasing or generalize non-contractual gift rhythms to SaaS/subscription billing.

## 3. At-Risk Customers & Testable Experiment Hypothesis
- **At-Risk Population:** `324` repeat customers (`>= 3` purchase days) have exceeded their personal median cadence (`> max(1.5x median, median + 14d)`), representing `£1,390,289.84` in historical revenue.
- **Proposed Experiment:**
  - **Hypothesis:** Sending a personalized restock reminder triggered at `1.5x` a repeat buyer's median inter-purchase gap will increase 30-day reactivation compared to no outreach.
  - **Target Population:** Customers flagged in `get_at_risk_customers` randomized 50/50 into Treatment (cadence-triggered reminder) vs. Control (holdout, no outreach).
  - **Primary Metric:** 30-day Reactivation Rate (at least one valid non-cancelled invoice within 30 days of trigger) and Net Incremental Revenue per Customer (net of `C`-prefixed returns).

## 4. Isolated Shopify Subscription Layer
- **Contract Snapshot (`shopify_live:komo-retention.myshopify.com`):** `0` total contracts (`{}`).
- **Integrity Checks (`HEALTHY`):** Flagged `0` selling-plan order missing a contract ID and `0` ACTIVE contract missing `nextBillingDate`.

---
### Cryptographic Run Manifest Stamp
```json
{
  "artifact_sha256_hashes": {
    "at_risk_customers.json": "9c08c4fdb950a6522fa84227c8cac11b8d46f959d15275ab4ee9dde8faa2bd49",
    "churn_summary.json": "427779fce744cb0af715dde4dcb4a688ab1a110a6da4f622967f5902fc95abdb",
    "cohort_retention.json": "1f67a96d572e8cfb4f97fe9f2342fd76a0b17e6be4984e3e0e43d46122e57467",
    "exclusions_ledger.json": "bd84bd6f0084225d70f7f23c4363bd0cb8690845ada208aae4e70343aef113c3",
    "health_report.json": "b7900cfa84077efadcbeed646bd2670bd56dbf611d70bae3c6fdad1a5796f90b"
  },
  "git_commit": "efd877224ac2ac28faf1c83b439b4577e3ec5a81",
  "metrics_version": "1.0.0",
  "package_versions": {
    "anthropic": "2.1.0",
    "duckdb": "2.0.0",
    "mcp": "2.3.0",
    "openpyxl": "3.1.5",
    "pandas": "3.0.6",
    "pyarrow": "26.0.0",
    "pydantic": "2.14.1"
  },
  "parameters": {
    "anchor_date_rule": "MAX(invoice_date)",
    "at_risk_min_purchase_days": 3,
    "bootstrap_seed": 42,
    "sensitivity_thresholds_days": [
      60,
      90,
      120,
      180
    ]
  },
  "python_version": "3.15.0",
  "raw_xlsx_sha256": "bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980"
}
```
