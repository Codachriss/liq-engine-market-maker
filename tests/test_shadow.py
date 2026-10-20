"""Shadow mode: the reserve reader, pair planning, outcome labelling, and A5 readiness.

The driver's round itself was exercised live against Base when it landed (`liq shadow run`);
what is pinned here is the logic whose failure would be silent — a cure recorded for an account
that was actually liquidated inside the follower's lag, a liquidation before our detection
counted as a race we were in, a reorg rescan writing a second label, a conservative quote sized
smaller than the conservative seizure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from liqengine.config.loader import load_registry
from liqengine.core.abi import selector
from liqengine.core.errors import ProviderError
from liqengine.core.fixedpoint import RAY, WAD
from liqengine.core.types import Address, BlockHash, ChainId, TxHash, to_address
from liqengine.positions.book import PositionBook
from liqengine.positions.follow import EventFollower
from liqengine.protocols.aave_v3 import events
from liqengine.protocols.aave_v3.events import LiquidationCall
from liqengine.protocols.aave_v3.math import calculate_seizure
from liqengine.protocols.aave_v3.reserves import AaveReserveReader, ReserveReadError, _decode
from liqengine.protocols.aave_v3.state import ReserveState, UserAccount, UserReserve
from liqengine.shadow.driver import STRATEGY_ID, ShadowDriver
from liqengine.shadow.plan import plan_account
from liqengine.shadow.readiness import assess
from liqengine.store.records import DecisionRecord, ShadowOutcomeRecord
from liqengine.store.sqlite import EventStore

VENUE = "aave_v3_base"
POOL = to_address("0xa238dd80c259a72e81d7e4664a9801593f98d1c5")
PROVIDER = to_address("0x0f43731eb8d45a581f4a36dd74f5f358bc90c73a")
WETH = to_address("0x4200000000000000000000000000000000000006")
USDC = to_address("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
CBBTC = to_address("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf")
BORROWER = to_address("0x" + "b0" * 20)
OTHER = to_address("0x" + "0e" * 20)
LIQUIDATOR = to_address("0x" + "1c" * 20)
NOW = 1_800_000_000


def _registry_venue():
    return load_registry("config", environ={}).venue(VENUE)


@pytest.fixture
def store(tmp_path: Path):
    s = EventStore(tmp_path / "shadow.sqlite")
    s.initialise()
    try:
        yield s
    finally:
        s.close()


def _word(value: int) -> str:
    return f"{value:064x}"


def _addr(address: str) -> str:
    return "00" * 12 + address.removeprefix("0x")


# --------------------------------------------------------------------------------------
# reserves (W6's live-read slice)
# --------------------------------------------------------------------------------------

#: WETH on Base as read live: getReserveConfigurationData, then getReserveData.
WETH_CONFIG = [18, 8000, 8300, 10500, 1500, 1, 1, 0, 1, 0]
WETH_DATA = [0, 9, 8, 0, 7, 145 * 10**23, 207 * 10**23, 0, 0, 1051 * 10**24, 1080 * 10**24, NOW]
A_WETH = to_address("0xd4a0e0b9149bcee3c920d2e00b5de09138fd8bb7")
V_WETH = to_address("0x24e6e0795b3c7c71d965fcc4f371803d1c1dca1e")


def _payload(values: list[int]) -> str:
    return "0x" + "".join(_word(v) for v in values)


def _weth_raw() -> list[object | None]:
    return [
        _payload(WETH_CONFIG),
        _payload(WETH_DATA),
        _payload([1000]),
        _payload([0]),
        _payload([int(A_WETH, 16), 0, int(V_WETH, 16)]),
        _payload([0]),
        _payload([0]),
        _payload([1]),
    ]


def test_reserve_decodes_every_field_from_the_data_provider() -> None:
    reserve = _decode(WETH, "WETH", _weth_raw())

    assert reserve.decimals == 18
    assert reserve.ltv_bps == 8000
    assert reserve.liquidation_threshold_bps == 8300
    assert reserve.liquidation_bonus_bps == 10500, "raw Aave encoding: 105%, not 500"
    assert reserve.liquidation_protocol_fee_bps == 1000
    assert reserve.liquidity_index_ray == 1051 * 10**24
    assert reserve.variable_borrow_index_ray == 1080 * 10**24
    assert reserve.current_liquidity_rate_ray == 145 * 10**23
    assert reserve.last_update_timestamp == NOW
    assert reserve.a_token == A_WETH
    assert reserve.variable_debt_token == V_WETH
    assert reserve.is_active and not reserve.is_paused and not reserve.is_frozen
    assert reserve.flashloan_enabled and not reserve.is_isolated


def test_an_incomplete_reserve_is_refused_not_defaulted() -> None:
    """A reserve with half its parameters would be priced on whatever defaults filled the other
    half. The bonus defaulting to 0 or the fee to 0 is a plausible-looking wrong number."""
    raw = _weth_raw()
    raw[2] = None  # protocol fee unreadable
    with pytest.raises(ReserveReadError, match="protocol fee"):
        _decode(WETH, "WETH", raw)


def test_a_debt_ceiling_marks_isolation() -> None:
    raw = _weth_raw()
    raw[5] = _payload([5_000_000_00])
    assert _decode(WETH, "WETH", raw).is_isolated


class _ReservePool:
    """Answers getReservesList, the eight data-provider reads, and the v3.2 eMode accessors."""

    def __init__(self) -> None:
        self.calls = 0
        self.raw = dict(zip(_SIGS, _weth_raw(), strict=True))

    async def call(self, method: str, params: list, *, policy=None) -> str:
        self.calls += 1
        data = str(params[0]["data"])
        sig = data[:10]
        if sig == selector("getReservesList()"):
            return "0x" + _word(32) + _word(3) + _addr(WETH) + _addr(USDC) + _addr(CBBTC)
        if sig == selector("getEModeCategoryCollateralConfig(uint8)"):
            return _payload([8000, 8300, 10400])
        if sig == selector("getEModeCategoryCollateralBitmap(uint8)"):
            return _payload([0b100])  # reserve id 2: cbBTC
        if sig == selector("getEModeCategoryBorrowableBitmap(uint8)"):
            return _payload([0b010])  # reserve id 1: USDC
        if sig in self.raw:
            value = self.raw[sig]
            if value is None:
                raise ProviderError("fake", "execution reverted")
            return str(value)
        raise AssertionError(f"unexpected {sig}")

    async def batch(self, calls: list) -> list[object]:
        return [await self.call(m, list(p)) for m, p in calls]


_SIGS = [
    selector(s)
    for s in (
        "getReserveConfigurationData(address)",
        "getReserveData(address)",
        "getLiquidationProtocolFee(address)",
        "getPaused(address)",
        "getReserveTokensAddresses(address)",
        "getDebtCeiling(address)",
        "getSiloedBorrowing(address)",
        "getFlashLoanEnabled(address)",
    )
]


def _reserve_reader(pool: _ReservePool) -> AaveReserveReader:
    reader = AaveReserveReader(
        venue=_registry_venue(),
        pool=pool,  # type: ignore[arg-type]
        pool_address=POOL,
        data_provider=PROVIDER,
    )
    reader._symbols.update({WETH: "WETH", USDC: "USDC", CBBTC: "cbBTC"})
    return reader


async def test_reserves_are_cached_for_the_ttl_then_reread() -> None:
    pool = _ReservePool()
    reader = _reserve_reader(pool)

    first = await reader.reserves(now=NOW)
    calls = pool.calls
    again = await reader.reserves(now=NOW + reader.ttl_seconds - 1)
    assert again is first and pool.calls == calls, "inside the TTL nothing is re-read"

    await reader.reserves(now=NOW + reader.ttl_seconds)
    assert pool.calls > calls


async def test_emode_sets_come_from_bitmaps_indexed_by_reserve_id() -> None:
    """Category 10 on Base: cbBTC collateral, USDC borrowable. The id is the position in
    getReservesList, which W1 verified against all fifteen Base reserves."""
    reader = _reserve_reader(_ReservePool())
    await reader.reserves(now=NOW)
    category = await reader.emode(10)

    assert category is not None
    assert category.liquidation_threshold_bps == 8300
    assert category.liquidation_bonus_bps == 10400
    assert category.collateral_assets == frozenset({CBBTC})
    assert category.borrowable_assets == frozenset({USDC})
    assert await reader.emode(0) is None


async def test_emode_before_reserves_is_an_error_not_an_empty_set() -> None:
    reader = _reserve_reader(_ReservePool())
    with pytest.raises(ReserveReadError, match="reserves"):
        await reader.emode(10)


# --------------------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------------------


def _reserve(asset: Address, symbol: str, decimals: int, threshold: int = 8300) -> ReserveState:
    return ReserveState(
        asset=asset,
        symbol=symbol,
        decimals=decimals,
        ltv_bps=8000,
        liquidation_threshold_bps=threshold,
        liquidation_bonus_bps=10500,
        liquidation_protocol_fee_bps=1000,
        liquidity_index_ray=RAY,
        variable_borrow_index_ray=RAY,
        current_liquidity_rate_ray=0,
        current_variable_borrow_rate_ray=0,
        last_update_timestamp=NOW,
    )


RESERVES = {WETH: _reserve(WETH, "WETH", 18), USDC: _reserve(USDC, "USDC", 6, 7800)}
PRICES = {WETH: 2_000 * 10**8, USDC: 10**8}


def _account(collateral_weth: int, debt_usdc: int) -> UserAccount:
    return UserAccount(
        address=BORROWER,
        reserves=(
            UserReserve(asset=WETH, scaled_a_token_balance=collateral_weth),
            UserReserve(asset=USDC, scaled_variable_debt=debt_usdc),
        ),
    )


def test_plan_sizes_at_the_close_factor_and_quotes_the_adverse_seizure() -> None:
    """10 WETH at $2,000 against 17,000 USDC: HF ~0.976, above 0.95, so 50% close factor.
    The quote is sized at the conservative seizure, which is larger — quoting the expected
    size would make the conservative case fail on exit liquidity for a reason that is ours."""
    policy = _registry_venue().protocol.close_factor
    plan = plan_account(
        _account(10 * 10**18, 17_000 * 10**6), RESERVES, PRICES, now=NOW, policy=policy
    )

    assert 0.95 * WAD < plan.valuation.health_factor_wad < WAD
    assert len(plan.pairs) == 1
    pair = plan.pairs[0]
    assert pair.close_factor_bps == 5000
    assert pair.debt_to_cover == 8_500 * 10**6
    assert pair.debt_to_cover_base == 8_500 * 10**8
    assert not pair.same_asset

    expected = calculate_seizure(
        debt_to_cover=pair.debt_to_cover,
        debt_reserve=RESERVES[USDC],
        collateral_reserve=RESERVES[WETH],
        debt_price=PRICES[USDC],
        collateral_price=PRICES[WETH],
        user_collateral_balance=pair.user_collateral,
    )
    # With the restated conservative scenario (exit risk is a proceeds haircut, not a price
    # shift), the adverse seizure no longer exceeds the expected one, so the quote size equals
    # the expected seizure. The plan still quotes `max(expected, adverse)`, so a custom scenario
    # that does shift prices would still widen it.
    assert pair.quote_amount == expected.collateral_to_liquidator


def test_plan_refuses_an_account_with_an_unpriced_leg() -> None:
    policy = _registry_venue().protocol.close_factor
    with pytest.raises(KeyError):
        plan_account(
            _account(10 * 10**18, 17_000 * 10**6),
            RESERVES,
            {WETH: PRICES[WETH]},
            now=NOW,
            policy=policy,
        )


def test_zero_threshold_collateral_is_not_seizable() -> None:
    """GHO and syrupUSDC on Base report a zero threshold: not collateral, never a pair."""
    reserves = dict(RESERVES)
    reserves[WETH] = _reserve(WETH, "WETH", 18, threshold=0)
    policy = _registry_venue().protocol.close_factor
    plan = plan_account(
        _account(10 * 10**18, 17_000 * 10**6), reserves, PRICES, now=NOW, policy=policy
    )
    assert plan.pairs == ()


# --------------------------------------------------------------------------------------
# follower hands liquidations through
# --------------------------------------------------------------------------------------


def _liquidation_log(user: Address, block: int, log_index: int = 3) -> dict:
    return {
        "topics": [
            events.TOPIC_LIQUIDATION_CALL,
            "0x" + _addr(WETH),
            "0x" + _addr(USDC),
            "0x" + _addr(user),
        ],
        "data": "0x" + _word(8_500 * 10**6) + _word(4 * 10**18) + _addr(LIQUIDATOR) + _word(0),
        "blockNumber": hex(block),
        "blockHash": "0x" + "ab" * 32,
        "transactionHash": "0x" + f"{block:064x}",
        "transactionIndex": hex(7),
        "logIndex": hex(log_index),
    }


class _LogPool:
    def __init__(self, logs: list[dict]) -> None:
        self.logs = logs

    async def get_logs(self, **_: object) -> list[dict]:
        return self.logs


async def test_scan_returns_decoded_liquidations_and_still_touches_the_owner(store) -> None:
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    follower = EventFollower(
        venue=_registry_venue(),
        pool=_LogPool([_liquidation_log(BORROWER, 500)]),  # type: ignore[arg-type]
        book=book,
        store=store,
        pool_address=POOL,
    )
    result = await follower.scan_range(400, 600)

    assert BORROWER in book.dirty
    assert len(result.liquidations) == 1
    liq = result.liquidations[0]
    assert (liq.user, liq.liquidator, liq.block_number) == (BORROWER, LIQUIDATOR, 500)
    assert liq.debt_to_cover == 8_500 * 10**6


async def test_an_undecodable_liquidation_loses_the_label_not_the_scan(store) -> None:
    trimmed = _liquidation_log(BORROWER, 500)
    del trimmed["transactionIndex"]
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    follower = EventFollower(
        venue=_registry_venue(),
        pool=_LogPool([trimmed]),  # type: ignore[arg-type]
        book=book,
        store=store,
        pool_address=POOL,
    )
    result = await follower.scan_range(400, 600)
    assert BORROWER in book.dirty
    assert result.liquidations == ()


# --------------------------------------------------------------------------------------
# labelling
# --------------------------------------------------------------------------------------


def _driver(store: EventStore) -> ShadowDriver:
    return ShadowDriver(
        venue=_registry_venue(),
        pool=None,  # type: ignore[arg-type]
        store=store,
        book=PositionBook(venue=VENUE, chain_id=ChainId(8453)),
        follower=None,  # type: ignore[arg-type]
        reader=None,  # type: ignore[arg-type]
        reserves=None,  # type: ignore[arg-type]
        oracle=None,  # type: ignore[arg-type]
        adapter=None,  # type: ignore[arg-type]
        quoters=[],
    )


def _open(store: EventStore, borrower: Address, block: int, bucket: str = "under_10k") -> int:
    return store.record_decision(
        DecisionRecord(
            opportunity_uid=f"{VENUE}:{borrower}:{block}",
            venue=VENUE,
            trace_id="t",
            block_number=block,
            block_hash=BlockHash("0x" + "ab" * 32),
            borrower=borrower,
            outcome="simulated",
            strategy_id=STRATEGY_ID,
            state_snapshot={"size_bucket": bucket},
        )
    )


def _liq(user: Address, block: int, log_index: int = 3) -> LiquidationCall:
    return LiquidationCall(
        block_number=block,
        block_hash="0x" + "ab" * 32,
        transaction_hash=TxHash("0x" + f"{block:064x}"),
        transaction_index=7,
        log_index=log_index,
        collateral_asset=WETH,
        debt_asset=USDC,
        user=user,
        debt_to_cover=8_500 * 10**6,
        liquidated_collateral_amount=4 * 10**18,
        liquidator=LIQUIDATOR,
        receive_a_token=False,
    )


def _outcomes(store: EventStore) -> list[tuple[str, int | None]]:
    return [(r["outcome"], r["blocks_after_detection"]) for r in store.shadow_outcome_rows(VENUE)]


def _label(driver: ShadowDriver, *liqs: LiquidationCall):
    from liqengine.shadow.driver import RoundReport

    report = RoundReport()
    driver._label_liquidations(liqs, driver.open_opportunities(), RESERVES, PRICES, report)
    return report


def test_a_flagged_account_liquidated_later_is_taken_with_our_lead(store) -> None:
    driver = _driver(store)
    _open(store, BORROWER, 1_000)

    report = _label(driver, _liq(BORROWER, 1_003))

    assert _outcomes(store) == [("taken", 3)]
    assert report.labels == {"taken": 1}
    assert driver.open_opportunities() == {}


def test_a_liquidation_we_never_flagged_is_missed_and_sized(store) -> None:
    driver = _driver(store)
    _label(driver, _liq(OTHER, 1_000))

    rows = store.shadow_outcome_rows(VENUE)
    assert [(r["outcome"], r["size_bucket"]) for r in rows] == [("missed", "under_10k")]


def test_a_liquidation_before_our_detection_is_a_miss_and_ours_stays_open(store) -> None:
    """We read the account after it had already been partly liquidated. That race happened
    without us; the opportunity we flagged is on what was left, and is still open."""
    driver = _driver(store)
    _open(store, BORROWER, 1_000)

    _label(driver, _liq(BORROWER, 999))

    assert _outcomes(store) == [("missed", None)]
    assert BORROWER in driver.open_opportunities()


def test_a_rescanned_liquidation_is_labelled_once(store) -> None:
    """A reorg rescan re-reads the same logs. The second sighting must be a no-op."""
    driver = _driver(store)
    _open(store, BORROWER, 1_000)
    _label(driver, _liq(BORROWER, 1_002))
    report = _label(driver, _liq(BORROWER, 1_002), _liq(OTHER, 1_002, log_index=9))
    report = _label(driver, _liq(OTHER, 1_002, log_index=9))

    assert sorted(o for o, _ in _outcomes(store)) == ["missed", "taken"]
    assert report.labels == {}


def test_a_cure_waits_for_the_confirmed_scan_to_pass_it(store) -> None:
    """The follower runs behind the head. Recovery read at block 1,010 could be a liquidation
    at 1,009 the scan has not reached, so the cure is held until it has."""
    from liqengine.shadow.driver import RoundReport

    driver = _driver(store)
    _open(store, BORROWER, 1_000)
    driver._pending_cure[f"{VENUE}:{BORROWER}:1000"] = 1_010

    driver._confirm_cures(driver.open_opportunities(), scanned_through=1_008, report=RoundReport())
    assert _outcomes(store) == []

    driver._confirm_cures(driver.open_opportunities(), scanned_through=1_010, report=RoundReport())
    assert _outcomes(store) == [("cured", 10)]


def test_a_liquidation_inside_the_lag_beats_the_pending_cure(store) -> None:
    driver = _driver(store)
    _open(store, BORROWER, 1_000)
    driver._pending_cure[f"{VENUE}:{BORROWER}:1000"] = 1_010

    _label(driver, _liq(BORROWER, 1_009))

    assert _outcomes(store) == [("taken", 9)]
    assert driver._pending_cure == {}


def test_an_opportunity_open_past_the_horizon_is_unclaimed(store) -> None:
    from liqengine.shadow.driver import RoundReport

    driver = _driver(store)
    _open(store, BORROWER, 1_000)

    driver._expire(driver.open_opportunities(), 1_000 + driver.horizon_blocks - 1, RoundReport())
    assert _outcomes(store) == []
    driver._expire(driver.open_opportunities(), 1_000 + driver.horizon_blocks, RoundReport())
    assert _outcomes(store) == [("unclaimed", driver.horizon_blocks)]


def test_open_opportunities_survive_a_restart(store) -> None:
    _open(store, BORROWER, 1_000, bucket="over_10k")
    restarted = _driver(store)
    op = restarted.open_opportunities()[BORROWER]
    assert (op.detected_block, op.size_bucket) == (1_000, "over_10k")


def test_outcome_record_rules() -> None:
    with pytest.raises(ValueError, match="missed"):
        ShadowOutcomeRecord(
            venue=VENUE,
            borrower=BORROWER,
            outcome="missed",
            resolved_block=1,
            opportunity_uid="x",
        )
    with pytest.raises(ValueError, match="name the opportunity"):
        ShadowOutcomeRecord(venue=VENUE, borrower=BORROWER, outcome="taken", resolved_block=1)
    with pytest.raises(ValueError, match="outcome must be"):
        ShadowOutcomeRecord(venue=VENUE, borrower=BORROWER, outcome="won", resolved_block=1)


# --------------------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------------------


def _row(outcome: str, bucket: str, block: int, lead: int | None = 2) -> dict:
    return {
        "outcome": outcome,
        "size_bucket": bucket,
        "resolved_block": block,
        "blocks_after_detection": lead if outcome != "missed" else None,
    }


def test_nothing_yet_is_not_ready() -> None:
    readiness = assess([])
    assert not readiness.ready
    assert all(not c.passed for c in readiness.checks)


def test_ready_when_all_four_pass() -> None:
    day = 43_200
    rows = [_row("taken", "under_10k", i * day // 10) for i in range(70)]
    rows += [_row("unclaimed", "under_1k", 8 * day) for _ in range(40)]
    rows += [_row("missed", "under_10k", 3 * day) for _ in range(20)]
    readiness = assess(rows)

    assert readiness.ready, [(c.name, c.have, c.need) for c in readiness.checks]
    assert readiness.race_by_bucket == {"under_10k": 70, "under_1k": 40}


def test_dust_and_cures_do_not_count_as_race_labels() -> None:
    """Sub-$100 liquidations are a market nobody contests, and a cure had no race in it."""
    day = 43_200
    rows = [_row("taken", "under_100", i * day // 10) for i in range(200)]
    rows += [_row("cured", "under_10k", 9 * day) for _ in range(200)]
    readiness = assess(rows)

    labels = next(c for c in readiness.checks if c.name.startswith("race labels"))
    assert labels.have == 0
    assert not readiness.ready


def test_low_coverage_blocks_readiness_even_with_enough_labels() -> None:
    """If most real liquidations are ones we never flagged, our labels are the easy subset."""
    day = 43_200
    rows = [_row("taken", "under_10k", i * day // 10) for i in range(110)]
    rows += [_row("missed", "under_10k", 5 * day) for _ in range(200)]
    readiness = assess(rows)

    coverage = next(c for c in readiness.checks if c.name == "detection coverage")
    assert not coverage.passed
    assert not readiness.ready


def test_one_burst_is_not_enough_however_many_labels() -> None:
    rows = [_row("taken", "under_10k", 1_000 + i) for i in range(500)]
    readiness = assess(rows)
    span = next(c for c in readiness.checks if c.name.startswith("calendar"))
    assert not span.passed
    assert not readiness.ready


def test_lead_counts_split_same_block_from_ahead() -> None:
    rows = [_row("taken", "under_10k", 10, lead=0), _row("taken", "under_10k", 20, lead=4)]
    readiness = assess(rows)
    assert (readiness.taken_with_lead, readiness.taken_same_block) == (1, 1)


# --------------------------------------------------------------------------------------
# restart safety
# --------------------------------------------------------------------------------------


def test_the_unread_set_round_trips_through_the_store(store) -> None:
    store.save_dirty(VENUE, [BORROWER, OTHER])
    assert store.load_dirty(VENUE) == sorted([BORROWER, OTHER])
    store.save_dirty(VENUE, [OTHER])
    assert store.load_dirty(VENUE) == [OTHER], "replaced, not appended"
    assert store.load_dirty("aave_v3_arbitrum") == []


async def test_a_round_that_stops_early_still_saves_the_unread_set(store) -> None:
    """The first live run: backfill touched 3,039 accounts, round 1 hit a rate limit before
    its save step, and a restart found the cursor written and the accounts gone."""

    class _Throttled:
        async def follow_once(self, **_: object):
            raise ProviderError("primary", "rate limited by provider")

    class _NoReads:
        async def read_accounts(self, *_: object, **__: object):
            raise ProviderError("primary", "rate limited by provider")

    class _NoHead:
        async def block_by_number(self, *_: object, **__: object):
            raise ProviderError("primary", "rate limited by provider")

    driver = _driver(store)
    driver.follower = _Throttled()  # type: ignore[assignment]
    driver.reader = _NoReads()  # type: ignore[assignment]
    driver.pool = _NoHead()  # type: ignore[assignment]
    driver.book.touch_all([BORROWER, OTHER])

    report = await driver.run_round()

    assert report.rate_limited
    assert store.load_dirty(VENUE) == sorted([BORROWER, OTHER])


def test_same_asset_conservative_keeps_adverse_costs_but_no_price_shift() -> None:
    """USDC collateral against USDC debt: the first live smoke run priced the conservative
    case above the expected one, because the default scenario moved one token two ways."""
    from liqengine.shadow.driver import SAME_ASSET_CONSERVATIVE
    from liqengine.simulate.inputs import CONSERVATIVE

    assert SAME_ASSET_CONSERVATIVE.collateral_price_drop_bps == 0
    assert SAME_ASSET_CONSERVATIVE.debt_price_rise_bps == 0
    assert SAME_ASSET_CONSERVATIVE.gas_price_multiple_bps == CONSERVATIVE.gas_price_multiple_bps
    assert SAME_ASSET_CONSERVATIVE.gas_limit_multiple_bps == CONSERVATIVE.gas_limit_multiple_bps
    assert SAME_ASSET_CONSERVATIVE.use_worst_slippage


# --------------------------------------------------------------------------------------
# backfill: resumable, rate-limit tolerant, never loses what it found
# --------------------------------------------------------------------------------------


class _BackfillFollower:
    """Touches one synthetic account per chunk; can fail a set number of times first."""

    def __init__(self, book: PositionBook, head: int, failures: int = 0) -> None:
        from liqengine.ingest.pool import CallPolicy

        self.book = book
        self._head = head
        self.failures = failures
        self.confirmations = 2
        self.log_policy = CallPolicy.BULK
        self.prefer_wide_logs = False
        self.policies: list[object] = []
        self.ranges: list[tuple[int, int]] = []

    async def head(self) -> int:
        return self._head

    async def scan_range(self, start: int, end: int):
        from liqengine.positions.follow import ScanResult

        self.policies.append(self.log_policy)
        if self.failures:
            self.failures -= 1
            raise ProviderError("primary", "eth_getLogs: rate limited by provider")
        self.ranges.append((start, end))
        self.book.touch(to_address(f"0x{start:040x}"))
        return ScanResult(start, end, 1, 1)


def _backfill_driver(store, head: int, failures: int = 0, monkeypatch=None) -> ShadowDriver:
    driver = _driver(store)
    driver.follower = _BackfillFollower(driver.book, head, failures)  # type: ignore[assignment]
    driver.backfill_chunk_blocks = 1_000
    driver.backfill_pause_seconds = 0
    driver.catch_up_blocks = 0
    return driver


async def test_backfill_saves_found_accounts_with_every_checkpoint(store) -> None:
    from liqengine.positions.follow import STREAM

    base = 3_000_000  # above Base's deployment block, which the backfill floors at
    driver = _backfill_driver(store, head=base + 3_002)
    await driver.backfill(base)

    cursor = store.get_cursor(VENUE, STREAM)
    assert (cursor.from_block, cursor.scanned_through) == (base, base + 3_000)
    assert len(store.load_dirty(VENUE)) == 4, "one account per chunk, all saved"


async def test_backfill_resumes_from_the_cursor_and_uses_wide_window_providers(store) -> None:
    """The user's run: rate limited a third of the way through. A rerun continues, it does not
    restart, and it does not hand the rest of the gap to the 16-block BULK providers."""
    from liqengine.ingest.pool import CallPolicy
    from liqengine.positions.follow import STREAM

    store.set_cursor(VENUE, STREAM, 1_000, 2_999)
    driver = _backfill_driver(store, head=6_002)
    driver.book.touch(BORROWER)  # membership survived, so this is a resume, not a rescan

    await driver.backfill(1_000)

    assert driver.follower.ranges[0][0] == 3_000
    assert set(driver.follower.policies) == {CallPolicy.FAST}
    assert driver.follower.log_policy is CallPolicy.BULK, "restored to what it was"
    assert driver.follower.prefer_wide_logs is False
    assert store.get_cursor(VENUE, STREAM).scanned_through == 6_000


async def test_a_cursor_with_no_saved_accounts_is_rescanned(store) -> None:
    """The exact state the failed run left: cursor written, accounts never saved."""
    from liqengine.positions.follow import STREAM

    store.set_cursor(VENUE, STREAM, 1_000, 2_999)
    driver = _backfill_driver(store, head=3_502)

    await driver.backfill(1_000)

    assert driver.follower.ranges[0][0] == 1_000
    assert store.load_dirty(VENUE)


async def test_backfill_is_a_noop_close_to_the_head(store) -> None:
    from liqengine.positions.follow import STREAM

    store.set_cursor(VENUE, STREAM, 1_000, 9_000)
    driver = _backfill_driver(store, head=9_500)
    driver.catch_up_blocks = 2_000
    driver.book.touch(BORROWER)
    assert await driver.backfill(1_000) is None
    assert driver.follower.ranges == []


async def test_backfill_retries_a_rate_limited_chunk(store, monkeypatch) -> None:
    import liqengine.shadow.driver as driver_module
    from liqengine.positions.follow import STREAM

    monkeypatch.setattr(driver_module, "BACKFILL_FIRST_RETRY_SECONDS", 0.0)
    store.set_cursor(VENUE, STREAM, 1_000, 2_999)
    driver = _backfill_driver(store, head=4_002, failures=3)
    driver.book.touch(BORROWER)

    await driver.backfill(1_000)
    assert store.get_cursor(VENUE, STREAM).scanned_through == 4_000


async def test_a_provider_that_stays_down_raises_with_progress_kept(store, monkeypatch) -> None:
    import liqengine.shadow.driver as driver_module
    from liqengine.positions.follow import STREAM

    monkeypatch.setattr(driver_module, "BACKFILL_FIRST_RETRY_SECONDS", 0.0)
    store.set_cursor(VENUE, STREAM, 1_000, 2_999)
    driver = _backfill_driver(store, head=6_002, failures=0)
    driver.book.touch(BORROWER)

    original = driver.follower.scan_range
    calls = {"n": 0}

    async def flaky(start: int, end: int):
        calls["n"] += 1
        if start >= 4_000:
            raise ProviderError("primary", "rate limited by provider")
        return await original(start, end)

    driver.follower.scan_range = flaky  # type: ignore[method-assign]
    with pytest.raises(ProviderError):
        await driver.backfill(1_000)
    assert store.get_cursor(VENUE, STREAM).scanned_through == 3_999, "the good chunk is kept"


# --------------------------------------------------------------------------------------
# the watchlist survives a restart
# --------------------------------------------------------------------------------------


def _reconciled(borrower: Address, hf_wad: int, block: int = 1_000):
    from liqengine.core.types import BlockRef
    from liqengine.positions.book import ReconciledAccount

    return ReconciledAccount(
        account=_account(10 * 10**18, 17_000 * 10**6).__class__(
            address=borrower,
            reserves=_account(10 * 10**18, 17_000 * 10**6).reserves,
        ),
        health_factor_wad=hf_wad,
        at_block=BlockRef(
            chain_id=ChainId(8453),
            number=block,
            hash=BlockHash("0x" + "ab" * 32),
            parent_hash=BlockHash("0x" + "cd" * 32),
            timestamp=NOW,
        ),
    )


def test_the_health_factor_persists_with_the_position(store) -> None:
    """The first live session read 1,962 accounts and saved none of their health factors, so
    a restart would have had no idea which to read first."""
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    book.apply_reconciliation(_reconciled(BORROWER, 1_015 * 10**15), RESERVES, PRICES)
    book.apply_reconciliation(_reconciled(OTHER, 3 * WAD), RESERVES, PRICES)
    assert book.persist(store) == 2

    reloaded = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    assert reloaded.load(store) == 2
    assert reloaded.reconciled_health_factor(BORROWER) == 1_015 * 10**15
    assert reloaded.reconciled_health_factor(OTHER) == 3 * WAD


async def test_start_rebuilds_the_watchlist_from_saved_health_factors(store) -> None:
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    book.apply_reconciliation(_reconciled(BORROWER, 1_015 * 10**15), RESERVES, PRICES)
    book.apply_reconciliation(_reconciled(OTHER, 3 * WAD), RESERVES, PRICES)
    book.persist(store)

    driver = _driver(store)
    await driver.start()

    assert driver._last_hf == {BORROWER: 1_015 * 10**15}, "near liquidation: watched"
    assert driver._targets({})[0] == BORROWER, "and read first"


# --------------------------------------------------------------------------------------
# the read queue: borrowers first, suppliers not re-read
# --------------------------------------------------------------------------------------


def _event_log(topic: str, user: Address) -> dict:
    topics = [topic, "0x" + _addr(USDC), "0x" + _addr(user)]
    if topic in (events.TOPIC_SUPPLY, events.TOPIC_BORROW):
        topics.append("0x" + "00" * 32)
    if topic in (events.TOPIC_WITHDRAW, events.TOPIC_REPAY):
        topics.append("0x" + _addr(user))
    return {"topics": topics, "data": "0x" + "00" * 128}


def test_likely_borrowers_are_read_before_suppliers() -> None:
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    supplier = to_address("0x" + "01" * 20)
    borrower = to_address("0x" + "ff" * 20)  # sorts last alphabetically
    book.touch(supplier)
    book.touch(borrower, likely_debt=True)

    assert book.dirty_ordered() == [borrower, supplier]
    assert {b: p for b, p, _ in book.dirty_entries()} == {supplier: 0, borrower: 1}


async def test_a_supply_by_a_known_debt_free_account_is_not_re_read(store) -> None:
    """Two in three accounts touching Aave on Base only supply. Re-reading one every time it
    deposits spends the paced budget on an account that cannot be liquidated; it is read
    again only when an event implies debt."""
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    debt_free = UserAccount(
        address=BORROWER, reserves=(UserReserve(asset=USDC, scaled_a_token_balance=5),)
    )
    book.apply_reconciliation(
        _reconciled(BORROWER, 3 * WAD).__class__(
            account=debt_free,
            health_factor_wad=_reconciled(BORROWER, 3 * WAD).health_factor_wad,
            at_block=_reconciled(BORROWER, 3 * WAD).at_block,
        ),
        RESERVES,
        PRICES,
    )
    assert book.known_debt_free(BORROWER)

    follower = EventFollower(
        venue=_registry_venue(),
        pool=_LogPool([_event_log(events.TOPIC_SUPPLY, BORROWER)]),  # type: ignore[arg-type]
        book=book,
        store=store,
        pool_address=POOL,
    )
    result = await follower.scan_range(1, 10)
    assert BORROWER not in book.dirty
    assert result.owners_skipped == 1

    follower.pool = _LogPool([_event_log(events.TOPIC_BORROW, BORROWER)])  # type: ignore[assignment]
    result = await follower.scan_range(11, 20)
    assert BORROWER in book.dirty
    assert book.dirty_ordered() == [BORROWER]
    assert result.owners_skipped == 0


async def test_an_unknown_account_is_read_whatever_the_event(store) -> None:
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    follower = EventFollower(
        venue=_registry_venue(),
        pool=_LogPool([_event_log(events.TOPIC_SUPPLY, OTHER)]),  # type: ignore[arg-type]
        book=book,
        store=store,
        pool_address=POOL,
    )
    await follower.scan_range(1, 10)
    assert OTHER in book.dirty
    assert {b: p for b, p, _ in book.dirty_entries()}[OTHER] == 0


def test_queue_priority_round_trips_through_the_store(store) -> None:
    store.save_dirty(VENUE, [(OTHER, 0), (BORROWER, 1), to_address("0x" + "02" * 20)])
    entries = store.load_dirty_entries(VENUE)
    assert entries[0] == (BORROWER, 1, None), "likely borrowers come back first"
    assert {p for _, p, _ in entries[1:]} == {0}


async def test_start_restores_queue_priority(store) -> None:
    store.save_dirty(VENUE, [(OTHER, 0), (BORROWER, 1)])
    driver = _driver(store)
    await driver.start()
    assert driver.book.dirty_ordered() == [BORROWER, OTHER]


def test_a_v2_store_gains_the_priority_column(tmp_path) -> None:
    """The user's live store predates the column. Opening it must add the column, not fail
    on the first INSERT that names it."""
    import sqlite3

    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (2, 'x');
        CREATE TABLE dirty_account (
            venue TEXT NOT NULL, borrower TEXT NOT NULL, PRIMARY KEY (venue, borrower));
        INSERT INTO dirty_account VALUES ('aave_v3_base', '0xabc');
        """
    )
    conn.commit()
    conn.close()

    with EventStore(path) as s:
        s.initialise()
        assert s.load_dirty_entries(VENUE) == [("0xabc", 0, None)]
        s.save_dirty(VENUE, [("0xdef", 1, 52_000_000)])
        assert s.load_dirty_entries(VENUE) == [("0xdef", 1, 52_000_000)]


