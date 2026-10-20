"""Base Flashblocks probe — the OP-stack path to same-block oracle back-run.

A Base block (~2s) is built as a sequence of ~200ms "flashblocks". This streams them, decodes each
tx's `to` (minimal RLP), and measures: (1) flashblocks per block and inter-flashblock ms, (2) when a
Chainlink `transmit()` to a watched aggregator lands, how much of the block remains after it (the
reactive back-run window), and (3) any Morpho liquidations and their flashblock position vs the
transmit. If a transmit typically lands with multiple flashblocks (hundreds of ms) still to come,
a reactive same-block liquidation via Flashblocks is mechanically possible — the remaining unknown is
our submit-to-sequencer latency.

Read-only. Streams the public Flashblocks WS. NEVER signs.

Run:  python -I scripts/flashblocks_probe.py [seconds]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import brotli
import websockets

FLASHBLOCKS_WS = "wss://mainnet.flashblocks.base.org/ws"

# Watched Chainlink OCR aggregators + feed proxies on Base (resolved by oracle_latency_probe.py).
# transmit() is sent to the OCR aggregator address.
AGGREGATORS = {
    "0xfa9ebd1fc381c5babb495bb53664ae9b6665e4e4": "XRP/USD",
    "0xd175aa4ed3aeccd03266c2bbbb16b0b419d005aa": "LTC/USD",
    "0x71e021bc2e8a709b72ac7b6036e5b2bf30f263d0": "cbETH/USD",
    "0x57b823541ab83ecdcd0b25a23d8fd572764d4ad9": "SOL/USD",
    "0x05c84a58fe042275b37db038baacd15f410c7bb0": "ETH/USD",
    "0x68be4c50235205ede361ac8244b1ee221cdda5e2": "USDC/USD",
    # feed proxies too, in case a transmit path targets them
    "0x9f0c1dd78c4cbdf5b9cf923a549a201edc676d34": "XRP/USD(proxy)",
    "0x71041dddad3595f9ced3dccfbe3d1f4b0a16bb70": "ETH/USD(proxy)",
}
MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"  # singleton; liquidations route via bots → it
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # liveness sentinel: constant Base traffic


def _read_item(b: bytes) -> tuple[bytes, int]:
    b0 = b[0]
    if b0 < 0x80:
        return b[0:1], 1
    if b0 <= 0xb7:
        n = b0 - 0x80
        return b[1 : 1 + n], 1 + n
    if b0 <= 0xbf:
        ln = b0 - 0xb7
        n = int.from_bytes(b[1 : 1 + ln], "big")
        return b[1 + ln : 1 + ln + n], 1 + ln + n
    if b0 <= 0xf7:
        n = b0 - 0xc0
        return b[0 : 1 + n], 1 + n  # nested list: whole slice
    ln = b0 - 0xf7
    n = int.from_bytes(b[1 : 1 + ln], "big")
    return b[0 : 1 + ln + n], 1 + ln + n


def _rlp_list(b: bytes) -> list[bytes]:
    if not b or b[0] < 0xc0:
        return []
    if b[0] <= 0xf7:
        payload = b[1 : 1 + (b[0] - 0xc0)]
    else:
        ll = b[0] - 0xf7
        n = int.from_bytes(b[1 : 1 + ll], "big")
        payload = b[1 + ll : 1 + ll + n]
    items, i = [], 0
    while i < len(payload):
        it, used = _read_item(payload[i:])
        items.append(it)
        i += used
    return items


def tx_to(raw: str) -> str | None:
    """Extract the `to` address from a raw (possibly typed) tx hex. None for creation/undecodable."""
    try:
        b = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        if b[0] in (0x01, 0x02, 0x03):  # EIP-2930 / 1559 / 4844
            idx = {0x01: 4, 0x02: 5, 0x03: 5}[b[0]]
            items = _rlp_list(b[1:])
        elif b[0] == 0x7E:  # OP deposit system tx
            return None
        else:  # legacy
            idx = 3
            items = _rlp_list(b)
        to = items[idx]
        return "0x" + to.hex() if len(to) == 20 else None
    except Exception:
        return None


class FlashState:
    def __init__(self) -> None:
        self.cur_block: int | None = None
        self.cur_start: float = 0.0
        self.fb_times: list[float] = []  # flashblock arrival times in current block
        self.transmit_in_block: dict[str, tuple[int, float]] = {}  # label -> (fb_index, t_seen)
        self.liq_in_block: list[tuple[int, float]] = []
        # outputs
        self.fb_per_block: list[int] = []
        self.inter_fb_ms: list[float] = []
        self.transmit_windows: list[dict] = []  # {label, fb_index, remaining_ms, fb_after}
        self.usdc_hits = 0
        self.transmit_count = 0
        self.liq_count = 0

    def finalize_block(self, now: float) -> None:
        if self.cur_block is None:
            return
        n_fb = len(self.fb_times)
        self.fb_per_block.append(n_fb)
        for label, (fb_idx, t_seen) in self.transmit_in_block.items():
            remaining_ms = (now - t_seen) * 1000  # until this (next) block started
            fb_after = n_fb - 1 - fb_idx
            self.transmit_windows.append(
                {"block": self.cur_block, "label": label, "fb_index": fb_idx,
                 "fb_total": n_fb, "fb_after": fb_after, "remaining_ms": remaining_ms,
                 "liqs_after": sum(1 for (li, lt) in self.liq_in_block if lt > t_seen)}
            )

    def on_flashblock(self, index: int, block: int, txs: list[str]) -> None:
        now = time.monotonic()
        if block != self.cur_block:
            self.finalize_block(now)
            self.cur_block = block
            self.cur_start = now
            self.fb_times = []
            self.transmit_in_block = {}
            self.liq_in_block = []
        if self.fb_times:
            self.inter_fb_ms.append((now - self.fb_times[-1]) * 1000)
        self.fb_times.append(now)

        for raw in txs:
            to = tx_to(raw)
            if not to:
                continue
            to = to.lower()
            if to == USDC:
                self.usdc_hits += 1
            elif to in AGGREGATORS:
                label = AGGREGATORS[to]
                self.transmit_count += 1
                if label not in self.transmit_in_block:
                    self.transmit_in_block[label] = (index, now)
                print(f"  *** TRANSMIT {label} in block {block} flashblock #{index} "
                      f"(tx→{to[:10]})")
            elif to == MORPHO:
                self.liq_count += 1
                self.liq_in_block.append((index, now))
                print(f"  ·· Morpho tx in block {block} flashblock #{index}")

    def summary(self) -> None:
        from statistics import median
        print("\n" + "=" * 72)
        print("FLASHBLOCKS PROBE SUMMARY")
        print("=" * 72)
        print(f"blocks observed: {len(self.fb_per_block)}")
        if self.fb_per_block:
            print(f"flashblocks per block: min={min(self.fb_per_block)} "
                  f"median={median(self.fb_per_block):.0f} max={max(self.fb_per_block)}")
        if self.inter_fb_ms:
            print(f"inter-flashblock gap (ms): median={median(self.inter_fb_ms):.0f} "
                  f"(n={len(self.inter_fb_ms)})")
        print(f"RLP decode liveness — USDC txs seen: {self.usdc_hits} "
              f"({'OK, decoder works' if self.usdc_hits else 'NONE — decoder/stream issue!'})")
        print(f"watched transmits seen: {self.transmit_count}")
        print(f"Morpho singleton txs seen: {self.liq_count}")
        if self.transmit_windows:
            print("\nTransmit back-run windows (how much block remained after the transmit):")
            for w in self.transmit_windows:
                verdict = "WINDOW EXISTS" if w["fb_after"] >= 1 else "last flashblock — NO window"
                print(f"  block {w['block']} {w['label']}: landed fb#{w['fb_index']}/{w['fb_total']}"
                      f"  {w['fb_after']} flashblocks (~{w['remaining_ms']:.0f}ms) after it"
                      f"  liqs_after={w['liqs_after']}  => {verdict}")
            wins = sum(1 for w in self.transmit_windows if w["fb_after"] >= 1)
            print(f"\n{wins}/{len(self.transmit_windows)} transmits had ≥1 flashblock of reactive "
                  f"window remaining in the same block.")
        else:
            print("\nno watched transmit landed during the run (feeds quiet) — but the "
                  "flashblocks/block + gap numbers above bound the structural window.")


async def run(seconds: float) -> None:
    st = FlashState()
    print(f"streaming Flashblocks (run {seconds:.0f}s)…")
    try:
        async with websockets.connect(FLASHBLOCKS_WS, max_size=None, ping_interval=20) as ws:
            deadline = time.monotonic() + seconds
            last_beat = time.monotonic()
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
                except asyncio.TimeoutError:
                    break
                if isinstance(raw, str):
                    raw = raw.encode()
                try:
                    msg = json.loads(brotli.decompress(raw))
                except Exception:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                diff = msg.get("diff") or {}
                meta = msg.get("metadata") or {}
                block = diff.get("block_number") or meta.get("block_number")
                block = int(block, 16) if isinstance(block, str) else block
                index = msg.get("index")
                txs = diff.get("transactions") or []
                if block is not None and index is not None:
                    st.on_flashblock(index, block, txs)
                if time.monotonic() - last_beat > 30:
                    last_beat = time.monotonic()
                    print(f"  .. {time.monotonic()-(deadline-seconds):.0f}s "
                          f"blocks={len(st.fb_per_block)} usdc={st.usdc_hits} "
                          f"transmits={st.transmit_count}")
    except Exception as exc:  # noqa: BLE001
        print(f"WS error: {type(exc).__name__}: {exc}")
    st.summary()


if __name__ == "__main__":
    asyncio.run(run(float(sys.argv[1]) if len(sys.argv) > 1 else 300.0))
