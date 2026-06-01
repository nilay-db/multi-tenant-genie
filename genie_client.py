"""
Genie Conversation API client — Pattern B (one shared SP + custom identity claim).

Per request, the host app mints an OAuth token from the SINGLE shared SP with
custom_claim=<tenant>. Unity Catalog's row filter reads the claim via
current_oauth_custom_identity_claim() and isolates the tenant's rows.
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
    def __init__(self, config=None):
        self.cfg = config or load_config()
        self.host = self.cfg["workspace_url"].rstrip("/")
        self.space_id = self.cfg["genie_space_id"]
        self.sp = self.cfg["app_sp"]

    # ---- auth: ONE shared SP, claim injected per request --------------------
    def _token(self, claim):
        basic = base64.b64encode(f"{self.sp['client_id']}:{self.sp['client_secret']}".encode()).decode()
        r = requests.post(
            f"{self.host}/oidc/v1/token",
            headers={"Authorization": f"Basic {basic}"},
            # custom_claim is the key bit — it embeds {"custom":{"claim":"<value>"}} in the JWT.
            data={"grant_type": "client_credentials", "scope": "all-apis", "custom_claim": claim},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    # ---- Conversation API ---------------------------------------------------
    def ask(self, claim, question, poll_seconds=3, max_polls=60):
        """Ask Genie scoped to the given claim. Returns {text, sql, columns, data, status}."""
        token = self._token(claim)
        h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        base = f"{self.host}/api/2.0/genie/spaces/{self.space_id}"

        start = requests.post(f"{base}/start-conversation",
                              headers=h, json={"content": question}, timeout=60).json()
        conv = start.get("conversation_id") or start.get("conversation", {}).get("id")
        msg = start.get("message_id") or start.get("message", {}).get("id")

        message = {}
        for _ in range(max_polls):
            message = requests.get(f"{base}/conversations/{conv}/messages/{msg}", headers=h, timeout=60).json()
            if message.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
                break
            time.sleep(poll_seconds)

        out = {"status": message.get("status"), "text": None, "sql": None, "columns": None, "data": None}
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
        r = c.ask(t["claim"], "What was my total revenue?")
        print(f"{t['name']:<22} claim={t['claim']} status={r['status']} data={r['data']}")
