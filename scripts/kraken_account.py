"""Read-only Kraken key + balance check. Proves the API key works before any trading is wired.

SECURITY: reads KRAKEN_API_KEY / KRAKEN_API_SECRET from .env (git-ignored). Never pass keys on the
command line or in chat. This script only queries balances and fee tiers — it places NO orders and
needs no withdrawal permission. If it can read your balance, the key is good.

Setup: add to .env (in your editor, not here):
    KRAKEN_API_KEY=your_key
    KRAKEN_API_SECRET=your_secret
Then run:  python -I scripts/kraken_account.py
"""
from __future__ import annotations

import os

import ccxt
from dotenv import load_dotenv


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    key = os.getenv("KRAKEN_API_KEY")
    secret = os.getenv("KRAKEN_API_SECRET")
    if not key or not secret:
        print("No KRAKEN_API_KEY / KRAKEN_API_SECRET in .env — add them (in your editor) first.")
        print("Make sure the key has Query Funds + Create/Modify Orders, and NO withdrawal rights.")
        return

    ex = ccxt.kraken({"apiKey": key, "secret": secret, "enableRateLimit": True})
    try:
        bal = ex.fetch_balance()
    except Exception as e:  # noqa: BLE001
        print(f"Key check FAILED: {type(e).__name__}: {str(e)[:160]}")
        print("Common causes: wrong key/secret, missing Query-Funds permission, or IP not allowed.")
        return

    print("Key works. Non-zero balances:")
    total = bal.get("total", {})
    for asset, amt in sorted(total.items()):
        if amt and amt > 0:
            print(f"  {asset:8} {amt}")
    print("\nReminder: for the MM universe (USD-quoted pairs) you'll want USD or USDT balance; "
          "convert GBP/USDC on Kraken if needed. No orders were placed by this script.")


if __name__ == "__main__":
    main()
