"""W4: a measured exit quote, expressed as a deviation from oracle parity.

Verified against live Base alongside these tests with `liq quote` at block ~52,048,000:

- 1 WETH -> USDC: 1bps tier, 15bps below oracle parity net of fee, 18bps at the 2x probe.
- 200 WETH -> USDC: the 30bps tier wins at that size, 11bps slippage, 13bps at the probe.
- 5 cbETH -> USDbC and 10 wstETH -> USDC: a tier answers, at ~1% of parity. Those assets'
  depth on Base is against WETH, not stables, so a single hop finds a near-empty pool. The
  quote is still measured and returned; the simulator's net rejects it.

The first quoter address recorded in the yaml was mistyped in its last four characters and had
no code, which made every pair read as "no route" — hence `verify()`.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from liqengine.config.loader import load_registry
from liqengine.config.models import ExitVenue, ExitVenueKind
from liqengine.core.abi import selector, word_to_int, words
from liqengine.core.errors import ProviderError
from liqengine.core.types import to_address
from liqengine.simulate.engine import _convert
from liqengine.simulate.inputs import QuoteSource
from liqengine.simulate.quotes import (
    DEFAULT_DEPTH_PROBE_BPS,
    QuoteRequest,
    UniswapV3Quoter,
    parity_amount_out,
    shortfall_bps,
)

QUOTER = to_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
WETH = to_address("0x4200000000000000000000000000000000000006")
USDC = to_address("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")

#: Oracle prices in Aave's 8-decimal base currency.
ETH_PRICE = 2_500_00000000
USDC_PRICE = 1_00000000
ONE_ETH = 10**18
#: 1 ETH at $2,500 is 2,500 USDC at parity.
PARITY_OUT = 2_500 * 10**6

_SIG = selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")


def _request(amount_in: int = ONE_ETH) -> QuoteRequest:
    return QuoteRequest(
        collateral=WETH,
        collateral_decimals=18,
        collateral_price=ETH_PRICE,
        debt=USDC,
        debt_decimals=6,
        debt_price=USDC_PRICE,
        amount_in=amount_in,
    )


def _venue(**kwargs) -> ExitVenue:
    return ExitVenue(
        name="uniswap_v3",
        kind=ExitVenueKind.UNISWAP_V3_QUOTER_V2,
        quoter=QUOTER,
        **kwargs,
    )


class _FakeQuoter:
    """Answers `quoteExactInputSingle` from a table keyed by (amount_in, fee_tier).

    A missing key reverts, because that is what a tier with no pool does on chain. Everything
    goes through `call`; `batch` is an assertion failure because W4 must not batch (one
    reverting tier would discard the tiers that answered).
    """

    def __init__(self, fills: dict[tuple[int, int], int]) -> None:
        self.fills = fills
        self.calls: list[tuple[str, int, int, str]] = []

    async def call(self, method: str, params: list, *, policy=None) -> str:
        assert method == "eth_call"
        target = to_address(str(params[0]["to"]))
        data = str(params[0]["data"])
        assert target == QUOTER
        assert data[:10] == _SIG
        chunks = words("0x" + data[10:])
        token_in = to_address("0x" + chunks[0][12:].hex())
        token_out = to_address("0x" + chunks[1][12:].hex())
        amount_in = word_to_int(chunks[2])
        fee = word_to_int(chunks[3])
        assert word_to_int(chunks[4]) == 0, "sqrtPriceLimitX96 must be 0 for a full-impact quote"
        self.calls.append((f"{token_in}->{token_out}", amount_in, fee, str(params[1])))
        out = self.fills.get((amount_in, fee))
        if out is None:
            raise ProviderError("fake", "execution reverted")
        # QuoterV2 returns (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate).
        return "0x" + "".join(f"{v:064x}" for v in (out, 0, 1, 100_000))

    async def batch(self, calls: list) -> list[object]:
        raise AssertionError("W4 must quote tier by tier, never as a batch")


def _quoter(fills: dict[tuple[int, int], int], **kwargs) -> UniswapV3Quoter:
    return UniswapV3Quoter(venue=_venue(**kwargs), pool=_FakeQuoter(fills))  # type: ignore[arg-type]


def _calls(quoter: UniswapV3Quoter) -> list[tuple[str, int, int, str]]:
    return quoter.pool.calls  # type: ignore[attr-defined]


def _bps_below_parity(bps: int, parity: int = PARITY_OUT) -> int:
    return parity - parity * bps // 10_000


# --------------------------------------------------------------------------------------
# parity and shortfall arithmetic
# --------------------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(
    amount=st.integers(min_value=0, max_value=10**30),
    from_decimals=st.integers(min_value=0, max_value=24),
    to_decimals=st.integers(min_value=0, max_value=24),
    from_price=st.integers(min_value=1, max_value=10**16),
    to_price=st.integers(min_value=1, max_value=10**16),
)
def test_parity_matches_the_simulator_exactly(
    amount: int, from_decimals: int, to_decimals: int, from_price: int, to_price: int
) -> None:
    """If `parity_amount_out` and `engine._convert` ever disagree about what parity means, the
    slippage W4 reports is measured against a different mid than the one the simulator applies
    it to, and every net is off by the difference. So they must be identical."""
    kwargs = dict(
        from_decimals=from_decimals,
        from_price=from_price,
        to_decimals=to_decimals,
        to_price=to_price,
    )
    assert parity_amount_out(amount, **kwargs) == _convert(amount, **kwargs)


def test_parity_with_a_zero_price_is_zero_not_a_crash() -> None:
    assert (
        parity_amount_out(ONE_ETH, from_decimals=18, from_price=1, to_decimals=6, to_price=0) == 0
    )


def test_shortfall_is_in_bps_of_parity_and_signed() -> None:
    assert shortfall_bps(10_000, 9_950) == 50
    assert shortfall_bps(10_000, 10_000) == 0
    assert shortfall_bps(10_000, 10_020) == -20, "a pool beating the oracle shows as negative here"
    assert shortfall_bps(0, 5) == 0


def test_request_parity_is_the_oracle_conversion_of_the_size() -> None:
    assert _request().parity_out == PARITY_OUT


# --------------------------------------------------------------------------------------
# quoting
# --------------------------------------------------------------------------------------


async def test_best_tier_wins_and_slippage_is_shortfall_net_of_fee() -> None:
    """Three tiers answer. The 5bps tier returns the most USDC, so it is the route, its fee is
    the fee, and the slippage is whatever remains of the shortfall after that fee."""
    probe = ONE_ETH * DEFAULT_DEPTH_PROBE_BPS // 10_000
    quoter = _quoter(
        {
            (ONE_ETH, 500): _bps_below_parity(5 + 3),  # 5bps fee + 3bps impact
            (ONE_ETH, 3000): _bps_below_parity(30 + 1),
            (ONE_ETH, 10000): _bps_below_parity(100 + 0),
            # probe at 2x on the winning tier: fee plus 12bps impact.
            (probe, 500): _bps_below_parity(5 + 12, parity=2 * PARITY_OUT),
        }
    )
    quote = await quoter.quote(_request())

    assert quote is not None
    assert quote.venue == "uniswap_v3"
    assert quote.fee_bps == 5
    assert quote.slippage_bps == 3
    assert quote.worst_slippage_bps == 12
    assert quote.liquidity_available == ONE_ETH
    assert quote.source is QuoteSource.ROUTER
    assert quote.is_measured, "D18 lifts its refusal only on a measured quote"


async def test_a_pool_beating_the_oracle_is_floored_at_zero_slippage() -> None:
    """Pool mid above the oracle between updates is real and transient. The quoter's output is
    net of fee, so total shortfall can be below the fee alone; crediting that would inflate
    every downstream net. The upside is discarded, the fee is still charged."""
    quoter = _quoter({(ONE_ETH, 500): PARITY_OUT + PARITY_OUT * 2 // 10_000})
    quote = await quoter.quote(_request())

    assert quote is not None
    assert quote.fee_bps == 5
    assert quote.slippage_bps == 0
    assert quote.worst_slippage_bps == 0


async def test_no_tier_answers_means_none_not_zero_cost() -> None:
    """A pair with no pool anywhere. `None` is the only honest answer: by D18 the opportunity
    is then not executable, which is correct for long-tail collateral without a direct pair."""
    quoter = _quoter({})
    assert await quoter.quote(_request()) is None
    # Every tier was asked at both sizes before giving up.
    assert len(_calls(quoter)) == 2 * len(_venue().fee_tiers)


async def test_zero_size_is_not_quoted() -> None:
    quoter = _quoter({(0, 500): 0})
    assert await quoter.quote(_request(amount_in=0)) is None
    assert _calls(quoter) == []


async def test_a_zero_output_fill_is_treated_as_no_fill() -> None:
    quoter = _quoter({(ONE_ETH, 500): 0})
    assert await quoter.quote(_request()) is None


# --------------------------------------------------------------------------------------
# the conservative bound
# --------------------------------------------------------------------------------------


async def test_unobservable_probe_falls_back_to_the_expected_figure() -> None:
    """The 2x probe reverts (not enough depth). The conservative bound must not be invented:
    it equals the expected figure, and the caller can see that they are equal."""
    quoter = _quoter({(ONE_ETH, 3000): _bps_below_parity(30 + 7)})
    quote = await quoter.quote(_request())

    assert quote is not None
    assert quote.fee_bps == 30
    assert quote.slippage_bps == 7
    assert quote.worst_slippage_bps == 7


async def test_probe_on_a_different_tier_does_not_count() -> None:
    """Depth observed on the 30bps pool says nothing about the 5bps pool we would route through."""
    probe = ONE_ETH * 2
    quoter = _quoter(
        {
            (ONE_ETH, 500): _bps_below_parity(5 + 2),
            (probe, 3000): _bps_below_parity(30 + 40, parity=2 * PARITY_OUT),
        }
    )
    quote = await quoter.quote(_request())

    assert quote is not None
    assert quote.fee_bps == 5
    assert quote.worst_slippage_bps == quote.slippage_bps == 2


async def test_probe_better_than_expected_cannot_lower_the_bound() -> None:
    """A probe that happens to fill tighter (rounding, a tick boundary) must not make the
    conservative case flatter than the expected one; `ExitQuote` rejects that outright."""
    probe = ONE_ETH * 2
    quoter = _quoter(
        {
            (ONE_ETH, 500): _bps_below_parity(5 + 9),
            (probe, 500): _bps_below_parity(5 + 4, parity=2 * PARITY_OUT),
        }
    )
    quote = await quoter.quote(_request())

    assert quote is not None
    assert quote.slippage_bps == 9
    assert quote.worst_slippage_bps == 9


async def test_probe_size_follows_the_configured_multiple() -> None:
    venue = _venue(fee_tiers=[500])
    pool = _FakeQuoter({(ONE_ETH, 500): _bps_below_parity(5)})
    quoter = UniswapV3Quoter(venue=venue, pool=pool, depth_probe_bps=30_000)  # type: ignore[arg-type]
    await quoter.quote(_request())

    assert [c[1] for c in pool.calls] == [ONE_ETH, 3 * ONE_ETH]


async def test_a_probe_no_larger_than_the_size_is_skipped() -> None:
    venue = _venue(fee_tiers=[500])
    pool = _FakeQuoter({(ONE_ETH, 500): _bps_below_parity(5)})
    quoter = UniswapV3Quoter(venue=venue, pool=pool, depth_probe_bps=10_000)  # type: ignore[arg-type]
    quote = await quoter.quote(_request())

    assert quote is not None
    assert [c[1] for c in pool.calls] == [ONE_ETH]
    assert quote.worst_slippage_bps == quote.slippage_bps


# --------------------------------------------------------------------------------------
# calls
# --------------------------------------------------------------------------------------


async def test_every_call_is_pinned_to_the_requested_block() -> None:
    """Quoting at `latest` while the balances were read at block N mixes two states. The
    simulator's inputs must all come from one block, so the quote takes one."""
    quoter = _quoter({(ONE_ETH, 500): _bps_below_parity(5)}, fee_tiers=[500])
    await quoter.quote(_request(), block=52_000_000)
    assert {c[3] for c in _calls(quoter)} == {hex(52_000_000)}

    quoter = _quoter({(ONE_ETH, 500): _bps_below_parity(5)}, fee_tiers=[500])
    await quoter.quote(_request())
    assert {c[3] for c in _calls(quoter)} == {"latest"}


