"""Ask Genie as a given merchant, from the terminal. Pattern B (shared SP + claim).

Two enforcement modes (both isolate the same way: claim in the token, read by
current_oauth_custom_identity_claim() — Genie's generated SQL has no tenant predicate):

  --mode rowfilter  (DEFAULT)  UC ROW FILTER on the `orders` table.        -> genie_space_id
  --mode dv                    Dynamic secure VIEW `orders_secure_dv`;     -> genie_space_id_dv
                               base table `orders_base` is left open for
                               non-claim apps, and the merchant SP is
                               DENIED base-table SELECT (bypass closed).
"""
import argparse
import json
from genie_client import GenieClient, load_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merchant", required=True, help="claim value, e.g. M001")
    ap.add_argument("--question", required=True)
    ap.add_argument("--mode", choices=["rowfilter", "dv"], default="rowfilter",
                    help="rowfilter (default, the live-demo path) or dv (dynamic secure view)")
    args = ap.parse_args()

    cfg = load_config()
    space_id = cfg["genie_space_id_dv"] if args.mode == "dv" else cfg["genie_space_id"]

    r = GenieClient(cfg, space_id=space_id).ask(args.merchant, args.question)
    print(f"\nmode:   {args.mode}  (space {space_id})")
    print(f"claim:  {args.merchant}")
    print(f"status: {r['status']}")
    if r.get("text"):
        print(f"answer: {r['text']}")
    if r.get("sql"):
        print(f"sql:    {r['sql']}")
    if r.get("data") is not None:
        print(f"data:   {json.dumps(r['data'])}")


if __name__ == "__main__":
    main()
