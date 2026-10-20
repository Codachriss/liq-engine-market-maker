"""Event store: uint256 safety, cursor monotonicity, idempotency, and reorg marking."""

from __future__ import annotations

from pathlib import Path

import pytest

from liqengine.core.types import BlockHash, BlockRef, ChainId, FinalityTier
from liqengine.protocols.aave_v3.events import LiquidationCall
from liqengine.store.sqlite import EventStore

CHAIN = 42161
VENUE = "aave_v3_arbitrum"


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    s = EventStore(tmp_path / "test.sqlite")
    s.initialise()
    yield s
    s.close()


def liquidation(
    *,
    tx: str = "0xaa",
    log_index: int = 0,
    block: int = 100,
    block_hash: str = "0xblock",
    debt: int = 1000 * 10**6,
    collateral: int = 5 * 10**17,
    liquidator: str = "0x0000000000000000000000000000000000000044",
) -> LiquidationCall:
    return LiquidationCall(
        block_number=block,
        block_hash=block_hash,
        transaction_hash=tx,  # type: ignore[arg-type]
        transaction_index=1,
        log_index=log_index,
        collateral_asset="0x0000000000000000000000000000000000000011",  # type: ignore[arg-type]
        debt_asset="0x0000000000000000000000000000000000000022",  # type: ignore[arg-type]
        user="0x0000000000000000000000000000000000000033",  # type: ignore[arg-type]
        liquidator=liquidator,  # type: ignore[arg-type]
        debt_to_cover=debt,
        liquidated_collateral_amount=collateral,
        receive_a_token=False,
    )


def block_ref(number: int, tag: str = "a", timestamp: int = 1_700_000_000) -> BlockRef:
    return BlockRef(
        chain_id=ChainId(CHAIN),
        number=number,
        hash=BlockHash(f"0x{tag}{number}"),
        parent_hash=BlockHash(f"0x{tag}{number - 1}"),
        timestamp=timestamp + number,
    )


# --------------------------------------------------------------------------- schema


def test_initialise_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.sqlite"
    for _ in range(2):
        s = EventStore(path)
        s.initialise()
        s.close()


# --------------------------------------------------------------------------- uint256


def test_uint256_amounts_survive_a_round_trip(store: EventStore) -> None:
    """SQLite integers are 64-bit; a 1e18-denominated balance overflows them silently."""
    huge = 2**200
    store.upsert_liquidations(VENUE, CHAIN, [liquidation(collateral=huge)])
    row = store.liquidation_rows(VENUE)[0]
    assert int(row["collateral_seized"]) == huge


def test_negative_amount_is_rejected(store: EventStore) -> None:
    with pytest.raises(ValueError, match="negative"):
        store.upsert_liquidations(VENUE, CHAIN, [liquidation(debt=-1)])


# --------------------------------------------------------------------------- idempotency


def test_rescanning_the_same_range_does_not_duplicate(store: EventStore) -> None:
    events = [liquidation(tx="0xaa", log_index=0), liquidation(tx="0xbb", log_index=1)]
    store.upsert_liquidations(VENUE, CHAIN, events)
    store.upsert_liquidations(VENUE, CHAIN, events)
    assert store.liquidation_count(VENUE) == 2


def test_same_transaction_with_two_logs_is_two_liquidations(store: EventStore) -> None:
    """A single transaction can liquidate several positions; the uid must separate them."""
    store.upsert_liquidations(
        VENUE, CHAIN, [liquidation(tx="0xaa", log_index=0), liquidation(tx="0xaa", log_index=1)]
    )
    assert store.liquidation_count(VENUE) == 2


def test_venues_are_isolated(store: EventStore) -> None:
    store.upsert_liquidations(VENUE, CHAIN, [liquidation()])
    store.upsert_liquidations("aave_v3_base", 8453, [liquidation()])
    assert store.liquidation_count(VENUE) == 1
    assert store.liquidation_count("aave_v3_base") == 1


# --------------------------------------------------------------------------- cursors


def test_cursor_starts_absent(store: EventStore) -> None:
    assert store.get_cursor(VENUE, "stream") is None


def test_cursor_advances_forward(store: EventStore) -> None:
    store.set_cursor(VENUE, "stream", 100, 200)
    store.set_cursor(VENUE, "stream", 201, 300)
    cursor = store.get_cursor(VENUE, "stream")
    assert cursor is not None
    assert cursor.from_block == 100
    assert cursor.scanned_through == 300
    assert cursor.next_block == 301


