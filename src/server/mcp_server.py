import datetime
import hashlib
import json
import re
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as MCPServer
    except ImportError:
        class MCPServer:  # Minimal shim when running unit tests without mcp installed
            def __init__(self, name: str):
                self.name = name

            def tool(self):
                def decorator(fn):
                    return fn
                return decorator

            def run(self):
                pass

from analysis.manifest import build_manifest
from shopify.tools import get_shopify_snapshot_data, get_shopify_health_data

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
REPORTS_DIR = REPO_ROOT / "reports"
LATEST_REPORT_PATH = REPORTS_DIR / "weekly_retention_report.md"

MANIFEST_SECTION_HEADING = "## (f) Run manifest stamp"

mcp = MCPServer("Reproducible-Retention-Server")


def _read_artifact(filename: str) -> dict:
    path = PROCESSED_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Artifact {filename} missing. Run `make reproduce` first."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def strip_model_manifest(body: str) -> str:
    """
    Removes any model-written manifest section (such as `## (f) Run manifest stamp`
    or `### Cryptographic Run Manifest Stamp`) from the end of `body` so the model
    never types or transcribes hashes itself.
    """
    pattern = re.compile(
        r"(?:\n+---\s*)?\n*(?:#{2,3}\s*(?:\(f\)\s*)?(?:Cryptographic\s+)?Run\s+manifest\s+stamp\b).*$",
        re.IGNORECASE | re.DOTALL,
    )
    cleaned = pattern.sub("", body.strip())
    return cleaned.rstrip()


