import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


class ShopifyDevClient:
    """
    Handles post-Jan-2026 Shopify Dev Dashboard OAuth Client Credentials exchange
    and read-only GraphQL Admin API queries.
    """

    def __init__(self) -> None:
        self.shop_domain = os.getenv("SHOPIFY_SHOP_DOMAIN", "")
        self.client_id = os.getenv("SHOPIFY_CLIENT_ID", "")
        self.client_secret = os.getenv("SHOPIFY_CLIENT_SECRET", "")
        self.api_version = os.getenv("SHOPIFY_API_VERSION", "2026-01")
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(self.shop_domain and self.client_id and self.client_secret)

    def _get_token(self) -> str:
        now = time.time()
        if self._access_token and now < (self._token_expires_at - 60):
            return self._access_token

        url = f"https://{self.shop_domain}/admin/oauth/access_token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        # Dev Dashboard client credential tokens expire after 24 hours (86,400s) by default
        expires_in = int(data.get("expires_in", 86400))
        self._token_expires_at = now + expires_in
        return self._access_token

    def graphql_query(self, query: str, variables: dict | None = None) -> dict:
        token = self._get_token()
        url = f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"
        headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }
        resp = requests.post(
            url, json={"query": query, "variables": variables or {}}, headers=headers, timeout=20
        )
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"Shopify GraphQL error: {body['errors']}")
        return body["data"]