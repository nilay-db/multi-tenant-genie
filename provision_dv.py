"""
Provision the DYNAMIC-VIEW variant (Pattern B-DV) via the Statement Execution API.

WHY a view instead of a row filter:
  Kustom's base `orders` table must stay usable by OTHER apps that carry NO merchant
  claim. A row filter on the base table fails closed for every claim-less caller, which
  would break those apps. So the merchant-facing path goes through a dynamic SECURE VIEW
  whose body calls current_oauth_custom_identity_claim(); the base table itself is left
  open (no row filter) for non-claim consumers.

Objects (separate from the row-filter demo's `orders`, so the two are independent):
  - nt_workspace_catalog.kustom_claims_demo.orders_base       plain table, NO row filter
  - nt_workspace_catalog.kustom_claims_demo.orders_secure_dv  dynamic view (claim predicate)

Grant model under test (production-safe / non-bypassable):
  - merchant-facing principal = the shared SP Genie uses.
  - SP gets SELECT on the VIEW only; SP's SELECT on orders_base is withheld/revoked.
  - The VIEW is owned by an identity that DOES have base SELECT (definer's rights),
    so the view resolves while the caller (SP) cannot touch the base table directly.

Eager-eval gotcha: current_oauth_custom_identity_claim() is evaluated at CREATE VIEW
time, so the view is created using a claim-bearing token.

Admin identity comes from `databricks auth token --profile <profile>` (passed in via
DATABRICKS_TOKEN env var by run.sh / the caller), else admin_token in config.yaml.
"""
import os
import sys
import base64
import requests
from genie_client import load_config

# Same 3-tenant data as the row-filter demo's orders, so totals match:
#   M001 = 14.75+42.00+28.50 = 85.25 ; M002 = 120+45+180 = 345.00 ; M003 = 249+88+420 = 757.00
SAMPLE_ORDERS = [
    ("O-1001", "M001", "2026-05-04", "Espresso Beans 1kg", "Beverages", "14.75", "C-1"),
    ("O-1002", "M001", "2026-05-11", "Oat Milk Case", "Beverages", "42.00", "C-2"),
    ("O-1003", "M001", "2026-05-19", "Pastry Box", "Food", "28.50", "C-1"),
    ("O-2001", "M002", "2026-05-03", "Running Shoes", "Footwear", "120.00", "C-7"),
    ("O-2002", "M002", "2026-05-09", "Compression Tee", "Apparel", "45.00", "C-8"),
    ("O-2003", "M002", "2026-05-22", "Trail Jacket", "Apparel", "180.00", "C-9"),
    ("O-3001", "M003", "2026-05-02", "Cordless Drill", "Tools", "249.00", "C-4"),
    ("O-3002", "M003", "2026-05-14", "Steel Bolts 500ct", "Hardware", "88.00", "C-5"),
    ("O-3003", "M003", "2026-05-27", "Workbench", "Furniture", "420.00", "C-6"),
]

BASE = "orders_base"
VIEW = "orders_secure_dv"


