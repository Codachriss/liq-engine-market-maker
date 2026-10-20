"""Tests for the profit simulator.

The properties pinned here are the ones that separate an honest simulator from a flattering one:
the line items must reconcile to the net, the conservative case must actually be worse, failure
risk must not subtract from net, and a trade that cannot legally execute must be refused for the
reason it cannot rather than for arriving at a bad number.
"""

from __future__ import annotations

import pytest

from liqengine.core.fixedpoint import RAY
from liqengine.core.types import Address, to_address
from liqengine.protocols.aave_v3.state import EModeCategory, ReserveState
from liqengine.simulate import (
    CONSERVATIVE,
    EXPECTED,
    ExitQuote,
    ExitStrategy,
    FlashLoanQuote,
    GasQuote,
    LiquidationRequest,
    QuoteSource,
    RejectReason,
    Scenario,
    VenueCosts,
    WinProbability,
    simulate,
)

WETH = to_address("0x" + "e1" * 20)
USDC = to_address("0x" + "c2" * 20)
BORROWER = to_address("0x" + "a0" * 20)

PRICE_WETH = 3_000 * 10**8
PRICE_USDC = 1 * 10**8

MEASURED = WinProbability(bps=5_000, source="backtest_v1")
PLACEHOLDER = WinProbability(bps=5_000)


def reserve(
    asset: Address,
    decimals: int,
    *,
    bonus_bps: int = 10_500,
    protocol_fee_bps: int = 1_000,
    paused: bool = False,
) -> ReserveState:
    return ReserveState(
        asset=asset,
        symbol=asset[:6],
        decimals=decimals,
        ltv_bps=7_500,
        liquidation_threshold_bps=8_000,
        liquidation_bonus_bps=bonus_bps,
        liquidation_protocol_fee_bps=protocol_fee_bps,
        liquidity_index_ray=RAY,
        variable_borrow_index_ray=RAY,
        current_liquidity_rate_ray=0,
        current_variable_borrow_rate_ray=0,
        last_update_timestamp=0,
        is_paused=paused,
    )


WETH_RESERVE = reserve(WETH, 18)
USDC_RESERVE = reserve(USDC, 6)

#: 400k gas at 0.02 gwei is realistic for Arbitrum, and comes to well under a dollar.
GAS = GasQuote(
    gas_limit=400_000,
    base_fee_wei=10_000_000,
    priority_fee_wei=10_000_000,
    native_price=PRICE_WETH,
    native_decimals=18,
)

ROUTER_QUOTE = ExitQuote(
    venue="uniswap_v3",
    fee_bps=5,
    slippage_bps=10,
    worst_slippage_bps=40,
    liquidity_available=1_000 * 10**18,
)

BALANCER = FlashLoanQuote(source="balancer", premium_bps=0, available=10_000_000 * 10**6)
AAVE_FLASH = FlashLoanQuote(source="aave_v3", premium_bps=5, available=10_000_000 * 10**6)


def request(**overrides) -> LiquidationRequest:
    """A healthy 10,000 USDC liquidation against WETH collateral."""
    base = dict(
        borrower=BORROWER,
        collateral_reserve=WETH_RESERVE,
        debt_reserve=USDC_RESERVE,
        collateral_price=PRICE_WETH,
        debt_price=PRICE_USDC,
        debt_to_cover=10_000 * 10**6,
        user_collateral_balance=100 * 10**18,
        gas=GAS,
        venue=VenueCosts(reverts_cost_gas=True),
        exit_quote=ROUTER_QUOTE,
        flash=BALANCER,
    )
    return LiquidationRequest(**{**base, **overrides})


# ---------------------------------------------------------------- the arithmetic


def test_a_profitable_liquidation_is_executable() -> None:
    result = simulate(request(), MEASURED)
    assert result.executable
    assert result.expected is not None and result.conservative is not None
    assert result.expected.net_base > 0
    assert result.conservative.net_base > 0


