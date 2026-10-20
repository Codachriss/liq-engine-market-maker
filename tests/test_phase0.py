"""Phase 0: scan-range resumption and the venue scorecard."""

from __future__ import annotations

from pathlib import Path

import pytest

from liqengine.config.loader import load_registry
from liqengine.core.errors import AllProvidersFailed
from liqengine.phase0.collect import (
    STREAM,
    _checkpoints,
    _ranges_to_scan,
    _window_start,
    collect_liquidations,
)
from liqengine.phase0.report import build_cohorts, build_scorecard
from liqengine.protocols.aave_v3.events import LiquidationCall
from liqengine.store.sqlite import EventStore

USDC_ARBITRUM = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
WETH_ARBITRUM = "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"


# --------------------------------------------------------------------------- resume ranges


def test_first_run_scans_the_whole_window() -> None:
    assert _ranges_to_scan(100, 200, None, None) == [(100, 200)]


def test_empty_window_scans_nothing() -> None:
    assert _ranges_to_scan(200, 100, None, None) == []


def test_second_run_only_extends_forward() -> None:
    assert _ranges_to_scan(100, 300, 100, 200) == [(201, 300)]


def test_nothing_to_do_when_already_current() -> None:
    assert _ranges_to_scan(100, 200, 100, 200) == []


def test_widening_backwards_scans_only_the_new_history() -> None:
    """Asking for more history must fetch the older range, not silently assume it is covered."""
    assert _ranges_to_scan(50, 200, 100, 200) == [(50, 99)]


def test_widening_both_directions_scans_two_ranges() -> None:
    assert _ranges_to_scan(50, 300, 100, 200) == [(50, 99), (201, 300)]


def test_a_narrower_request_does_not_rescan() -> None:
    assert _ranges_to_scan(120, 180, 100, 200) == []


def test_window_start_is_clamped_to_the_deployment_block() -> None:
    registry = load_registry("config", environ={})
    venue = registry.venue("aave_v3_arbitrum")
    # Ten years of Arbitrum blocks is far more than the deployment has existed for.
    assert _window_start(venue, head=500_000_000, days=3650) == venue.protocol.deploy_block


def test_window_start_uses_the_chains_block_time() -> None:
    """A day of Arbitrum is ~345k blocks; a day of Ethereum is ~7.2k. Same code, both right."""
    registry = load_registry("config", environ={})
    arbitrum = registry.venue("aave_v3_arbitrum")
    ethereum = registry.venue("aave_v3_ethereum")

    arb_span = 500_000_000 - _window_start(arbitrum, head=500_000_000, days=1)
    eth_span = 25_000_000 - _window_start(ethereum, head=25_000_000, days=1)
    assert arb_span == pytest.approx(345_600, rel=0.01)
    assert eth_span == pytest.approx(7_200, rel=0.01)


# --------------------------------------------------------------------------- checkpointing


def test_checkpoints_cover_the_whole_range_exactly_once() -> None:
    pieces = _checkpoints(0, 249, span=100)
    assert pieces == [(0, 99), (100, 199), (200, 249)]


def test_a_range_smaller_than_the_span_is_one_piece() -> None:
    assert _checkpoints(10, 20, span=100) == [(10, 20)]


def test_checkpointing_bounds_how_much_a_mid_scan_failure_can_lose() -> None:
    """A provider failing on piece 3 of 5 must not cost pieces 1 and 2 too — that loss is what
    an unbounded single get_logs call over the whole range produced before this existed."""
    pieces = _checkpoints(0, 499, span=100)
    assert len(pieces) == 5
    assert pieces[0] == (0, 99)
    assert pieces[-1] == (400, 499)


_FAKE_ADDRESS_WORD = "0x" + "00" * 12 + "11" * 20


class _FakePool:
    """Just enough of `ProviderPool` for `collect_liquidations`: address resolution reads,
    a head, and a `get_logs` that fails once the scan reaches into blocks the test wants left
    unscanned — simulating exactly the free-tier failure this checkpointing exists to survive.
    """

    def __init__(self, head: int, fails_below: int) -> None:
        self.head = head
        self.fails_below = fails_below

    async def call(self, method: str, params: list, *, policy=None) -> str:
        assert method == "eth_call"
        return _FAKE_ADDRESS_WORD

    async def head_number(self, *, policy=None) -> int:
        return self.head

    async def get_logs(self, *, address, topics, from_block, to_block, policy=None):
        if from_block < self.fails_below:
            raise AllProvidersFailed("fake: below the simulated failure line")
        return []


