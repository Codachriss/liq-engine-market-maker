"""Tests for the Gate A5 backtest.

The properties that matter: the seizure value identity has to actually match what
`calculate_seizure` computes (or the "no archive data needed" trick is just wrong), EV has to
be D2/D19-correct rather than a naive multiply, and the win-probability sweep has to actually
show sensitivity rather than collapse to one number.
"""

from __future__ import annotations

import pytest

from liqengine.backtest import (
    BacktestAssumptions,
    monthly_ev,
    replay_trade,
    sweep_win_probability,
)
from liqengine.core.fixedpoint import RAY
from liqengine.core.types import to_address
from liqengine.protocols.aave_v3.math import calculate_seizure
from liqengine.protocols.aave_v3.state import ReserveState

WETH = to_address("0x" + "e1" * 20)
USDC = to_address("0x" + "c2" * 20)


def reserve(asset, decimals, *, bonus_bps=10_500, protocol_fee_bps=1_000) -> ReserveState:
    return ReserveState(
        asset=asset, symbol=asset[:6], decimals=decimals,
        ltv_bps=7_500, liquidation_threshold_bps=8_000, liquidation_bonus_bps=bonus_bps,
        liquidation_protocol_fee_bps=protocol_fee_bps, liquidity_index_ray=RAY,
        variable_borrow_index_ray=RAY, current_liquidity_rate_ray=0,
        current_variable_borrow_rate_ray=0, last_update_timestamp=0,
    )


# ---------------------------------------------------------------- the value identity


def test_the_bonus_value_identity_matches_calculate_seizure() -> None:
    """The whole trick this module depends on: gross bonus value in USD is exactly
    debt_repaid * (bonus_bps/10000 - 1), independent of collateral price, as long as the
    liquidation was not collateral-capped. If this test fails, the "no archive data needed"
    claim in the module docstring is simply wrong and the backtest is not measuring what it
    claims to."""
    debt_price = 1 * 10**8  # USDC
    collateral_price = 3_000 * 10**8  # WETH
    debt_reserve = reserve(USDC, 6)
    collateral_reserve = reserve(WETH, 18, bonus_bps=10_500)
    debt_to_cover = 10_000 * 10**6  # $10,000

    seizure = calculate_seizure(
        debt_to_cover=debt_to_cover,
        debt_reserve=debt_reserve,
        collateral_reserve=collateral_reserve,
        debt_price=debt_price,
        collateral_price=collateral_price,
        user_collateral_balance=100 * 10**18,  # far more than needed: not capped
    )
    assert not seizure.collateral_capped

    # collateral_seized valued back in USD at the same price used to compute it.
    seized_usd = seizure.collateral_seized * collateral_price / 10**18 / 10**8
    debt_repaid_usd = seizure.debt_to_cover_actual / 10**6
    gross_bonus_usd_from_seizure = seized_usd - debt_repaid_usd

    gross_bonus_usd_from_identity = debt_repaid_usd * (10_500 - 10_000) / 10_000

    assert gross_bonus_usd_from_seizure == pytest.approx(gross_bonus_usd_from_identity, rel=1e-9)


def test_a_capped_liquidation_would_overstate_the_identity() -> None:
    """Stated as a limitation in the module docstring, pinned here so it is not forgotten:
    when the borrower's collateral runs out first, the identity overstates the real bonus."""
    debt_price = 1 * 10**8
    collateral_price = 3_000 * 10**8
    debt_reserve = reserve(USDC, 6)
    collateral_reserve = reserve(WETH, 18, bonus_bps=10_500)

    seizure = calculate_seizure(
        debt_to_cover=10_000 * 10**6,
        debt_reserve=debt_reserve,
        collateral_reserve=collateral_reserve,
        debt_price=debt_price,
        collateral_price=collateral_price,
        user_collateral_balance=10**16,  # a tiny collateral balance: forces a cap
    )
    assert seizure.collateral_capped
    assert seizure.debt_to_cover_actual < 10_000 * 10**6  # less debt was actually repaid


# ---------------------------------------------------------------- replay_trade arithmetic


def test_replay_trade_reconciles_gross_bonus_minus_costs_to_net() -> None:
    trade = replay_trade("u1", 100, 1_700_000_000, 10_000.0)
    assert trade.net_usd == pytest.approx(
        trade.gross_bonus_usd - trade.protocol_fee_usd - trade.exit_cost_usd - trade.gas_cost_usd
    )


