# Pattern A — Service Principal per tenant (dynamic view)

Each tenant gets its **own** Databricks Service Principal. The host app authenticates to the
Genie Conversation API with **that tenant's SP**, and a **dynamic view** isolates rows by the
calling SP's identity. Built entirely from **GA** primitives.

> Companion: [Pattern B](../pattern-b-shared-sp-claim/) collapses all the per-tenant SPs into one
> shared SP + a token claim (Private Preview). See the [root README](../README.md) for the full
> Pattern A vs B comparison.

## Components

This pattern adds exactly two objects on top of the shared [`orders_base`](../common/) table:

1. **Per-tenant Service Principals** — one OAuth M2M Service Principal per tenant. The host app
   holds each SP's `client_id` + secret and picks the right one for the logged-in merchant.

2. **`sp_tenant_map`** — a tiny mapping table: which SP identity belongs to which tenant.

   | column | example | meaning |
   |---|---|---|
   | `sp_identity` | `784f0043-c248-4650-b9fa-75f5aac21b62` | the SP's **application (client) id** |
   | `tenant_id` | `M001` | the tenant that SP represents |
   | `tenant_name` | `Cafe Aurora` | display only |

3. **`orders_secure_a`** — the dynamic view that does the isolation:

   ```sql
   CREATE VIEW orders_secure_a AS
   SELECT o.*
   FROM   orders_base o
   JOIN   sp_tenant_map m ON o.tenant_id = m.tenant_id
   WHERE  m.sp_identity = current_user();
   ```

### Why `current_user()` is the key

For an SP-authenticated session, `current_user()` returns the **SP's application (client) id** —
verified empirically on FEVM (all three tenant SPs):

```
SELECT current_user();   -- as SP 784f0043-… → '784f0043-c248-4650-b9fa-75f5aac21b62'
                         -- as SP bed11145-… → 'bed11145-a25f-4359-b30f-1976fe167172'
                         -- as SP 5a2288d4-… → '5a2288d4-0107-47a0-8d8d-0f02b27ccfc8'
```

So `sp_tenant_map.sp_identity` is populated with each SP's **`client_id`**, and the view's
`WHERE m.sp_identity = current_user()` keeps only the calling SP's tenant. `current_user()`
always reflects the **caller** inside a view (independent of who owns the view), so a Genie space
with `run_as = VIEWER` isolates each SP to its own tenant automatically.

## How it isolates

```
Merchant logs into host app (no Databricks account)
        │  app resolves merchant → that tenant's SP credentials
        ▼
POST /oidc/v1/token   (client_credentials, scope=all-apis)   ← the tenant's OWN SP
        │  Bearer token (tenant's SP)
        ▼
Genie Conversation API → generated SQL over orders_secure_a →
   the view evaluates current_user() = the calling SP
   → JOIN sp_tenant_map → keeps only that SP's tenant rows
```

Genie's generated SQL is just `SELECT SUM(amount) … FROM orders_secure_a WHERE amount IS NOT
NULL` — **no tenant predicate**. The view applies the isolation.

## Grant model (the bypass is closed)

- `orders_secure_a` is **owned by a definer** (the provisioning admin / `view_owner`) that holds
  `SELECT` on `orders_base` and `sp_tenant_map`.
- Each tenant SP gets `USE CATALOG`, `USE SCHEMA`, and **`SELECT` on `orders_secure_a` only** —
  **not** on `orders_base`. (It doesn't even need `SELECT` on `sp_tenant_map`: definer's rights
  resolve the join.)
- Each tenant SP gets `CAN_USE` on the warehouse and **`CAN_RUN`** on the Genie space.

Because the base-table privilege is satisfied by the **owner** while the **identity** is read
from the **caller**, a tenant SP can be denied `orders_base` access yet still use the view.
Verified: a tenant SP selecting `orders_base` directly is denied —
`INSUFFICIENT_PERMISSIONS` / `SQLSTATE 42501`.

## Setup

```bash
pip install -r ../requirements.txt
cp config.example.yaml config.yaml     # fill in workspace_url, warehouse_id, the 3 SP creds, view_owner

# 0) build the shared base table first (once):  cd ../common && python3 setup_base_data.py

# 1) provision sp_tenant_map + orders_secure_a + grants (runs as admin):
export DATABRICKS_TOKEN=$(databricks auth token --profile <profile> | jq -r .access_token)
python3 provision.py

# 2) create a Genie space over <catalog>.<schema>.orders_secure_a with run_as = VIEWER,
#    grant each tenant SP CAN_RUN, and put its id in genie_space_id in config.yaml.
#    (The space can be created via the Genie spaces API or the UI.)

# 3) ask Genie as each merchant — proves isolation:
python3 run_cli.py --merchant M001 --question "What was my total revenue?"   # -> 85.25
python3 run_cli.py --merchant M002 --question "What was my total revenue?"   # -> 345.00
python3 run_cli.py --merchant M003 --question "What was my total revenue?"   # -> 757.00

# or the Streamlit portal:
streamlit run app.py
```

## Files

| file | role |
|---|---|
| `provision.py` | builds `sp_tenant_map` + `orders_secure_a`, transfers ownership, applies grants |
| `genie_client.py` | Conversation API client — mints an M2M token from the tenant's SP |
| `run_cli.py` | ask a question as a given merchant from the terminal |
| `app.py` | Streamlit "merchant portal" UI |
| `config.example.yaml` | copy to `config.yaml` (gitignored) and fill in |

## Pros / cons

**Pros**

- **GA only** — Service Principals, views, `current_user()`, mapping tables. No preview features.
- The Genie/SQL **UI still renders** (unlike Pattern B) — `current_user()` resolves in any
  session, so an admin or the SP can preview data interactively.
- Simple mental model: "this SP *is* this tenant."

**Cons**

- **One SP + secret per tenant.** At Kustom's scale (~24k tenants) that's ~24k SPs to create,
  rotate, and revoke — significant identity-management overhead.
- **`sp_tenant_map` upkeep** — every onboard/offboard is a row change plus an SP lifecycle event.
- Per-tenant grants on the Genie space (`CAN_RUN`) to maintain.

When the tenant count grows into the thousands, this is exactly the pain
[Pattern B](../pattern-b-shared-sp-claim/) removes.
