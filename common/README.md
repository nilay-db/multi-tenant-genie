# common/ — the one shared base table

Both patterns read from a **single base table with identical data**. This folder builds it.
The whole point of the repo is that this table is **plain** — no row filter, no secure view,
no per-tenant logic. All isolation lives in each pattern's dynamic view, not here.

## What gets created

```
<catalog>.<schema>.orders_base      (default: nt_workspace_catalog.kustom_claims_demo.orders_base)
```

A plain Delta table, 7 columns:

| column | type | notes |
|---|---|---|
| `order_id` | STRING | |
| `tenant_id` | STRING | `M001` / `M002` / `M003` — the tenant the row belongs to |
| `order_date` | DATE | |
| `product` | STRING | |
| `category` | STRING | |
| `amount` | DECIMAL(12,2) | the value summed in the demo question |
| `customer_id` | STRING | |

### Sample rows and per-tenant totals

Nine rows, three per tenant, chosen so each tenant's total is distinct and easy to eyeball:

| tenant | rows | total `amount` |
|---|---|---|
| M001 (Cafe Aurora) | 3 | **85.25** |
| M002 (Velocity Sportswear) | 3 | **345.00** |
| M003 (Nordic Hardware) | 3 | **757.00** |
| **full table (no isolation)** | 9 | **1187.25** |

When you ask *"What was my total revenue?"* through either pattern, a correctly isolated tenant
sees their own number above; the full `1187.25` should appear **only** to an admin reading
`orders_base` directly (with no isolation).

## Setup

```bash
cp config.example.yaml config.yaml      # set workspace_url, warehouse_id (catalog/schema have defaults)

# Provisioning runs as an admin identity over the Statement Execution API.
export DATABRICKS_TOKEN=$(databricks auth token --profile <profile> | jq -r .access_token)
python3 setup_base_data.py
```

The script is **idempotent**: if `orders_base` already exists with exactly the expected 9 rows
and total `1187.25`, it is left untouched; otherwise it is (re)created and repopulated.

> Note: on a metastore with Default Storage, `CREATE CATALOG IF NOT EXISTS` may report a benign
> failure if the catalog already exists — the script continues and the schema/table steps are
> what matter.

After this, go provision **Pattern A** and/or **Pattern B** (see their READMEs). Each pattern's
`provision.py` assumes `orders_base` already exists.
