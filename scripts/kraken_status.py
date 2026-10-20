"""Read-only Kraken status: open orders + non-zero balances. Places nothing. Reads .env."""
from __future__ import annotations

import os

import ccxt
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    key, secret = os.getenv("KRAKEN_API_KEY"), os.getenv("KRAKEN_API_SECRET")
    if not key or not secret:
        print("No keys in .env.")
        return
    ex = ccxt.kraken({"apiKey": key, "secret": secret, "enableRateLimit": True})
    opens = ex.fetch_open_orders()
    print(f"OPEN ORDERS: {len(opens)}")
    for o in opens:
        print(f"  {o['symbol']} {o['side']} {o['amount']} @ {o['price']}  "
              f"placed={o.get('datetime')}  id={o['id']}")
    print("non-zero balances:")
    for a, amt in sorted(ex.fetch_balance()["total"].items()):
        if amt and amt > 0:
            print(f"  {a:8} {amt}")


if __name__ == "__main__":
    main()
