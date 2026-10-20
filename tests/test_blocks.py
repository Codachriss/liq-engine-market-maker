"""Canonical chain tracking: reorgs, gaps, and finality tiers."""

from __future__ import annotations

import pytest

from liqengine.core.types import BlockHash, BlockRef, ChainId, FinalityTier
from liqengine.ingest.blocks import BlockTracker, UpdateKind

CHAIN = 42161


def block(number: int, tag: str, parent: str, timestamp: int | None = None) -> BlockRef:
    return BlockRef(
        chain_id=ChainId(CHAIN),
        number=number,
        hash=BlockHash(f"0x{tag}"),
        parent_hash=BlockHash(f"0x{parent}"),
        timestamp=timestamp if timestamp is not None else 1_700_000_000 + number,
    )


def chain(tracker: BlockTracker, start: int, count: int, prefix: str = "a") -> list[BlockRef]:
    """Feed a contiguous run of blocks and return them."""
    made = []
    for i in range(count):
        n = start + i
        b = block(n, f"{prefix}{n}", f"{prefix}{n - 1}")
        tracker.ingest(b)
        made.append(b)
    return made


# --------------------------------------------------------------------------- happy path


def test_first_block_extends_an_empty_tracker() -> None:
    t = BlockTracker(chain_id=CHAIN)
    update = t.ingest(block(100, "a100", "a99"))
    assert update.kind is UpdateKind.EXTENDED
    assert t.head is not None and t.head.number == 100


def test_contiguous_blocks_extend_the_head() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 5)
    assert t.head is not None and t.head.number == 104
    assert len(t) == 5


def test_replayed_block_is_a_duplicate_not_a_reorg() -> None:
    """The same block arriving from a second provider must not disturb anything."""
    t = BlockTracker(chain_id=CHAIN)
    blocks = chain(t, 100, 3)
    update = t.ingest(blocks[1])
    assert update.kind is UpdateKind.DUPLICATE
    assert t.head is not None and t.head.number == 102


def test_block_from_another_chain_is_rejected() -> None:
    t = BlockTracker(chain_id=CHAIN)
    wrong = BlockRef(
        chain_id=ChainId(1), number=1, hash=BlockHash("0xa"), parent_hash=BlockHash("0xb"),
        timestamp=0,
    )
    with pytest.raises(ValueError, match="chain"):
        t.ingest(wrong)


# --------------------------------------------------------------------------- gaps


def test_a_jump_ahead_reports_the_missing_range() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)  # head 102
    update = t.ingest(block(106, "a106", "a105"))
    assert update.kind is UpdateKind.GAP
    assert update.missing_range == (103, 105)


def test_a_gap_block_is_not_appended() -> None:
    """Appending across a hole makes the chain look contiguous when it is not."""
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)
    t.ingest(block(106, "a106", "a105"))
    assert t.head is not None and t.head.number == 102
    assert t.get(106) is None


def test_backfill_closes_a_gap() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)
    gap = t.ingest(block(106, "a106", "a105"))
    assert gap.missing_range is not None

    start, end = gap.missing_range
    filled = [block(n, f"a{n}", f"a{n - 1}") for n in range(start, end + 1)]
    updates = t.apply_backfill(filled)
    assert all(u.kind is UpdateKind.EXTENDED for u in updates)

    assert t.ingest(block(106, "a106", "a105")).kind is UpdateKind.EXTENDED
    assert t.head is not None and t.head.number == 106


# --------------------------------------------------------------------------- reorgs


def test_a_competing_head_is_a_one_block_reorg() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)  # ...a101, a102
    update = t.ingest(block(102, "b102", "a101"))

    assert update.kind is UpdateKind.REORG
    assert update.depth == 1
    assert update.orphaned[0].hash == "0xa102"
    assert update.common_ancestor is not None
    assert update.common_ancestor.hash == "0xa101"


def test_reorg_replaces_the_orphaned_block() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)
    t.ingest(block(102, "b102", "a101"))
    assert t.head is not None and t.head.hash == "0xb102"
    assert t.get(102) is not None and t.get(102).hash == "0xb102"