async def test_direction_is_collateral_in_debt_out() -> None:
    quoter = _quoter({(ONE_ETH, 500): _bps_below_parity(5)}, fee_tiers=[500])
    await quoter.quote(_request())
    assert _calls(quoter)[0][0] == f"{WETH}->{USDC}"


async def test_configured_tiers_are_the_tiers_asked() -> None:
    quoter = _quoter({}, fee_tiers=[100, 500])
    await quoter.quote(_request())
    assert sorted({c[2] for c in _calls(quoter)}) == [100, 500]


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


def test_wrong_venue_dialect_is_rejected_at_construction() -> None:
    class _OtherKind:
        pass

    venue = _venue()
    object.__setattr__(venue, "kind", _OtherKind())
    with pytest.raises(ValueError, match="dialect"):
        UniswapV3Quoter(venue=venue, pool=_FakeQuoter({}))  # type: ignore[arg-type]


def test_base_config_names_the_uniswap_quoter() -> None:
    """The address is load-bearing (D18): a missing exit venue means no account on this venue
    is ever executable. So its presence and normalisation are asserted, not assumed."""
    registry = load_registry("config", environ={})
    protocol = registry.venue("aave_v3_base").protocol

    assert [v.name for v in protocol.exit_venues] == ["uniswap_v3"]
    venue = protocol.exit_venues[0]
    assert venue.kind is ExitVenueKind.UNISWAP_V3_QUOTER_V2
    assert venue.quoter == QUOTER
    assert venue.fee_tiers == [100, 500, 3000, 10000]


