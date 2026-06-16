"""
Standalone reproducer: query the dynamic secure view per-tenant by minting a
shared-SP token that carries a custom identity claim (Pattern B, --mode dv).

The view (`<catalog>.<schema>.orders_secure_dv`) filters with
`current_oauth_custom_identity_claim()` in its body, so the SAME query returns
different rows depending on the claim in the caller's token — the caller never
writes a tenant predicate.

All connection details + the shared-SP secret come from config.yaml (gitignored).

Usage:
    python query_dv_view.py M001        # -> 85.25
    python query_dv_view.py M003        # -> 757.00
    python query_dv_view.py M001 "SELECT * FROM <view>"
    python query_dv_view.py --noclaim   # -> errors OAUTH_CUSTOM_IDENTITY_CLAIM_NOT_PROVIDED (fails safe)
"""
import sys, os, base64, requests, yaml

CFG  = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.yaml")))
HOST = CFG["workspace_url"].rstrip("/")
WH   = CFG["warehouse_id"]
VIEW = f'{CFG["catalog"]}.{CFG["schema"]}.orders_secure_dv'
CID  = CFG["app_sp"]["client_id"]
SEC  = CFG["app_sp"]["client_secret"]


def sp_token(claim):
    basic = base64.b64encode(f"{CID}:{SEC}".encode()).decode()
    data = {"grant_type": "client_credentials", "scope": "all-apis"}
    if claim is not None:
        data["custom_claim"] = claim
    r = requests.post(f"{HOST}/oidc/v1/token", headers={"Authorization": f"Basic {basic}"}, data=data, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def main():
    args = sys.argv[1:]
    claim = None
    if args and args[0] == "--noclaim":
        sql = args[1] if len(args) > 1 else f"SELECT SUM(amount) FROM {VIEW}"
    else:
        claim = args[0] if args else "M001"
        sql = args[1] if len(args) > 1 else f"SELECT SUM(amount) AS total FROM {VIEW}"

    tok = sp_token(claim)
    r = requests.post(f"{HOST}/api/2.0/sql/statements",
                      headers={"Authorization": f"Bearer {tok}"},
                      json={"warehouse_id": WH, "statement": sql, "wait_timeout": "50s"}, timeout=120)
    j = r.json(); st = j.get("status", {})
    print(f"claim={claim!r}  state={st.get('state')}")
    if st.get("error"):
        print(f"  {st['error'].get('error_code','')}: {st['error'].get('message','')}")
    rows = (j.get("result") or {}).get("data_array")
    if rows is not None:
        print(f"  rows: {rows}")


if __name__ == "__main__":
    main()
