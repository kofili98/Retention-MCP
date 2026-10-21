# validations/validate_shopify.py
from server.mcp_server import shopify_data_health_check, shopify_subscription_snapshot
from shopify.client import ShopifyDevClient

print("Validating Phases 6 & 7: Shopify Client & Tools...")

client = ShopifyDevClient()
print(f"Live Shopify credentials configured: {client.is_configured}")

snap = shopify_subscription_snapshot(use_fixture=True)
assert snap["total_contracts"] == 8
assert snap["status_breakdown"] == {"ACTIVE": 4, "PAUSED": 2, "CANCELLED": 2}
assert snap["explicit_contract_churn_rate"] == 0.25

health = shopify_data_health_check(use_fixture=True)
assert health["status"] == "WARNING"
checks = {c["rule_id"]: c for c in health["checks"]}
assert checks["SHOPIFY_01_ORPHAN_SELLING_PLAN_ORDER"]["failing_count"] == 1
assert checks["SHOPIFY_02_ACTIVE_MISSING_NEXT_BILLING"]["failing_count"] == 1

print("PHASES 6 & 7 PASSED: Shopify snapshot and health checks verified!")