# shopify/tools.py
import json
from pathlib import Path
from shopify.client import ShopifyDevClient

FIXTURE_PATH = Path("tests/fixtures/shopify_sanitized_fixture.json")

def _load_payload(use_fixture: bool = True) -> dict:
    client = ShopifyDevClient()
    if use_fixture or not client.is_configured:
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    # Fallback or live query if configured
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_shopify_snapshot_data(use_fixture: bool = True) -> dict:
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

def get_shopify_health_data(use_fixture: bool = True) -> dict:
    payload = _load_payload(use_fixture=use_fixture)
    contracts = payload["subscription_contracts"]
    orders = payload["selling_plan_orders"]
    known_contract_ids = {c["id"] for c in contracts}
    orphan_orders = [
        o["order_id"] for o in orders if not o.get("contract_id") or o["contract_id"] not in known_contract_ids
    ]
    active_missing_billing = [
        c["id"] for c in contracts if c["status"] == "ACTIVE" and not c.get("nextBillingDate")
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