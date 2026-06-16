"""
Streamlit "merchant portal" — Pattern B (one shared SP + custom identity claim).

Simulates Kustom's customer portal: a merchant is "logged in" (picked in the sidebar);
the portal mints a token from the SINGLE shared SP carrying custom_claim=<tenant>, and
Unity Catalog returns only that merchant's data.
"""
import streamlit as st
import pandas as pd
from genie_client import GenieClient, load_config

st.set_page_config(page_title="Merchant Analytics — Pattern B (custom claim)", page_icon="🔑", layout="wide")

cfg = load_config()
client = GenieClient(cfg)
tenants = cfg["tenants"]

with st.sidebar:
    st.header("🔐 Merchant portal")
    st.caption("Pattern B — one shared SP + OAuth custom identity claim")
    names = {t["name"]: t for t in tenants}
    picked = st.selectbox("Logged-in merchant", list(names.keys()))
    tenant = names[picked]
    st.markdown(
        f"**Claim value:** `{tenant['claim']}`\n\n"
        f"**Auth:** shared SP `{cfg['app_sp']['client_id'][:8]}…`\n\n"
        "Every merchant uses the **same** Service Principal. The portal mints a per-request token "
        "with `custom_claim` set to this merchant; a UC row filter reads "
        "`current_oauth_custom_identity_claim()` and returns only this merchant's rows."
    )
    st.divider()
    st.caption("One SP for unlimited tenants — switch merchants and ask the same question; "
               "the answer changes via the claim, not a different SP.")

st.title("🔑 Merchant Analytics")
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
    with st.spinner(f"Asking Genie as {picked} (claim={tenant['claim']})…"):
        r = client.ask(tenant["claim"], question)
    st.session_state.history.insert(0, (picked, tenant["claim"], question, r))

for who, claim, q, r in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(f"**{who}** (claim `{claim}`) asked: {q}")
    with st.chat_message("assistant"):
        if r["status"] != "COMPLETED":
            st.error(f"Genie status: {r['status']}")
        if r.get("text"):
            st.markdown(r["text"])
        if r.get("data") is not None:
            df = pd.DataFrame(r["data"], columns=r.get("columns") or None)
            st.dataframe(df, use_container_width=True, hide_index=True)
        if r.get("sql"):
            with st.expander("SQL Genie generated (no tenant predicate — UC row filter + claim isolate)"):
                st.code(r["sql"], language="sql")
        if r.get("api"):
            with st.expander("🔌 Genie API call (one shared SP — the tenant rides in the token claim)"):
                a = r["api"]
                st.markdown(
                    f"- **Auth:** {a['auth_method']}\n"
                    f"- **Authenticated as:** `{a['authenticated_as']}`\n"
                    f"- **1. Mint token:** `{a['token_endpoint']}`\n"
                    f"- **2. Ask Genie:** `{a['genie_endpoint']}`\n"
                    f"- **3. Poll for result:** `{a['poll_endpoint']}`\n"
                    f"- **Genie space:** `{a['space_id']}`  ·  **conversation:** `{a['conversation_id']}`  ·  **message:** `{a['message_id']}`"
                )
                st.caption("Same shared SP for every merchant — only the `custom_claim` in the token changes. "
                           "Unity Catalog's row filter reads that claim via current_oauth_custom_identity_claim().")