def test_line_items_reconcile_to_net() -> None:
    """A line item that does not add up is fiction, and the divergence alarms depend on it."""
    result = simulate(request(), MEASURED)
    assert result.expected is not None and result.conservative is not None
    assert result.expected.reconciles()
    assert result.conservative.reconciles()


def test_the_bonus_is_not_the_profit() -> None:
    """5% headline on 10,000 is 500. What survives the protocol fee and the exit is less."""
    result = simulate(request(), MEASURED)
    expected = result.expected
    assert expected is not None
    assert expected.gross_bonus_base < 500 * 10**8       # protocol fee already taken
    assert expected.net_base < expected.gross_bonus_base  # then the exit and the gas
    assert expected.protocol_fee_base > 0


def test_the_flash_premium_is_a_real_cost() -> None:
    """D3 prefers zero-fee sources for a reason: 5bps on 10,000 is 5, against a ~450 bonus."""
    free = simulate(request(flash=BALANCER), MEASURED)
    paid = simulate(request(flash=AAVE_FLASH), MEASURED)
    assert free.expected is not None and paid.expected is not None
    assert free.expected.flash_premium_base == 0
    assert paid.expected.flash_premium_base == pytest.approx(5 * 10**8, rel=1e-3)
    assert paid.expected.net_base < free.expected.net_base


def test_slippage_scales_with_the_exit_quote() -> None:
    thin = ExitQuote(
        venue="thin_pool",
        fee_bps=30,
        slippage_bps=500,
        worst_slippage_bps=1_200,
        liquidity_available=1_000 * 10**18,
    )
    deep = simulate(request(), MEASURED)
    shallow = simulate(request(exit_quote=thin), MEASURED)
    assert deep.expected is not None and shallow.expected is not None
    assert shallow.expected.slippage_base > deep.expected.slippage_base
    # Long-tail collateral: thin liquidity eats the entire bonus, which is the correlation
    # between low competition and no profit that ARCHITECTURE warns about.
    assert shallow.expected.net_base < 0
    assert shallow.reject_reason is RejectReason.EXPECTED_UNPROFITABLE


def test_collateral_running_out_reduces_the_repayment() -> None:
    """Using the requested amount downstream prices a partial fill as a full one."""
    result = simulate(request(user_collateral_balance=10**18), MEASURED)  # 1 WETH only
    assert result.expected is not None
    assert result.expected.collateral_capped
    assert result.expected.debt_repaid < 10_000 * 10**6


def test_emode_lowers_the_bonus() -> None:
    """An eMode account uses its category bonus; the reserve default overstates the trade."""
    emode = EModeCategory(
        id=1, ltv_bps=9_000, liquidation_threshold_bps=9_300, liquidation_bonus_bps=10_100
    )
    default = simulate(request(), MEASURED)
    in_emode = simulate(request(emode=emode), MEASURED)
    assert default.expected is not None and in_emode.expected is not None
    assert in_emode.expected.gross_bonus_base < default.expected.gross_bonus_base


# ---------------------------------------------------------------- the two scenarios


def test_the_conservative_case_is_worse_on_every_axis() -> None:
    result = simulate(request(), MEASURED)
    expected, conservative = result.expected, result.conservative
    assert expected is not None and conservative is not None
    assert conservative.slippage_base > expected.slippage_base
    assert conservative.gas_base > expected.gas_base
    assert conservative.net_base < expected.net_base


def test_conservatism_moves_inputs_not_the_output() -> None:
    """The bounds have to be checkable after the fact, which a haircut on net is not.

    Conservatism about the exit is an adverse collateral-vs-debt move on the swap proceeds
    (`exit_haircut_bps`), surfaced as a larger slippage line — not an independent price shift,
    which would inflate the bonus and could make the conservative case beat the expected one.
    """
    result = simulate(request(), MEASURED)
    conservative = result.conservative
    expected = result.expected
    assert conservative is not None and expected is not None
    # The haircut lands on the proceeds, so it shows up as a bigger slippage line and a worse net.
    assert conservative.slippage_base > expected.slippage_base
    assert conservative.net_base < expected.net_base


