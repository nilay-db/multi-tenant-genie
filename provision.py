"""
Provision UC objects for Pattern B (shared SP + custom claim) via the Statement Execution API.

Handles the claim-eval ordering automatically:
  - admin creates schema / table / data / grants and transfers table ownership to the shared SP
  - the shared SP (using a claim-bearing token) creates the row-filter function and applies it,
    because current_oauth_custom_identity_claim() is eager-evaluated and needs a claim present.

Admin token resolved from env DATABRICKS_TOKEN, else `admin_token` in config.yaml.
"""
import os
import sys
import base64
import requests
from genie_client import load_config

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


def main():
    cfg = load_config()
    host = cfg["workspace_url"].rstrip("/")
    wh = cfg["warehouse_id"]
    cat, sch = cfg["catalog"], cfg["schema"]
    sp = cfg["app_sp"]
    fq = f"{cat}.{sch}"

    admin = os.environ.get("DATABRICKS_TOKEN") or cfg.get("admin_token")
    if not admin:
        sys.exit("Set DATABRICKS_TOKEN env var or admin_token in config.yaml (an admin identity).")

    # SP token WITH a claim — needed to create/attach the eager-evaluated row filter.
    basic = base64.b64encode(f"{sp['client_id']}:{sp['client_secret']}".encode()).decode()
    sp_tok = requests.post(f"{host}/oidc/v1/token",
                           headers={"Authorization": f"Basic {basic}"},
                           data={"grant_type": "client_credentials", "scope": "all-apis", "custom_claim": "M001"},
                           timeout=30).json()["access_token"]

    def run(sql, label, token):
        r = requests.post(f"{host}/api/2.0/sql/statements",
                          headers={"Authorization": f"Bearer {token}"},
                          json={"warehouse_id": wh, "statement": sql, "wait_timeout": "50s"}, timeout=120)
        st = r.json().get("status", {})
        ok = st.get("state") == "SUCCEEDED"
        print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f"  -> {st.get('error', {}).get('message', '')[:160]}"))
        return ok

    print("Provisioning Pattern B objects…")
    run(f"CREATE SCHEMA IF NOT EXISTS {fq}", "schema", admin)
    run(f"CREATE OR REPLACE TABLE {fq}.orders (order_id STRING, tenant_id STRING, order_date DATE, "
        f"product STRING, category STRING, amount DECIMAL(12,2), customer_id STRING)", "orders table", admin)
    vals = ",".join(f"('{o}','{t}',DATE'{d}','{p}','{c}',{a},'{cu}')" for (o, t, d, p, c, a, cu) in SAMPLE_ORDERS)
    run(f"INSERT INTO {fq}.orders VALUES {vals}", "orders data", admin)

    app = sp["client_id"]
    run(f"GRANT USE CATALOG ON CATALOG {cat} TO `{app}`", "grant use catalog", admin)
    run(f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{app}`", "grant use schema", admin)
    run(f"GRANT CREATE FUNCTION ON SCHEMA {fq} TO `{app}`", "grant create function", admin)
    run(f"GRANT SELECT ON TABLE {fq}.orders TO `{app}`", "grant select", admin)
    run(f"ALTER TABLE {fq}.orders OWNER TO `{app}`", "transfer table ownership to SP", admin)

    pr = requests.patch(f"{host}/api/2.0/permissions/warehouses/{wh}",
                        headers={"Authorization": f"Bearer {admin}"},
                        json={"access_control_list": [{"service_principal_name": app, "permission_level": "CAN_USE"}]}, timeout=60)
    print(f"  [{'ok' if pr.ok else 'FAIL'}] warehouse CAN_USE -> {app[:8]}")

    # These two must run AS THE SP, with a claim present.
    run(f"CREATE OR REPLACE FUNCTION {fq}.rf_tenant(tid STRING) RETURN tid = current_oauth_custom_identity_claim()",
        "row-filter function (as SP, claim present)", sp_tok)
    run(f"ALTER TABLE {fq}.orders SET ROW FILTER {fq}.rf_tenant ON (tenant_id)",
        "apply row filter (as SP, claim present)", sp_tok)

    print("\nDone. Next: create a Genie space over "
          f"{fq}.orders (run_as = VIEWER), grant the shared SP CAN_RUN, set genie_space_id in config.yaml.")


if __name__ == "__main__":
    main()