def main():
    cfg = load_config()
    host = cfg["workspace_url"].rstrip("/")
    wh = cfg["warehouse_id"]
    cat, sch = cfg["catalog"], cfg["schema"]
    sp = cfg["app_sp"]
    fq = f"{cat}.{sch}"

    admin = os.environ.get("DATABRICKS_TOKEN") or cfg.get("admin_token")
    if not admin:
        sys.exit("Provide an admin token: export DATABRICKS_TOKEN=$(databricks auth token --profile <p> | jq -r .access_token)")

    # Identity that will OWN the view (definer's rights). Must be a principal that holds
    # base-table SELECT and is NOT the merchant-facing SP, so the SP can be denied base access.
    view_owner = cfg.get("view_owner") or "nilay.tiwari@databricks.com"

    # SP token WITH a claim — needed to create the eager-evaluated view body.
    basic = base64.b64encode(f"{sp['client_id']}:{sp['client_secret']}".encode()).decode()
    sp_tok = requests.post(f"{host}/oidc/v1/token",
                           headers={"Authorization": f"Basic {basic}"},
                           data={"grant_type": "client_credentials", "scope": "all-apis", "custom_claim": "M001"},
                           timeout=30).json()["access_token"]

    def run(sql, label, token=admin):
        r = requests.post(f"{host}/api/2.0/sql/statements",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"warehouse_id": wh, "statement": sql, "wait_timeout": "50s"}, timeout=120)
        st = r.json().get("status", {})
        ok = st.get("state") == "SUCCEEDED"
        print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f"  -> {st.get('error', {}).get('message', '')[:180]}"))
        return ok

    app = sp["client_id"]
    print("Provisioning DV variant objects…")

    run(f"CREATE SCHEMA IF NOT EXISTS {fq}", "schema")

    # 1) Plain base table — NO row filter. Owned by admin (so non-claim 'other apps' can read it).
    run(f"CREATE OR REPLACE TABLE {fq}.{BASE} (order_id STRING, tenant_id STRING, order_date DATE, "
        f"product STRING, category STRING, amount DECIMAL(12,2), customer_id STRING)", f"{BASE} table")
    vals = ",".join(f"('{o}','{t}',DATE'{d}','{p}','{c}',{a},'{cu}')" for (o, t, d, p, c, a, cu) in SAMPLE_ORDERS)
    run(f"INSERT INTO {fq}.{BASE} VALUES {vals}", f"{BASE} data")

    # SP needs catalog/schema traversal to reach the view.
    run(f"GRANT USE CATALOG ON CATALOG {cat} TO `{app}`", "grant use catalog")
    run(f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{app}`", "grant use schema")

    # 2) Dynamic view. Created WITH a claim present (eager-eval). The CREATE runs as ADMIN,
    #    so the VIEW OWNER = admin, who HAS base SELECT (definer's rights resolve the body).
    #    BUT: admin has no claim in a normal session, so we must pass the claim-bearing token.
    #    The SP can create-as-itself only if it owns the schema; instead we create as admin
    #    but with admin... wait — admin token carries NO claim. So we create the view AS THE SP
    #    (claim-bearing), which makes the SP the owner. We then test whether definer's rights
    #    (SP-as-owner WITH base SELECT) work, then REVOKE base SELECT to test bypass closure.
    #
    #    To get a claim-bearing definer that also has base SELECT, create the view as the SP
    #    while the SP temporarily holds base SELECT, then revoke the SP's base SELECT.
    run(f"GRANT SELECT ON TABLE {fq}.{BASE} TO `{app}`", "temp grant base SELECT to SP (for view creation)")
    run(f"GRANT CREATE FUNCTION ON SCHEMA {fq} TO `{app}`", "grant create function (schema)")
    run(f"GRANT CREATE TABLE ON SCHEMA {fq} TO `{app}`", "grant create table/view (schema)")

    # Idempotent re-runs: drop any pre-existing view AS ADMIN (it may be owned by the definer
    # from a prior run, in which case the SP lacks MANAGE to CREATE OR REPLACE it).
    run(f"DROP VIEW IF EXISTS {fq}.{VIEW}", "drop pre-existing view (as admin)")

    ok_view = run(
        f"CREATE VIEW {fq}.{VIEW} AS "
        f"SELECT * FROM {fq}.{BASE} WHERE tenant_id = current_oauth_custom_identity_claim()",
        "create dynamic view (as SP, claim present)", token=sp_tok)
    if not ok_view:
        print("View creation failed — see error above. Aborting.")
        return

    # 3) Decouple owner from caller. The view was created as the SP (claim-bearing), so the
    #    SP is currently its owner. Transfer ownership to a definer identity that HOLDS base
    #    SELECT. UC definer's rights then resolve the base-table read through the OWNER, while
    #    the claim is read from the CALLER's token — these are independent. This is what lets
    #    us deny the SP base access yet keep the view working (proven in verify_dv_grants.py).
    run(f"ALTER VIEW {fq}.{VIEW} OWNER TO `{view_owner}`", f"transfer view ownership to definer ({view_owner})")

    # SP gets SELECT on the VIEW (merchant-facing path) ...
    run(f"GRANT SELECT ON VIEW {fq}.{VIEW} TO `{app}`", "grant SELECT on view to SP")
    # ... and the SP's base-table SELECT is REVOKED — the bypass is closed.
    run(f"REVOKE SELECT ON TABLE {fq}.{BASE} FROM `{app}`", "REVOKE SP base SELECT (close the bypass)")

    print("\nDone provisioning. Run `python3 verify_dv_grants.py` to test the bypass-closure grant model.")
    print("Next: create a Genie space over "
          f"{fq}.{VIEW} (run_as=VIEWER), grant the shared SP CAN_RUN, set genie_space_id_dv in config.yaml.")


if __name__ == "__main__":
    main()
