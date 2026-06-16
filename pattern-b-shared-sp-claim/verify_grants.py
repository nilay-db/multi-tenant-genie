"""
Prove the production-safe / non-bypassable grant model for the DV variant.

Run AFTER provision.py, which leaves this end state:
  - VIEW orders_secure_b  owned by a definer identity that HOLDS base SELECT.
  - shared SP              has SELECT on the VIEW only; its base SELECT is REVOKED.

Tests (prints verbatim error codes/messages so the finding is exact):
  A. SP + claim=M001 -> SELECT FROM VIEW   -> isolated rows (85.25)
  A2.SP + claim=M003 -> SELECT FROM VIEW   -> isolated rows (757.00)
  B. SP queries orders_base DIRECTLY       -> DENIED (bypass closed)
  C. 'Other app' = admin (base SELECT, NO claim) reads orders_base -> all rows (1187.25)
  D. SP, NO claim, queries VIEW            -> fail-safe (claim not provided)

The decisive point: the claim is read from the CALLER's token while the base-table
privilege is satisfied by the VIEW OWNER (definer's rights). They are independent, so
the merchant principal can be denied base access yet still use the view.
"""
import os
import sys
import base64
import json
import requests
from genie_client import load_config

cfg = load_config()
HOST = cfg["workspace_url"].rstrip("/")
WH = cfg["warehouse_id"]
CAT, SCH = cfg["catalog"], cfg["schema"]
FQ = f"{CAT}.{SCH}"
SP = cfg["app_sp"]
APP = SP["client_id"]
BASE = f"{FQ}.orders_base"
VIEW = f"{FQ}.orders_secure_b"

ADMIN = os.environ.get("DATABRICKS_TOKEN") or cfg.get("admin_token")
if not ADMIN:
    sys.exit("export DATABRICKS_TOKEN=$(databricks auth token --profile <p> | jq -r .access_token)")


def sp_token(claim=None):
    basic = base64.b64encode(f"{APP}:{SP['client_secret']}".encode()).decode()
    data = {"grant_type": "client_credentials", "scope": "all-apis"}
    if claim is not None:
        data["custom_claim"] = claim
    r = requests.post(f"{HOST}/oidc/v1/token", headers={"Authorization": f"Basic {basic}"}, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def stmt(sql, token):
    r = requests.post(f"{HOST}/api/2.0/sql/statements",
                      headers={"Authorization": f"Bearer {token}"},
                      json={"warehouse_id": WH, "statement": sql, "wait_timeout": "50s"}, timeout=120)
    j = r.json()
    st = j.get("status", {})
    return {
        "state": st.get("state"),
        "error_code": (st.get("error") or {}).get("error_code"),
        "error": (st.get("error") or {}).get("message"),
        "rows": (j.get("result") or {}).get("data_array"),
    }


def show(label, res):
    print(f"\n### {label}")
    print(f"    state      = {res['state']}")
    if res["error_code"] or res["error"]:
        print(f"    error_code = {res['error_code']}")
        print(f"    error      = {res['error']}")
    if res["rows"] is not None:
        print(f"    rows       = {json.dumps(res['rows'])}")


def main():
    print("=" * 78)
    print("DV GRANT-MODEL VERIFICATION  (verbatim results)")
    print("=" * 78)
    g = stmt(f"SHOW GRANTS ON TABLE {BASE}", ADMIN)
    print(f"\ngrants on orders_base: {json.dumps(g['rows'])}")
    o = [r for r in (stmt(f'DESCRIBE EXTENDED {VIEW}', ADMIN)['rows'] or []) if r and r[0] == 'Owner']
    print(f"owner of orders_secure_b: {o}")

    show("A.  SP + claim=M001 -> SELECT SUM(amount) FROM VIEW  (SP has NO base SELECT)",
         stmt(f"SELECT SUM(amount) AS total FROM {VIEW}", sp_token("M001")))
    show("A2. SP + claim=M003 -> SELECT SUM(amount) FROM VIEW  (SP has NO base SELECT)",
         stmt(f"SELECT SUM(amount) AS total FROM {VIEW}", sp_token("M003")))
    show("B.  SP -> SELECT FROM orders_base DIRECTLY  (expect DENIED / bypass closed)",
         stmt(f"SELECT SUM(amount) FROM {BASE}", sp_token("M001")))
    show("C.  'Other app' (admin, base SELECT, NO claim) -> SELECT FROM orders_base (expect 1187.25)",
         stmt(f"SELECT SUM(amount) AS total FROM {BASE}", ADMIN))
    show("D.  SP, NO claim -> SELECT FROM VIEW  (expect fail-safe: claim not provided)",
         stmt(f"SELECT SUM(amount) FROM {VIEW}", sp_token(None)))


if __name__ == "__main__":
    main()
