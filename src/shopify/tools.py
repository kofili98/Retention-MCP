import datetime
import hashlib
import json
from pathlib import Path
from shopify.client import ShopifyDevClient

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "shopify_sanitized_fixture.json"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
SHOPIFY_PROVENANCE_PATH = PROCESSED_DIR / "shopify_query_provenance.json"

LIVE_SUBSCRIPTION_QUERY = """
query GetSubscriptionSnapshot {
  subscriptionContracts(first: 100) {
    nodes {
      id
      status
      nextBillingDate
      billingPolicy {
        interval
        intervalCount
      }
    }
  }
  orders(first: 100, query: "selling_plan:*") {
    nodes {
      id
      lineItems(first: 20) {
        nodes {
          sellingPlan {
            name
          }
          contract {
            id
          }
        }
      }
    }
  }
}
"""


def _record_shopify_provenance(payload: dict, query_timestamp_utc: str) -> dict:
    """Computes and persists the SHA-256 hash and UTC query time of the Shopify payload for `build_manifest()`."""
    canonical_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    response_hash = hashlib.sha256(canonical_bytes).hexdigest()
    prov = {
        "shopify_data_source": payload.get("source", "unknown"),
        "shopify_response_sha256": response_hash,
        "shopify_query_timestamp_utc": query_timestamp_utc,
    }
    try:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        with open(SHOPIFY_PROVENANCE_PATH, "w", encoding="utf-8", newline="\n") as f:
            json.dump(prov, f, indent=2, sort_keys=True)
            f.write("\n")
    except Exception:
        pass
    return prov


def _load_payload(use_fixture: bool = False) -> dict:
    query_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if use_fixture:
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        _record_shopify_provenance(payload, query_ts)
        return payload

    client = ShopifyDevClient()
    if not client.is_configured:
        raise RuntimeError(
            "Live Shopify data requested (use_fixture=False), but Shopify credentials "
            "are not configured in .env."
        )

    data = client.graphql_query(LIVE_SUBSCRIPTION_QUERY)
    contracts = [
        {
            "id": node["id"],
            "status": node["status"],
            "nextBillingDate": node.get("nextBillingDate"),
            "interval": node.get("billingPolicy", {}).get("interval"),
            "intervalCount": node.get("billingPolicy", {}).get("intervalCount"),
        }
        for node in data.get("subscriptionContracts", {}).get("nodes", [])
    ]
    orders = []
    for order in data.get("orders", {}).get("nodes", []):
        for item in order.get("lineItems", {}).get("nodes", []):
            if item.get("sellingPlan"):
                orders.append(
                    {
                        "order_id": order["id"],
                        "selling_plan_name": item["sellingPlan"]["name"],
                        "contract_id": (item.get("contract") or {}).get("id"),
                    }
                )
    payload = {
        "source": f"shopify_live:{client.shop_domain}",
        "subscription_contracts": contracts,
        "selling_plan_orders": orders,
    }
    _record_shopify_provenance(payload, query_ts)
    return payload


def get_shopify_snapshot_data(use_fixture: bool = False) -> dict:
    payload = _load_payload(use_fixture=use_fixture)
    contracts = payload["subscription_contracts"]
    status_counts = {}
    for c in contracts:
        status_counts[c["status"]] = status_counts.get(c["status"], 0) + 1
    total = len(contracts)
    cancelled = status_counts.get("CANCELLED", 0)
    active = status_counts.get("ACTIVE", 0)
    paused = status_counts.get("PAUSED", 0)
    return {
        "isolation_notice": "Shopify dev store holds test records demonstrating subscription contract integration. Never merged with the UCI warehouse.",
        "data_source": payload["source"],
        "total_contracts": total,
        "status_breakdown": status_counts,
        "explicit_contract_churn_rate": round(cancelled / total, 4) if total else 0.0,
        "active_plus_paused_retention_rate": round((active + paused) / total, 4) if total else 0.0,
        "selling_plan_orders_count": len(payload["selling_plan_orders"]),
    }


def get_shopify_health_data(use_fixture: bool = False) -> dict:
    payload = _load_payload(use_fixture=use_fixture)
    contracts = payload["subscription_contracts"]
    orders = payload["selling_plan_orders"]
    known_contract_ids = {c["id"] for c in contracts}
    orphan_orders = [
        o["order_id"]
        for o in orders
        if not o.get("contract_id") or o["contract_id"] not in known_contract_ids
    ]
    active_missing_billing = [
        c["id"]
        for c in contracts
        if c["status"] == "ACTIVE" and not c.get("nextBillingDate")
    ]
    return {
        "data_source": payload["source"],
        "status": "WARNING" if (orphan_orders or active_missing_billing) else "HEALTHY",
        "checks": [
            {
                "rule_id": "SHOPIFY_01_ORPHAN_SELLING_PLAN_ORDER",
                "severity": "HIGH",
                "description": "Orders carrying a sellingPlan with no matching SubscriptionContract.",
                "failing_count": len(orphan_orders),
                "sample_ids": orphan_orders,
            },
            {
                "rule_id": "SHOPIFY_02_ACTIVE_MISSING_NEXT_BILLING",
                "severity": "WARNING",
                "description": "ACTIVE subscription contracts missing a nextBillingDate timestamp.",
                "failing_count": len(active_missing_billing),
                "sample_ids": active_missing_billing,
            },
        ],
    }