# Multi-Tenant Genie — Two Patterns for External-Facing, Per-Tenant Isolation

You want to embed [Databricks AI/BI Genie](https://docs.databricks.com/aws/en/genie/) in a
SaaS product so each of your customers ("tenants") can ask natural-language questions about
**their own data** — and only their own data. The tenants are external users who have **no
Databricks account**; they log into *your* app, and your app calls Genie's
[Conversation API](https://docs.databricks.com/api/workspace/genie) on their behalf.

The hard part is **isolation**: every tenant hits the *same* Genie space over the *same*
table, yet each must see only their rows. You never want to trust the LLM to write a correct
`WHERE tenant_id = …` — isolation has to be enforced by Unity Catalog, underneath Genie.

This repo shows **two production patterns** that do exactly that, both proven end-to-end
through the Genie Conversation API:

| | **Pattern A** | **Pattern B** |
|---|---|---|
| Name | **Service Principal per tenant** | **One shared SP + custom identity claim** |
| Folder | [`pattern-a-sp-per-tenant/`](pattern-a-sp-per-tenant/) | [`pattern-b-shared-sp-claim/`](pattern-b-shared-sp-claim/) |

## The one idea that makes this clean: identical data, isolation lives only in a view

Both patterns read from **one shared base table** with **identical data**:

```
common/setup_base_data.py  →  nt_workspace_catalog.kustom_claims_demo.orders_base
                              (a plain table — NO row filter, NO security on it)
```

The tenant-isolation logic does **not** live in that table. It lives entirely in a **dynamic
view**, and each pattern ships its own view that feeds its own Genie space:

```
                         orders_base   (shared, identical data, no isolation)
                          ┌────────┴─────────┐
        Pattern A:        ▼                   ▼        Pattern B:
   orders_secure_a                            orders_secure_b
   WHERE m.sp_identity = current_user()       WHERE tenant_id = current_oauth_custom_identity_claim()
        │                                          │
        ▼                                          ▼
   Genie space A (run_as=VIEWER)             Genie space B (run_as=VIEWER)
```

The two views differ in **one line** — the `WHERE` clause — and that single line is the entire
teaching point of this repo. Everything else (data, base table, Genie wiring) is the same.

## Pattern A vs Pattern B

| | **Pattern A — SP per tenant** | **Pattern B — shared SP + claim** |
|---|---|---|
| Who authenticates to Genie | the tenant's **own** Service Principal | **one shared** Service Principal, for every tenant |
| What changes per request | the **credentials** (different SP) | only the **token claim** (`custom_claim=<tenant>`); same SP |
| Identity at query time | `current_user()` → the calling SP's application id | `current_oauth_custom_identity_claim()` → the `custom.claim` in the token |
| The view's `WHERE` clause | `m.sp_identity = current_user()` (join through a mapping table) | `tenant_id = current_oauth_custom_identity_claim()` |
| Extra object needed | `sp_tenant_map` (SP → tenant) | none |
| Number of Service Principals | **one per tenant** (N SPs, N secrets) | **one, total** |
| Onboarding a tenant | create an SP + secret, add a map row, grant the space | issue a claim value — **no new Databricks object** |
| Scaling ceiling | the per-workspace SP count (thousands) + secret management | **effectively unlimited** (the tenant is just a string in a token) |
| Maturity | **GA** building blocks (SPs, views, `current_user()`) | relies on **`current_oauth_custom_identity_claim()` — Private Preview** |
| Best when | tens to low-thousands of tenants; you want GA-only pieces | many thousands of tenants (e.g. Kustom's ~24k); SP sprawl is the pain |

Both patterns share the **non-negotiables**:

- The Genie space `run_as` **must be `VIEWER`** — the query runs as the *caller*, so the view
  sees the caller's identity/claim. (Run-as-owner would collapse every tenant to the owner.)
- Tenant SPs (or the shared SP) get **`SELECT` on the view only** — never on `orders_base`.
  The view is owned by a **definer** that holds base `SELECT`, so the base privilege is
  satisfied by the owner while the *identity/claim* is read from the caller. This is what lets
  you deny callers direct base access and still have the view resolve — the bypass is closed.
- The merchant SP gets **`CAN_RUN`** on the Genie space.
- **Never** rely on Genie's generated SQL to isolate — in both patterns it contains **no tenant
  predicate**. Unity Catalog applies the filter transparently.

## About `current_oauth_custom_identity_claim()` (Pattern B)

`current_oauth_custom_identity_claim()` is a **Private Preview** SQL function. It returns the
value your host app placed in the OAuth token via `custom_claim=<value>` at `client_credentials`
time. Important surface limits:

- It **only resolves on token-authenticated surfaces** — the **Statement Execution API**, JDBC
  to a warehouse/cluster, and (verified in this repo) the **Genie Conversation API**.
- It does **not** resolve in the **SQL editor / notebook UI**, because those sessions carry no
  custom claim. So while the Pattern B view is in place, the **Genie/SQL UI cannot render sample
  data** — that is expected, not a bug. Drive Pattern B through the API (or `run_cli.py`).
- It is **eager-evaluated at `CREATE VIEW` time**, which is why provisioning creates the view as
  the SP with a claim-bearing token, then transfers ownership (see Pattern B's README).

Pattern A uses only **GA** primitives (`current_user()`, views, mapping table), so it has none of
these surface limits.

For the exact token request (`curl`), the JWT shape, the Conversation API call sequence, and a
GA-vs-Private-Preview docs table, see **["Passing the custom claim — the API in detail"](pattern-b-shared-sp-claim/README.md)** in the Pattern B README.

## Repo layout

```
multi-tenant-genie/
├── README.md                       ← you are here (the big picture + A-vs-B)
├── requirements.txt                ← shared Python deps
├── .gitignore                      ← ignores **/config.yaml, .venv/, __pycache__/
├── common/
│   ├── README.md                   ← how to build the shared base table
│   ├── setup_base_data.py          ← creates + populates orders_base (run this FIRST)
│   ├── sql/                        ← (reserved for base DDL)
│   └── config.example.yaml
├── pattern-a-sp-per-tenant/
│   ├── README.md
│   ├── provision.py                ← sp_tenant_map + orders_secure_a + grants
│   ├── genie_client.py  run_cli.py  app.py
│   └── config.example.yaml
└── pattern-b-shared-sp-claim/
    ├── README.md
    ├── provision.py                ← orders_secure_b (definer-owned, SP base SELECT revoked)
    ├── verify_grants.py            ← verbatim proof the bypass is closed + fail-safe
    ├── genie_client.py  run_cli.py  app.py
    └── config.example.yaml
```

## Quick start

```bash
pip install -r requirements.txt

# An admin token is used for provisioning (DDL, grants, ownership transfer):
export DATABRICKS_TOKEN=$(databricks auth token --profile <profile> | jq -r .access_token)

# 1) Build the ONE shared base table (run once).
cd common && cp config.example.yaml config.yaml   # fill in workspace_url, warehouse_id
python3 setup_base_data.py

# 2) Then provision EITHER pattern (or both) — see each folder's README.
#    Each provision.py builds that pattern's view + Genie space wiring.
```

Sample data totals (so you can confirm isolation): **M001 = 85.25, M002 = 345.00,
M003 = 757.00**, full table = **1187.25**.

## Verified results (FEVM)

Both patterns were tested **through the Genie Conversation API** (`run_cli.py`), asking the same
question — *"What was my total revenue?"* — as each tenant:

| Tenant | Pattern A answer | Pattern B answer |
|---|---|---|
| M001 | **85.25** | **85.25** |
| M002 | **345.00** | (345.00) |
| M003 | **757.00** | **757.00** |

In every case Genie's generated SQL was `SELECT SUM(amount) … FROM orders_secure_<a|b> WHERE
amount IS NOT NULL` — **no tenant predicate**. Pattern B additionally fails safe: a token with
**no claim** returns `OAUTH_CUSTOM_IDENTITY_CLAIM_NOT_PROVIDED` (SQLSTATE 22KD2), and a merchant
SP querying `orders_base` directly is denied with `INSUFFICIENT_PERMISSIONS` (SQLSTATE 42501).

## Security notes (both patterns)

- Service-principal secrets live only in the host-app backend (`config.yaml`, gitignored). Never
  ship them to the browser.
- The host app — not Databricks — authenticates the tenant and chooses the SP (A) or claim (B).
  Treat that choice as security-critical: a wrong SP/claim means cross-tenant exposure.
- The custom claim (B) must not contain PII — it can appear in audit logs.
- The view is the only thing trusted to isolate. Never rely on the LLM's `WHERE` clause.
