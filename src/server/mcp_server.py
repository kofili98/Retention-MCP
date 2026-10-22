from pathlib import Path
from shopify.client import ShopifyDevClient
import json

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer
from shopify.tools import get_shopify_snapshot_data, get_shopify_health_data

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

mcp = MCPServer(
    "Reproducible-Retention-Server"
)


def _read_artifact(filename: str) -> dict:
    path = PROCESSED_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Artifact {filename} missing. Run `make reproduce` first."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

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
def get_cohort_retention(cohort_by: str = "all", period_months: int = 12) -> dict:
    """
    Returns deterministic monthly cohort retention rates from the UCI Online Retail II warehouse.
    - cohort_by: Grouping dimension. Must be one of 'all', 'buyer_type' (Wholesale_Proxy vs Retail_Proxy), or 'geo_segment' (UK vs International).
    - period_months: Maximum number of months after acquisition (0 to period_months) to include.
    Note: Retention is non-monotonic because non-contractual customers can return after inactive months.
    """
    data = _read_artifact("cohort_retention.json")
    if cohort_by not in data["groupings"]:
        raise ValueError(f"cohort_by must be one of {list(data['groupings'].keys())}")

    filtered = [
        r for r in data["groupings"][cohort_by] if r["period_n"] <= period_months
    ]

    target_records = [r for r in filtered if r["period_n"] == period_months]
    summary_by_segment = {}
    for seg in sorted({r["segment"] for r in target_records}):
        seg_rows = [r for r in target_records if r["segment"] == seg]
        total_active = sum(r["active_customers"] for r in seg_rows)
        total_cohort = sum(r["cohort_size"] for r in seg_rows)
        summary_by_segment[seg] = {
            "eligible_cohorts": len(seg_rows),
            "active_customers_at_period": total_active,
            "total_cohort_customers": total_cohort,
            "weighted_retention_rate": round(total_active / total_cohort, 6) if total_cohort else 0.0,
            "unweighted_mean_retention_rate": round(
                sum(r["retention_rate"] for r in seg_rows) / len(seg_rows), 6
            ) if seg_rows else 0.0,
        }

    return {
        "metrics_version": data["metrics_version"],
        "cohort_by": cohort_by,
        "max_period_months": period_months,
        "non_monotonic_warning": "Retail customers can return after multi-month gaps; retention rates are not monotonically decreasing.",
        "period_target_summary": summary_by_segment,
        "records": filtered,
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
    Sorted by lifetime revenue descending. Returns anonymized customer IDs and aggregates only.
    """
    data = _read_artifact("at_risk_customers.json")
    capped_limit = max(1, min(limit, 100))
    return {
        "metrics_version": data["metrics_version"],
        "anchor_date_t_max": data["anchor_date_t_max"],
        "eligibility_criteria": data["eligibility_criteria"],
        "total_at_risk_count": data["total_at_risk_count"],
        "total_at_risk_lifetime_revenue": data["total_at_risk_lifetime_revenue"],
        "top_at_risk_customers": data["customers"][:capped_limit],
    }


@mcp.tool()
def run_data_health_check() -> dict:
    """
    Returns the UCI warehouse data-quality health report and the exact exclusions ledger
    reconciling 1,067,371 raw rows down to the clean analysis dataset.
    """
    return _read_artifact("health_report.json")


#@mcm.tool() if False else None # placeholder safety

@mcp.tool()
def get_run_manifest() -> dict:
    """
    Returns cryptographic provenance for the analysis: raw dataset SHA-256, output artifact hashes,
    git commit hash, Python version, and pinned package versions. Call this to verify trust.
    """
    return _read_artifact("run_manifest.json")


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


if __name__ == "__main__":
    mcp.run()