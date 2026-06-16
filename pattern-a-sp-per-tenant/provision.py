"""
Provision Pattern A (Service Principal per tenant) — isolation via a DYNAMIC VIEW.

Pattern A's only moving parts on top of the shared base table are:
  1. sp_tenant_map        — maps each tenant SP's identity -> its tenant_id
  2. orders_secure_a      — a view that joins orders_base to sp_tenant_map and keeps only
                            the rows whose tenant matches the CALLING SP, via current_user()

The view body:

    SELECT o.*
    FROM   <fq>.orders_base o
    JOIN   <fq>.sp_tenant_map m ON o.tenant_id = m.tenant_id
    WHERE  m.sp_identity = current_user()

current_user() inside a view always reflects the CALLER (the SP), regardless of who owns
the view — so a Genie space with run_as = VIEWER isolates each SP to its own tenant. The
privilege check on orders_base / sp_tenant_map is satisfied by the VIEW OWNER (definer's
rights), so the tenant SPs are granted SELECT on the VIEW ONLY — never on orders_base.

This is plain UC DDL (no OAuth custom claim needed), so an admin runs the whole thing.
Admin token: env DATABRICKS_TOKEN, else admin_token in config.yaml.

Run common/setup_base_data.py FIRST (creates orders_base).
"""
import os
import sys
import yaml
import requests
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.yaml")

BASE = "orders_base"
MAP = "sp_tenant_map"
VIEW = "orders_secure_a"


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    host = cfg["workspace_url"].rstrip("/")
    wh = cfg["warehouse_id"]
    cat, sch = cfg["catalog"], cfg["schema"]
    tenants = cfg["tenants"]
    fq = f"{cat}.{sch}"
    admin = os.environ.get("DATABRICKS_TOKEN") or cfg.get("admin_token")
    if not admin:
        sys.exit("Set DATABRICKS_TOKEN env var or admin_token in config.yaml (an admin identity).")
    view_owner = cfg.get("view_owner")

    def run(sql, label):
        r = requests.post(f"{host}/api/2.0/sql/statements",
                          headers={"Authorization": f"Bearer {admin}"},
                          json={"warehouse_id": wh, "statement": sql, "wait_timeout": "50s"}, timeout=120)
        st = r.json().get("status", {})
        ok = st.get("state") == "SUCCEEDED"
        print(f"  [{'ok' if ok else 'FAIL'}] {label}"
              + ("" if ok else f"  -> {st.get('error', {}).get('message', '')[:160]}"))
        return ok

    print("Provisioning Pattern A objects (assumes common/setup_base_data.py already ran)…")
    run(f"CREATE SCHEMA IF NOT EXISTS {fq}", "schema")

    # 1) Mapping table: SP identity -> tenant. sp_identity holds the SP's application (client)
    #    id, which is exactly what current_user() returns for an SP-authenticated session.
    run(f"CREATE OR REPLACE TABLE {fq}.{MAP} (sp_identity STRING, tenant_id STRING, tenant_name STRING)",
        f"{MAP} table")
    mvals = ",".join(f"('{t['sp_client_id']}','{t['tenant_id']}','{t['name']}')" for t in tenants)
    run(f"INSERT INTO {fq}.{MAP} VALUES {mvals}", f"{MAP} data")

    # 2) Dynamic view. current_user() is the CALLER, so isolation follows the calling SP.
    run(f"CREATE OR REPLACE VIEW {fq}.{VIEW} AS "
        f"SELECT o.* FROM {fq}.{BASE} o "
        f"JOIN {fq}.{MAP} m ON o.tenant_id = m.tenant_id "
        f"WHERE m.sp_identity = current_user()", f"{VIEW} dynamic view")

    # 3) The view runs with definer's rights for base/map privileges. Own it with an identity
    #    that holds SELECT on orders_base + sp_tenant_map and is NOT a tenant SP.
    if view_owner:
        run(f"ALTER VIEW {fq}.{VIEW} OWNER TO `{view_owner}`",
            f"transfer view ownership to definer ({view_owner})")

    # 4) Grants. Each tenant SP gets catalog/schema traversal + SELECT on the VIEW ONLY.
    #    No SELECT on orders_base (the bypass stays closed); no SELECT on sp_tenant_map
    #    needed (definer's rights resolve the join).
    print("Grants…")
    for t in tenants:
        app = t["sp_client_id"]
        run(f"GRANT USE CATALOG ON CATALOG {cat} TO `{app}`", f"use catalog -> {app[:8]}")
        run(f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{app}`", f"use schema -> {app[:8]}")
        run(f"GRANT SELECT ON VIEW {fq}.{VIEW} TO `{app}`", f"select on view -> {app[:8]}")
        pr = requests.patch(f"{host}/api/2.0/permissions/warehouses/{wh}",
                            headers={"Authorization": f"Bearer {admin}"},
                            json={"access_control_list": [
                                {"service_principal_name": app, "permission_level": "CAN_USE"}]}, timeout=60)
        print(f"  [{'ok' if pr.ok else 'FAIL'}] warehouse CAN_USE -> {app[:8]}")

    print("\nDone. Next: create a Genie space over "
          f"{fq}.{VIEW} (run_as = VIEWER), grant each tenant SP CAN_RUN, set genie_space_id in config.yaml.")


if __name__ == "__main__":
    main()
