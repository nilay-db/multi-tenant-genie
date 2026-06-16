"""
Create the ONE shared base table that BOTH patterns read.

    <catalog>.<schema>.orders_base   — a plain table, NO row filter, NO secure view.

This is the whole point of the blog: the data is identical for both patterns. The
tenant-isolation logic lives ONLY in each pattern's dynamic view (orders_secure_a /
orders_secure_b), never in this table. Run this once, then provision either pattern.

Per-tenant totals (so you can verify isolation downstream):
    M001 = 14.75 + 42.00 + 28.50  =  85.25
    M002 = 120.00 + 45.00 + 180.00 = 345.00
    M003 = 249.00 + 88.00 + 420.00 = 757.00
    full (no isolation)            = 1187.25

Idempotent: if orders_base already exists with exactly this data, it is left as-is.
Otherwise it is (re)created. An admin identity runs this — it is plain UC DDL with no
OAuth custom claim required. The admin token is read from env DATABRICKS_TOKEN, else
`admin_token` in config.yaml.

Config is read from this folder's config.yaml (copy common/config.example.yaml).
"""
import os
import sys
import yaml
import requests
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.yaml")

# Identical data for both patterns. (order_id, tenant_id, order_date, product, category, amount, customer_id)
SAMPLE_ORDERS = [
    ("O-1001", "M001", "2026-05-04", "Espresso Beans 1kg", "Beverages", "14.75", "C-1"),
    ("O-1002", "M001", "2026-05-11", "Oat Milk Case",      "Beverages", "42.00", "C-2"),
    ("O-1003", "M001", "2026-05-19", "Pastry Box",         "Food",      "28.50", "C-1"),
    ("O-2001", "M002", "2026-05-03", "Running Shoes",      "Footwear",  "120.00", "C-7"),
    ("O-2002", "M002", "2026-05-09", "Compression Tee",    "Apparel",   "45.00", "C-8"),
    ("O-2003", "M002", "2026-05-22", "Trail Jacket",       "Apparel",   "180.00", "C-9"),
    ("O-3001", "M003", "2026-05-02", "Cordless Drill",     "Tools",     "249.00", "C-4"),
    ("O-3002", "M003", "2026-05-14", "Steel Bolts 500ct",  "Hardware",  "88.00", "C-5"),
    ("O-3003", "M003", "2026-05-27", "Workbench",          "Furniture", "420.00", "C-6"),
]

BASE = "orders_base"
EXPECTED_ROWS = len(SAMPLE_ORDERS)
EXPECTED_TOTAL = "1187.25"


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    host = cfg["workspace_url"].rstrip("/")
    wh = cfg["warehouse_id"]
    cat, sch = cfg["catalog"], cfg["schema"]
    fq = f"{cat}.{sch}"
    admin = os.environ.get("DATABRICKS_TOKEN") or cfg.get("admin_token")
    if not admin:
        sys.exit("Set DATABRICKS_TOKEN env var or admin_token in config.yaml (an admin identity).")

    def run(sql, label):
        r = requests.post(f"{host}/api/2.0/sql/statements",
                          headers={"Authorization": f"Bearer {admin}"},
                          json={"warehouse_id": wh, "statement": sql, "wait_timeout": "50s"}, timeout=120)
        j = r.json()
        st = j.get("status", {})
        ok = st.get("state") == "SUCCEEDED"
        rows = (j.get("result") or {}).get("data_array")
        print(f"  [{'ok' if ok else 'FAIL'}] {label}"
              + ("" if ok else f"  -> {st.get('error', {}).get('message', '')[:160]}"))
        return ok, rows

    print(f"Setting up shared base data in {fq}.{BASE} …")
    run(f"CREATE CATALOG IF NOT EXISTS {cat}", "catalog")
    run(f"CREATE SCHEMA IF NOT EXISTS {fq}", "schema")

    # Idempotency: if the table already holds exactly the expected data, leave it untouched.
    ok, rows = run(
        f"SELECT COUNT(*) AS n, CAST(COALESCE(SUM(amount),0) AS STRING) AS total "
        f"FROM {fq}.{BASE}", "probe existing orders_base")
    if ok and rows and rows[0][0] is not None:
        n, total = int(rows[0][0]), rows[0][1]
        if n == EXPECTED_ROWS and total == EXPECTED_TOTAL:
            print(f"  orders_base already has the expected data ({n} rows, total {total}). Reusing.")
            return
        print(f"  orders_base exists but differs (rows={n}, total={total}); recreating.")

    run(f"CREATE OR REPLACE TABLE {fq}.{BASE} ("
        f"order_id STRING, tenant_id STRING, order_date DATE, product STRING, "
        f"category STRING, amount DECIMAL(12,2), customer_id STRING)", f"{BASE} table")
    vals = ",".join(
        f"('{o}','{t}',DATE'{d}','{p}','{c}',{a},'{cu}')" for (o, t, d, p, c, a, cu) in SAMPLE_ORDERS)
    run(f"INSERT INTO {fq}.{BASE} VALUES {vals}", f"{BASE} data ({EXPECTED_ROWS} rows)")

    print(f"\nDone. {fq}.{BASE} holds {EXPECTED_ROWS} rows (full total {EXPECTED_TOTAL}). "
          "Now provision Pattern A and/or Pattern B.")


if __name__ == "__main__":
    main()
