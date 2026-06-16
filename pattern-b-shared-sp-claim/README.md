# Pattern B — One shared SP + OAuth custom identity claim (dynamic view)

**One** Service Principal serves **every** tenant. The host app mints a per-request OAuth token
carrying a **custom identity claim** (the tenant id); a **dynamic view** reads that claim via
`current_oauth_custom_identity_claim()` and returns only that tenant's rows. There are **no
per-tenant Service Principals and no per-tenant secrets** — the tenant rides inside the token.

> Companion: [Pattern A](../pattern-a-sp-per-tenant/) uses one SP per tenant (GA). Pattern B
> removes the per-tenant SP entirely but relies on a **Private Preview** function. See the
> [root README](../README.md) for the full Pattern A vs B comparison.

## Components

This pattern adds exactly one object on top of the shared [`orders_base`](../common/) table:

1. **One shared Service Principal** — the only SP in the system. The host app holds its single
   `client_id` + secret and uses it for every tenant.

2. **A per-request token carrying `custom_claim`** — at `client_credentials` time the host app
   sets `custom_claim=<tenant>`, which embeds `{"custom":{"claim":"<tenant>"}}` in the JWT.

3. **`orders_secure_b`** — the dynamic view that does the isolation:

   ```sql
   CREATE VIEW orders_secure_b AS
   SELECT * FROM orders_base
   WHERE tenant_id = current_oauth_custom_identity_claim();
   ```

   `current_oauth_custom_identity_claim()` returns the claim value from the **caller's** token, so
   the same view returns different rows per request — driven by the token, not by a different SP.

## How it isolates

```
Merchant logs into host app (no Databricks account)
        │  app resolves merchant → tenant claim, e.g. "M001"
        ▼
POST /oidc/v1/token   (client_credentials, scope=all-apis, custom_claim=M001)   ← the ONE shared SP
        │  Bearer token (shared SP + claim rides inside the JWT)
        ▼
Genie Conversation API → generated SQL over orders_secure_b →
   the view evaluates current_oauth_custom_identity_claim() = "M001"
   → keeps only tenant_id = 'M001' rows
```

Genie's generated SQL is `SELECT SUM(amount) … FROM orders_secure_b WHERE amount IS NOT NULL` —
**no tenant predicate**. The view + claim isolate.

## Grant model (the bypass is closed)

- `orders_secure_b` is **owned by a definer** (the provisioning admin / `view_owner`) that holds
  `SELECT` on `orders_base`.
- The shared SP gets `USE CATALOG`, `USE SCHEMA`, and **`SELECT` on `orders_secure_b` only** —
  its `SELECT` on `orders_base` is **REVOKED**.
- The shared SP gets `CAN_USE` on the warehouse and **`CAN_RUN`** on the Genie space.