def test_a_larger_trade_produces_a_larger_gross_bonus() -> None:
    small = replay_trade("u1", 100, None, 100.0)
    large = replay_trade("u2", 100, None, 100_000.0)
    assert large.gross_bonus_usd > small.gross_bonus_usd


def test_protocol_fee_reduces_the_bonus_the_liquidator_keeps() -> None:
    no_fee = replay_trade("u1", 100, None, 10_000.0, BacktestAssumptions(protocol_fee_bps=0))
    with_fee = replay_trade("u1", 100, None, 10_000.0, BacktestAssumptions(protocol_fee_bps=2_000))
    assert with_fee.net_usd < no_fee.net_usd
    assert with_fee.protocol_fee_usd > 0
    assert no_fee.protocol_fee_usd == 0


def test_exit_cost_scales_with_slippage_assumption() -> None:
    tight = replay_trade("u1", 100, None, 10_000.0, BacktestAssumptions(exit_slippage_bps=5))
    wide = replay_trade("u1", 100, None, 10_000.0, BacktestAssumptions(exit_slippage_bps=500))
    assert wide.exit_cost_usd > tight.exit_cost_usd
    assert wide.net_usd < tight.net_usd


def test_a_zero_bonus_produces_a_negative_net() -> None:
    """No bonus, but gas and exit costs are still real — net must not be zero, it must be
    negative, or the costs are being silently dropped."""
    trade = replay_trade("u1", 100, None, 10_000.0, BacktestAssumptions(bonus_bps=10_000))
    assert trade.gross_bonus_usd == 0
    assert trade.net_usd < 0


# ---------------------------------------------------------------- EV and the win-probability sweep


def test_expected_value_is_not_a_naive_multiply() -> None:
    """D2/D19: EV subtracts the failure cost on the losing branch, it does not just scale net
    by the win probability."""
    trade = replay_trade("u1", 100, None, 10_000.0)
    ev = trade.expected_value_usd(0.5)
    naive = trade.net_usd * 0.5
    assert ev != naive
    assert ev == pytest.approx(0.5 * trade.net_usd - 0.5 * trade.gas_cost_usd)


def test_expected_value_rejects_an_out_of_range_probability() -> None:
    trade = replay_trade("u1", 100, None, 10_000.0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        trade.expected_value_usd(1.5)


def test_a_losing_trade_still_costs_gas_at_zero_win_probability() -> None:
    """Arbitrum: reverts_cost_gas is true. A certain loss is not free."""
    trade = replay_trade("u1", 100, None, 10_000.0)
    assert trade.expected_value_usd(0.0) == pytest.approx(-trade.gas_cost_usd)


def test_the_sweep_shows_real_sensitivity_not_one_collapsed_number() -> None:
    """The whole point of 13b: report a range, not a single assumed figure."""
    trades = [replay_trade(f"u{i}", 100, None, 500.0) for i in range(20)]
    results = sweep_win_probability(trades, scenarios=(0.1, 0.5, 0.9))
    means = [r.mean_ev_usd for r in results]
    assert means == sorted(means)  # EV should rise monotonically with assumed win rate
    assert means[0] != means[-1]


def test_sweep_of_no_trades_does_not_divide_by_zero() -> None:
    results = sweep_win_probability([], scenarios=(0.5,))
    assert results[0].mean_ev_usd == 0.0
    assert results[0].positive_ev_share == 0.0


# ---------------------------------------------------------------- monthly aggregation


def test_monthly_ev_groups_by_utc_month() -> None:
    jan = 1_735_700_000  # early January 2025-ish, exact date not the point
    feb = jan + 40 * 86_400
    trades = [
        replay_trade("u1", 100, jan, 1_000.0),
        replay_trade("u2", 100, jan + 1000, 1_000.0),
        replay_trade("u3", 100, feb, 1_000.0),
    ]
    grouped = monthly_ev(trades, win_probability=1.0)
    assert len(grouped) == 2
    assert sum(grouped.values()) == pytest.approx(sum(t.expected_value_usd(1.0) for t in trades))


def test_monthly_ev_skips_trades_with_no_timestamp() -> None:
    trades = [replay_trade("u1", 100, None, 1_000.0)]
    assert monthly_ev(trades, win_probability=1.0) == {}