async def test_an_interrupted_backward_widen_leaves_no_false_gap(tmp_path: Path) -> None:
    """The bug this pins: a checkpoint recorded before the pieces between it and the existing
    span have actually run claims a contiguous span that has a hole in it. A future scan must
    see that hole as still-unscanned, not as already covered.
    """
    registry = load_registry("config", environ={})
    venue = registry.venue("aave_v3_base")
    store = EventStore(tmp_path / "gap.sqlite")
    store.initialise()

    head = 60_000_000
    # An existing narrow cursor, as if only a few recent days had been scanned before.
    store.set_cursor(venue.key, STREAM, 59_000_000, head)

    # Widen to 180 days of history (target_from lands around 52.2M). The fake fails on
    # anything below 55,000,000, so the widen can only get partway back before it is
    # interrupted.
    fake_pool = _FakePool(head=head, fails_below=55_000_000)
    with pytest.raises(AllProvidersFailed):
        await collect_liquidations(venue, store, days=180, provider_pool=fake_pool)

    cursor = store.get_cursor(venue.key, STREAM)
    assert cursor is not None
    # Genuinely covered now: [cursor.from_block, 58_999_999] (new) contiguous with
    # [59_000_000, head] (old). Nothing below cursor.from_block was actually scanned.
    assert cursor.from_block >= 55_000_000
    assert cursor.scanned_through == head

    # The decisive check: a second run must still see the untouched history as work to do,
    # not as already covered.
    ranges = _ranges_to_scan(
        _window_start(venue, head, 180), head, cursor.from_block, cursor.scanned_through
    )
    assert ranges, "a real gap below the achieved cursor must still be reported as unscanned"
    assert ranges[0][1] < cursor.from_block  # the reported range ends where coverage begins

    store.close()


def test_descending_checkpoints_start_adjacent_to_the_existing_span() -> None:
    """A backward widen must checkpoint from the boundary nearest existing coverage outward,
    or an interrupted run records a from_block with a hole behind it.

    `_ranges_to_scan`'s backward range always ends at `cursor_from - 1` — directly adjacent to
    whatever is already covered — so the first descending piece must be the one touching that
    boundary, not the one touching the far historical edge.
    """
    pieces = _checkpoints(0, 499, span=100, descending=True)
    assert pieces[0] == (400, 499)  # adjacent to the existing span
    assert pieces[-1] == (0, 99)    # the far historical edge, reached last
    assert sorted(pieces) == _checkpoints(0, 499, span=100)  # same pieces, only order differs


# --------------------------------------------------------------------------- scorecard


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    s = EventStore(tmp_path / "phase0.sqlite")
    s.initialise()
    yield s
    s.close()


def event(
    *,
    tx: str,
    liquidator: str,
    borrower: str = "0x00000000000000000000000000000000000000bb",
    tx_index: int = 0,
    debt: int = 1000 * 10**6,
    debt_asset: str = USDC_ARBITRUM,
    receive_a_token: bool = False,
    block: int = 100,
) -> LiquidationCall:
    return LiquidationCall(
        block_number=block,
        block_hash=f"0xblock{block}",
        transaction_hash=tx,  # type: ignore[arg-type]
        transaction_index=tx_index,
        log_index=0,
        collateral_asset=WETH_ARBITRUM,  # type: ignore[arg-type]
        debt_asset=debt_asset,  # type: ignore[arg-type]
        user=borrower,  # type: ignore[arg-type]
        liquidator=liquidator,  # type: ignore[arg-type]
        debt_to_cover=debt,
        liquidated_collateral_amount=5 * 10**17,
        receive_a_token=receive_a_token,
    )


def seed(store: EventStore, events: list[LiquidationCall]) -> None:
    registry = load_registry("config", environ={})
    venue = registry.venue("aave_v3_arbitrum")
    store.upsert_liquidations(venue.key, venue.chain_id, events)
    store.upsert_tokens(venue.chain_id, {USDC_ARBITRUM: ("USDC", 6), WETH_ARBITRUM: ("WETH", 18)})


def scorecard(store: EventStore):  # type: ignore[no-untyped-def]
    registry = load_registry("config", environ={})
    return build_scorecard(registry.venue("aave_v3_arbitrum"), store)


def test_empty_venue_produces_an_empty_scorecard(store: EventStore) -> None:
    card = scorecard(store)
    assert card.liquidations == 0
    assert card.competition_note == "no data"


