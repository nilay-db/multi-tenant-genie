# Multi-Tenant Genie — Pattern B: One Shared SP + OAuth Custom Identity Claim

A runnable reference implementation of **external-facing, multi-tenant Genie** that serves an
**unlimited number of tenants with a single Service Principal**. Per request, the host app mints
an OAuth token carrying a **custom identity claim** (the tenant id); a **Unity Catalog row filter**
reads that claim via `current_oauth_custom_identity_claim()` and returns only that tenant's rows.

External users (merchants) log into the host app — they have **no Databricks account, and there is
no per-tenant Service Principal.**

> This is **Pattern B**. See the companion repo **`genie-demo-per-tenant-sp`** for **Pattern A**
> (one SP per tenant, GA). Pattern B removes the per-tenant SP entirely but relies on a
> **Private Preview** capability (see *Status* below).

---

## Architecture

```
   Merchant logs into host app (no Databricks account)
                 │
                 ▼
   ┌───────────────────────────────────────────────────────┐
   │ HOST APP (this demo: Streamlit portal)                  │
   │  • resolve user → tenant claim (e.g. "M001")            │
   │  • mint OAuth token from the ONE shared SP with:        │
   │      grant_type=client_credentials                      │
   │      scope=all-apis                                      │
   │      custom_claim=M001       ← rides inside the JWT      │
   └───────────────────────────┬────────────────────────────┘
                               │  Bearer token (shared SP + claim)
                               ▼
   ┌───────────────────────────────────────────────────────┐
   │ DATABRICKS                                              │
   │  Genie Conversation API → generated SQL →               │
   │  Unity Catalog ROW FILTER:                              │
   │     tenant_id = current_oauth_custom_identity_claim()   │
   │  → returns only the claimed tenant's rows                │
   └───────────────────────────────────────────────────────┘
```

- **Isolation key:** the `custom.claim` value inside the token, read by
  `current_oauth_custom_identity_claim()` (a 0-arg SQL function).
- **One Service Principal** serves every tenant; the per-request token differs only by the claim.
- **Enforcement layer:** Unity Catalog row filter — Genie's generated SQL has no tenant predicate.
- **Genie space `run_as` must be `VIEWER`** (runs as the caller), not run-as-owner.

### Trade-offs

| | Pattern A (`genie-demo-per-tenant-sp`) | Pattern B (this repo) |
|---|---|---|
| Service principals | one per tenant | **one shared** |
| Grants to maintain | per SP | **once** |
| Scale ceiling | ~10k SP soft limit | **effectively unlimited** |
| Maturity | GA | **Private Preview** |

---

## Two enforcement modes: row filter vs dynamic secure view

This demo ships **two interchangeable ways** to isolate tenants. Both rely on the *same*
mechanism — the custom claim in the token, read by `current_oauth_custom_identity_claim()`
— and in both, Genie's generated SQL has **no tenant predicate**. They differ only in
*where* the predicate lives.

| | `--mode rowfilter` (DEFAULT) | `--mode dv` |
|---|---|---|
| Enforcement object | UC **row filter** on table `orders` | dynamic secure **view** `orders_secure_dv` over `orders_base` |
| Base table for non-claim apps | unusable — the row filter **fails closed** for any caller without a claim | **`orders_base` stays open**: an app with base SELECT and *no* claim reads all rows |
| Genie space | `genie_space_id` | `genie_space_id_dv` |
| Provision script | `provision.py` | `provision_dv.py` |
| Status | live-demo path | **verified end-to-end through the Genie Conversation API** (this addition) |

**Why the DV mode exists (Kustom):** Kustom's base `orders` table must remain readable by
**other applications that carry no merchant claim**. A row filter on the base table would
break those apps (it fails closed for every claim-less caller). The DV mode leaves the base
table open and routes the merchant-facing path through a dynamic secure view instead.

### How the DV mode isolates — and why the bypass is truly closed

```
orders_base                         plain table, NO row filter        owner: admin/definer
orders_secure_dv  =  SELECT * FROM orders_base
                     WHERE tenant_id = current_oauth_custom_identity_claim()   owner: definer
```

The production-safe grant model (built by `provision_dv.py`, proven by `verify_dv_grants.py`):

- **View owner** = a definer identity (e.g. the provisioning admin) that **holds base SELECT**.
- **Merchant SP** = the shared SP Genie uses; it gets **SELECT on the view only**, and its
  **base-table SELECT is REVOKED**.

UC resolves this cleanly because the two checks are **independent**:

