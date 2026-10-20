"""Quick on-chain verification of Aerodrome (Base) addresses before building the arb scanner."""
from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from liqengine.config.loader import load_registry
from liqengine.core.abi import encode_call, word_to_address, word_to_int, words
from liqengine.ingest.pool import CallPolicy, ProviderPool

AERO_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
UNI_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"


async def call(pool, to, data):
    try:
        return await pool.call("eth_call", [{"to": to, "data": data}, "latest"], policy=CallPolicy.FAST)
    except Exception as e:
        return f"ERR {str(e)[:80]}"


async def main():
    load_dotenv(".env")
    venue = load_registry("config").venue("morpho_base")
    async with ProviderPool(venue.chain) as pool:
        for name, addr in [("Aero factory", AERO_FACTORY), ("Uni quoter", UNI_QUOTER)]:
            code = await pool.call("eth_getCode", [addr, "latest"])
            print(f"{name} {addr}: code={'yes' if isinstance(code,str) and len(code)>2 else 'NO'}")

        # getPool(WETH, USDC, stable=false) and (…, true)
        for stable in (0, 1):
            data = encode_call("getPool(address,address,bool)", WETH, USDC, stable)
            raw = await call(pool, AERO_FACTORY, data)
            if isinstance(raw, str) and raw.startswith("0x") and len(raw) >= 66:
                poolad = word_to_address(words(raw)[0])
                print(f"getPool(WETH,USDC,stable={stable}) -> {poolad}")
                if int(poolad, 16) != 0:
                    # getAmountOut(1 WETH, WETH) -> USDC out
                    amt = 10**18
                    d2 = encode_call("getAmountOut(uint256,address)", amt, WETH)
                    r2 = await call(pool, poolad, d2)
                    if isinstance(r2, str) and r2.startswith("0x"):
                        out = word_to_int(words(r2)[0])
                        print(f"   getAmountOut(1 WETH) -> {out/1e6:.2f} USDC")
                    else:
                        print(f"   getAmountOut err: {r2}")
            else:
                print(f"getPool(WETH,USDC,stable={stable}) -> {raw}")


if __name__ == "__main__":
    asyncio.run(main())
