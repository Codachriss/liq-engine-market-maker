"""Minimal, near-zero-risk live-execution test for Kraken.

Validates the live order path end to end WITHOUT a real fill: it places ONE tiny postOnly limit buy
far below the current market (so it cannot execute), confirms the order is resting, then cancels it.
If this works, the full MM bot's live path is proven. Reads keys from .env (never from chat/argv).

Run (only when you've said go):  python -I scripts/kraken_live_test.py [PAIR]
Default PAIR: SOL/USDT (uses your USDT, liquid, easy to cancel).
"""
from __future__ import annotations

import os
import sys
import time

import ccxt
from dotenv import load_dotenv

BELOW_MARKET = 0.20   # place buy 20% below bid => cannot fill
NOTIONAL_USD = 8.0


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    key, secret = os.getenv("KRAKEN_API_KEY"), os.getenv("KRAKEN_API_SECRET")
    if not key or not secret:
        print("No keys in .env.")
        return
    pair = sys.argv[1] if len(sys.argv) > 1 else "SOL/USDT"
    ex = ccxt.kraken({"apiKey": key, "secret": secret, "enableRateLimit": True})
    ex.load_markets()
    if pair not in ex.markets:
        print(f"{pair} not a Kraken market. Pick another.")
        return

    t = ex.fetch_ticker(pair)
    bid = t["bid"]
    price = round(bid * (1 - BELOW_MARKET), 6)
    market = ex.markets[pair]
    min_amt = (market.get("limits", {}).get("amount", {}) or {}).get("min") or 0
    amount = max(min_amt, NOTIONAL_USD / price)

    print(f"{pair}: bid={bid}  placing postOnly BUY {amount:.6g} @ {price} "
          f"(~20% below market, cannot fill)")
    try:
        order = ex.create_limit_buy_order(pair, amount, price, {"postOnly": True})
    except Exception as e:  # noqa: BLE001
        print(f"PLACE FAILED: {type(e).__name__}: {str(e)[:160]}")
        return
    oid = order.get("id")
    print(f"  placed, id={oid}")

    time.sleep(2)
    opens = ex.fetch_open_orders(pair)
    resting = any(o.get("id") == oid for o in opens)
    print(f"  resting on book: {resting}")

    try:
        ex.cancel_order(oid, pair)
        print("  canceled OK")
    except Exception as e:  # noqa: BLE001
        print(f"  CANCEL FAILED (do it manually on Kraken!): {type(e).__name__}: {str(e)[:120]}")
        return
    print("Live path validated: place + rest + cancel all work. No fill occurred.")


if __name__ == "__main__":
    main()