- the **base-table privilege** is satisfied by the **view owner** (definer's rights), and
- the **claim** is read from the **caller's** token (the SP), at view-evaluation time.

So the merchant SP can be **denied base-table access and still use the view**. Verbatim
verification (`verify_dv_grants.py`):

```
A.  SP + claim=M001  -> SELECT FROM orders_secure_dv   = 85.25     (isolated, SP has NO base SELECT)
A2. SP + claim=M003  -> SELECT FROM orders_secure_dv   = 757.00    (isolated)
B.  SP -> SELECT FROM orders_base DIRECTLY  -> FAILED  [INSUFFICIENT_PERMISSIONS]
        User does not have SELECT on Table '...orders_base'. SQLSTATE: 42501   (bypass CLOSED)
C.  other app (base SELECT, NO claim) -> SELECT FROM orders_base = 1187.25     (non-claim use case works)
D.  SP, NO claim -> SELECT FROM orders_secure_dv -> FAILED
        [OAUTH_CUSTOM_IDENTITY_CLAIM_NOT_PROVIDED] SQLSTATE: 22KD2             (fails safe)
```

**Genie-over-DV proof** (the whole point — through the Conversation API, not Statement Execution):

```
--mode dv --merchant M001 "What was my total revenue?"  -> 85.25
--mode dv --merchant M003 "What was my total revenue?"  -> 757.00
generated SQL: SELECT SUM(`amount`) AS total_revenue
               FROM `nt_workspace_catalog`.`kustom_claims_demo`.`orders_secure_dv`
               WHERE `amount` IS NOT NULL          ← NO tenant predicate; isolation is in the view
```

> **Eager-eval gotcha (DV mode):** `current_oauth_custom_identity_claim()` is evaluated at
> `CREATE VIEW` time, so `provision_dv.py` creates the view **as the SP with a claim-bearing
> token**, then **transfers view ownership to the definer** and **revokes the SP's base SELECT**.

---

## Status — Private Preview

The capability is **"Custom Identity Claims"** (a.k.a. Identity Claim) — currently **Private
Preview**. Verified working end-to-end through the **Genie Conversation API** on the FEVM
workspace (this demo). For another workspace it must be enabled by Databricks.

Supported surfaces for the claim: **JDBC + warehouse, JDBC + cluster, Statement Execution API,
and (verified here) the Genie Conversation API.** *Not* supported on UI surfaces (SQL editor,
notebook cells), Jobs, or Lakeflow/DLT — so the Genie/SQL **UI cannot render sample data** while
the claim row filter is on (the claim only exists on token-authenticated calls).

---

## Layout

```
sql/01_setup.sql      catalog/schema, orders table + sample data, claim row-filter function,
                      applies the row filter  (see notes — must be created WITH a claim)
sql/02_grants.sql     grants for the single shared SP + warehouse + Genie space
genie_client.py       Conversation API client (mints one token per request with custom_claim)
app.py                Streamlit "merchant portal" UI
run_cli.py            ask a question as a given merchant (--mode rowfilter|dv) from the terminal
provision.py          rowfilter mode: schema/table/data/row-filter/grants, handling claim-eval order
provision_dv.py       dv mode: orders_base + orders_secure_dv, definer-owned view, SP base SELECT revoked
verify_dv_grants.py   dv mode: prints verbatim proof the bypass is closed and non-claim apps still read base
query_dv_view.py      dv mode: query orders_secure_dv directly via Statement Execution API (non-Genie path)
config.example.yaml   copy to config.yaml and fill in (config.yaml is gitignored)
```

---

## Prerequisites

- A Pro or Serverless SQL warehouse.
- **One** Databricks Service Principal (shared across all tenants) with an **OAuth secret**
  (`databricks service-principal-secrets-proxy create <sp-scim-id>`).
- The **Custom Identity Claims** Private Preview enabled on the workspace.
- Python 3.10+.

## Setup

```bash
cp config.example.yaml config.yaml      # fill in workspace_url, warehouse_id, the shared SP secret
pip install -r requirements.txt

python3 provision.py                    # creates schema/table/data/row-filter/grants
# create a Genie space over <catalog>.<schema>.orders (run_as = VIEWER),
# grant the shared SP CAN_RUN, put space_id in config.yaml (see sql/02_grants.sql)

streamlit run app.py
# ...or:
python3 run_cli.py --merchant M001 --question "What was my total revenue?"
```

### DV mode (dynamic secure view)

```bash
# admin token used for provisioning (creates objects, transfers ownership, revokes grants):
export DATABRICKS_TOKEN=$(databricks auth token --profile <profile> | jq -r .access_token)

python3 provision_dv.py                 # orders_base + orders_secure_dv, definer-owned, SP base SELECT revoked
python3 verify_dv_grants.py             # prints verbatim proof the bypass is closed
# create a Genie space over <catalog>.<schema>.orders_secure_dv, grant the shared SP CAN_RUN,
# put its id in genie_space_id_dv in config.yaml, then:
python3 run_cli.py --mode dv --merchant M001 --question "What was my total revenue?"   # -> 85.25
python3 run_cli.py --mode dv --merchant M003 --question "What was my total revenue?"   # -> 757.00
```

### ⚠️ Setup gotcha — the row filter must be created WITH a claim present

`current_oauth_custom_identity_claim()` is **eager-evaluated** during both
`CREATE FUNCTION` and `ALTER TABLE … SET ROW FILTER`. A normal admin/user session has no claim,
so those two statements fail with `OAUTH_CUSTOM_IDENTITY_CLAIM_NOT_PROVIDED`. `provision.py`
handles this by running them **as the shared SP using a claim-bearing token** (and the SP must
own the table). This is why the SP owns `orders` in this demo.

## How isolation is proven

Ask the **same question** as different merchants — the host app mints a token with a different
`custom_claim` each time, and each answer is scoped to that tenant, even though Genie generates
identical SQL with no tenant predicate.

## Security notes

- The single SP secret lives only in the host app backend (`config.yaml`, gitignored). The host
  app — not Databricks — authenticates the merchant and chooses the claim. Treat claim selection
  as a security-critical control (a wrong claim = cross-tenant data exposure).
- The custom claim must not contain PII (it can surface in audit logs).
- The row filter is the only thing trusted to isolate data — never rely on Genie/the LLM.