def test_monopolised_venue_reports_maximum_concentration(store: EventStore) -> None:
    """One winner taking everything is HHI 10,000 — the top of the scale."""
    seed(store, [event(tx=f"0x{i}", liquidator="0x00000000000000000000000000000000000000aa")
                 for i in range(10)])
    card = scorecard(store)
    assert card.unique_liquidators == 1
    assert card.top1_share == 1.0
    assert card.hhi == pytest.approx(10_000)
    assert "highly concentrated" in card.competition_note


def test_fragmented_venue_reports_low_concentration(store: EventStore) -> None:
    seed(store, [event(tx=f"0x{i}", liquidator=f"0x{i:040x}") for i in range(20)])
    card = scorecard(store)
    assert card.unique_liquidators == 20
    assert card.hhi == pytest.approx(500)
    assert "fragmented" in card.competition_note


def test_top5_share_is_computed_over_the_leaders(store: EventStore) -> None:
    # One dominant winner with 6, plus 8 one-off winners: 14 liquidations, 9 liquidators.
    events = [event(tx=f"0xa{i}", liquidator="0x" + "a" * 40) for i in range(6)]
    events += [event(tx=f"0xb{i}", liquidator=f"0x{i:040x}") for i in range(8)]
    seed(store, events)
    card = scorecard(store)
    assert card.unique_liquidators == 9
    assert card.top1_share == pytest.approx(6 / 14)
    assert card.top5_share == pytest.approx(10 / 14)  # 6 + four singles


def test_repeat_borrowers_are_measured(store: EventStore) -> None:
    """Whether the same wallets keep getting liquidated decides if a watchlist pays off."""
    events = [
        event(tx="0x1", liquidator="0x" + "a" * 40, borrower="0x" + "1" * 40),
        event(tx="0x2", liquidator="0x" + "a" * 40, borrower="0x" + "1" * 40),
        event(tx="0x3", liquidator="0x" + "a" * 40, borrower="0x" + "2" * 40),
    ]
    seed(store, events)
    card = scorecard(store)
    assert card.unique_borrowers == 2
    assert card.repeat_borrower_share == pytest.approx(2 / 3)


def test_position_in_block_is_summarised(store: EventStore) -> None:
    events = [
        event(tx="0x1", liquidator="0x" + "a" * 40, tx_index=0),
        event(tx="0x2", liquidator="0x" + "b" * 40, tx_index=0),
        event(tx="0x3", liquidator="0x" + "c" * 40, tx_index=9),
        event(tx="0x4", liquidator="0x" + "d" * 40, tx_index=11),
    ]
    seed(store, events)
    card = scorecard(store)
    assert card.share_first_in_block == pytest.approx(0.5)
    assert card.share_late_in_block == pytest.approx(0.5)


def test_atoken_retention_is_measured(store: EventStore) -> None:
    """Keeping the aToken means the winner chose not to exit — warehousing, not flipping."""
    events = [
        event(tx="0x1", liquidator="0x" + "a" * 40, receive_a_token=True),
        event(tx="0x2", liquidator="0x" + "b" * 40, receive_a_token=False),
    ]
    seed(store, events)
    assert scorecard(store).receive_atoken_share == pytest.approx(0.5)


def test_usd_sizing_covers_only_stablecoin_debt(store: EventStore) -> None:
    """Non-stable debt is left out of the total rather than guessed at, and coverage says so."""
    events = [
        event(tx="0x1", liquidator="0x" + "a" * 40, debt_asset=USDC_ARBITRUM, debt=1000 * 10**6),
        event(tx="0x2", liquidator="0x" + "b" * 40, debt_asset=USDC_ARBITRUM, debt=3000 * 10**6),
        event(tx="0x3", liquidator="0x" + "c" * 40, debt_asset=WETH_ARBITRUM, debt=10**18),
    ]
    seed(store, events)
    card = scorecard(store)
    assert card.usd_coverage == pytest.approx(2 / 3)
    assert card.usd_total == pytest.approx(4000)
    assert card.usd_median == pytest.approx(2000)


def test_orphaned_liquidations_are_excluded_from_the_scorecard(store: EventStore) -> None:
    registry = load_registry("config", environ={})
    venue = registry.venue("aave_v3_arbitrum")
    seed(store, [event(tx="0x1", liquidator="0x" + "a" * 40, block=100),
                 event(tx="0x2", liquidator="0x" + "b" * 40, block=101)])
    store.mark_orphaned(venue.chain_id, ["0xblock101"])
    assert scorecard(store).liquidations == 1