def test_a_trade_profitable_only_in_the_expected_case_is_refused() -> None:
    """The whole point of the specification's last line."""
    marginal = ExitQuote(
        venue="uniswap_v3",
        fee_bps=5,
        slippage_bps=380,
        worst_slippage_bps=460,
        liquidity_available=1_000 * 10**18,
    )
    result = simulate(request(exit_quote=marginal), MEASURED)
    assert result.expected is not None and result.conservative is not None
    assert result.expected.net_base > 0
    assert result.conservative.net_base <= 0
    assert not result.executable
    assert result.reject_reason is RejectReason.CONSERVATIVE_UNPROFITABLE


def test_scenario_bounds_are_configurable() -> None:
    brutal = Scenario(
        name="brutal",
        collateral_price_drop_bps=1_000,
        use_worst_slippage=True,
        gas_price_multiple_bps=50_000,
    )
    result = simulate(request(), MEASURED, conservative_scenario=brutal)
    assert result.conservative is not None
    assert result.conservative.gas_base > simulate(request(), MEASURED).conservative.gas_base


def test_a_worst_case_below_the_expected_case_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="must not flatter"):
        ExitQuote(
            venue="wishful",
            fee_bps=5,
            slippage_bps=100,
            worst_slippage_bps=50,
            liquidity_available=10**18,
        )


# ---------------------------------------------------------------- expected value


def test_failure_risk_does_not_subtract_from_net() -> None:
    """It belongs in EV. Subtracting it from net gives a number that is neither case."""
    result = simulate(request(), MEASURED)
    assert result.expected is not None and result.expected_value_base is not None
    assert result.expected_value_base < result.expected.net_base
    assert result.expected.net_base == result.expected.gross_bonus_base - (
        result.expected.dex_fee_base
        + result.expected.slippage_base
        + result.expected.flash_premium_base
        + result.expected.gas_base
    )


def test_failure_cost_is_venue_physics() -> None:
    """A lost race on Arbitrum burns gas. A losing bundle costs nothing. Different EV."""
    fcfs = simulate(request(venue=VenueCosts(reverts_cost_gas=True)), MEASURED)
    bundle = simulate(request(venue=VenueCosts(reverts_cost_gas=False)), MEASURED)
    assert fcfs.failure_cost_base > 0
    assert bundle.failure_cost_base == 0
    assert bundle.expected_value_base > fcfs.expected_value_base


def test_expected_value_uses_the_conservative_gas_for_failure() -> None:
    """A race is most likely lost exactly when gas is spiking."""
    result = simulate(request(), MEASURED)
    assert result.conservative is not None
    assert result.failure_cost_base == result.conservative.gas_base


def test_a_placeholder_win_probability_is_marked_as_one() -> None:
    """An invented p(win) is how a strategy looks profitable in simulation and is not live."""
    assert simulate(request(), PLACEHOLDER).ev_is_placeholder
    assert not simulate(request(), MEASURED).ev_is_placeholder


def test_min_expected_value_is_enforced() -> None:
    result = simulate(
        request(venue=VenueCosts(reverts_cost_gas=True, min_expected_value_base=10_000 * 10**8)),
        MEASURED,
    )
    assert result.reject_reason is RejectReason.BELOW_MIN_EV
    assert result.expected is not None  # the evidence is kept, not discarded with the rejection


# ---------------------------------------------------------------- preconditions


def test_sequencer_grace_refuses_before_pricing() -> None:
    """Simulating through the grace window is how a backtest invents profit during volatility."""
    result = simulate(request(sequencer_grace_active=True), MEASURED)
    assert result.reject_reason is RejectReason.SEQUENCER_GRACE
    assert result.expected is None


def test_a_paused_reserve_refuses() -> None:
    result = simulate(request(collateral_reserve=reserve(WETH, 18, paused=True)), MEASURED)
    assert result.reject_reason is RejectReason.RESERVE_PAUSED