def test_a_full_watchlist_cannot_starve_the_queue(store) -> None:
    """The live session: 15 accounts below HF 1.10, 15 reads per round, queue never moved."""
    driver = _driver(store)
    driver.max_reads = 15
    for i in range(15):
        driver._last_hf[to_address(f"0x{0xA000 + i:040x}")] = 105 * 10**16  # near, not urgent
    queued = [to_address(f"0x{0xB000 + i:040x}") for i in range(40)]
    driver.book.touch_all(queued)

    for round_index in range(1, 5):
        driver._round_index = round_index
        targets = driver._targets({})
        assert len(targets) == 15
        assert sum(1 for t in targets if t in queued) >= 5, "a third is always the queue's"


def test_urgent_accounts_are_read_every_round_and_near_ones_rotate(store) -> None:
    driver = _driver(store)
    driver.max_reads = 15
    urgent = to_address("0x" + "aa" * 20)
    driver._last_hf[urgent] = 101 * 10**16
    near = [to_address(f"0x{0xC000 + i:040x}") for i in range(8)]
    for b in near:
        driver._last_hf[b] = 108 * 10**16

    seen: dict[Address, int] = {b: 0 for b in near}
    for round_index in range(1, 9):
        driver._round_index = round_index
        targets = driver._targets({})
        assert targets[0] == urgent, "urgent first, every round"
        for b in near:
            seen[b] += b in targets
    assert all(count == 2 for count in seen.values()), "each near account once per 4 rounds"