def test_deeper_reorg_orphans_every_block_above_the_fork() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 6)  # a100..a105
    update = t.ingest(block(102, "b102", "a101"))

    assert update.kind is UpdateKind.REORG
    assert update.depth == 4  # a102, a103, a104, a105
    assert [b.number for b in update.orphaned] == [102, 103, 104, 105]
    assert t.head is not None and t.head.number == 102


def test_orphans_are_reported_deepest_first() -> None:
    """Callers unwind in this order, so it must be ascending by height."""
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 6)
    update = t.ingest(block(102, "b102", "a101"))
    numbers = [b.number for b in update.orphaned]
    assert numbers == sorted(numbers)


def test_new_head_whose_parent_we_never_saw_is_disconnected() -> None:
    """We refuse to guess the fork point; guessing here corrupts the position book silently."""
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)
    update = t.ingest(block(103, "b103", "b102"))
    assert update.kind is UpdateKind.DISCONNECTED


def test_reorg_deeper_than_the_window_is_disconnected() -> None:
    t = BlockTracker(chain_id=CHAIN, capacity=4)
    chain(t, 100, 8)  # window now holds 104..107
    update = t.ingest(block(103, "b103", "b102"))
    assert update.kind is UpdateKind.DISCONNECTED


def test_reset_clears_the_window() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)
    t.reset()
    assert t.head is None
    assert t.ingest(block(500, "c500", "c499")).kind is UpdateKind.EXTENDED


def test_requires_action_flags_exactly_the_dangerous_outcomes() -> None:
    t = BlockTracker(chain_id=CHAIN)
    assert not t.ingest(block(100, "a100", "a99")).requires_action
    assert not t.ingest(block(100, "a100", "a99")).requires_action  # duplicate
    assert t.ingest(block(110, "a110", "a109")).requires_action     # gap
    assert t.ingest(block(100, "z100", "a99")).requires_action      # reorg


# --------------------------------------------------------------------------- capacity


def test_window_is_bounded_and_drops_the_oldest() -> None:
    t = BlockTracker(chain_id=CHAIN, capacity=10)
    chain(t, 100, 50)
    assert len(t) == 10
    assert t.oldest is not None and t.oldest.number == 140
    assert t.head is not None and t.head.number == 149


# --------------------------------------------------------------------------- finality


def test_unknown_tags_never_promote_past_confirmed() -> None:
    """Missing finality data stalls realisation rather than fabricating it."""
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 5)  # head 104
    assert t.finality_of(100) is FinalityTier.CONFIRMED
    assert t.finality_of(104) is FinalityTier.SOFT


def test_head_is_soft_until_it_is_buried() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 3)
    assert t.finality_of(102, confirmed_depth=1) is FinalityTier.SOFT
    assert t.finality_of(101, confirmed_depth=1) is FinalityTier.CONFIRMED


def test_tags_promote_blocks_to_their_strongest_tier() -> None:
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 10)
    t.update_tags(safe=106, finalized=103, settled=101)

    assert t.finality_of(101) is FinalityTier.SETTLED
    assert t.finality_of(103) is FinalityTier.FINALIZED
    assert t.finality_of(105) is FinalityTier.SAFE
    assert t.finality_of(107) is FinalityTier.CONFIRMED


def test_tag_heights_never_move_backwards() -> None:
    """A provider serving a stale `finalized` must not downgrade already-realised P&L."""
    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 10)
    t.update_tags(finalized=108)
    t.update_tags(finalized=104)
    assert t.finality_of(108) is FinalityTier.FINALIZED


def test_realisation_tier_is_reached_only_with_a_finalized_tag() -> None:
    from liqengine.core.types import REALISATION_TIER

    t = BlockTracker(chain_id=CHAIN)
    chain(t, 100, 10)
    assert t.finality_of(100) < REALISATION_TIER
    t.update_tags(finalized=100)
    assert t.finality_of(100) >= REALISATION_TIER
