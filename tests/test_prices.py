"""W3: oracle prices, freshness, and the L2 sequencer gate.

Verified against live Base alongside these tests: all 15 reserves priced, and for the four
whose oracle source is a plain Chainlink aggregator, the feed's own `latestRoundData` answer
equals `AaveOracle.getAssetPrice` exactly — which is what validates source resolution rather
than assuming it. The other eleven sources revert on `latestRoundData` (capped and composite
adapters), which is why `updated_at` is optional rather than a field we could require.
"""

from __future__ import annotations

import pytest

from liqengine.config.loader import load_registry
from liqengine.core.abi import selector
from liqengine.core.errors import HaltCondition, LiqEngineError, ProviderError
from liqengine.core.types import Address, BlockHash, BlockRef, ChainId, to_address
from liqengine.protocols.aave_v3.prices import (
    DEFAULT_MAX_PRICE_AGE_SECONDS,
    AaveOracleReader,
    PriceQuote,
    SequencerStatus,
    usable_prices,
)

ORACLE = to_address("0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156")
POOL = to_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")
PINNED_DATA_PROVIDER = to_address("0x0f43731eb8d45a581f4a36dd74f5f358bc90c73a")

WETH = to_address("0x4200000000000000000000000000000000000006")
USDC = to_address("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
CBETH = to_address("0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22")

WETH_FEED = to_address("0x9da00d23465282005db222a441a663ee7b9dfcc8")
USDC_FEED = to_address("0xf52d010c7d4ecbfda92c2509900593ce34535d86")
#: cbETH's source on Base is a capped adapter: it prices, but reverts on latestRoundData.
CBETH_ADAPTER = to_address("0xb6e9fbba669763bd811b10a761110c449824b5bb")

#: Base's configured sequencer uptime feed.
UPTIME_FEED = to_address("0xBCF85224fc0756B9Fa45aA7892530B47e10b6433")

BASE_UNIT = 100_000_000  # config: base_currency_unit
NOW = 1_800_000_000


def _uint(value: int) -> str:
    return f"{value:064x}"


def _addr_word(address: str) -> str:
    return "00" * 12 + address.removeprefix("0x")


def _round_data(*, answer: int, started_at: int, updated_at: int) -> str:
    """(roundId, answer, startedAt, updatedAt, answeredInRound)."""
    return "0x" + "".join(_uint(v) for v in (1, answer, started_at, updated_at, 1))


# --------------------------------------------------------------------------------------
# PriceQuote
# --------------------------------------------------------------------------------------


def test_a_price_older_than_the_threshold_is_stale() -> None:
    quote = PriceQuote(asset=WETH, price=1, at_block=1, updated_at=NOW - 100_000)
    assert quote.age_seconds(NOW) == 100_000
    assert quote.is_stale(NOW)


def test_a_recent_price_is_not_stale() -> None:
    quote = PriceQuote(asset=WETH, price=1, at_block=1, updated_at=NOW - 300)
    assert not quote.is_stale(NOW)


def test_unknown_freshness_is_not_stale_and_that_is_a_decision() -> None:
    """Eleven of Base's fifteen reserves report no `updatedAt` because their oracle source is
    an adapter rather than an aggregator. Treating unknown as stale would silently drop that
    whole class of collateral — including wstETH and cbETH — out of valuation."""
    quote = PriceQuote(asset=CBETH, price=3_073_511_300, at_block=1, updated_at=None)
    assert quote.age_seconds(NOW) is None
    assert not quote.is_stale(NOW)


def test_a_zero_price_is_not_a_free_asset() -> None:
    """Aave returns 0 for an asset it cannot price. Carrying that into a valuation would make
    collateral worthless and debt free, in opposite and equally wrong directions."""
    assert not PriceQuote(asset=WETH, price=0, at_block=1).priced
    assert PriceQuote(asset=WETH, price=1, at_block=1).priced


def test_a_clock_behind_the_feed_does_not_produce_a_negative_age() -> None:
    quote = PriceQuote(asset=WETH, price=1, at_block=1, updated_at=NOW + 60)
    assert quote.age_seconds(NOW) == 0
    assert not quote.is_stale(NOW)


# --------------------------------------------------------------------------------------
# usable_prices
# --------------------------------------------------------------------------------------


def test_usable_prices_drops_stale_and_unpriced_but_keeps_unknown_freshness() -> None:
    quotes = {
        WETH: PriceQuote(asset=WETH, price=100, at_block=1, updated_at=NOW - 60),
        USDC: PriceQuote(asset=USDC, price=100, at_block=1, updated_at=NOW - 200_000),
        CBETH: PriceQuote(asset=CBETH, price=100, at_block=1, updated_at=None),
        to_address("0x" + "11" * 20): PriceQuote(
            asset=to_address("0x" + "11" * 20), price=0, at_block=1, updated_at=NOW
        ),
    }
    fit = usable_prices(quotes, NOW)

    assert set(fit) == {WETH, CBETH}
    assert fit[WETH] == 100


def test_dropping_a_price_degrades_to_invisibility_not_a_wrong_valuation() -> None:
    """`PositionBook.project` skips an account whose collateral has no price. Excluding a dead
    feed here therefore means "we cannot see those accounts", which is true, rather than
    valuing them at a price nobody is honouring."""
    stale = {USDC: PriceQuote(asset=USDC, price=100, at_block=1, updated_at=NOW - 10**9)}
    assert usable_prices(stale, NOW) == {}


def test_the_staleness_threshold_is_caller_supplied() -> None:
    quotes = {WETH: PriceQuote(asset=WETH, price=5, at_block=1, updated_at=NOW - 7_200)}
    assert usable_prices(quotes, NOW) == {WETH: 5}
    assert usable_prices(quotes, NOW, max_age_seconds=3_600) == {}


def test_the_default_threshold_tolerates_a_chainlink_heartbeat() -> None:
    """Majors on Base run a ~24h heartbeat, so a tighter default would flag healthy feeds in
    quiet markets — constantly, and precisely when nothing is happening anyway."""
    assert DEFAULT_MAX_PRICE_AGE_SECONDS > 86_400


# --------------------------------------------------------------------------------------
# SequencerStatus
# --------------------------------------------------------------------------------------


def test_a_down_sequencer_blocks_liquidations() -> None:
    status = SequencerStatus(
        up=False, status_since=NOW - 10_000, grace_period_seconds=3_600, at_block=1
    )
    assert not status.liquidations_allowed(NOW)


def test_a_sequencer_back_up_inside_the_grace_period_still_blocks() -> None:
    """The clause that is easy to drop. Aave reverts every liquidation during the grace period,
    so without this a simulator prices a queue of opportunities that cannot be taken — at
    exactly the moment a backlog of underwater accounts exists."""
    status = SequencerStatus(
        up=True, status_since=NOW - 30, grace_period_seconds=3_600, at_block=1
    )
    assert not status.liquidations_allowed(NOW)


def test_a_sequencer_up_beyond_the_grace_period_allows_liquidations() -> None:
    status = SequencerStatus(
        up=True, status_since=NOW - 7_200, grace_period_seconds=3_600, at_block=1
    )
    assert status.liquidations_allowed(NOW)


def test_the_grace_boundary_is_exclusive() -> None:
    """Aave's own check is `>`, not `>=`."""
    status = SequencerStatus(
        up=True, status_since=NOW - 3_600, grace_period_seconds=3_600, at_block=1
    )
    assert not status.liquidations_allowed(NOW)


# --------------------------------------------------------------------------------------
# The reader
# --------------------------------------------------------------------------------------


class _FakePool:
    def __init__(
        self,
        *,
        prices: dict[Address, int] | None = None,
        sources: dict[Address, Address] | None = None,
        rounds: dict[Address, str] | None = None,
        base_unit: int | None = BASE_UNIT,
        uptime: str | None = None,
    ) -> None:
        self.prices = prices or {}
        self.sources = sources or {}
        self.rounds = rounds or {}
        self.base_unit = base_unit
        self.uptime = uptime
        self.tags: list[str] = []
        self.round_calls: list[Address] = []
        self.batches: list[int] = []
        self.batch_broken = False
        self.uptime_answers = False

    async def block_by_number(self, number, *, policy=None) -> BlockRef:
        return BlockRef(
            chain_id=ChainId(8453),
            number=52_000_000 if number == "latest" else int(number),
            hash=BlockHash("0x" + "ab" * 32),
            parent_hash=BlockHash("0x" + "cd" * 32),
            timestamp=NOW,
        )

    async def call(self, method: str, params: list, *, policy=None) -> str:
        assert method == "eth_call"
        self.tags.append(str(params[1]))
        return self._dispatch(to_address(str(params[0]["to"])), str(params[0]["data"]))

    async def batch(self, calls: list) -> list[object]:
        if self.batch_broken:
            raise ProviderError("fake", "batching unsupported")
        self.batches.append(len(calls))
        return [await self.call(m, list(p)) for m, p in calls]

    def _dispatch(self, target: Address, data: str) -> str:
        sig = data[:10]
        arg = "0x" + data[34:74] if len(data) >= 74 else None

        if sig == selector("getPool()"):
            return "0x" + _addr_word(POOL)
        if sig == selector("getPriceOracle()"):
            return "0x" + _addr_word(ORACLE)
        if sig == selector("getPoolDataProvider()"):
            return "0x" + _addr_word(PINNED_DATA_PROVIDER)
        if sig == selector("BASE_CURRENCY_UNIT()"):
            if self.base_unit is None:
                raise ProviderError("fake", "not implemented")
            return "0x" + _uint(self.base_unit)
        if sig == selector("getSourceOfAsset(address)"):
            asset = to_address(str(arg))
            source = self.sources.get(asset)
            return "0x" + _addr_word(source or "0x" + "00" * 20)
        if sig == selector("getAssetPrice(address)"):
            return "0x" + _uint(self.prices.get(to_address(str(arg)), 0))
        if sig == selector("latestRoundData()"):
            self.round_calls.append(target)
            if target == UPTIME_FEED:
                if self.uptime is None:
                    raise ProviderError("fake", "no uptime feed here")
                return self.uptime
            payload = self.rounds.get(target)
            if payload is None:
                raise ProviderError("fake", "execution reverted")
            return payload
        raise AssertionError(f"unexpected call {sig} to {target}")


def _reader(pool: _FakePool, **kwargs) -> AaveOracleReader:
    registry = load_registry("config", environ={})
    return AaveOracleReader(
        venue=registry.venue("aave_v3_base"),
        pool=pool,  # type: ignore[arg-type]
        oracle_address=ORACLE,
        **kwargs,
    )


async def test_prices_and_freshness_are_read_together() -> None:
    pool = _FakePool(
        prices={WETH: 269_472_000_000, USDC: 100_000_000},
        sources={WETH: WETH_FEED, USDC: USDC_FEED},
        rounds={
            WETH_FEED: _round_data(answer=269_472_000_000, started_at=0, updated_at=NOW - 300),
            USDC_FEED: _round_data(answer=100_000_000, started_at=0, updated_at=NOW - 60),
        },
    )
    quotes = await _reader(pool).read_prices([WETH, USDC])

    assert quotes[WETH].price == 269_472_000_000
    assert quotes[WETH].source == WETH_FEED
    assert quotes[WETH].updated_at == NOW - 300
    assert quotes[USDC].price == 100_000_000


async def test_a_source_that_reverts_yields_unknown_freshness_not_a_failure() -> None:
    """cbETH on Base: the capped adapter prices the asset but is not an aggregator. Losing the
    price because the freshness read failed would be the wrong trade-off."""
    pool = _FakePool(
        prices={CBETH: 307_351_130_000},
        sources={CBETH: CBETH_ADAPTER},
        rounds={},  # the adapter reverts
    )
    quotes = await _reader(pool).read_prices([CBETH])

    assert quotes[CBETH].price == 307_351_130_000
    assert quotes[CBETH].source == CBETH_ADAPTER
    assert quotes[CBETH].updated_at is None


async def test_an_asset_with_no_source_is_still_priced() -> None:
    pool = _FakePool(prices={WETH: 1_000}, sources={})
    quotes = await _reader(pool).read_prices([WETH])
    assert quotes[WETH].price == 1_000
    assert quotes[WETH].source is None
    assert quotes[WETH].updated_at is None


async def test_every_call_is_pinned_to_one_block() -> None:
    pool = _FakePool(
        prices={WETH: 1, USDC: 1},
        sources={WETH: WETH_FEED},
        rounds={WETH_FEED: _round_data(answer=1, started_at=0, updated_at=NOW)},
    )
    await _reader(pool).read_prices([WETH, USDC], block=52_000_123)

    pinned = [t for t in pool.tags if t != "latest"]
    assert pinned
    assert set(pinned) == {hex(52_000_123)}


async def test_a_base_currency_unit_mismatch_halts() -> None:
    """Every valuation in the system is denominated in this unit. A mismatch makes every health
    factor and every profit figure wrong by a power of ten, and nothing else would notice."""
    pool = _FakePool(prices={WETH: 1}, base_unit=10**18)
    with pytest.raises(HaltCondition, match="BASE_CURRENCY_UNIT"):
        await _reader(pool).read_prices([WETH])


async def test_an_oracle_that_does_not_expose_the_unit_is_not_a_mismatch() -> None:
    """Absence of evidence. Halting here would refuse to run against a legitimate deployment
    that simply does not implement the getter."""
    pool = _FakePool(prices={WETH: 7}, base_unit=None)
    quotes = await _reader(pool).read_prices([WETH])
    assert quotes[WETH].price == 7


async def test_sources_are_cached_across_reads() -> None:
    pool = _FakePool(prices={WETH: 1}, sources={WETH: WETH_FEED}, rounds={})
    reader = _reader(pool)
    await reader.read_prices([WETH])
    priming = sum(1 for t in pool.tags if t == "latest")

    await reader.read_prices([WETH])
    assert sum(1 for t in pool.tags if t == "latest") == priming


async def test_a_provider_that_cannot_batch_still_produces_prices() -> None:
    pool = _FakePool(
        prices={WETH: 42},
        sources={WETH: WETH_FEED},
        rounds={WETH_FEED: _round_data(answer=42, started_at=0, updated_at=NOW)},
    )
    pool.batch_broken = True
    quotes = await _reader(pool).read_prices([WETH])

    assert pool.batches == []
    assert quotes[WETH].price == 42
    assert quotes[WETH].updated_at == NOW


async def test_no_assets_makes_no_calls() -> None:
    pool = _FakePool()
    assert await _reader(pool).read_prices([]) == {}
    assert pool.tags == []


# --------------------------------------------------------------------------------------
# Sequencer reads
# --------------------------------------------------------------------------------------


async def test_the_uptime_feeds_zero_answer_means_up() -> None:
    """Inverted relative to every other Chainlink feed: 0 is up, 1 is down. A sign error here
    reads a healthy sequencer as a halted one, or worse, the reverse."""
    pool = _FakePool(uptime=_round_data(answer=0, started_at=NOW - 10**6, updated_at=NOW))
    status = await _reader(pool).read_sequencer()

    assert status is not None
    assert status.up
    assert status.liquidations_allowed(NOW)


async def test_a_one_answer_means_down() -> None:
    pool = _FakePool(uptime=_round_data(answer=1, started_at=NOW - 60, updated_at=NOW))
    status = await _reader(pool).read_sequencer()

    assert status is not None
    assert not status.up
    assert not status.liquidations_allowed(NOW)


async def test_the_grace_period_comes_from_chain_config() -> None:
    pool = _FakePool(uptime=_round_data(answer=0, started_at=NOW, updated_at=NOW))
    status = await _reader(pool).read_sequencer()

    assert status is not None
    assert status.grace_period_seconds == 3_600, "base.yaml"
    assert not status.liquidations_allowed(NOW), "just came back up"


async def test_an_unreadable_feed_raises_rather_than_reporting_up() -> None:
    """The property that matters is fail-closed: there must be no path on which "we could not
    tell" becomes "liquidations are allowed". Deliberately *not* asserting `HaltCondition` —
    the pool does not distinguish a revert from a 429, and promoting both to a latching halt
    would take a venue offline for a rate limit that resolves itself."""
    pool = _FakePool(uptime=None)  # the aggregator reverts
    with pytest.raises(LiqEngineError):
        await _reader(pool).read_sequencer()


async def test_a_truncated_feed_response_halts() -> None:
    """A call that succeeds but returns the wrong shape is not transient — the contract at that
    address is not the aggregator we decoded against."""
    pool = _FakePool(uptime="0x" + _uint(1))  # only roundId; no answer or startedAt
    with pytest.raises(HaltCondition, match="latestRoundData"):
        await _reader(pool).read_sequencer()


async def test_a_chain_with_no_configured_feed_returns_none_not_allowed() -> None:
    """`None` is "not applicable", and the caller has to decide what that means. Silently
    returning a permissive status is how the grace period gets skipped."""
    pool = _FakePool()
    reader = _reader(pool)
    object.__setattr__(reader.venue.chain, "sequencer_uptime_feed", None)

    assert await reader.read_sequencer() is None


async def test_a_source_that_reverts_on_round_data_is_not_asked_again() -> None:
    """Eleven of Base's fifteen sources revert on latestRoundData. Re-asking every round fails
    the whole batch and falls back to one call per source, doubling the oracle's share of a
    paced free-tier budget to relearn the same fact."""
    pool = _FakePool(
        prices={WETH: 269_472_000_000, CBETH: 300_000_000_000},
        sources={WETH: WETH_FEED, CBETH: CBETH_ADAPTER},
        rounds={WETH_FEED: _round_data(answer=1, started_at=0, updated_at=NOW - 60)},
    )
    reader = _reader(pool)

    first = await reader.read_prices([WETH, CBETH])
    assert first[CBETH].updated_at is None and first[WETH].updated_at == NOW - 60
    asked_before = sum(1 for c in pool.round_calls if c == CBETH_ADAPTER)
    assert asked_before >= 1

    await reader.read_prices([WETH, CBETH])
    asked_after = sum(1 for c in pool.round_calls if c == CBETH_ADAPTER)
    assert asked_after == asked_before, "the adapter was remembered as having no round data"
    assert sum(1 for c in pool.round_calls if c == WETH_FEED) >= 2, "the real feed still is"
