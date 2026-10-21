# Reproducible Retention Analysis: MCP + Agent Pipeline

An open, deterministic cohort retention and inferred churn pipeline built on real public transaction data, exposed via a read-only **Model Context Protocol (MCP)** server and automated weekly reporting agent, alongside an isolated **Shopify Subscriptions** integration layer.

## 1. Problem Statement & Core Claim
Retention analyses frequently fail basic reproducibility checks because churn definitions are implicit, data cleaning happens ad-hoc in spreadsheets, or relative dates (`NOW()`) cause historical numbers to drift.

**Core Claim:** Anyone can clone this repository, run `make reproduce`, and obtain the exact same cohort retention tables, exclusions ledger, and inferred churn sensitivity numbers—verified down to the SHA-256 hash of every output artifact.

## 2. Goals & Non-Goals
- **Goals:**
  1. Compute cohort retention, inferred churn, and personalized at-risk flags using pre-registered definitions (`docs/METRICS.md`).
  2. Surface real data-quality defects via an auditable **Exclusions Ledger** where `Raw Rows - Exclusions = Analysis Rows`.
  3. Enforce cryptographic provenance via a `run_manifest.json` stamping raw data hashes, git commit, and package versions.
  4. Expose read-only MCP tools so Claude can query verifiable numbers without hallucinating calculations.
  5. Demonstrate live/fixture Shopify Selling Plan & Subscription Contract health checks in isolation.
- **Non-Goals:**
  - Predicting churn with black-box ML models.
  - Claiming non-contractual gift-ware findings generalize to SaaS or recurring subscription businesses.
  - Merging or reconciling the Shopify test store with the UCI transaction warehouse.

## 3. Dataset Attribution & License
- **Dataset:** [UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online+retail+ii) (`1,067,371` raw rows across two workbook sheets covering `2009-12-01` to `2011-12-09`).
- **Credit:** Daqing Chen, Course Director: MSc Data Science, School of Engineering, London South Bank University, UK.
- **License:** Licensed under **Creative Commons Attribution 4.0 International (CC BY 4.0)**. Raw files are fetched directly from UCI via `make data` and verified by SHA-256 checksum.

## 4. Data Quality Findings & Exclusions Ledger
Rather than silently dropping malformed rows, `analysis/health.py` classifies every row sequentially across five mutually exclusive rules:

| Priority | Rule ID | Disposition | Description |
|---|---|---|---|
| 1 | `1_EXACT_DUPLICATE` | `EXCLUDE` | Identical transaction rows (including overlapping Dec 2010 boundary rows between Sheet 1 and Sheet 2). |
| 2 | `2_MISSING_CUSTOMER_ID` | `EXCLUDE_FROM_COHORTS` | ~243k guest/counter transactions lacking a `Customer ID`; cannot be tracked across months. |
| 3 | `3_CANCELLATION_INVOICE` | `EXCLUDE_AS_RETURN` | Invoices prefixed with `C` representing returns/cancellations rather than active purchases. |
| 4 | `4_NON_PRODUCT_STOCK_CODE` | `EXCLUDE` | Postage (`POST`), manuals (`M`), bank charges, Amazon fees (`DOT`), bad debt (`ADJUST`), and `TEST` entries. |
| 5 | `5_INVALID_QTY_OR_PRICE` | `EXCLUDE` | Non-cancellation lines with `Quantity <= 0` or `Price <= 0.0` (inventory write-offs). |
| — | `0_VALID_ANALYSIS_ROW` | `KEEP` | Clean customer purchase records reconciled into `clean_transactions.parquet`. |

## 5. Pre-Registered Metric Definitions & Sensitivity Context
Full equations live in [`docs/METRICS.md`](docs/METRICS.md):
- **Anchor Timestamp (`T_MAX`):** `2011-12-09` (`MAX(invoice_date)`), preventing clock drift.
- **Non-Monotonic Cohort Retention:** Share of a cohort active in month $C_0 + n$. Because customers can skip a month and return later, retention curves are non-monotonic.
- **Inferred Churn & Right-Censoring:** Evaluated across $T \in \{60, 90, 120, 180\}$ days plus the empirical 75th percentile of inter-purchase gaps. Customers acquired within $T$ days of `T_MAX` are flagged as **right-censored** and excluded from the denominator.
- **At-Risk Customers:** Repeat customers ($\ge 3$ active purchase days) whose days since last purchase exceeds $\max(1.5 \times \text{median\_gap}, \text{median\_gap} + 14)$.

## 6. Shopify Subscription Layer Learnings
- Built against the post-January 2026 **Shopify Dev Dashboard** OAuth Client Credentials flow (`shopify/client.py`).
- Demonstrates how contractual subscriptions (`ACTIVE`, `PAUSED`, `CANCELLED` via `SubscriptionContract` and `SellingPlan`) differ structurally from inferred retail churn.
- Includes automated integrity checks for orphan selling-plan orders missing a contract ID and active contracts missing a `nextBillingDate`.

## 7. Quickstart (One-Command Reproduction)
```bash
git clone <repo-url>
cd reproducible-retention-mcp
pip install -e .
make reproduce
make report