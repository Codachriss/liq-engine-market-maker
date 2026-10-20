"""Submit-latency probe — the last unknown for the reactive Flashblocks back-run path.

When a Chainlink transmit lands in an early/mid flashblock, ~several flashblocks (~161ms each)
remain in the block. To win reactively we must get our liquidation to Base's sequencer and have it
included in a *later* flashblock of the same block. This measures the network+ingress round-trip to
the sequencer send endpoint — the dominant, measurable part of that submit latency — WITHOUT signing
or sending any real transaction (an empty "0x" payload round-trips the ingress and is rejected at
RLP decode).

What this does NOT measure: sequencer-internal processing + the wait for the next flashblock build
(adds up to ~161ms), and it is entirely dependent on THIS machine's network location — colocated
infra near the sequencer will be far faster. Treat the result as a latency floor for this host.

Run:  python -I scripts/submit_latency_probe.py [samples]
"""
from __future__ import annotations

import os
import sys
import time
from statistics import median
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

SEQUENCER = "https://mainnet-sequencer.base.org"
FLASHBLOCK_MS = 161  # measured median inter-flashblock gap (flashblocks_probe.py)


def _pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


def sample(client: httpx.Client, url: str, payload: dict, n: int, warmup: int) -> list[float]:
    out: list[float] = []
    for i in range(n + warmup):
        try:
            t0 = time.perf_counter()
            client.post(url, json=payload)
            dt = (time.perf_counter() - t0) * 1000
        except Exception:
            continue
        if i >= warmup:
            out.append(dt)
    return out


def report(name: str, xs: list[float]) -> None:
    if not xs:
        print(f"{name}: no samples")
        return
    print(
        f"{name}: n={len(xs)}  p50={median(xs):.0f}ms  p95={_pct(xs,0.95):.0f}ms  "
        f"min={min(xs):.0f}ms  max={max(xs):.0f}ms"
    )


def main(n: int) -> None:
    load_dotenv(".env")
    rpc = os.getenv("BASE_RPC_PRIMARY")

    with httpx.Client(
        timeout=10, limits=httpx.Limits(max_keepalive_connections=4, max_connections=8)
    ) as client:
        print(f"sequencer send-path RTT -> {urlparse(SEQUENCER).netloc} ({n} samples, keep-alive)")
        seq = sample(
            client, SEQUENCER,
            {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": ["0x"]},
            n, warmup=3,
        )
        report("  sequencer send ingress", seq)

        if rpc:
            print(f"\nbaseline read RTT -> {urlparse(rpc).netloc} (Alchemy, for comparison)")
            base = sample(
                client, rpc,
                {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                n, warmup=3,
            )
            report("  alchemy eth_blockNumber", base)

    print("\n" + "=" * 72)
    print("INTERPRETATION")
    print("=" * 72)
    if seq:
        p50, p95 = median(seq), _pct(seq, 0.95)
        budget = FLASHBLOCK_MS
        print(f"per-flashblock budget: ~{budget}ms; a full block = ~11 flashblocks (~1.8s).")
        print(f"this host's send-ingress RTT: p50={p50:.0f}ms p95={p95:.0f}ms.")
        fb_p50 = p50 / budget
        print(
            f"=> submitting costs ~{fb_p50:.1f} flashblock(s) of the window at p50 "
            f"(network ingress alone, before sequencer processing + next-flashblock wait)."
        )
        if p95 < budget:
            print("   Ingress fits inside one flashblock — reactive same-block landing is")
            print("   PLAUSIBLE from here IF the transmit lands with ≥2-3 flashblocks remaining.")
            print("   Confirm with a real signed landing test before trusting it.")
        else:
            print("   Ingress RTT already eats ≥1 flashblock and is jittery — from this host the")
            print("   reactive window is tight. Colocated infra near the sequencer would be needed")
            print("   to compete; this home/office latency likely loses most same-block races.")
        print("\nNOTE: this is a per-host network floor. The incumbents almost certainly colocate")
        print("near the sequencer (single-digit-ms RTT). Your real bar is their latency, not the")
        print("flashblock budget alone.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
