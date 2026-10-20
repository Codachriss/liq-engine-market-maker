"""Kraken market-making viability scanner (public API, no account, no capital, no auth).

The necessary condition to profit from passive market-making is: the pair's current spread must be
wider than the round-trip maker fee (you pay the maker fee on both the buy fill and the sell fill).
This pulls every USD/USDC/USDT-quoted Kraken pair, its maker fee and min order size, and its live
bid/ask, then ranks the pairs where spread > 2x maker fee with margin — i.e. where a dynamic-universe
MM bot could actually make money right now. It is a NECESSARY (not sufficient) check: real capture is
less than the full spread and adverse selection bites, so a pair failing this definitely can't work,
and one passing it is a candidate, not a guarantee.

Run:  python -I scripts/kraken_mm_scan.py
"""
from __future__ import annotations

import time

import httpx

API = "https://api.kraken.com/0/public"
USD_QUOTES = {"ZUSD", "USD", "USDC", "USDT"}
ACCOUNT_USD = 500.0  # context for min-order feasibility


def get(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    r = client.get(f"{API}/{path}", params=params or {}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise RuntimeError(f"{path}: {body['error']}")
    return body["result"]


def main() -> None:
    with httpx.Client() as client:
        pairs = get(client, "AssetPairs")
        # Keep USD-like quoted, spot pairs; record maker fee (lowest-volume tier) + min order.
        candidates: dict[str, dict] = {}
        for key, p in pairs.items():
            if p.get("quote") not in USD_QUOTES:
                continue
            if ".d" in key or p.get("status") != "online":
                continue
            fees_maker = p.get("fees_maker") or p.get("fees") or [[0, 0.25]]
            maker_bps = float(fees_maker[0][1]) * 100
            candidates[key] = {
                "alt": p.get("altname", key),
                "maker_bps": maker_bps,
                "ordermin": float(p.get("ordermin", 0) or 0),
                "base": p.get("base"),
            }

        print(f"USD-quoted online pairs: {len(candidates)}")

        # Ticker in batches.
        keys = list(candidates)
        tickers: dict[str, dict] = {}
        for i in range(0, len(keys), 60):
            batch = keys[i : i + 60]
            try:
                tickers.update(get(client, "Ticker", {"pair": ",".join(batch)}))
            except Exception as e:
                print(f"  ticker batch {i} err: {str(e)[:80]}")
            time.sleep(0.4)

        rows = []
        for key, c in candidates.items():
            t = tickers.get(key)
            if not t:
                continue
            try:
                ask = float(t["a"][0])
                bid = float(t["b"][0])
                vol_quote = float(t["v"][1]) * float(t["c"][0])  # 24h volume in quote approx
            except (KeyError, ValueError, IndexError):
                continue
            if ask <= 0 or bid <= 0 or ask <= bid:
                continue
            mid = (ask + bid) / 2
            spread_bps = (ask - bid) / mid * 10_000
            roundtrip_fee_bps = 2 * c["maker_bps"]
            edge_bps = spread_bps - roundtrip_fee_bps
            rows.append({
                "pair": c["alt"], "spread_bps": spread_bps, "fee_rt_bps": roundtrip_fee_bps,
                "edge_bps": edge_bps, "vol24_usd": vol_quote, "ordermin_usd": c["ordermin"] * mid,
            })

        viable = [r for r in rows if r["edge_bps"] > 0 and r["vol24_usd"] > 50_000
                  and r["ordermin_usd"] < ACCOUNT_USD / 4]
        viable.sort(key=lambda r: -r["edge_bps"])

        print(f"\npairs priced: {len(rows)}")
        print(f"pairs with spread > 2x maker fee, >$50k/24h vol, min-order affordable at ${ACCOUNT_USD:.0f}: "
              f"{len(viable)}\n")
        print(f"{'pair':16}{'spread_bps':>11}{'fee_rt_bps':>11}{'edge_bps':>10}"
              f"{'vol24_$':>14}{'min_ord_$':>11}")
        for r in viable[:30]:
            print(f"{r['pair']:16}{r['spread_bps']:>11.1f}{r['fee_rt_bps']:>11.1f}"
                  f"{r['edge_bps']:>10.1f}{r['vol24_usd']:>14,.0f}{r['ordermin_usd']:>11.2f}")

        if viable:
            med = sorted(r["edge_bps"] for r in viable)[len(viable) // 2]
            print(f"\nmedian edge among viable pairs: {med:.1f} bps (gross of adverse selection)")
        else:
            print("\nNO pairs clear spread > 2x fee at tradeable volume — MM not viable on Kraken "
                  "at the low-volume fee tier right now.")


if __name__ == "__main__":
    main()
