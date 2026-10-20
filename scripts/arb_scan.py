"""Live cross-DEX arb scanner for Base — Uniswap V3 vs Aerodrome, round-trip, net of gas.

For each token it quotes the REAL executable round trip USDC -> token -> USDC with the buy leg on
one DEX and the sell leg on the other (both directions), at several sizes, through the actual pool
contracts (so price impact + fees are already baked in). It then subtracts a live gas estimate and
reports only what is net-positive. Because it is a round trip starting and ending in USDC, profit is
read directly in USDC — no price oracle, no token-decimal bookkeeping — and a token that cannot be
quoted on either leg is simply skipped, never a false positive.

Read-only. NEVER signs. This is the quoting core the live tool will reuse.

Run:  python -I scripts/arb_scan.py [loops] [sleep_s]
"""
from __future__ import annotations

import asyncio
import sys
import time

from dotenv import load_dotenv

from liqengine.config.loader import load_registry
from liqengine.core.abi import encode_call, word_to_address, word_to_int, words
from liqengine.core.errors import LiqEngineError
from liqengine.ingest.pool import CallPolicy, ProviderPool

UNI_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
UNI_TIERS = [100, 500, 3000, 10000]
AERO_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
GAS_UNITS = 400_000  # atomic flash-loan + 2 swaps, conservative
L1_DATA_BUFFER_USD = 0.05

# Liquid-ish Base tokens vs USDC. Unquotable ones skip safely (round-trip returns nothing).
TOKENS = {
    "WETH": WETH,
    "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    "DEGEN": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    "VIRTUAL": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
    "BRETT": "0x532f27101965dd16442E59d40670FaF5eBB142E4",
    "DAI": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
    "cbETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
}
SIZES_USDC = [100, 500, 1000, 5000]


async def _call(pool: ProviderPool, to: str, data: str) -> int | None:
    try:
        raw = await pool.call(
            "eth_call", [{"to": to, "data": data}, "latest"], policy=CallPolicy.FAST
        )
    except LiqEngineError:
        return None
    if not isinstance(raw, str) or len(raw) < 66:
        return None
    try:
        return word_to_int(words(raw)[0])
    except ValueError:
        return None


async def uni_out(pool: ProviderPool, tin: str, tout: str, amount_in: int) -> int | None:
    """Best Uniswap V3 amountOut across fee tiers (None if no pool answers)."""
    best: int | None = None
    for tier in UNI_TIERS:
        data = encode_call(
            "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
            tin, tout, amount_in, tier, 0,
        )
        out = await _call(pool, UNI_QUOTER, data)
        if out and (best is None or out > best):
            best = out
    return best


async def _aero_pool(pool: ProviderPool, a: str, b: str, stable: int) -> str | None:
    data = encode_call("getPool(address,address,bool)", a, b, stable)
    try:
        raw = await pool.call("eth_call", [{"to": AERO_FACTORY, "data": data}, "latest"],
                              policy=CallPolicy.FAST)
    except LiqEngineError:
        return None
    if not isinstance(raw, str) or len(raw) < 66:
        return None
    addr = word_to_address(words(raw)[0])
    return addr if int(addr, 16) != 0 else None


async def aero_out(pool: ProviderPool, tin: str, tout: str, amount_in: int) -> int | None:
    """Best Aerodrome amountOut across the volatile and stable pools for the pair."""
    best: int | None = None
    for stable in (0, 1):
        p = await _aero_pool(pool, tin, tout, stable)
        if not p:
            continue
        data = encode_call("getAmountOut(uint256,address)", amount_in, tin)
        out = await _call(pool, p, data)
        if out and (best is None or out > best):
            best = out
    return best


async def gas_usd(pool: ProviderPool) -> float:
    try:
        gp = word_to_int(words("0x" + "0" * 63 + "0")) if False else None
    except Exception:
        gp = None
    try:
        raw = await pool.call("eth_gasPrice", [], policy=CallPolicy.FAST)
        gas_price = int(raw, 16)
    except Exception:
        gas_price = 50_000_000  # 0.05 gwei fallback
    eth_usdc = await aero_out(pool, WETH, USDC, 10**18) or 2400_000000
    eth_price = eth_usdc / 1e6
    eth_cost = GAS_UNITS * gas_price / 1e18
    return eth_cost * eth_price + L1_DATA_BUFFER_USD


async def scan_once(pool: ProviderPool) -> list[dict]:
    g = await gas_usd(pool)
    print(f"gas estimate per arb: ${g:.3f}")
    rows: list[dict] = []
    for sym, token in TOKENS.items():
        for size in SIZES_USDC:
            usdc_in = size * 1_000_000
            # buy token with USDC on each DEX
            t_uni = await uni_out(pool, USDC, token, usdc_in)
            t_aero = await aero_out(pool, USDC, token, usdc_in)
            best_net = None
            best_dir = None
            # Dir A: buy uni, sell aero
            if t_uni:
                back = await aero_out(pool, token, USDC, t_uni)
                if back:
                    net = back / 1e6 - size - g
                    if best_net is None or net > best_net:
                        best_net, best_dir = net, "buy Uni / sell Aero"
            # Dir B: buy aero, sell uni
            if t_aero:
                back = await uni_out(pool, token, USDC, t_aero)
                if back:
                    net = back / 1e6 - size - g
                    if best_net is None or net > best_net:
                        best_net, best_dir = net, "buy Aero / sell Uni"
            if best_net is not None:
                gross_bps = (best_net + g) / size * 10_000
                rows.append({"token": sym, "size": size, "dir": best_dir,
                             "net_usd": best_net, "gross_bps": gross_bps})
    return rows


def report(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: -r["net_usd"])
    print(f"\n{'token':8}{'size$':>7}{'gross_bps':>11}{'net_$':>10}  direction")
    for r in rows:
        flag = "  <== POSITIVE" if r["net_usd"] > 0 else ""
        print(f"{r['token']:8}{r['size']:>7}{r['gross_bps']:>11.1f}"
              f"{r['net_usd']:>10.2f}  {r['dir']}{flag}")
    pos = [r for r in rows if r["net_usd"] > 0]
    print(f"\nnet-positive opportunities this pass: {len(pos)} / {len(rows)}")


async def run(loops: int, sleep_s: float) -> None:
    load_dotenv(".env")
    venue = load_registry("config").venue("morpho_base")
    async with ProviderPool(venue.chain) as pool:
        best_seen: dict[str, float] = {}
        for i in range(loops):
            print(f"\n===== pass {i+1}/{loops} @ {time.strftime('%H:%M:%S')} =====")
            rows = await scan_once(pool)
            report(rows)
            for r in rows:
                k = f"{r['token']}@{r['size']}"
                if k not in best_seen or r["net_usd"] > best_seen[k]:
                    best_seen[k] = r["net_usd"]
            if i < loops - 1:
                await asyncio.sleep(sleep_s)
        if loops > 1:
            print("\n===== best net seen per token/size across all passes =====")
            for k, v in sorted(best_seen.items(), key=lambda kv: -kv[1])[:15]:
                print(f"  {k:16} best_net=${v:.2f}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    s = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    asyncio.run(run(n, s))