def test_cursor_never_moves_scanned_through_backwards(store: EventStore) -> None:
    """A stale writer must not erase coverage that another range already established."""
    store.set_cursor(VENUE, "stream", 100, 500)
    store.set_cursor(VENUE, "stream", 100, 200)
    cursor = store.get_cursor(VENUE, "stream")
    assert cursor is not None and cursor.scanned_through == 500


def test_widening_the_window_backwards_lowers_from_block(store: EventStore) -> None:
    """Asking for more history must record the wider window, not claim it was already covered."""
    store.set_cursor(VENUE, "stream", 1000, 2000)
    store.set_cursor(VENUE, "stream", 500, 999)
    cursor = store.get_cursor(VENUE, "stream")
    assert cursor is not None
    assert cursor.from_block == 500
    assert cursor.scanned_through == 2000


# --------------------------------------------------------------------------- blocks


def test_blocks_and_timestamp_backfill(store: EventStore) -> None:
    store.upsert_liquidations(VENUE, CHAIN, [liquidation(block=100, block_hash="0xa100")])
    assert store.liquidation_rows(VENUE)[0]["block_timestamp"] is None

    store.upsert_blocks([block_ref(100)], FinalityTier.CONFIRMED)
    store.attach_block_timestamps(VENUE)
    assert store.liquidation_rows(VENUE)[0]["block_timestamp"] == 1_700_000_100


def test_finality_only_ratchets_upwards(store: EventStore) -> None:
    store.upsert_blocks([block_ref(100)], FinalityTier.FINALIZED)
    store.upsert_blocks([block_ref(100)], FinalityTier.SOFT)
    row = store._conn.execute(
        "SELECT finality FROM block WHERE chain_id = ? AND number = 100", (CHAIN,)
    ).fetchone()
    assert row["finality"] == int(FinalityTier.FINALIZED)


def test_block_timestamps_lookup_handles_large_batches(store: EventStore) -> None:
    store.upsert_blocks([block_ref(n) for n in range(1200)], FinalityTier.CONFIRMED)
    found = store.block_timestamps(CHAIN, list(range(1200)))
    assert len(found) == 1200


# --------------------------------------------------------------------------- tokens


def test_token_metadata_upsert_preserves_known_values(store: EventStore) -> None:
    """A later probe that fails to read the symbol must not erase the one we already have."""
    address = "0x0000000000000000000000000000000000000011"
    store.upsert_tokens(CHAIN, {address: ("USDC", 6)})
    store.upsert_tokens(CHAIN, {address: (None, None)})
    assert store.tokens(CHAIN)[address] == ("USDC", 6)


# --------------------------------------------------------------------------- reorgs


def test_orphaning_marks_rather_than_deletes(store: EventStore) -> None:
    """What we believed at the time is data; a reorg must not make it vanish."""
    store.upsert_blocks([block_ref(100)], FinalityTier.CONFIRMED)
    store.upsert_liquidations(VENUE, CHAIN, [liquidation(block=100, block_hash="0xa100")])

    affected = store.mark_orphaned(CHAIN, ["0xa100"])
    assert affected >= 2

    assert store.liquidation_count(VENUE) == 0  # excluded from canonical reads
    remaining = store._conn.execute(
        "SELECT COUNT(*) AS n FROM liquidation WHERE orphaned = 1"
    ).fetchone()
    assert remaining["n"] == 1  # but still on disk


def test_orphaning_ignores_unrelated_blocks(store: EventStore) -> None:
    store.upsert_liquidations(
        VENUE,
        CHAIN,
        [
            liquidation(tx="0xaa", block=100, block_hash="0xa100"),
            liquidation(tx="0xbb", block=101, block_hash="0xa101"),
        ],
    )
    store.mark_orphaned(CHAIN, ["0xa101"])
    rows = store.liquidation_rows(VENUE)
    assert len(rows) == 1 and rows[0]["block_number"] == 100


def test_orphaning_nothing_is_a_no_op(store: EventStore) -> None:
    assert store.mark_orphaned(CHAIN, []) == 0


# --------------------------------------------------------------------------- operations


def test_halt_events_are_recorded(store: EventStore) -> None:
    store.record_halt("provider_disagreement", "a and b disagree at 100", venue=VENUE)
    row = store._conn.execute("SELECT * FROM halt_event").fetchone()
    assert row["reason"] == "provider_disagreement"
    assert row["cleared_at"] is None