def test_open_opportunities_come_first_whatever_the_cap(store) -> None:
    driver = _driver(store)
    driver.max_reads = 3
    _open(store, BORROWER, 1_000)
    driver._last_hf[OTHER] = 101 * 10**16
    targets = driver._targets(driver.open_opportunities())
    assert targets[0] == BORROWER


# --------------------------------------------------------------------------------------
# restart: never re-read what a later read already covered
# --------------------------------------------------------------------------------------


def _seed_read(store, borrower: Address, hf_wad: int, block: int = 1_000) -> None:
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    book.apply_reconciliation(_reconciled(borrower, hf_wad, block), RESERVES, PRICES)
    book.persist(store)


async def test_restart_rechecks_only_borrowers_near_the_line(store) -> None:
    """The second live session re-queued all ~795 known borrowers on every start and spent
    its first hour re-reading them. Only accounts close enough for price alone to matter."""
    healthy = to_address("0x" + "11" * 20)
    close = to_address("0x" + "22" * 20)
    _seed_read(store, healthy, 2 * WAD)
    _seed_read(store, close, 115 * 10**16)

    driver = _driver(store)
    await driver.start()

    assert close in driver.book.dirty
    assert healthy not in driver.book.dirty
    assert driver.restart_rechecked == 1


async def test_a_saved_entry_queued_before_the_last_read_is_dropped(store) -> None:
    _seed_read(store, BORROWER, 2 * WAD, block=1_000)
    store.save_dirty(VENUE, [(BORROWER, 1, 990)])

    driver = _driver(store)
    await driver.start()

    assert BORROWER not in driver.book.dirty
    assert driver.restart_dropped == 1


