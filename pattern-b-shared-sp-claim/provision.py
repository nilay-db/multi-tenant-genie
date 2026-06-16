"""
Provision Pattern B (one shared SP + OAuth custom identity claim) — isolation via a
DYNAMIC VIEW.

Pattern B's only moving part on top of the shared base table is one view:

    orders_secure_b  =  SELECT * FROM orders_base
                        WHERE tenant_id = current_oauth_custom_identity_claim()

current_oauth_custom_identity_claim() reads the `custom.claim` carried in the CALLER's
OAuth token. With the shared SP minting a per-request token that sets custom_claim=<tenant>,
the SAME view returns different rows per request — and Genie's generated SQL has no tenant
predicate.

Grant model (production-safe / non-bypassable):
  - the view is OWNED by a definer identity that HOLDS base SELECT (definer's rights), and
  - the shared SP gets SELECT on the VIEW ONLY; its base-table SELECT is REVOKED.
The base-table privilege is satisfied by the owner, while the claim is read from the
caller's token — independent checks — so the SP can be denied base access yet use the view.

⚠️ Eager-eval gotcha: current_oauth_custom_identity_claim() is evaluated at CREATE VIEW
time. An admin session carries no claim, so the CREATE would fail with
OAUTH_CUSTOM_IDENTITY_CLAIM_NOT_PROVIDED. So we CREATE the view AS THE SHARED SP using a
claim-bearing token, THEN transfer ownership to the definer and REVOKE the SP's base SELECT.

Run common/setup_base_data.py FIRST (creates orders_base).
Admin token: env DATABRICKS_TOKEN, else admin_token in config.yaml.
"""
import os
import sys
import base64
import yaml
import requests
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.yaml")

BASE = "orders_base"
VIEW = "orders_secure_b"


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    host = cfg["workspace_url"].rstrip("/")
    wh = cfg["warehouse_id"]
    cat, sch = cfg["catalog"], cfg["schema"]
    sp = cfg["app_sp"]
    fq = f"{cat}.{sch}"
    app = sp["client_id"]

    admin = os.environ.get("DATABRICKS_TOKEN") or cfg.get("admin_token")
    if not admin:
        sys.exit("Set DATABRICKS_TOKEN env var or admin_token in config.yaml (an admin identity).")
    view_owner = cfg.get("view_owner") or "nilay.tiwari@databricks.com"

    # Shared-SP token WITH a claim — needed to create the eager-evaluated view body.
    basic = base64.b64encode(f"{app}:{sp['client_secret']}".encode()).decode()
    sp_tok = requests.post(f"{host}/oidc/v1/token",
                           headers={"Authorization": f"Basic {basic}"},
                           data={"grant_type": "client_credentials", "scope": "all-apis",
                                 "custom_claim": "M001"},
                           timeout=30).json()["access_token"]

    def run(sql, label, token=admin):
        r = requests.post(f"{host}/api/2.0/sql/statements",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"warehouse_id": wh, "statement": sql, "wait_timeout": "50s"}, timeout=120)
        st = r.json().get("status", {})
        ok = st.get("state") == "SUCCEEDED"
        print(f"  [{'ok' if ok else 'FAIL'}] {label}"
              + ("" if ok else f"  -> {st.get('error', {}).get('message', '')[:180]}"))
        return ok

    print("Provisioning Pattern B objects (assumes common/setup_base_data.py already ran)…")
    run(f"CREATE SCHEMA IF NOT EXISTS {fq}", "schema")

    # SP needs catalog/schema traversal to reach the view.
    run(f"GRANT USE CATALOG ON CATALOG {cat} TO `{app}`", "grant use catalog")
    run(f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{app}`", "grant use schema")

    # Eager-eval handling: create the view AS THE SP (claim-bearing token). The SP must hold
    # base SELECT + create privileges for the duration of the CREATE; both are revoked after.
    run(f"GRANT SELECT ON TABLE {fq}.{BASE} TO `{app}`", "temp grant base SELECT to SP (for view creation)")
    run(f"GRANT CREATE TABLE ON SCHEMA {fq} TO `{app}`", "grant create table/view (schema)")
    # Idempotent re-runs: a prior view may be owned by the definer (SP lacks MANAGE), so drop as admin.
    run(f"DROP VIEW IF EXISTS {fq}.{VIEW}", "drop pre-existing view (as admin)")

    ok = run(f"CREATE VIEW {fq}.{VIEW} AS "
             f"SELECT * FROM {fq}.{BASE} "
             f"WHERE tenant_id = current_oauth_custom_identity_claim()",
             "create dynamic view (as SP, claim present)", token=sp_tok)
    if not ok:
        print("View creation failed — see error above. Aborting.")
        return

    # Decouple owner from caller: transfer ownership to a definer that holds base SELECT.
    run(f"ALTER VIEW {fq}.{VIEW} OWNER TO `{view_owner}`",
        f"transfer view ownership to definer ({view_owner})")
    # SP keeps SELECT on the VIEW (merchant path) but loses base SELECT (bypass closed).
    run(f"GRANT SELECT ON VIEW {fq}.{VIEW} TO `{app}`", "grant SELECT on view to SP")
    run(f"REVOKE SELECT ON TABLE {fq}.{BASE} FROM `{app}`", "REVOKE SP base SELECT (close the bypass)")
    run(f"REVOKE CREATE TABLE ON SCHEMA {fq} FROM `{app}`", "REVOKE SP create-table (cleanup)")

    pr = requests.patch(f"{host}/api/2.0/permissions/warehouses/{wh}",
                        headers={"Authorization": f"Bearer {admin}"},
                        json={"access_control_list": [
                            {"service_principal_name": app, "permission_level": "CAN_USE"}]}, timeout=60)
    print(f"  [{'ok' if pr.ok else 'FAIL'}] warehouse CAN_USE -> {app[:8]}")

    print("\nDone. Optionally run `python3 verify_grants.py` to print verbatim bypass-closure proof.")
    print("Next: create a Genie space over "
          f"{fq}.{VIEW} (run_as = VIEWER), grant the shared SP CAN_RUN, set genie_space_id in config.yaml.")


if __name__ == "__main__":
    main()