def test_asset_symbols_prefer_configured_annotations(store: EventStore) -> None:
    seed(store, [event(tx="0x1", liquidator="0x" + "a" * 40)])
    card = scorecard(store)
    assert card.top_debt_assets[0].symbol == "USDC"
    assert card.top_collateral_assets[0].symbol == "WETH"


# ------------------------------------------------------------------- competition by size


def cohorts(store: EventStore):  # type: ignore[no-untyped-def]
    registry = load_registry("config", environ={})
    return build_cohorts(registry.venue("aave_v3_arbitrum"), store)


DUST_BOT = "0x" + "d" * 40
BIG_BOT = "0x" + "e" * 40


def test_dust_hides_the_concentration_that_matters(store: EventStore) -> None:
    """The finding this whole section exists to make visible.

    One actor takes every sub-dollar liquidation and never touches a large one; a second takes
    every large one. Pooled, that is two winners with a middling HHI. Split by size, the
    cohort worth entering has a single winner taking everything.
    """
    events = [
        event(tx=f"0xd{i}", liquidator=DUST_BOT, debt=10**4, tx_index=3, block=100 + i)
        for i in range(90)
    ]
    events += [
        event(tx=f"0xb{i}", liquidator=BIG_BOT, debt=50_000 * 10**6, tx_index=9, block=300 + i)
        for i in range(10)
    ]
    seed(store, events)

    comparison = cohorts(store)
    pooled = comparison.cohort(0.0)
    large = comparison.cohort(10_000.0)

    assert pooled.unique_liquidators == 2
    assert pooled.top1_share == pytest.approx(0.9)
    assert large.unique_liquidators == 1
    assert large.top1_share == 1.0
    assert large.hhi == pytest.approx(10_000)
    assert "highly concentrated" in large.competition_note


def test_the_control_cohort_isolates_size_from_pricing(store: EventStore) -> None:
    """`all priceable` must exclude unpriceable debt too, or the comparison confounds the two."""
    seed(
        store,
        [
            event(tx="0x1", liquidator=DUST_BOT, debt=10**4),
            event(tx="0x2", liquidator=BIG_BOT, debt=50_000 * 10**6),
            # WETH debt: real, but not sizeable without a historical oracle price.
            event(tx="0x3", liquidator=BIG_BOT, debt=10**18, debt_asset=WETH_ARBITRUM),
        ],
    )
    comparison = cohorts(store)
    assert comparison.total_liquidations == 3
    assert comparison.priceable_liquidations == 2
    assert comparison.cohort(0.0).liquidations == 2
    assert comparison.priceable_share == pytest.approx(2 / 3)


def test_value_concentrates_even_when_counts_do_not(store: EventStore) -> None:
    """Dust dominates the count and contributes nothing to the value."""
    events = [
        event(tx=f"0xd{i}", liquidator=DUST_BOT, debt=10**4, block=100 + i) for i in range(99)
    ]
    events.append(event(tx="0xb", liquidator=BIG_BOT, debt=100_000 * 10**6, block=500))
    seed(store, events)

    comparison = cohorts(store)
    pooled = comparison.cohort(0.0)
    large = comparison.cohort(10_000.0)
    assert large.share_of_priceable == pytest.approx(0.01)
    assert large.usd_total / pooled.usd_total > 0.99


def test_thin_cohorts_are_flagged_rather_than_reported_as_findings(store: EventStore) -> None:
    """An HHI over eleven events is not a measurement of market structure."""
    events = [
        event(tx=f"0xd{i}", liquidator=DUST_BOT, debt=10**4, block=100 + i) for i in range(60)
    ]
    events += [
        event(tx=f"0xb{i}", liquidator=BIG_BOT, debt=50_000 * 10**6, block=300 + i)
        for i in range(5)
    ]
    seed(store, events)

    thin = cohorts(store).thin(min_rows=30)
    assert [c.min_usd for c in thin] == [100.0, 1_000.0, 10_000.0]


def test_an_empty_cohort_reports_zero_rather_than_dividing_by_it(store: EventStore) -> None:
    seed(store, [event(tx="0x1", liquidator=DUST_BOT, debt=10**4)])
    large = cohorts(store).cohort(10_000.0)
    assert large.liquidations == 0
    assert large.hhi == 0.0
    assert large.competition_note == "no data"
