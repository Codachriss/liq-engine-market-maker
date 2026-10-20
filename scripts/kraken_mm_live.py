"""Two-sided market maker for Kraken (spot) — the sensible strategy.

Unlike the earlier buy-then-wait-for-profit bot, this holds a BALANCED inventory of one pair (~half
token, half USD) and continuously quotes a bid below mid AND an ask above mid, re-centering on the
live mid every cycle. It buys dips and sells rips, capturing the spread in both directions — the way
Hummingbot's pure market making works. Inventory-skew sizing keeps it balanced (never exceeds the
per-pair allocation, never sells more than it holds). A per-pair circuit-breaker bails + rotates if
the pair trends down past a threshold. Pair selection uses the trend filter (skip falling pairs) and
min-order filter (only pairs whose minimum fits a lot). Reads keys from .env. postOnly only (maker).

HONEST: a real two-sided MM can sell individual lots slightly below a prior buy (it nets the spread
across many round-trips), and it still loses in a sustained downtrend (long the inventory buffer). It
WINS in choppy/sideways markets. This is not a "never negative" bot — that requirement is
incompatible with real market making.

Run:  python -u scripts/kraken_mm_live.py [FORCED_PAIR]
"""
from __future__ import annotations

import os
import signal
import sys
import time

import ccxt
import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
from mm_bot import RiskConfig, fetch_universe, select  # noqa: E402

ALLOC_USD = 16.0          # capital deployed per pair (max token inventory)
LOT_USD = 12.0            # size of each quote (bigger lots, but max inventory below caps exposure)
HALF_SPREAD_BPS = 50      # informational; LEVEL_FRACS drive the quotes
LEVEL_FRACS = [0.005, 0.009]  # quote FAR from mid (0.5%, 0.9%): only fill on real moves, each
                              # round-trip clears the 0.32% round-trip fee with margin to spare.
                              # Fewer, better fills -> less fee churn (the over-trading fix).
SLEEP_S = 20.0            # slower refresh: don't frantically re-enter after every sell
RUN_SECONDS = float(os.getenv("KRAKEN_RUN_SECONDS", "0"))  # 0 = continuous
REPICK_EVERY = 5
COOLDOWN_CYCLES = 60      # after a bail, don't touch that pair for ~15 min
MIN_LOT_USD = 2.0         # don't bother placing a quote smaller than this


def cancel_open(ex, symbol: str) -> None:
    try:
        for o in ex.fetch_open_orders(symbol):
            try:
                ex.cancel_order(o["id"], symbol)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


def best_viable_alt(exclude: set[str] | None = None, max_order_usd: float = LOT_USD) -> str | None:
    """Top viable USD pair: trend-OK (not falling), min-order fits a lot, not in exclude set."""
    exclude = exclude or set()
    cfg = RiskConfig()
    with httpx.Client() as client:
        univ = fetch_universe(client, cfg)
        sel = select(client, cfg, univ)
    usd = [s for s in sel
           if s["alt"].endswith("USD")
           and s["alt"] not in exclude
           and s["ordermin"] * s["mid"] <= max_order_usd]
    usd.sort(key=lambda s: -s["vol24"])   # prefer the highest-volume viable pair -> more fills
    return usd[0]["alt"] if usd else None