async def test_a_saved_entry_queued_after_the_last_read_is_kept(store) -> None:
    """A Borrow after the read can have moved the health factor; that one must be read."""
    _seed_read(store, BORROWER, 2 * WAD, block=1_000)
    store.save_dirty(VENUE, [(BORROWER, 1, 1_050)])

    driver = _driver(store)
    await driver.start()

    assert BORROWER in driver.book.dirty
    assert driver.restart_dropped == 0


async def test_legacy_entries_for_read_healthy_accounts_are_dropped(store) -> None:
    """The user's file: ~680 already-read borrowers sit in the queue with no block recorded,
    put there by the old restart rule. Healthy ones go; one still near the line stays; an
    account never read stays whatever its entry says."""
    healthy = to_address("0x" + "11" * 20)
    close = to_address("0x" + "22" * 20)
    never_read = to_address("0x" + "33" * 20)
    _seed_read(store, healthy, 2 * WAD)
    _seed_read(store, close, 115 * 10**16)
    store.save_dirty(VENUE, [(healthy, 1), (close, 1), (never_read, 0)])

    driver = _driver(store)
    await driver.start()

    assert healthy not in driver.book.dirty
    assert close in driver.book.dirty
    assert never_read in driver.book.dirty
    assert driver.restart_dropped == 1


async def test_events_record_the_block_that_queued_an_account(store) -> None:
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    follower = EventFollower(
        venue=_registry_venue(),
        pool=_LogPool([_liquidation_log(BORROWER, 777)]),  # type: ignore[arg-type]
        book=book,
        store=store,
        pool_address=POOL,
    )
    await follower.scan_range(700, 800)
    assert {b: q for b, _, q in book.dirty_entries()}[BORROWER] == 777


def test_a_read_clears_the_queued_block(store) -> None:
    book = PositionBook(venue=VENUE, chain_id=ChainId(8453))
    book.touch(BORROWER, at_block=999)
    book.apply_reconciliation(_reconciled(BORROWER, 2 * WAD, 1_000), RESERVES, PRICES)
    assert book.dirty_entries() == []
    assert book.read_block(BORROWER) == 1_000
