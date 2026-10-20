"""Kraken dynamic-universe market-making bot — scaffold (paper mode; live slots in behind ccxt).

The bot you asked for: instead of a fixed 3 pairs, it scans the whole USD-quoted universe each cycle,
keeps only pairs whose current spread clears the round-trip maker fee by a risk margin, and
market-makes those — rotating as the viable set shifts, so it is never idle because "its coins are
down". Risk is enforced by hard caps (position size, min-edge gate, inventory stop-loss), so a bad
pair bounds the loss rather than running it to zero.

HONESTY ON PAPER MODE: simulated fills cannot model adverse selection or queue position, so paper
P&L is OPTIMISTIC — treat it as a plumbing/behaviour check, not a profit forecast. The real edge is
only revealed by LIVE trading at tiny size (needs a Kraken API key with trade perms + a deposit).

Run (paper):  python -I scripts/mm_bot.py [cycles] [sleep_s]
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

import httpx

API = "https://api.kraken.com/0/public"
USD_QUOTES = {"ZUSD", "USD", "USDC", "USDT"}


@dataclass
class RiskConfig:
    account_usd: float = 80.0           # actual tiny starting balance
    quote_size_usd: float = 8.0         # size per side per pair
    min_edge_bps: float = 15.0          # require spread to clear 2x fee by this margin
    max_spread_bps: float = 400.0       # EXCLUDE dead/dislocated markets (the FIS 3919bps trap)
    min_vol24_usd: float = 75_000.0     # skip illiquid pairs
    min_trades24: int = 150             # require real trade activity (fills only happen if it trades)
    max_pairs: int = 2                  # how many pairs to make at once
    max_position_usd: float = 20.0      # hard inventory cap per pair
    stop_loss_bps: float = 300.0        # unwind a pair's inventory if MTM loss exceeds this
    max_ordermin_frac: float = 0.25     # pair's min order must be < this * account
    max_downtrend_bps: float = 150.0    # skip pairs trading >this far below their 24h VWAP (falling)


@dataclass
class Book:
    pair: str
    maker_bps: float
    inventory_base: float = 0.0   # units of base token held (can be +)
    cash_usd: float = 0.0         # realised quote cash flow (fees included)
    quoted_bid: float | None = None
    quoted_ask: float | None = None
    fills: int = 0

    def mtm(self, mid: float) -> float:
        return self.cash_usd + self.inventory_base * mid


def get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    r = client.get(f"{API}/{path}", params=params or {}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(f"{path}: {body['error']}")
    return body["result"]


def fetch_universe(client: httpx.Client, cfg: RiskConfig) -> dict[str, dict]:
    pairs = get(client, "AssetPairs")
    out: dict[str, dict] = {}
    for key, p in pairs.items():
        if p.get("quote") not in USD_QUOTES or p.get("status") != "online" or ".d" in key:
            continue
        fees_maker = p.get("fees_maker") or p.get("fees") or [[0, 0.25]]
        out[key] = {
            "alt": p.get("altname", key),
            "maker_bps": float(fees_maker[0][1]) * 100,
            "ordermin": float(p.get("ordermin", 0) or 0),
        }
    return out


def select(client: httpx.Client, cfg: RiskConfig, univ: dict[str, dict]) -> list[dict]:
    """Rank the universe by current net edge, apply risk filters, return the pairs to make."""
    keys = list(univ)
    tickers: dict[str, dict] = {}
    for i in range(0, len(keys), 60):
        try:
            tickers.update(get(client, "Ticker", {"pair": ",".join(keys[i : i + 60])}))
        except Exception:
            pass
        time.sleep(0.3)

    ranked = []
    for key, c in univ.items():
        t = tickers.get(key)
        if not t:
            continue
        try:
            ask, bid, last = float(t["a"][0]), float(t["b"][0]), float(t["c"][0])
            vol24 = float(t["v"][1]) * last
            trades24 = int(t["t"][1])
            vwap24 = float(t["p"][1])
        except (KeyError, ValueError, IndexError):
            continue
        if ask <= bid <= 0:
            continue
        mid = (ask + bid) / 2
        spread_bps = (ask - bid) / mid * 10_000
        edge_bps = spread_bps - 2 * c["maker_bps"]
        # trend: last price vs 24h VWAP. Deeply below VWAP = falling -> skip (don't MM a downtrend).
        trend_bps = (last / vwap24 - 1) * 10_000 if vwap24 > 0 else 0.0
        if (cfg.min_edge_bps <= edge_bps and spread_bps <= cfg.max_spread_bps
                and vol24 >= cfg.min_vol24_usd and trades24 >= cfg.min_trades24
                and trend_bps >= -cfg.max_downtrend_bps
                and c["ordermin"] * mid < cfg.max_ordermin_frac * cfg.account_usd):
            ranked.append({**c, "key": key, "bid": bid, "ask": ask, "mid": mid,
                           "spread_bps": spread_bps, "edge_bps": edge_bps,
                           "vol24": vol24, "trades24": trades24, "trend_bps": trend_bps})
    ranked.sort(key=lambda r: -r["edge_bps"])
    return ranked[: cfg.max_pairs]


def paper_fill(book: Book, bid: float, ask: float, cfg: RiskConfig) -> None:
    """Conservative simulated fills: our resting quote fills only if the market trades through it.
    Optimistic nonetheless (no queue/adverse-selection modelling) — behaviour check, not P&L truth."""
    size_base_bid = cfg.quote_size_usd / book.quoted_bid if book.quoted_bid else 0
    fee = book.maker_bps / 10_000
    # Our prior bid fills if the new best ask dropped to/below it (price came down into us)
    if book.quoted_bid and ask <= book.quoted_bid and book.inventory_base * book.quoted_bid < cfg.max_position_usd:
        cost = size_base_bid * book.quoted_bid
        book.inventory_base += size_base_bid
        book.cash_usd -= cost * (1 + fee)
        book.fills += 1
    # Our prior ask fills if the new best bid rose to/above it (price came up to us)
    if book.quoted_ask and bid >= book.quoted_ask and book.inventory_base > 0:
        size_base_ask = min(book.inventory_base, cfg.quote_size_usd / book.quoted_ask)
        proceeds = size_base_ask * book.quoted_ask
        book.inventory_base -= size_base_ask
        book.cash_usd += proceeds * (1 - fee)
        book.fills += 1


def run(cycles: int, sleep_s: float) -> None:
    cfg = RiskConfig()
    with httpx.Client() as client:
        univ = fetch_universe(client, cfg)
        print(f"universe: {len(univ)} USD-quoted online pairs; risk cfg: "
              f"size=${cfg.quote_size_usd} max_pos=${cfg.max_position_usd} "
              f"min_edge={cfg.min_edge_bps}bps max_pairs={cfg.max_pairs}")
        books: dict[str, Book] = {}
        for i in range(cycles):
            sel = select(client, cfg, univ)
            print(f"\n--- cycle {i+1}/{cycles} @ {time.strftime('%H:%M:%S')} — "
                  f"making {len(sel)} pairs ---")
            live = {s["key"] for s in sel}
            for s in sel:
                b = books.setdefault(s["key"], Book(s["alt"], s["maker_bps"]))
                paper_fill(b, s["bid"], s["ask"], cfg)
                # stop-loss: unwind inventory at market (cross the spread) if MTM loss too deep
                mtm = b.mtm(s["mid"])
                if b.inventory_base * s["mid"] > 0 and mtm < -cfg.stop_loss_bps / 10_000 * cfg.quote_size_usd:
                    b.cash_usd += b.inventory_base * s["bid"] * (1 - b.maker_bps / 10_000)
                    b.inventory_base = 0.0
                    print(f"    [{b.pair}] STOP-LOSS unwind, mtm=${mtm:.3f}")
                # place next quotes just inside the spread
                b.quoted_bid = s["bid"] * (1 + 0.0001)
                b.quoted_ask = s["ask"] * (1 - 0.0001)
                print(f"    {b.pair:12} edge={s['edge_bps']:6.1f}bps  quote {b.quoted_bid:.6g}/"
                      f"{b.quoted_ask:.6g}  inv=${b.inventory_base*s['mid']:.2f}  fills={b.fills}")
            if i < cycles - 1:
                time.sleep(sleep_s)

        print("\n===== paper session (OPTIMISTIC — plumbing check, not profit) =====")
        total = 0.0
        for b in books.values():
            # mark remaining inventory at last quoted bid as a rough close
            mtm = b.cash_usd  # inventory valued ~0 here; realised cash only, conservative
            total += mtm
            print(f"  {b.pair:12} fills={b.fills:3d}  realised_cash=${b.cash_usd:+.3f}  "
                  f"open_inv_base={b.inventory_base:.4g}")
        print(f"  TOTAL realised cash (ex-open-inventory): ${total:+.3f}")
        print("  NOTE: open inventory not marked; paper fills ignore adverse selection. "
              "Real edge = live tiny-size only.")


if __name__ == "__main__":
    c = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    s = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    run(c, s)
