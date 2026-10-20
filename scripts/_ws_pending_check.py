"""Validate whether Alchemy's Base alchemy_pendingTransactions actually delivers pre-inclusion txs.

Subscribes filtered to two very high-traffic Base contracts (USDC, WETH). If we see pending txs for
them, the stream works and the oracle aggregators were simply quiet. If we see ZERO even for these,
the provider surfaces no usable mempool on Base -> same-block back-run via this RPC is not possible.

Run:  python -I scripts/_ws_pending_check.py [seconds]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from urllib.parse import urlparse, urlunparse

import websockets
from dotenv import load_dotenv

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"


async def run(seconds: float) -> None:
    load_dotenv(".env")
    http = os.getenv("BASE_RPC_PRIMARY")
    p = urlparse(http)
    wss = urlunparse(("wss", p.netloc, p.path, p.params, p.query, p.fragment))
    print(f"WSS -> {p.netloc}  (run {seconds:.0f}s)")

    counts = {"filtered_pending": 0, "all_pending": 0, "heads": 0}
    unfiltered = len(sys.argv) > 2 and sys.argv[2] == "all"
    async with websockets.connect(wss, max_size=None, ping_interval=20) as ws:
        params = ["alchemy_pendingTransactions", {"hashesOnly": True}] if unfiltered else [
            "alchemy_pendingTransactions", {"toAddress": [USDC, WETH], "hashesOnly": True}]
        print(f"mode: {'UNFILTERED (all pending)' if unfiltered else 'filtered USDC/WETH'}")
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
            "params": params}))
        # id=2: newHeads as a liveness control
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2,
            "method": "eth_subscribe", "params": ["newHeads"]}))
        sub_filtered = sub_heads = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            m = json.loads(raw)
            if m.get("id") == 1:
                sub_filtered = m.get("result")
                print(f"filtered pending sub: {sub_filtered}  err={m.get('error')}")
            elif m.get("id") == 2:
                sub_heads = m.get("result")
                print(f"heads sub: {sub_heads}")
            elif m.get("method") == "eth_subscription":
                sub = (m.get("params") or {}).get("subscription")
                if sub == sub_filtered:
                    counts["filtered_pending"] += 1
                    if counts["filtered_pending"] <= 3:
                        print(f"  [pending USDC/WETH] {(m['params']['result'])}")
                elif sub == sub_heads:
                    counts["heads"] += 1
    print(f"\nRESULT after {seconds:.0f}s:")
    print(f"  heads received:            {counts['heads']}")
    print(f"  USDC/WETH pending received: {counts['filtered_pending']}")
    if counts["filtered_pending"] == 0 and counts["heads"] > 0:
        print("  => stream alive (heads) but ZERO pending for top contracts:")
        print("     Alchemy Base exposes NO usable pre-inclusion mempool. Same-block back-run")
        print("     via this RPC is not possible; winning requires off-chain price prediction")
        print("     and/or a direct sequencer/self-hosted-node feed.")
    elif counts["filtered_pending"] > 0:
        print("  => pending stream WORKS. The oracle aggregators were simply quiet in the run;")
        print("     re-run the latency harness longer or during a volatile window.")


if __name__ == "__main__":
    asyncio.run(run(float(sys.argv[1]) if len(sys.argv) > 1 else 40.0))
