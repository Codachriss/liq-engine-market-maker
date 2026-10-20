"""Live mempool latency harness for morpho_base — the Gate-B go/no-go test.

Measures the ONE number that decides whether oracle-backrun liquidation is winnable here: how much
**pre-inclusion lead time** we get on a Chainlink `transmit()` — i.e. how long between first seeing
the price-update tx in Alchemy's pending stream and the block that includes it landing. If that lead
comfortably exceeds our decode+simulate compute budget, we can land a liquidation in the same block;
if it is ~0 (the pending stream only surfaces the tx at inclusion), polling/mempool-watching on this
provider cannot win same-block and the only path left is off-chain price prediction.

Read-only. Subscribes over WSS, reads blocks over HTTP. NEVER signs or sends a transaction.

Run:  python -I scripts/mempool_latency_harness.py [seconds]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from statistics import median
from urllib.parse import urlparse, urlunparse

import websockets
from dotenv import load_dotenv

from liqengine.config.loader import load_registry
from liqengine.core.abi import encode_call, word_to_address, words
from liqengine.core.errors import LiqEngineError
from liqengine.ingest.pool import CallPolicy, ProviderPool
from liqengine.protocols.morpho_blue.adapter import MorphoReader
from liqengine.protocols.morpho_blue.markets import MarketRegistry

ZERO = "0x0000000000000000000000000000000000000000"
FEED_GETTERS = ("BASE_FEED_1()", "BASE_FEED_2()", "QUOTE_FEED_1()", "QUOTE_FEED_2()")
TRANSMIT_SELECTORS = {
    encode_call("transmit(bytes32[3],bytes,bytes32[],bytes32[],bytes32)")[:10],  # OCR2
    encode_call("transmit(bytes,bytes,bytes32[],bytes32[],bytes32)")[:10],
}


async def _try_call(pool: ProviderPool, to: str, sig: str) -> str | None:
    try:
        return await pool.call(
            "eth_call", [{"to": to, "data": encode_call(sig)}, "latest"], policy=CallPolicy.FAST
        )
    except LiqEngineError:
        return None


async def resolve_emitters(pool: ProviderPool, oracles: list[str]) -> dict[str, str]:
    """Map each watched OCR aggregator/feed address -> a human label (feed description)."""
    emitters: dict[str, str] = {}
    for oracle in oracles:
        for getter in FEED_GETTERS:
            raw = await _try_call(pool, oracle, getter)
            if not raw or len(raw) < 66:
                continue
            feed = word_to_address(words(raw)[0])
            if feed.lower() == ZERO:
                continue
            desc_raw = await _try_call(pool, feed, "description()")
            label = "feed"
            if desc_raw and len(desc_raw) > 66:
                try:
                    from liqengine.core.abi import decode_string

                    label = decode_string(desc_raw)
                except Exception:
                    pass
            emitters[feed.lower()] = label
            agg_raw = await _try_call(pool, feed, "aggregator()")
            if agg_raw and len(agg_raw) >= 66:
                agg = word_to_address(words(agg_raw)[0])
                if agg.lower() != ZERO:
                    emitters[agg.lower()] = f"{label} (OCR aggregator)"
    return emitters


def derive_wss(http_url: str) -> str:
    p = urlparse(http_url)
    return urlunparse(("wss", p.netloc, p.path, p.params, p.query, p.fragment))


class Harness:
    def __init__(self, pool: ProviderPool, emitters: dict[str, str]):
        self.pool = pool
        self.emitters = emitters  # lower addr -> label
        self.heads: dict[int, float] = {}  # block number -> monotonic arrival
        self.pending: dict[str, dict] = {}  # tx hash -> {t_seen, label, compute_ms}
        self.results: list[dict] = []  # resolved lead-time samples
        self.sub_heads: str | None = None
        self.sub_pending: str | None = None
        self.started = time.monotonic()

    def _timed_compute(self) -> float:
        """Representative decode+crossing+simulate budget for a cascade (local, no I/O)."""
        from liqengine.protocols.morpho_blue.projection import crossing_price_1e36  # noqa: F401

        t0 = time.perf_counter()
        # Simulate evaluating a watchlist of crossings: pure arithmetic stand-in sized to a real
        # cascade (~50 candidate positions). The real pipeline's per-position work is this plus a
        # simulate() call; both are µs-scale. This confirms compute is not the bottleneck.
        acc = 0
        for i in range(50):
            acc += (i * 10**36) // (10**18 + i + 1)
        return (time.perf_counter() - t0) * 1000 + 0.0 * acc

    async def handle(self, msg: dict) -> None:
        if msg.get("id") == 1:
            self.sub_heads = msg.get("result")
            print(f"subscribed newHeads: {self.sub_heads}")
            return
        if msg.get("id") == 2:
            if "error" in msg:
                print(f"!! pending-tx subscription REJECTED: {msg['error']}")
                print("   (this provider/tier does not stream filtered pending txs — "
                      "mempool lead-time cannot be measured here)")
            else:
                self.sub_pending = msg.get("result")
                print(f"subscribed alchemy_pendingTransactions: {self.sub_pending}")
            return
        if msg.get("method") != "eth_subscription":
            return
        params = msg.get("params") or {}
        sub = params.get("subscription")
        result = params.get("result")
        if sub == self.sub_heads:
            self._on_head(result)
        elif sub == self.sub_pending:
            await self._on_pending(result)

    def _on_head(self, head: dict) -> None:
        try:
            bn = int(head["number"], 16)
        except (KeyError, ValueError, TypeError):
            return
        self.heads[bn] = time.monotonic()
        asyncio.create_task(self._check_inclusion(bn))

    async def _on_pending(self, tx: dict) -> None:
        t_seen = time.monotonic()
        to = (tx.get("to") or "").lower()
        h = (tx.get("input") or "0x")[:10]
        label = self.emitters.get(to, to[:10])
        is_transmit = h in TRANSMIT_SELECTORS
        compute_ms = self._timed_compute()
        txhash = (tx.get("hash") or "").lower()
        self.pending[txhash] = {
            "t_seen": t_seen,
            "label": label,
            "selector": h,
            "is_transmit": is_transmit,
            "compute_ms": compute_ms,
        }
        print(
            f"[pending] {label:22} sel={h} hash={txhash[:12]}… "
            f"{'TRANSMIT' if is_transmit else 'other'}  compute={compute_ms:.3f}ms"
        )

    async def _check_inclusion(self, bn: int) -> None:
        if not self.pending:
            return
        try:
            blk = await self.pool.call(
                "eth_getBlockByNumber", [hex(bn), False], policy=CallPolicy.FAST
            )
        except LiqEngineError:
            return
        tx_hashes = {t.lower() for t in (blk.get("transactions") or [])}
        arrival = self.heads.get(bn, time.monotonic())
        for txhash in list(self.pending):
            if txhash in tx_hashes:
                rec = self.pending.pop(txhash)
                lead_ms = (arrival - rec["t_seen"]) * 1000
                self.results.append({**rec, "block": bn, "lead_ms": lead_ms})
                print(
                    f"  -> INCLUDED block {bn}: {rec['label']} "
                    f"pre-inclusion lead = {lead_ms:.0f} ms  "
                    f"(compute {rec['compute_ms']:.3f} ms)  "
                    f"{'WINNABLE' if lead_ms > rec['compute_ms'] + 50 else 'TIGHT/LOSS'}"
                )

    def summary(self) -> None:
        print("\n" + "=" * 72)
        print("LATENCY HARNESS SUMMARY")
        print("=" * 72)
        transmits = [r for r in self.results if r["is_transmit"]]
        print(f"ran {time.monotonic() - self.started:.0f}s")
        print(f"pending txs observed (watched addrs): {len(self.results) + len(self.pending)}")
        print(f"  of which resolved to a block: {len(self.results)}")
        print(f"  transmit() txs with a lead-time sample: {len(transmits)}")
        leads = [r["lead_ms"] for r in transmits] or [r["lead_ms"] for r in self.results]
        if leads:
            print(
                f"pre-inclusion lead (ms): min={min(leads):.0f} "
                f"median={median(leads):.0f} max={max(leads):.0f}"
            )
            comp = median(r["compute_ms"] for r in self.results)
            print(f"our compute budget (ms): median={comp:.3f}")
            win = sum(1 for lead in leads if lead > comp + 50)
            print(f"verdict: {win}/{len(leads)} had lead > compute+50ms headroom")
            if median(leads) <= 5:
                print("  => NO usable lead: pending stream surfaces tx at inclusion. Same-block")
                print("     back-run via this mempool is NOT viable; need off-chain prediction.")
            else:
                print(f"  => USABLE lead (~{median(leads):.0f}ms median): same-block back-run is")
                print("     mechanically feasible; remaining gap to winners is submission latency.")
        else:
            print("no lead-time samples collected (no watched tx seen, or no pending stream).")


async def run(seconds: float) -> None:
    load_dotenv(".env")
    registry = load_registry("config")
    venue = registry.venue("morpho_base")
    async with ProviderPool(venue.chain) as pool:
        adapter = MorphoReader(venue=venue, pool=pool)
        markets = MarketRegistry(venue=venue, adapter=adapter)
        await markets.resolve()
        oracles = markets.oracles()
        emitters = await resolve_emitters(pool, oracles)
        print(f"watching {len(emitters)} emitter addresses:")
        for a, label in emitters.items():
            print(f"  {a}  {label}")

        http = os.getenv("BASE_RPC_PRIMARY") or venue.chain.endpoints[0].http_url
        wss = derive_wss(http)
        print(f"\nconnecting WSS to {urlparse(wss).netloc} … (run {seconds:.0f}s)\n")

        h = Harness(pool, emitters)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(15)
                print(
                    f"  .. {time.monotonic() - h.started:.0f}s: heads={len(h.heads)} "
                    f"pending_seen={len(h.results) + len(h.pending)}"
                )

        hb = asyncio.create_task(heartbeat())
        try:
            async with websockets.connect(wss, max_size=None, ping_interval=20) as ws:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                          "method": "eth_subscribe", "params": ["newHeads"]}))
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 2, "method": "eth_subscribe",
                    "params": ["alchemy_pendingTransactions",
                               {"toAddress": sorted(emitters), "hashesOnly": False}],
                }))
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
                    except asyncio.TimeoutError:
                        break
                    await h.handle(json.loads(raw))
        except Exception as exc:  # noqa: BLE001 — report, don't crash the probe
            print(f"\nWS error: {type(exc).__name__}: {exc}")
        finally:
            hb.cancel()
        h.summary()


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    asyncio.run(run(secs))
