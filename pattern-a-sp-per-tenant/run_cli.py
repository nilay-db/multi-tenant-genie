"""Ask Genie as a given merchant, from the terminal. Pattern A (SP per tenant)."""
import argparse
import json
from genie_client import GenieClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merchant", required=True, help="tenant_id, e.g. M001")
    ap.add_argument("--question", required=True)
    args = ap.parse_args()

    r = GenieClient().ask(args.merchant, args.question)
    print(f"\nmerchant: {args.merchant}")
    print(f"status:   {r['status']}")
    if r.get("text"):
        print(f"answer:   {r['text']}")
    if r.get("sql"):
        print(f"sql:      {r['sql']}")
    if r.get("data") is not None:
        print(f"data:     {json.dumps(r['data'])}")


if __name__ == "__main__":
    main()
