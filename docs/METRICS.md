# Metric Definitions & Analytical Rules (Version 1.0.0)

All metrics are computed strictly from `data/processed/clean_transactions.parquet` (rows matching `0_VALID_ANALYSIS_ROW`).

## 1. Anchor Timestamp (`T_MAX`)
To prevent temporal drift across runs, "current date" is never evaluated against the system clock. All recency and inactivity calculations are anchored to the dataset's maximum valid invoice timestamp:
- `T_MAX = MAX(invoice_date)` (`2011-12-09 12:50:00`).

## 2. Active Customer & Acquisition Cohort
- **Active Customer in Month $M$:** A distinct `customer_id` with at least one valid (`0_VALID_ANALYSIS_ROW`) purchase where `DATE_TRUNC('month', invoice_date) = M`.
- **Acquisition Cohort ($C_0$):** The calendar month `YYYY-MM` of a customer's first valid purchase across the entire observation window (`MIN(DATE_TRUNC('month', invoice_date))`).

## 3. Monthly Cohort Retention ($R_{C, n}$)
- **Period Index ($n$):** The integer number of months elapsed between the cohort month $C_0$ and activity month $M$:
  $$n = (\text{Year}_M - \text{Year}_{C_0}) \times 12 + (\text{Month}_M - \text{Month}_{C_0})$$
- **Retention Rate at Month $n$:**
  $$R_{C, n} = \frac{\text{Active Customers in Cohort } C \text{ during Month } C_0 + n}{\text{Total Customers in Cohort } C \text{ at } n = 0}$$
- **Non-Monotonicity Note:** Because this is non-contractual retail data, customers can be inactive in month $n=1$ and return in month $n=2$. Therefore, $R_{C, n}$ is **not** monotonically decreasing.

## 4. Inferred Churn & Choosing $T$ from Data
Because the dataset contains no subscription contracts, churn is inferred from prolonged inactivity up to `T_MAX`.
- **Inter-Purchase Gap Distribution:** Computed across all consecutive active purchase days (`DATEDIFF('day', prev_purchase_date, purchase_date)`) for repeat customers.
- **Data-Driven Default Threshold ($T_{p75}$):** Set to the **75th percentile** of all observed inter-purchase gaps in the dataset, rounded to the nearest integer day, alongside a standard 90-day operational benchmark.
- **Sensitivity Table:** Published automatically for $T \in \{60, 90, 120, 180\}$ days.
- **Right-Censoring Rule:** Any customer whose **first purchase** occurred within $T$ days of `T_MAX` (`DATEDIFF('day', first_purchase_date, T_MAX) < T`) cannot yet be observed for a full $T$-day inactivity window. These customers are flagged as `RIGHT_CENSORED` and excluded from the denominator of the Confirmed Churn Rate for threshold $T$.
- **Inferred Churned Customer:** An uncensored customer whose days since last purchase exceeds $T$:
  $$\text{days\_since\_last} = \text{DATEDIFF}('day', \text{last\_purchase\_date}, T_{\text{MAX}}) > T$$

## 5. At-Risk Customers (Personalized Cadence Violation)
One-time buyers cannot have an individual cadence and are excluded to prevent over-flagging.
- **Eligibility:** Customers with $\ge 3$ distinct active purchase dates (`distinct_purchase_days >= 3`) who are not yet past the extreme 180-day global churn cutoff.
- **Customer Cadence ($\mu_i, \sigma_i$):** Mean and median inter-purchase gap in days for customer $i$.
- **At-Risk Flag:** A customer is flagged `AT_RISK` when:
  $$\text{days\_since\_last}_i > \max(1.5 \times \text{median\_gap}_i, \text{median\_gap}_i + 14)$$
- **Risk Ratio:** $\text{days\_since\_last}_i / \text{median\_gap}_i$.

## 6. Segments (Declared A Priori)
- **Geography (`geo_segment`):** `UK` if `country = 'United Kingdom'`, else `International`.
- **Order-Size Proxy (`buyer_type`):** Based on customer average order (invoice) value (`avg_invoice_ revenue`):
  - `Wholesale_Proxy`: Average invoice revenue $\ge £300.00$ OR lifetime total quantity $\ge 1,000$ units.
  - `Retail_Proxy`: All other customers.