def test_a_stale_price_refuses() -> None:
    result = simulate(request(price_is_stale=True), MEASURED)
    assert result.reject_reason is RejectReason.STALE_PRICE


def test_oracle_parity_cannot_authorise_a_trade() -> None:
    """Parity has no slippage information, so it cannot produce a conservative case."""
    inferred = ExitQuote(
        venue="inferred",
        fee_bps=5,
        slippage_bps=10,
        worst_slippage_bps=40,
        liquidity_available=1_000 * 10**18,
        source=QuoteSource.ORACLE_PARITY,
    )
    result = simulate(request(exit_quote=inferred), MEASURED)
    assert result.reject_reason is RejectReason.NO_EXIT_QUOTE


def test_no_exit_quote_at_all_refuses() -> None:
    assert simulate(request(exit_quote=None), MEASURED).reject_reason is RejectReason.NO_EXIT_QUOTE


def test_exit_depth_is_checked_against_the_actual_size() -> None:
    tiny = ExitQuote(
        venue="tiny",
        fee_bps=5,
        slippage_bps=10,
        worst_slippage_bps=40,
        liquidity_available=10**15,
    )
    result = simulate(request(exit_quote=tiny), MEASURED)
    assert result.reject_reason is RejectReason.INSUFFICIENT_EXIT_LIQUIDITY


def test_flash_liquidity_is_checked() -> None:
    small = FlashLoanQuote(source="balancer", premium_bps=0, available=100 * 10**6)
    result = simulate(request(flash=small), MEASURED)
    assert result.reject_reason is RejectReason.INSUFFICIENT_FLASH_LIQUIDITY


# ---------------------------------------------------------------- holding the collateral


def test_holding_needs_inventory_and_carries_exposure() -> None:
    """No slippage and no premium, but the result is a directional position, not cash."""
    held = simulate(
        request(
            exit_strategy=ExitStrategy.HOLD,
            exit_quote=None,
            flash=None,
            inventory_debt_asset=10_000 * 10**6,
        ),
        MEASURED,
    )
    assert held.executable
    assert held.expected is not None
    assert held.expected.slippage_base == 0
    assert held.expected.flash_premium_base == 0
    assert held.expected.unhedged_exposure_base > 0


def test_holding_without_inventory_refuses() -> None:
    """There is no flash loan on this route to fall back on."""
    result = simulate(
        request(exit_strategy=ExitStrategy.HOLD, exit_quote=None, flash=None), MEASURED
    )
    assert result.reject_reason is RejectReason.NO_INVENTORY


def test_swapping_and_holding_are_priced_differently() -> None:
    swapped = simulate(request(), MEASURED)
    held = simulate(
        request(
            exit_strategy=ExitStrategy.HOLD,
            exit_quote=None,
            flash=None,
            inventory_debt_asset=10_000 * 10**6,
        ),
        MEASURED,
    )
    assert swapped.expected is not None and held.expected is not None
    # Holding looks better because the exit has not been paid for yet, which is exactly why it
    # must not be compared to the swap route as though both were cash.
    assert held.expected.net_base > swapped.expected.net_base
    assert swapped.expected.unhedged_exposure_base == 0


# ---------------------------------------------------------------- scenario plumbing


def test_expected_scenario_shifts_nothing() -> None:
    assert EXPECTED.collateral_price_drop_bps == 0
    assert EXPECTED.gas_price_multiple_bps == 10_000
    result = simulate(request(), MEASURED)
    assert result.expected is not None
    assert result.expected.collateral_price == PRICE_WETH


def test_conservative_scenario_is_actually_adverse() -> None:
    assert CONSERVATIVE.use_worst_slippage
    assert CONSERVATIVE.gas_price_multiple_bps > 10_000
    # Exit risk is a collateral-vs-debt haircut on the proceeds, not an independent price shift
    # (which inflates the bonus — the bug this restatement fixed).
    assert CONSERVATIVE.exit_haircut_bps > 0
    assert CONSERVATIVE.collateral_price_drop_bps == 0
    assert CONSERVATIVE.debt_price_rise_bps == 0
