"""W2: event-driven position-book membership.

The `position_owner` fixtures below are real Base mainnet logs (pool
`0xa238dd80c259a72e81d7e4664a9801593f98d1c5`), kept verbatim rather than hand-built, because
the thing they pin is precisely which parameters Aave indexes — a fact the signature strings
in `events.py` do not record and that cannot be derived from them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liqengine.config.loader import load_registry
from liqengine.core.errors import ProviderError
from liqengine.core.types import Address, BlockHash, BlockRef, ChainId, to_address
from liqengine.positions.book import PositionBook
from liqengine.positions.follow import STREAM, EventFollower
from liqengine.protocols.aave_v3 import events
from liqengine.store.sqlite import EventStore

POOL = to_address("0xa238dd80c259a72e81d7e4664a9801593f98d1c5")


def _word(value: str) -> str:
    return "0x" + "00" * 12 + value.removeprefix("0x")


# Real logs, trimmed to the fields the decoder reads.
SUPPLY_LOG = {
    "topics": [
        events.TOPIC_SUPPLY,
        _word("833589fcd6edb6e08f4c7c32d4f71b54bda02913"),  # reserve (USDC)
        _word("82904927b7f526cf21683cb616f73b4b0e30e6ec"),  # onBehalfOf  <- the position
        "0x" + "00" * 32,  # referralCode
    ],
    "data": _word("82904927b7f526cf21683cb616f73b4b0e30e6ec") + "00" * 32,
}
WITHDRAW_LOG = {
    "topics": [
        events.TOPIC_WITHDRAW,
        _word("4200000000000000000000000000000000000006"),  # reserve (WETH)
        _word("318e184a281e2243eb196232ed08bcb3f8ff05bf"),  # user  <- the position
        _word("28e533fa2dbdb88705a431a7ae1932ca828cd57a"),  # to
    ],
    "data": "0x" + "00" * 32,
}
BORROW_LOG = {
    "topics": [
        events.TOPIC_BORROW,
        _word("cbb7c0000ab88b473b1f5afd9ef808440eed33bf"),  # reserve (cbBTC)
        _word("b549ca43c6c650cc9430e6a39d1a518f5bb22990"),  # onBehalfOf  <- the position
        "0x" + "00" * 32,  # referralCode
    ],
    "data": _word("b549ca43c6c650cc9430e6a39d1a518f5bb22990") + "00" * 96,
}
REPAY_LOG = {
    "topics": [
        events.TOPIC_REPAY,
        _word("833589fcd6edb6e08f4c7c32d4f71b54bda02913"),  # reserve
        _word("35b3fbf8527a5471567777815ba2d60459eccab5"),  # user  <- the position
        _word("35b3fbf8527a5471567777815ba2d60459eccab5"),  # repayer
    ],
    "data": "0x" + "00" * 64,
}
EMODE_LOG = {
    "topics": [
        events.TOPIC_USER_EMODE_SET,
        _word("20e4bef8137145e3923d002640b51ff5bd955b55"),  # user  <- the position
    ],
    "data": "0x" + "00" * 31 + "0a",  # category 10
}
LIQUIDATION_LOG = {
    "topics": [
        events.TOPIC_LIQUIDATION_CALL,
        _word("4200000000000000000000000000000000000006"),  # collateral
        _word("833589fcd6edb6e08f4c7c32d4f71b54bda02913"),  # debt
        _word("00000000000000000000000000000000deadbeef"),  # user  <- the position
    ],
    "data": "0x" + "00" * 128,
}


# --------------------------------------------------------------------------------------
# Owner extraction
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (SUPPLY_LOG, "0x82904927b7f526cf21683cb616f73b4b0e30e6ec"),
        (WITHDRAW_LOG, "0x318e184a281e2243eb196232ed08bcb3f8ff05bf"),
        (BORROW_LOG, "0xb549ca43c6c650cc9430e6a39d1a518f5bb22990"),
        (REPAY_LOG, "0x35b3fbf8527a5471567777815ba2d60459eccab5"),
        (EMODE_LOG, "0x20e4bef8137145e3923d002640b51ff5bd955b55"),
        (LIQUIDATION_LOG, "0x00000000000000000000000000000000deadbeef"),
    ],
)
def test_position_owner_for_every_membership_event(entry: dict, expected: str) -> None:
    assert events.position_owner(entry) == to_address(expected)


def test_supply_owner_is_on_behalf_of_not_the_caller() -> None:
    """The trap this pins. `Supply` puts the *caller* in data[0] and the account that ends up
    holding collateral in indexed `onBehalfOf`. Decoding the data field would miss every
    position opened through a router, aggregator or smart-contract wallet.
    """
    router = "0x" + "11" * 20
    beneficiary = "0x" + "22" * 20
    entry = {
        "topics": [
            events.TOPIC_SUPPLY,
            _word("833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
            _word(beneficiary),
            "0x" + "00" * 32,
        ],
        "data": _word(router) + "00" * 32,
    }
    assert events.position_owner(entry) == to_address(beneficiary)
    assert events.position_owner(entry) != to_address(router)


def test_repay_owner_is_the_borrower_not_the_repayer() -> None:
    """A third party repaying someone else's debt changes *their* position, not the payer's."""
    borrower = "0x" + "33" * 20
    repayer = "0x" + "44" * 20
    entry = {
        "topics": [
            events.TOPIC_REPAY,
            _word("833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
            _word(borrower),
            _word(repayer),
        ],
        "data": "0x" + "00" * 64,
    }
    assert events.position_owner(entry) == to_address(borrower)


def test_an_unmodelled_event_is_skipped_not_fatal() -> None:
    """A filter can deliver logs we do not model, and a protocol upgrade can add events. The
    follower must survive both rather than halting on the first unknown topic."""
    assert events.position_owner({"topics": [events.TOPIC_RESERVE_DATA_UPDATED]}) is None
    assert events.position_owner({"topics": []}) is None
    assert events.position_owner({}) is None


def test_a_known_event_with_too_few_topics_raises() -> None:
    """Not an unknown event — the contract at this address is not what we decoded against.
    Continuing would attribute the position to whatever sits at that topic index."""
    with pytest.raises(ValueError, match="owner"):
        events.position_owner({"topics": [events.TOPIC_REPAY, _word("11" * 20)]})


def test_topic_filter_is_one_or_clause_and_deterministic() -> None:
    """One request per range instead of six. The sort matters because some providers key a
    cache on the query, and an unordered set changes it on every process start."""
    first = events.position_topic_filter()
    assert len(first) == 1
    assert len(first[0]) == len(events.POSITION_TOPICS) == 6
    assert first == events.position_topic_filter()
    assert first[0] == sorted(first[0])


def test_emode_is_a_membership_event_even_though_it_moves_no_balance() -> None:
    """eMode swaps in category LT/LTV/bonus, so the same balances yield a different health
    factor. An account whose category changed must be re-read."""
    assert events.TOPIC_USER_EMODE_SET in events.POSITION_TOPICS


# --------------------------------------------------------------------------------------
# The follower
# --------------------------------------------------------------------------------------


class _FakePool:
    """Just enough of `ProviderPool`: a head, block hashes, and canned logs per range."""

    def __init__(
        self,
        head: int,
        logs: list[dict] | None = None,
        hashes: dict[int, str] | None = None,
    ) -> None:
        self._head = head
        self._logs = logs or []
        self._hashes = hashes or {}
        self.ranges: list[tuple[int, int]] = []
        self.unreadable = False

    async def head_number(self, *, policy=None) -> int:
        return self._head

    async def block_by_number(self, number, *, policy=None) -> BlockRef:
        if self.unreadable:
            raise ProviderError("fake", "anchor unreadable")
        return BlockRef(
            chain_id=ChainId(8453),
            number=int(number),
            hash=BlockHash(self._hashes.get(int(number), "0x" + "ab" * 32)),
            parent_hash=BlockHash("0x" + "cd" * 32),
            timestamp=1_700_000_000,
        )

    async def get_logs(
        self, *, address, topics, from_block, to_block, policy=None, prefer_wide=False
    ):
        self.ranges.append((from_block, to_block))
        return self._logs


def _follower(pool: _FakePool, store: EventStore, **kwargs) -> EventFollower:
    registry = load_registry("config", environ={})
    venue = registry.venue("aave_v3_base")
    return EventFollower(
        venue=venue,
        pool=pool,  # type: ignore[arg-type]
        book=PositionBook(venue="aave_v3_base", chain_id=ChainId(8453)),
        store=store,
        pool_address=POOL,
        **kwargs,
    )


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    store = EventStore(tmp_path / "t.sqlite")
    store.initialise()
    try:
        yield store
    finally:
        store.close()


async def test_a_scan_dirties_every_account_it_saw_exactly_once(store: EventStore) -> None:
    pool = _FakePool(head=1_000, logs=[SUPPLY_LOG, BORROW_LOG, SUPPLY_LOG])
    follower = _follower(pool, store)

    result = await follower.scan_range(900, 1_000)

    assert result.logs_seen == 3
    assert result.owners_touched == 2, "the repeated Supply is one account, not two"
    assert follower.book.dirty == frozenset(
        {
            Address("0x82904927b7f526cf21683cb616f73b4b0e30e6ec"),
            Address("0xb549ca43c6c650cc9430e6a39d1a518f5bb22990"),
        }
    )


async def test_a_scan_never_adds_an_account_only_marks_it_dirty(store: EventStore) -> None:
    """Membership is dirty-marking. Balances come from a multicall (W1), never from event
    amounts — `book.py` documents why deriving them here drifts toward understating debt."""
    pool = _FakePool(head=1_000, logs=[SUPPLY_LOG, BORROW_LOG])
    follower = _follower(pool, store)

    await follower.scan_range(900, 1_000)

    assert follower.book.accounts == {}
    assert len(follower.book.dirty) == 2


async def test_the_scan_stays_behind_the_head_by_the_confirmation_lag(store: EventStore) -> None:
    pool = _FakePool(head=1_000)
    follower = _follower(pool, store, confirmations=3)

    await follower.follow_once(cold_start=500)

    assert pool.ranges == [(500, 997)]


async def test_a_restart_resumes_from_the_cursor(store: EventStore) -> None:
    pool = _FakePool(head=1_000)
    follower = _follower(pool, store, confirmations=0)
    await follower.follow_once(cold_start=500)

    resumed = _follower(_FakePool(head=2_000), store, confirmations=0)
    assert resumed.resume_block() == 1_001

    await resumed.follow_once()
    assert resumed.pool.ranges == [(1_001, 2_000)]  # type: ignore[attr-defined]


async def test_nothing_new_is_not_an_empty_scan(store: EventStore) -> None:
    """Returning None rather than a zero-block ScanResult keeps "caught up" distinguishable
    from "scanned a range and found nothing"."""
    pool = _FakePool(head=1_000)
    follower = _follower(pool, store, confirmations=0)
    await follower.follow_once(cold_start=1_000)

    assert await follower.follow_once() is None


async def test_a_changed_anchor_hash_rescans_rather_than_trusting_the_cursor(
    store: EventStore,
) -> None:
    """The reorg case. Accounts whose reconciliation was anchored to an orphaned block must be
    re-dirtied; over-touching costs one redundant read, under-touching leaves an account stale
    until it next transacts."""
    pool = _FakePool(head=1_000, hashes={1_000: "0x" + "11" * 32})
    follower = _follower(pool, store, confirmations=0, rewind_blocks=50)
    await follower.follow_once(cold_start=1_000)
    assert follower._anchor == (1_000, "0x" + "11" * 32)

    pool._head = 1_010
    pool._hashes[1_000] = "0x" + "99" * 32  # same height, different hash
    result = await follower.follow_once()

    assert result is not None
    assert result.was_reorg
    assert result.from_block == 950, "rewound by rewind_blocks from the anchor"
    assert pool.ranges[-1] == (950, 1_010)


async def test_a_reorg_rescan_never_narrows_recorded_coverage(store: EventStore) -> None:
    """`scanned_through` must not rewind — those blocks were scanned, and the rescan re-dirties
    accounts, which is a fact about the book rather than about coverage.

    `from_block` legitimately widens backward to 950, because the rewind genuinely *did* scan
    950-999. That is a true claim, not the false-contiguity hazard `phase0.collect` guards
    against: the claim is only written after `scan_range` returns, so a rescan that fails
    part-way writes no cursor at all rather than claiming a range with a hole in it.
    """
    pool = _FakePool(head=1_000, hashes={1_000: "0x" + "11" * 32})
    follower = _follower(pool, store, confirmations=0, rewind_blocks=50)
    await follower.follow_once(cold_start=1_000)

    pool._head = 1_010
    pool._hashes[1_000] = "0x" + "99" * 32
    await follower.follow_once()

    cursor = store.get_cursor("aave_v3_base", STREAM)
    assert cursor is not None
    assert cursor.scanned_through == 1_010, "coverage never goes backward"
    assert cursor.from_block == 950, "and widens only as far as was actually rescanned"


async def test_an_unreadable_anchor_is_not_treated_as_a_reorg(store: EventStore) -> None:
    """A provider hiccup while checking the anchor must not read as a reorg — that turns a
    flaky endpoint into a permanent rewind loop."""
    pool = _FakePool(head=1_000, hashes={1_000: "0x" + "11" * 32})
    follower = _follower(pool, store, confirmations=0)
    await follower.follow_once(cold_start=1_000)

    pool._head = 1_010
    pool.unreadable = True
    result = await follower.follow_once()

    assert result is not None
    assert not result.was_reorg
    assert pool.ranges[-1] == (1_001, 1_010)


async def test_backfill_checkpoints_each_chunk(store: EventStore) -> None:
    """An interrupted backfill resumes where it stopped. Without per-chunk checkpointing a
    failure near the end discards the whole range."""
    pool = _FakePool(head=100_000)
    follower = _follower(pool, store)

    await follower.backfill(1_000, 25_000, chunk=10_000)

    assert pool.ranges == [(1_000, 10_999), (11_000, 20_999), (21_000, 25_000)]
    cursor = store.get_cursor("aave_v3_base", STREAM)
    assert cursor is not None
    assert cursor.scanned_through == 25_000


async def test_a_cold_start_defaults_to_the_deployment_block(store: EventStore) -> None:
    """Event history from deployment is the only complete source of membership. Starting
    part-way yields a book holding only accounts that happened to act since — fine for a
    smoke test, not a basis for deciding anything."""
    follower = _follower(_FakePool(head=100), store)
    assert follower.resume_block() == follower.venue.protocol.deploy_block
