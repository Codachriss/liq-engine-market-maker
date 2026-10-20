"""Gate-B feasibility probe for morpho_base: classify each market's oracle (push vs pull) and
measure the oracle-update -> liquidation back-run pattern on the recorded burst blocks.

Read-only. Uses the venue's own ProviderPool (archive provider for historical blocks). Nothing
here signs or writes chain state; it only reads.

Run:  python -I scripts/oracle_latency_probe.py
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from dotenv import load_dotenv

from liqengine.config.loader import load_registry
from liqengine.core.abi import (
    decode_string,
    encode_call,
    word_to_address,
    word_to_int,
    words,
)
from liqengine.core.errors import LiqEngineError
from liqengine.ingest.pool import CallPolicy, ProviderPool
from liqengine.protocols.morpho_blue.adapter import MorphoReader
from liqengine.protocols.morpho_blue.markets import MarketRegistry

from liqengine.core.abi import event_topic
from liqengine.protocols.morpho_blue.events import TOPIC_LIQUIDATE

ZERO = "0x0000000000000000000000000000000000000000"
# MorphoChainlinkOracleV2 feed getters: the up-to-four Chainlink aggregators it composes.
FEED_GETTERS = ("BASE_FEED_1()", "BASE_FEED_2()", "QUOTE_FEED_1()", "QUOTE_FEED_2()")
# Chainlink proxy emits AnswerUpdated; the underlying OCR aggregator emits NewTransmission.
TOPIC_ANSWER_UPDATED = event_topic("AnswerUpdated(int256,uint256,uint256)")
TOPIC_NEW_TRANSMISSION = event_topic(
    "NewTransmission(uint32,int192,address,int192[],bytes,bytes32[],bytes32[],bytes32)"
)
ORACLE_TOPICS = {TOPIC_ANSWER_UPDATED.lower(), TOPIC_NEW_TRANSMISSION.lower()}


async def _try_call(pool: ProviderPool, to: str, sig: str, *, block: str = "latest") -> str | None:
    try:
        return await pool.call(
            "eth_call", [{"to": to, "data": encode_call(sig)}, block], policy=CallPolicy.FAST
        )
    except LiqEngineError:
        return None


async def classify_oracle(pool: ProviderPool, oracle: str) -> dict:
    """Identify what an on-chain Morpho market oracle is built on."""
    out: dict = {"oracle": oracle, "type": "unknown", "feeds": []}

    feeds: list[str] = []
    for getter in FEED_GETTERS:
        raw = await _try_call(pool, oracle, getter)
        if raw and len(raw) >= 66:
            addr = word_to_address(words(raw)[0])
            if addr.lower() != ZERO:
                feeds.append(addr)

    if feeds:
        # A MorphoChainlinkOracleV2 composing Chainlink aggregators. Probe each feed for
        # Chainlink-ness (description + aggregator + latestRoundData all answer).
        out["type"] = "chainlink_push"
        for feed in feeds:
            desc_raw = await _try_call(pool, feed, "description()")
            agg_raw = await _try_call(pool, feed, "aggregator()")
            lrd = await _try_call(pool, feed, "latestRoundData()")
            desc = None
            if desc_raw and len(desc_raw) > 66:
                try:
                    desc = decode_string(desc_raw)
                except Exception:
                    desc = None
            agg = (
                word_to_address(words(agg_raw)[0])
                if agg_raw and len(agg_raw) >= 66
                else None
            )
            out["feeds"].append(
                {
                    "feed": feed,
                    "description": desc,
                    "aggregator": agg,  # the OCR contract that receives transmit()
                    "chainlink": lrd is not None,
                }
            )
        return out

    # No Chainlink feed getters. Probe for a Pyth-style pull oracle.
    price_id = await _try_call(pool, oracle, "priceId()")
    pyth = await _try_call(pool, oracle, "pyth()")
    if price_id is not None or pyth is not None:
        out["type"] = "pyth_pull"
        if pyth and len(pyth) >= 66:
            out["pyth_contract"] = word_to_address(words(pyth)[0])
        return out

    # Still readable price() but structure unknown -> custom adapter worth manual inspection.
    if await _try_call(pool, oracle, "price()") is not None:
        out["type"] = "custom_adapter"
    return out


async def analyze_block(
    pool: ProviderPool, block: int, singleton: str, emitters: set[str]
) -> dict:
    """Locate oracle-update and Morpho-liquidation txs in a block *by their emitted logs*.

    Liquidations do not call the Morpho singleton directly — each searcher routes through its own
    contract — so we identify a liquidation tx by a `Liquidate` log emitted *by* the singleton, and
    an oracle update by an `AnswerUpdated`/`NewTransmission` log from a watched feed/aggregator.
    """
    receipts = await pool.call("eth_getBlockReceipts", [hex(block)], policy=CallPolicy.BULK)
    singleton_l = singleton.lower()

    oracle_idx: list[int] = []
    liq_txs: list[dict] = []
    for rcpt in receipts or []:
        idx = int(rcpt.get("transactionIndex", "0x0"), 16)
        logs = rcpt.get("logs") or []
        is_oracle = any(
            (lg.get("address") or "").lower() in emitters
            and (lg.get("topics") or [""])[0].lower() in ORACLE_TOPICS
            for lg in logs
        )
        is_liq = any(
            (lg.get("address") or "").lower() == singleton_l
            and (lg.get("topics") or [""])[0].lower() == TOPIC_LIQUIDATE.lower()
            for lg in logs
        )
        if is_oracle:
            oracle_idx.append(idx)
        if is_liq:
            egp = rcpt.get("effectiveGasPrice")
            liq_txs.append(
                {
                    "idx": idx,
                    "from": (rcpt.get("from") or "")[:10],
                    "effective_gwei": (int(egp, 16) / 1e9) if egp else None,
                }
            )

    first_oracle = min(oracle_idx) if oracle_idx else None
    all_liq_idx = sorted(t["idx"] for t in liq_txs)
    # The back-run signal: liquidations landing in the same block *after* the oracle update.
    # (Liquidations before it are clearing crossings from an earlier push, not this one.)
    backrun_gap = None
    n_backrun = 0
    if first_oracle is not None:
        after = [i for i in all_liq_idx if i >= first_oracle]
        n_backrun = len(after)
        if after:
            backrun_gap = min(after) - first_oracle
    return {
        "block": block,
        "n_txs": len(receipts or []),
        "oracle_update_in_block": first_oracle is not None,
        "oracle_tx_indices": oracle_idx,
        "n_liqs": len(liq_txs),
        "liq_tx_indices": all_liq_idx,
        "first_oracle_idx": first_oracle,
        "backrun_gap_txs": backrun_gap,
        "n_liqs_after_push": n_backrun,
        "liq_txs": liq_txs,
    }


async def oracle_push_window(
    pool: ProviderPool, feeds: set[str], lo: int, hi: int
) -> dict[int, int]:
    """Count oracle pushes per block across [lo, hi] via AnswerUpdated/NewTransmission logs."""
    logs = await pool.call(
        "eth_getLogs",
        [
            {
                "address": sorted(feeds),
                "topics": [sorted(ORACLE_TOPICS)],
                "fromBlock": hex(lo),
                "toBlock": hex(hi),
            }
        ],
        policy=CallPolicy.BULK,
    )
    per_block: dict[int, int] = {}
    for lg in logs or []:
        bn = int(lg.get("blockNumber", "0x0"), 16)
        per_block[bn] = per_block.get(bn, 0) + 1
    return per_block


async def main() -> None:
    load_dotenv()
    registry = load_registry("config")
    venue = registry.venue("morpho_base")

    async with ProviderPool(venue.chain) as pool:
        adapter = MorphoReader(venue=venue, pool=pool)
        markets = MarketRegistry(venue=venue, adapter=adapter)
        await markets.resolve()
        params = markets.params()

        print("=" * 78)
        print("ORACLE CLASSIFICATION (per watched market)")
        print("=" * 78)
        emitters: set[str] = set()  # feed proxies + OCR aggregators that emit price-update logs
        label_by_id = {m.id: m.label for m in venue.protocol.markets}
        for mid, p in params.items():
            info = await classify_oracle(pool, p.oracle)
            label = label_by_id.get(mid, mid[:10])
            print(f"\n[{label}]  oracle={p.oracle}  => {info['type']}")
            for f in info.get("feeds", []):
                print(
                    f"    feed {f['feed']}  desc={f['description']!r}  "
                    f"chainlink={f['chainlink']}  aggregator={f['aggregator']}"
                )
                emitters.add(f["feed"].lower())
                if f.get("aggregator"):
                    emitters.add(f["aggregator"].lower())
            if info.get("pyth_contract"):
                print(f"    pyth contract: {info['pyth_contract']}")

        print("\n" + "=" * 78)
        print("BACK-RUN / LATENCY ANALYSIS (recorded burst blocks)")
        print("=" * 78)
        print(f"watching {len(emitters)} feed/aggregator emitter addresses for price-update logs")

        db = sqlite3.connect(f"file:{Path('data/shadow.sqlite')}?mode=ro", uri=True)
        blocks = [
            r[0]
            for r in db.execute(
                """SELECT resolved_block FROM shadow_outcome
                   WHERE venue='morpho_base' AND outcome='missed'
                   GROUP BY resolved_block ORDER BY count(*) DESC LIMIT 8"""
            ).fetchall()
        ]
        lo = db.execute(
            "SELECT min(resolved_block) FROM shadow_outcome WHERE venue='morpho_base' AND outcome='missed'"
        ).fetchone()[0]
        hi = db.execute(
            "SELECT max(resolved_block) FROM shadow_outcome WHERE venue='morpho_base' AND outcome='missed'"
        ).fetchone()[0]
        db.close()

        singleton = adapter.singleton()
        for block in blocks:
            r = await analyze_block(pool, block, singleton, emitters)
            verdict = (
                f"SAME-BLOCK back-run: oracle push at tx idx {r['first_oracle_idx']}, "
                f"nearest liquidation {r['backrun_gap_txs']} tx(s) later "
                f"({r['n_liqs_after_push']} liqs after the push)"
                if r["oracle_update_in_block"]
                else "no oracle push in this block (liqs clear a push from an earlier block)"
            )
            print(
                f"\nblock {r['block']}  liqs_detected={r['n_liqs']}"
                f"\n  liq tx indices: {r['liq_tx_indices'][:12]}"
                f"{' ...' if len(r['liq_tx_indices']) > 12 else ''}"
                f"\n  oracle-update tx indices: {r['oracle_tx_indices']}"
                f"\n  => {verdict}"
            )
            egps = [t["effective_gwei"] for t in r["liq_txs"] if t["effective_gwei"]]
            if egps:
                print(
                    f"  liq effective gas price (gwei): "
                    f"min={min(egps):.4f} max={max(egps):.4f}"
                )

        # Map oracle pushes vs liquidation blocks across the whole cascade window.
        print("\n" + "-" * 78)
        print(f"ORACLE-PUSH MAP across cascade window [{lo}, {hi}] ({hi - lo} blocks)")
        feeds_only = {e for e in emitters}
        pushes = await oracle_push_window(pool, feeds_only, lo, hi)
        print(f"  blocks with an oracle price-update log: {len(pushes)}")
        if pushes:
            top = sorted(pushes.items(), key=lambda kv: -kv[1])[:10]
            print("  busiest push blocks (block: #update-logs):")
            for bn, c in top:
                print(f"    {bn}: {c}")


if __name__ == "__main__":
    asyncio.run(main())