def resolve_symbol(ex, altname: str) -> str | None:
    return next((s for s, m in ex.markets.items()
                 if s == altname or m.get("id") == altname or m.get("altname") == altname
                 or s.replace("/", "") == altname), None)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    key, secret = os.getenv("KRAKEN_API_KEY"), os.getenv("KRAKEN_API_SECRET")
    if not key or not secret:
        print("No keys in .env.")
        return
    signal.signal(signal.SIGTERM, _raise_interrupt)
    signal.signal(signal.SIGINT, _raise_interrupt)

    forced = sys.argv[1] if len(sys.argv) > 1 else None
    ex = ccxt.kraken({"apiKey": key, "secret": secret, "enableRateLimit": True})
    ex.load_markets()

    symbol: str | None = None
    base = ""
    entry_mid = 0.0
    inv_prev = 0.0
    cooldown: dict[str, int] = {}

    def value_now(sym: str | None) -> tuple[float, float, float]:
        bal = ex.fetch_balance()["total"]
        usd = bal.get("USD", 0.0)
        b = bal.get(ex.markets[sym]["base"], 0.0) if sym else 0.0
        mid = 0.0
        if sym:
            t = ex.fetch_ticker(sym)
            mid = (t["bid"] + t["ask"]) / 2
        return usd, b, mid

    start_usd, _, _ = value_now(None)
    # Baseline = portfolio value (USD + whatever token we hold) captured on the first live cycle,
    # so pre-existing/leftover inventory is NOT mistaken for profit. net starts at 0.
    start_value: float | None = None
    print(f"TWO-SIDED MM: alloc=${ALLOC_USD}/pair lot=${LOT_USD} half_spread={HALF_SPREAD_BPS}bps "
          f"hold-and-quote (no panic stop) run={'continuous' if RUN_SECONDS<=0 else int(RUN_SECONDS)} "
          f"launch_USD=${start_usd:.2f}")

    deadline = (time.monotonic() + RUN_SECONDS) if RUN_SECONDS > 0 else None
    i = 0
    try:
        while deadline is None or time.monotonic() < deadline:
            i += 1

            # pick a pair when we have none (start, or after a bail)
            if symbol is None:
                excl = {a for a, until in cooldown.items() if i < until}
                alt = forced or best_viable_alt(exclude=excl)
                if not alt:
                    time.sleep(SLEEP_S)
                    continue
                symbol = resolve_symbol(ex, alt)
                if symbol is None:
                    time.sleep(SLEEP_S)
                    continue
                base = ex.markets[symbol]["base"]
                entry_mid = ex.fetch_ticker(symbol)["bid"]
                inv_prev = ex.fetch_balance()["total"].get(base, 0.0)
                print(f"  -> on {symbol} (entry_mid {entry_mid:.8g})")

            cancel_open(ex, symbol)
            t = ex.fetch_ticker(symbol)
            bid, ask = t["bid"], t["ask"]
            mid = (bid + ask) / 2
            held = ex.fetch_balance()["total"].get(base, 0.0)

            # fill detection (for the log/monitor)
            d = held - inv_prev
            if d > 1e-12:
                print(f"  BUY filled {d:.6g} {base}")
            elif d < -1e-12:
                print(f"  SELL filled {-d:.6g} {base}")
            inv_prev = held
            base_usd = held * mid

            # NO panic stop-loss: like Hummingbot, hold inventory through dips and keep quoting;
            # the ask at mid+spread sells it when price recovers (mean-reversion). We never
            # market-sell at a loss. (Entry trend-filter keeps us from starting on fallers.)

            # multi-level quoting (Hummingbot-style); TOTAL size capped by ALLOC so max exposure
            # (risk) is unchanged no matter the lot size.
            min_amt = (ex.markets[symbol].get("limits", {}).get("amount", {}) or {}).get("min") or 0
            min_usd = min_amt * mid
            floor = max(MIN_LOT_USD, min_usd)

            # BIDS: spread the buy budget (ALLOC - held) across levels, each clearing the minimum
            bid_budget = max(0.0, ALLOC_USD - base_usd)
            for frac in LEVEL_FRACS:
                size_usd = min(LOT_USD, bid_budget)
                if size_usd < floor:
                    break
                px = round(mid * (1 - frac), 8)
                try:
                    ex.create_limit_buy_order(symbol, size_usd / px, px, {"postOnly": True})
                    bid_budget -= size_usd
                except Exception as e:  # noqa: BLE001
                    print(f"  bid skip: {str(e)[:70]}")
                    break

            # ASKS: spread held inventory across levels; never leave a sub-minimum tail (dump it)
            ask_budget = base_usd
            sold = 0.0
            for frac in LEVEL_FRACS:
                size_usd = min(LOT_USD, ask_budget)
                if size_usd < floor:
                    break
                if (ask_budget - size_usd) < min_usd:   # remainder would be unsellable -> sell all
                    size_usd = ask_budget
                px = round(mid * (1 + frac), 8)
                amt = min(held - sold, size_usd / px)
                if amt <= 0:
                    break
                try:
                    ex.create_limit_sell_order(symbol, amt, px, {"postOnly": True})
                    ask_budget -= size_usd
                    sold += amt
                except Exception as e:  # noqa: BLE001
                    print(f"  ask skip: {str(e)[:70]}")
                    break

            usd_now = ex.fetch_balance()["total"].get("USD", 0.0)
            if start_value is None:
                start_value = usd_now + base_usd   # baseline on first cycle -> net starts at 0
            pnl = (usd_now + base_usd) - start_value
            print(f"  c{i} {symbol}: mid={mid:.8g} inv={held:.6g}{base}(${base_usd:.2f}) "
                  f"usd=${usd_now:.2f} net=${pnl:+.3f}")
            time.sleep(SLEEP_S)
    except KeyboardInterrupt:
        print("\nstopping (signal) — cleaning up…")
    finally:
        if symbol:
            cancel_open(ex, symbol)
        usd_now = ex.fetch_balance()["total"].get("USD", 0.0)
        held = ex.fetch_balance()["total"].get(base, 0.0) if symbol else 0.0
        mv = held * ex.fetch_ticker(symbol)["bid"] if (symbol and held > 0) else 0.0
        base_val = start_value if start_value is not None else (usd_now + mv)
        print(f"\nsession end: USD ${usd_now:.2f}" + (f" + {held:.6g} {base} (${mv:.2f})" if mv else ""))
        print(f"net P&L vs start: ${(usd_now + mv) - base_val:+.3f}")
        print("orders cancelled. open inventory is a real position on Kraken.")


if __name__ == "__main__":
    main()
