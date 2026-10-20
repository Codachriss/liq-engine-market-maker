#!/usr/bin/env python
"""Offline profitability backtest of already-recorded liquidations. No scanning, no live reads.

Every `missed`/`taken`/`unclaimed` row in the shadow store is a real on-chain liquidation whose
repaid-debt size we already know. This replays each one through the economics the live engine
would apply — Morpho's exact liquidation-incentive factor minus gas, exit cost, and flash premium
— to answer one question the shadow run never could: if we had caught these, how many would have
cleared gas, and how much net profit did the detection gap leave on the table?

The incentive factor is reused from `liqengine` (the function the 689 tests cover). The cost terms
are explicit, tunable flags so you can see exactly what assumption each number rests on. Loan token
is USDC (=$1) on every allowlisted Morpho/Base market, so `debt_repaid` is already dollars.

Run with the project venv:  .venv/bin/python scripts/replay_missed.py
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass

from liqengine.core.fixedpoint import WAD
from liqengine.protocols.morpho_blue.math import liquidation_incentive_factor_wad

BANDS = ("over_10k", "under_10k", "under_1k", "under_100", "under_10", "under_1")

# collateral_token -> lltv_wad, resolved from chain by scripts/resolve_lltv.py (2026-10-16).
# A liquidation's incentive is set by its market's LLTV, so each row is costed at its own.
COLLATERAL_LLTV = {
    "0xcb585250f852c6c6bf90434ab21a00f02833a4af": 625000000000000000,  # cbXRP
    "0x97be14dd8f994a5364573bc035d85309e7cb34de": 625000000000000000,  # JitoSOL
    "0x311935cd80b76769bf2ecc9d8ab7635b2139cf82": 625000000000000000,  # SOL
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": 770000000000000000,  # cbETH
    "0x4200000000000000000000000000000000000006": 860000000000000000,  # WETH
    "0xcb17c9db87b595717c857a08468793f5bab6445f": 625000000000000000,  # cbLTC
}


@dataclass(frozen=True)
class Row:
    outcome: str
    size_bucket: str
    debt_usd: float
    collateral: str


@dataclass(frozen=True)
class Costed:
    row: Row
    gross: float
    exit_cost: float
    gas: float
    flash: float

    @property
    def net(self) -> float:
        return self.gross - self.exit_cost - self.gas - self.flash


def load(db: str, venue: str, outcome: str, loan_decimals: int) -> list[Row]:
    scale = 10**loan_decimals
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.execute(
            "SELECT size_bucket, debt_repaid, collateral_asset FROM shadow_outcome "
            "WHERE venue = ? AND outcome = ? AND debt_repaid IS NOT NULL",
            (venue, outcome),
        )
        return [
            Row(outcome, str(b), int(d) / scale, str(c or "").lower())
            for b, d, c in cur.fetchall()
        ]
    finally:
        con.close()


def lif_for(row: Row, fallback_lltv: float) -> float:
    """Incentive factor from the row's own market LLTV, falling back to the flag for unknowns."""
    lltv_wad = COLLATERAL_LLTV.get(row.collateral, int(fallback_lltv * WAD))
    return liquidation_incentive_factor_wad(lltv_wad) / WAD


def cost(row: Row, lif: float, exit_bps: float, gas_usd: float, flash_bps: float) -> Costed:
    notional = row.debt_usd * lif  # value of collateral the liquidator must sell
    return Costed(
        row=row,
        gross=row.debt_usd * (lif - 1.0),
        exit_cost=notional * exit_bps / 10_000.0,
        gas=gas_usd,
        flash=row.debt_usd * flash_bps / 10_000.0,
    )