def _write_lf(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def _add_months(ym: str, n: int) -> str:
    year, month = map(int, ym.split("-"))
    total_months = year * 12 + (month - 1) + n
    return f"{total_months // 12:04d}-{(total_months % 12) + 1:02d}"


def _summarize_period_records(records: list[dict]) -> dict:
    summary = {}
    for seg in sorted({r["segment"] for r in records}):
        seg_rows = [r for r in records if r["segment"] == seg]
        total_active = sum(r["active_customers"] for r in seg_rows)
        total_cohort = sum(r["cohort_size"] for r in seg_rows)
        summary[seg] = {
            "cohort_count": len(seg_rows),
            "cohort_months_included": [r["cohort_month"] for r in seg_rows],
            "active_customers_at_period": total_active,
            "total_cohort_customers": total_cohort,
            "weighted_retention_rate": round(total_active / total_cohort, 6) if total_cohort else 0.0,
            "unweighted_mean_retention_rate": round(
                sum(r["retention_rate"] for r in seg_rows) / len(seg_rows), 6
            ) if seg_rows else 0.0,
        }
    return summary

@mcp.tool()
def shopify_store_overview(limit: int = 10) -> dict:
    """
    Fetches live shop metadata, products, recent orders, and customer counts from the Shopify dev store.
    - limit: Number of products and recent orders to retrieve (1 to 50).
    """
    limit = max(1, min(50, limit))
    client = ShopifyDevClient()
    if not client.is_configured:
        raise RuntimeError("Shopify credentials are not configured in .env")

    query = f"""
    query StoreOverview {{
      shop {{
        name
        myshopifyDomain
        currencyCode
        plan {{
          displayName
        }}
      }}
      products(first: {limit}) {{
        nodes {{
          id
          title
          handle
          status
          totalInventory
        }}
      }}
      orders(first: {limit}, sortKey: CREATED_AT, reverse: true) {{
        nodes {{
          id
          name
          createdAt
          displayFinancialStatus
          totalPriceSet {{
            shopMoney {{
              amount
              currencyCode
            }}
          }}
        }}
      }}
    }}
    """
    return client.graphql_query(query)

@mcp.tool()
def get_cohort_retention(
    cohort_by: str = "all",
    period_months: int = 12,
    exclude_truncated_cohorts: bool = True,
) -> dict:
    """
    Returns deterministic monthly cohort retention rates from the UCI Online Retail II warehouse.
    - cohort_by: Grouping dimension ('all', 'buyer_type', or 'geo_segment').
    - period_months: Maximum number of months after acquisition (0 to period_months) to include.
    - exclude_truncated_cohorts: If True, filters out left-censored 2009-12 and cohorts whose activity month
      lands in the right-truncated final month (2011-12, truncated at 2011-12-09).
    """
    data = _read_artifact("cohort_retention.json")
    if cohort_by not in data["groupings"]:
        raise ValueError(f"cohort_by must be one of {list(data['groupings'].keys())}")

    all_filtered = [
        r for r in data["groupings"][cohort_by] if r["period_n"] <= period_months
    ]
    untruncated_filtered = [
        r
        for r in all_filtered
        if r["cohort_month"] != "2009-12"
        and _add_months(r["cohort_month"], r["period_n"]) < "2011-12"
    ]

    active_records = untruncated_filtered if exclude_truncated_cohorts else all_filtered
    target_all = [r for r in all_filtered if r["period_n"] == period_months]
    target_untruncated = [r for r in untruncated_filtered if r["period_n"] == period_months]

    return {
        "metrics_version": data["metrics_version"],
        "cohort_by": cohort_by,
        "max_period_months": period_months,
        "exclude_truncated_cohorts": exclude_truncated_cohorts,
        "truncation_rule": "Excludes 2009-12 (left-censored start of dataset) and activity month 2011-12 (right-truncated at 2011-12-09, only 9 days observed).",
        "non_monotonic_warning": "Retail customers can return after multi-month gaps; retention rates are not monotonically decreasing.",
        "segment_definition_and_circularity_caveat": (
            "Wholesale_Proxy is defined over the full observation window as avg_invoice_revenue >= £300.00 "
            "OR lifetime_qty >= 1,000 units (docs/METRICS.md). Caveat: because lifetime_qty sums units across "
            "all repeat orders over the observation window, Wholesale_Proxy correlates with multi-month retention partly by construction."
        ),
        "period_target_summary": {
            "active_selection": _summarize_period_records(
                target_untruncated if exclude_truncated_cohorts else target_all
            ),
            "excluding_truncated_cohorts_2010_01_to_2011_05": _summarize_period_records(target_untruncated),
            "all_cohorts_including_truncated": _summarize_period_records(target_all),
        },
        "records": active_records,
    }


@mcp.tool()
def get_churn_summary(inactivity_days: int = 90) -> dict:
    """
    Returns inferred churn statistics at the requested inactivity_days threshold alongside
    the full sensitivity table (60, 90, 120, 180 days, plus empirical p75 gap) and right-censoring counts.
    Anchored to dataset T_MAX (2011-12-09) so results never drift over time.
    """
    data = _read_artifact("churn_summary.json")
    matching = [
        row
        for row in data["sensitivity_table"]
        if row["inactivity_threshold_days"] == inactivity_days
    ]
    selected = matching[0] if matching else None
    return {
        "metrics_version": data["metrics_version"],
        "anchor_date_t_max": data["anchor_date_t_max"],
        "requested_inactivity_days": inactivity_days,
        "selected_threshold_result": selected,
        "inter_purchase_gap_distribution_days": data["inter_purchase_gap_distribution_days"],
        "full_sensitivity_table": data["sensitivity_table"],
    }


@mcp.tool()
def get_at_risk_customers(limit: int = 25) -> dict:
    """
    Returns repeat customers (>= 3 purchase days) who have exceeded their personal inter-purchase cadence
    (days_since_last > max(1.5 * median_gap, median_gap + 14)) but are within 180 days of T_MAX.
    Sorted by lifetime revenue (in GBP £) descending. Returns anonymized customer IDs and aggregates only.
    """
    data = _read_artifact("at_risk_customers.json")
    capped_limit = max(1, min(limit, 100))
    total_rev = data["total_at_risk_lifetime_revenue"]
    top_customers = [
        {
            **cust,
            "currency": "GBP",
            "lifetime_revenue_gbp": f"£{cust['lifetime_revenue']:,.2f}",
        }
        for cust in data["customers"][:capped_limit]
    ]
    return {
        "metrics_version": data["metrics_version"],
        "anchor_date_t_max": data["anchor_date_t_max"],
        "currency": "GBP (£)",
        "eligibility_criteria": data["eligibility_criteria"],
        "total_at_risk_count": data["total_at_risk_count"],
        "total_at_risk_lifetime_revenue": total_rev,
        "total_at_risk_lifetime_revenue_gbp": f"£{total_rev:,.2f}",
        "top_at_risk_customers": top_customers,
    }


@mcp.tool()
def run_data_health_check() -> dict:
    """
    Returns the UCI warehouse data-quality health report and the exact exclusions ledger
    reconciling 1,067,371 raw rows down to the clean analysis dataset.
    """
    report = _read_artifact("health_report.json")
    ledger_counts = report["reconciliation"]["exclusions_by_rule"]
    rule_to_ledger_key = {
        "RULE_01_EXACT_DUPLICATE": (1, "1_EXACT_DUPLICATE"),
        "RULE_02_MISSING_CUSTOMER_ID": (2, "2_MISSING_CUSTOMER_ID"),
        "RULE_03_CANCELLATION_INVOICE": (3, "3_CANCELLATION_INVOICE"),
        "RULE_04_NON_PRODUCT_CODE": (4, "4_NON_PRODUCT_STOCK_CODE"),
        "RULE_05_NON_POSITIVE_PRICE_OR_QTY": (5, "5_INVALID_QTY_OR_PRICE"),
    }
    for rule in report.get("rules", []):
        priority, ledger_key = rule_to_ledger_key.get(rule["rule_id"], (None, None))
        if priority is not None:
            seq_count = ledger_counts.get(ledger_key, 0)
            rule["priority_order"] = priority
            rule["ledger_disposition_key"] = ledger_key
            rule["sequential_ledger_excluded_count"] = seq_count
            rule["already_excluded_by_higher_priority_rules"] = (
                rule["raw_failing_count"] - seq_count
            )

    report["rule_evaluation_order"] = [
        "1_EXACT_DUPLICATE",
        "2_MISSING_CUSTOMER_ID",
        "3_CANCELLATION_INVOICE",
        "4_NON_PRODUCT_STOCK_CODE",
        "5_INVALID_QTY_OR_PRICE",
    ]
    report["waterfall_vs_marginal_explanation"] = (
        "reconciliation.exclusions_by_rule applies rules 1 through 5 sequentially in a mutually exclusive CASE WHEN waterfall "
        "(each excluded row is counted once under the first rule it triggers, summing to 290,794). "
        "By contrast, rules[].raw_failing_count evaluates each rule independently against the raw table before prior exclusions "
        "(e.g., 243,007 total rows lack a Customer ID, of which 7,856 were already removed by Rule 1 as exact duplicates, leaving 235,151 in the ledger)."
    )
    return report


@mcp.tool()
def get_run_manifest() -> dict:
    """
    Builds and returns fresh cryptographic provenance for the analysis: raw dataset SHA-256,
    output artifact hashes, git commit & dirty-tree flag, lockfile hash, agent model ID,
    checker version, run timestamp, and Shopify response hash & query timestamp.
    """
    return build_manifest()


@mcp.tool()
def shopify_subscription_snapshot(use_fixture: bool = False) -> dict:
    """
    Returns subscription contract status counts, selling plan intervals, and cancellation reasons
    from the isolated Shopify dev store integration (never merged with the UCI warehouse).
    """
    return get_shopify_snapshot_data(use_fixture=use_fixture)


@mcp.tool()
def shopify_data_health_check(use_fixture: bool = False) -> dict:
    """
    Runs data integrity checks on the Shopify subscription layer: flags subscription orders missing
    a backing contract ID and ACTIVE contracts missing a nextBillingDate.
    """
    return get_shopify_health_data(use_fixture=use_fixture)


@mcp.tool()
def save_report(body: str) -> dict:
    """Finalize and save the report. Call last. Appends the run manifest."""
    clean_body = strip_model_manifest(body)
    manifest = build_manifest()
    stamp = json.dumps(manifest, indent=2, sort_keys=True)
    full_text = f"{clean_body}\n\n{MANIFEST_SECTION_HEADING}\n```json\n{stamp}\n```\n"

    date_slug = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    dated_path = _write_lf(REPORTS_DIR / f"{date_slug}.md", full_text)
    _write_lf(LATEST_REPORT_PATH, full_text)

    manifest_sha256 = hashlib.sha256(stamp.encode("utf-8")).hexdigest()
    return {
        "path": str(dated_path),
        "latest_path": str(LATEST_REPORT_PATH),
        "manifest_sha256": manifest_sha256,
    }


if __name__ == "__main__":
    mcp.run()