def test_exit_venue_normalises_the_quoter_address() -> None:
    venue = ExitVenue(
        name="x",
        kind=ExitVenueKind.UNISWAP_V3_QUOTER_V2,
        quoter="0x" + QUOTER[2:].upper(),
    )
    assert venue.quoter == QUOTER


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------


class _CodePool(_FakeQuoter):
    def __init__(self, code: str) -> None:
        super().__init__({})
        self.code = code

    async def call(self, method: str, params: list, *, policy=None) -> str:
        if method == "eth_getCode":
            assert to_address(str(params[0])) == QUOTER
            return self.code
        return await super().call(method, params, policy=policy)


async def test_verify_rejects_a_quoter_with_no_code() -> None:
    """An empty `eth_call` answer from a codeless address looks identical to 'no pool at this
    tier', so the address is checked once up front. This is the failure the first Base
    address produced."""
    from liqengine.simulate.quotes import ExitVenueUnavailable

    quoter = UniswapV3Quoter(venue=_venue(), pool=_CodePool("0x"))  # type: ignore[arg-type]
    with pytest.raises(ExitVenueUnavailable, match="no contract code"):
        await quoter.verify()


async def test_verify_accepts_a_quoter_with_code() -> None:
    quoter = UniswapV3Quoter(venue=_venue(), pool=_CodePool("0x6080604052"))  # type: ignore[arg-type]
    await quoter.verify()
