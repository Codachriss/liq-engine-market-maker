#!/usr/bin/env python
"""One-shot: resolve the allowlisted Morpho markets and print collateral -> LLTV.

Used to feed real per-market LLTVs into scripts/replay_missed.py so the profit estimate stops
depending on a single assumed LLTV. Needs a working BASE RPC (same .env as `liq shadow run`).

Run with the project venv:  .venv/bin/python scripts/resolve_lltv.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from liqengine.cli import _bootstrap, _select
from liqengine.ingest.pool import ProviderPool
from liqengine.protocols.morpho_blue.adapter import MorphoReader
from liqengine.protocols.morpho_blue.markets import MarketRegistry


async def main() -> None:
    registry = _bootstrap(Path("config"))
    resolved = _select(registry, "morpho_base")[0]
    async with ProviderPool(resolved.chain) as pool:
        adapter = MorphoReader(venue=resolved, pool=pool)
        markets = MarketRegistry(venue=resolved, adapter=adapter)
        await markets.resolve()
        print("    # collateral_token -> lltv_wad  (resolved from chain)")
        print("    COLLATERAL_LLTV = {")
        for params in markets.params().values():
            sym = markets.symbol(params.collateral_token)
            print(
                f'        "{params.collateral_token}": {params.lltv_wad},'
                f"  # {sym} lltv={params.lltv_wad / 10**18:.3f}"
            )
        print("    }")


if __name__ == "__main__":
    asyncio.run(main())
