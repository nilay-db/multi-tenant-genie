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
run_cli.py            ask a question as a given merchant from the terminal
provision.py          runs the setup against your workspace, handling the claim-eval order
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