def report(rows: list[Costed], threshold: float) -> None:
    by_band: dict[str, list[Costed]] = {b: [] for b in BANDS}
    for c in rows:
        by_band.setdefault(c.row.size_bucket, []).append(c)

    print(
        f"\n{'band':<10}{'n':>5}{'gross $':>12}{'net $':>12}"
        f"{f'exec (>=${threshold:g})':>16}{'profitable':>12}"
    )
    print("-" * 67)
    tot_n = tot_gross = tot_net = tot_exec = tot_prof = 0
    exec_net = 0.0
    for band in BANDS:
        cs = by_band.get(band, [])
        if not cs:
            continue
        n = len(cs)
        gross = sum(c.gross for c in cs)
        net = sum(c.net for c in cs)
        execd = [c for c in cs if c.net >= threshold]
        prof = sum(1 for c in cs if c.net > 0)
        print(
            f"{band:<10}{n:>5}{gross:>12,.0f}{net:>12,.0f}"
            f"{len(execd):>16}{prof:>12}"
        )
        tot_n += n
        tot_gross += gross
        tot_net += net
        tot_exec += len(execd)
        tot_prof += prof
        exec_net += sum(c.net for c in execd)
    print("-" * 67)
    print(
        f"{'TOTAL':<10}{tot_n:>5}{tot_gross:>12,.0f}{tot_net:>12,.0f}"
        f"{tot_exec:>16}{tot_prof:>12}"
    )
    print(
        f"\n{tot_exec} of {tot_n} would have been executable at >=${threshold:g} net; "
        f"their combined net = ${exec_net:,.0f} left on the table."
    )


def sensitivity(rows: list[Row], fallback_lltv: float, threshold: float) -> None:
    print(f"\nsensitivity — executable count at >=${threshold:g} net (per-market LLTV):")
    gas_grid = (0.10, 0.25, 0.50, 1.00)
    bps_grid = (20, 50, 100, 200)
    header = "exit_bps \\ gas$"
    print(f"{header:>16}" + "".join(f"{g:>10.2f}" for g in gas_grid))
    for bps in bps_grid:
        cells = []
        for gas in gas_grid:
            n = sum(
                1
                for r in rows
                if cost(r, lif_for(r, fallback_lltv), bps, gas, 0.0).net >= threshold
            )
            cells.append(f"{n:>10}")
        print(f"{bps:>16}" + "".join(cells))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/shadow.sqlite")
    ap.add_argument("--venue", default="morpho_base")
    ap.add_argument("--loan-decimals", type=int, default=6, help="USDC = 6")
    ap.add_argument(
        "--lltv",
        type=float,
        default=0.86,
        help="Fallback LLTV for any collateral not in COLLATERAL_LLTV (resolved markets are "
        "costed at their own on-chain LLTV automatically).",
    )
    ap.add_argument("--gas-usd", type=float, default=0.25, help="all-in Base gas incl L1 data fee")
    ap.add_argument("--exit-bps", type=float, default=50.0, help="DEX fee + slippage on the sale")
    ap.add_argument("--flash-bps", type=float, default=0.0, help="0 for Morpho/Balancer free flash")
    ap.add_argument("--threshold", type=float, default=10.0, help="net $ to count as executable")
    args = ap.parse_args()

    print(f"venue={args.venue}  per-market LLTV (fallback {args.lltv:g}):")
    for addr, lltv_wad in COLLATERAL_LLTV.items():
        lif = liquidation_incentive_factor_wad(lltv_wad) / WAD
        print(f"  {addr}  lltv={lltv_wad / WAD:.3f} -> LIF {lif:.4f} (+{(lif - 1) * 100:.2f}%)")
    print(
        f"costs: gas=${args.gas_usd:g}/liq, exit={args.exit_bps:g}bps, "
        f"flash={args.flash_bps:g}bps, threshold=${args.threshold:g}"
    )

    missed = [
        cost(r, lif_for(r, args.lltv), args.exit_bps, args.gas_usd, args.flash_bps)
        for r in load(args.db, args.venue, "missed", args.loan_decimals)
    ]
    if not missed:
        print("\nNo missed rows found. Check --venue / --db.")
        return

    print("\n=== MISSED liquidations (opportunities the detection gap cost us) ===")
    report(missed, args.threshold)
    sensitivity([c.row for c in missed], args.lltv, args.threshold)

    for other in ("taken", "unclaimed"):
        rows = [
            cost(r, lif_for(r, args.lltv), args.exit_bps, args.gas_usd, args.flash_bps)
            for r in load(args.db, args.venue, other, args.loan_decimals)
        ]
        if rows:
            print(f"\n=== {other.upper()} (for context) ===")
            report(rows, args.threshold)


if __name__ == "__main__":
    main()