The base-table privilege is satisfied by the **owner** (definer's rights); the **claim** is read
from the **caller's** token. The two checks are independent, so the SP can be denied direct base
access and still use the view.

### Verbatim proof (`verify_grants.py`)

```
A.  SP + claim=M001 → SELECT SUM(amount) FROM orders_secure_b   = 85.25     (isolated; SP has NO base SELECT)
A2. SP + claim=M003 → SELECT SUM(amount) FROM orders_secure_b   = 757.00    (isolated)
B.  SP → SELECT FROM orders_base DIRECTLY → FAILED
        [INSUFFICIENT_PERMISSIONS] … does not have SELECT on '…orders_base'. SQLSTATE: 42501   (bypass CLOSED)
C.  other app (base SELECT, NO claim) → SELECT FROM orders_base = 1187.25     (non-claim use still works)
D.  SP, NO claim → SELECT FROM orders_secure_b → FAILED
        [OAUTH_CUSTOM_IDENTITY_CLAIM_NOT_PROVIDED] … SQLSTATE: 22KD2          (fails SAFE)
```

And end-to-end **through the Genie Conversation API** (`run_cli.py`):

```
--merchant M001 "What was my total revenue?"  → 85.25
--merchant M003 "What was my total revenue?"  → 757.00
```

## ⚠️ The eager-eval CREATE gotcha

`current_oauth_custom_identity_claim()` is **evaluated at `CREATE VIEW` time**. A normal
admin/user session carries **no claim**, so a plain `CREATE VIEW … WHERE tenant_id =
current_oauth_custom_identity_claim()` fails with `OAUTH_CUSTOM_IDENTITY_CLAIM_NOT_PROVIDED`.

`provision.py` handles this in three moves:

1. **Create the view as the shared SP, using a claim-bearing token** (it temporarily grants the
   SP base `SELECT` + `CREATE` so the body validates with a claim present).
2. **Transfer ownership** of the view to the definer (`view_owner`) — an identity that holds base
   `SELECT` but is **not** the SP.
3. **Revoke the SP's base `SELECT`** (and the temporary create grant). Now the SP can reach the
   view but not the base table — the bypass is closed.

## Surface limitation — UI cannot render sample data

`current_oauth_custom_identity_claim()` only resolves on **token-authenticated** surfaces: the
Statement Execution API, JDBC to a warehouse/cluster, and (verified here) the **Genie Conversation
API**. It does **not** resolve in the **SQL editor / notebook UI** (no claim in those sessions),
so while `orders_secure_b` is in place the **Genie/SQL UI cannot preview the view's data** — that
is expected. Drive Pattern B through the API or `run_cli.py`. (It is also not supported on Jobs or
Lakeflow/DLT.)

## Setup

```bash
pip install -r ../requirements.txt
cp config.example.yaml config.yaml     # fill in workspace_url, warehouse_id, the ONE shared SP, view_owner

# 0) build the shared base table first (once):  cd ../common && python3 setup_base_data.py

# 1) provision orders_secure_b (definer-owned, SP base SELECT revoked):
export DATABRICKS_TOKEN=$(databricks auth token --profile <profile> | jq -r .access_token)
python3 provision.py
python3 verify_grants.py               # optional: prints the verbatim proof above

# 2) create a Genie space over <catalog>.<schema>.orders_secure_b with run_as = VIEWER,
#    grant the shared SP CAN_RUN, and put its id in genie_space_id in config.yaml.

# 3) ask Genie as each merchant — proves isolation via the claim:
python3 run_cli.py --merchant M001 --question "What was my total revenue?"   # -> 85.25
python3 run_cli.py --merchant M003 --question "What was my total revenue?"   # -> 757.00

# or the Streamlit portal:
streamlit run app.py
```

## Files

| file | role |
|---|---|
| `provision.py` | builds `orders_secure_b` (create-as-SP, transfer ownership, revoke base SELECT) |
| `verify_grants.py` | prints verbatim proof the bypass is closed and the no-claim path fails safe |
| `genie_client.py` | Conversation API client — mints one token per request with `custom_claim` |
| `run_cli.py` | ask a question as a given merchant from the terminal |
| `app.py` | Streamlit "merchant portal" UI |
| `config.example.yaml` | copy to `config.yaml` (gitignored) and fill in |

## Pros / cons

**Pros**

- **One SP for unlimited tenants** — collapses Kustom's ~24k per-tenant SPs into a single
  Service Principal. Onboarding a tenant is just issuing a new claim string; no Databricks object
  is created.
- **No mapping table, no per-tenant grants.** The tenant lives in the token.
- Same isolation guarantee as Pattern A, enforced by Unity Catalog under Genie.

**Cons**

- **`current_oauth_custom_identity_claim()` is Private Preview** — must be enabled per workspace
  by Databricks.
- **No UI data preview** while the view is active (claim only exists on token-auth calls).
- The host app must be trusted to set the correct claim — a wrong claim means cross-tenant
  exposure. The claim must not contain PII (it can surface in audit logs).
- The eager-eval CREATE flow is a one-time provisioning subtlety to get right.
