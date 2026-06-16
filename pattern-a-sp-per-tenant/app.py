"""
Streamlit "merchant portal" — Pattern A (Service Principal per tenant).

Simulates a SaaS customer portal: a merchant is "logged in" (picked in the sidebar);
the portal authenticates to Genie with that merchant's OWN Service Principal. Inside the
dynamic view orders_secure_a, current_user() = that SP, so Unity Catalog returns only
this merchant's rows. The merchant never logs into Databricks.
"""
import streamlit as st
import pandas as pd
from genie_client import GenieClient, load_config

st.set_page_config(page_title="Merchant Analytics — Pattern A (SP per tenant)", page_icon="📊", layout="wide")

cfg = load_config()
client = GenieClient(cfg)
tenants = cfg["tenants"]

with st.sidebar:
    st.header("🔐 Merchant portal")
    st.caption("Pattern A — one Service Principal per tenant")
    names = {t["name"]: t for t in tenants}
    picked = st.selectbox("Logged-in merchant", list(names.keys()))
    tenant = names[picked]
    st.markdown(
        f"**Tenant id:** `{tenant['tenant_id']}`\n\n"
        f"**Auth:** SP `{tenant['sp_client_id'][:8]}…`\n\n"
        "The portal authenticates to Genie as this merchant's own Service Principal. "
        "The dynamic view `orders_secure_a` maps `current_user()` (the SP) → tenant via "
        "`sp_tenant_map` and returns only this merchant's rows."
    )
    st.divider()
    st.caption("Switch merchants and ask the same question — the answer changes, "
               "though Genie generates identical SQL with no tenant predicate.")

st.title("📊 Merchant Analytics")
st.caption(f"Ask questions about **{picked}**'s data, in natural language.")

if "history" not in st.session_state:
    st.session_state.history = []

qbtns = cfg.get("questions", [])
cols = st.columns(len(qbtns) or 1)
for i, q in enumerate(qbtns):
    if cols[i].button(q, use_container_width=True):
        st.session_state.pending = q

question = st.chat_input("Ask a question about your data…")
if "pending" in st.session_state:
    question = st.session_state.pop("pending")

if question:
    with st.spinner(f"Asking Genie as {picked}…"):
        r = client.ask(tenant["tenant_id"], question)
    st.session_state.history.insert(0, (picked, question, r))

for who, q, r in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(f"**{who}** asked: {q}")
    with st.chat_message("assistant"):
        if r["status"] != "COMPLETED":
            st.error(f"Genie status: {r['status']}")
        if r.get("text"):
            st.markdown(r["text"])
        if r.get("data") is not None:
            df = pd.DataFrame(r["data"], columns=r.get("columns") or None)
            st.dataframe(df, use_container_width=True, hide_index=True)
        if r.get("sql"):
            with st.expander("SQL Genie generated (no tenant predicate — the dynamic view isolates)"):
                st.code(r["sql"], language="sql")
        if r.get("api"):
            with st.expander("🔌 Genie API call (per-tenant SP — the merchant never logs into Databricks)"):
                a = r["api"]
                st.markdown(
                    f"- **Auth:** {a['auth_method']}\n"
                    f"- **Authenticated as:** `{a['authenticated_as']}`\n"
                    f"- **1. Mint token:** `{a['token_endpoint']}`\n"
                    f"- **2. Ask Genie:** `{a['genie_endpoint']}`\n"
                    f"- **3. Poll for result:** `{a['poll_endpoint']}`\n"
                    f"- **Genie space:** `{a['space_id']}`  ·  **conversation:** `{a['conversation_id']}`  ·  **message:** `{a['message_id']}`"
                )
                st.caption("This is the Genie Conversation API (REST). The calling SP's identity "
                           "is what the view's current_user() uses to isolate rows.")
