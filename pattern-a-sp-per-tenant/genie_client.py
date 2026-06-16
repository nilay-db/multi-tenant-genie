"""
Genie Conversation API client — Pattern A (one Service Principal per tenant).

The host app resolves a merchant to a tenant, then authenticates to Genie with
*that tenant's* Service Principal. Inside the dynamic view orders_secure_a,
current_user() resolves to the calling SP's application id, which the sp_tenant_map
maps to a tenant — so Unity Catalog returns only that tenant's rows. Genie's generated
SQL never contains a tenant predicate.
"""
import time
import base64
import yaml
import requests
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


class GenieClient:
    def __init__(self, config=None, space_id=None):
        self.cfg = config or load_config()
        self.host = self.cfg["workspace_url"].rstrip("/")
        self.space_id = space_id or self.cfg["genie_space_id"]
        self._by_tenant = {t["tenant_id"]: t for t in self.cfg["tenants"]}

    # ---- auth: one SP per tenant -------------------------------------------
    def _token(self, tenant_id):
        t = self._by_tenant[tenant_id]
        basic = base64.b64encode(f"{t['sp_client_id']}:{t['sp_client_secret']}".encode()).decode()
        r = requests.post(
            f"{self.host}/oidc/v1/token",
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials", "scope": "all-apis"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    # ---- Conversation API ---------------------------------------------------
    def ask(self, tenant_id, question, poll_seconds=3, max_polls=60):
        """Ask Genie as the given tenant's SP. Returns {text, sql, columns, data, status, api}."""
        token = self._token(tenant_id)
        t = self._by_tenant[tenant_id]
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        base = f"{self.host}/api/2.0/genie/spaces/{self.space_id}"

        start = requests.post(f"{base}/start-conversation",
                              headers=h, json={"content": question}, timeout=60).json()
        conv = start.get("conversation_id") or start.get("conversation", {}).get("id")
        msg = start.get("message_id") or start.get("message", {}).get("id")

        # demo-visibility metadata (no secrets) — surfaced in the portal's "Genie API call" panel
        api = {
            "auth_method": "OAuth client_credentials (M2M) — per-tenant Service Principal",
            "authenticated_as": f"SP {t['sp_client_id']}  (current_user() at query time)",
            "token_endpoint": f"POST {self.host}/oidc/v1/token  (grant_type=client_credentials, scope=all-apis)",
            "genie_endpoint": f"POST {base}/start-conversation",
            "poll_endpoint": f"GET {base}/conversations/{conv}/messages/{msg}",
            "space_id": self.space_id,
            "conversation_id": conv,
            "message_id": msg,
        }

        message = {}
        for _ in range(max_polls):
            message = requests.get(f"{base}/conversations/{conv}/messages/{msg}", headers=h, timeout=60).json()
            if message.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
                break
            time.sleep(poll_seconds)

        out = {"status": message.get("status"), "text": None, "sql": None,
               "columns": None, "data": None, "api": api}
        for att in (message.get("attachments") or []):
            if "text" in att:
                out["text"] = att["text"].get("content")
            if "query" in att:
                out["sql"] = att["query"].get("query")
                aid = att.get("attachment_id")
                res = requests.get(f"{base}/conversations/{conv}/messages/{msg}/attachments/{aid}/query-result",
                                   headers=h, timeout=60).json()
                sr = res.get("statement_response") or {}
                manifest = sr.get("manifest") or {}
                out["columns"] = [c["name"] for c in (manifest.get("schema") or {}).get("columns", [])]
                out["data"] = (sr.get("result") or {}).get("data_array")
        return out


if __name__ == "__main__":
    c = GenieClient()
    for t in c.cfg["tenants"]:
        r = c.ask(t["tenant_id"], "What was my total revenue?")
        print(f"{t['name']:<22} {t['tenant_id']} status={r['status']} data={r['data']}")
