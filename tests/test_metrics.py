"""Tests for the pure metrics functions.

Weighted toward the places a metric can be quietly wrong: booking discipline (unrealised must
not count as realised), the sign of divergence (optimistic vs pessimistic matters, not just
magnitude), zero-filled gaps in the daily series, and division guards that must return "not
enough data" rather than a number that looks like one.
"""

from __future__ import annotations

from liqengine.core.types import to_address
from liqengine.metrics import (
    TradeRecord,
    capital_summary,
    daily_pnl_series,
    execution_summary,
    max_drawdown,
    performance_summary,
    pnl_by_asset,
    pnl_by_protocol,
    pnl_summary,
    predicted_vs_realised,
    predicted_vs_realised_win_rate,
    sharpe_ratio,
    sortino_ratio,
    volatility_base,
)

WETH = to_address("0x" + "e1" * 20)
USDC = to_address("0x" + "c2" * 20)
UNIT = 10**8
DAY = 86_400
T0 = 1_800_000_000  # arbitrary anchor, aligned to a day boundary is not required


def trade(**overrides) -> TradeRecord:
    base = dict(
        trace_id="t1",
        timestamp=T0,
        venue="aave_v3_arbitrum",
        protocol="aave_v3",
        collateral_asset=WETH,
        debt_asset=USDC,
        strategy_id="baseline",
        notional_base=1_000 * UNIT,
        predicted_net_base=50 * UNIT,
        predicted_gas_base=1 * UNIT,
        predicted_slippage_base=5 * UNIT,
        win_probability_bps=5_000,
    )
    return TradeRecord(**{**base, **overrides})


def booked_win(**overrides) -> TradeRecord:
    base = dict(attempted=True, succeeded=True, booked=True, realised_net_base=40 * UNIT,
                realised_gas_base=1 * UNIT, realised_slippage_base=6 * UNIT)
    return trade(**{**base, **overrides})


def booked_loss(**overrides) -> TradeRecord:
    base = dict(attempted=True, succeeded=False, booked=True, realised_net_base=-2 * UNIT,
                realised_gas_base=2 * UNIT, realised_slippage_base=5 * UNIT)
    return trade(**{**base, **overrides})


# ---------------------------------------------------------------- P&L booking discipline


def test_unrealised_pnl_is_not_counted_as_realised() -> None:
    """Confirmed but not finalised: real on screen, not yet real in the ledger (D11)."""
    pending = trade(attempted=True, succeeded=True, booked=False, realised_net_base=100 * UNIT)
    summary = pnl_summary([pending])
    assert summary.realised_base == 0
    assert summary.unrealised_base == 100 * UNIT
    assert summary.total_base == 100 * UNIT


def test_a_rejected_opportunity_contributes_nothing_to_pnl() -> None:
    rejected = trade(attempted=False)
    summary = pnl_summary([rejected])
    assert summary.total_base == 0


def test_pnl_groups_by_protocol_and_asset_without_double_counting() -> None:
    trades = [
        booked_win(protocol="aave_v3", debt_asset=USDC, realised_net_base=40 * UNIT),
        booked_win(protocol="compound", debt_asset=WETH, realised_net_base=10 * UNIT),
    ]
    by_protocol = pnl_by_protocol(trades)
    by_asset = pnl_by_asset(trades)
    assert by_protocol == {"aave_v3": 40 * UNIT, "compound": 10 * UNIT}
    assert by_asset == {USDC: 40 * UNIT, WETH: 10 * UNIT}
    assert sum(by_protocol.values()) == sum(by_asset.values())


# ---------------------------------------------------------------- performance


def test_win_rate_and_expectancy() -> None:
    trades = [booked_win(), booked_win(), booked_loss()]
    summary = performance_summary(trades)
    assert summary.trades == 3
    assert summary.wins == 2
    assert summary.losses == 1
    assert summary.win_rate_bps == 6_666
    assert summary.average_win_base == 40 * UNIT
    assert summary.average_loss_base == -2 * UNIT
    assert summary.expectancy_base == (80 * UNIT - 2 * UNIT) // 3


def test_profit_factor_is_none_without_any_losses() -> None:
    """Reporting infinity would look like a real number rather than "not enough data"."""
    summary = performance_summary([booked_win(), booked_win()])
    assert summary.profit_factor is None


def test_profit_factor_with_both_sides() -> None:
    summary = performance_summary([booked_win(realised_net_base=100 * UNIT),
                                    booked_loss(realised_net_base=-25 * UNIT)])
    assert summary.profit_factor == 4.0


def test_max_drawdown_is_the_cumulative_decline_not_the_worst_single_trade() -> None:
    """A run of small losses that never recovers is the risk; one bad trade among wins is not."""
    trades = [
        booked_win(timestamp=T0, realised_net_base=100 * UNIT),
        booked_loss(timestamp=T0 + DAY, realised_net_base=-10 * UNIT),
        booked_loss(timestamp=T0 + 2 * DAY, realised_net_base=-10 * UNIT),
        booked_loss(timestamp=T0 + 3 * DAY, realised_net_base=-10 * UNIT),
        booked_win(timestamp=T0 + 4 * DAY, realised_net_base=5 * UNIT),
    ]
    assert max_drawdown(trades) == 30 * UNIT


def test_max_drawdown_of_an_ever_rising_curve_is_zero() -> None:
    trades = [booked_win(timestamp=T0), booked_win(timestamp=T0 + DAY)]
    assert max_drawdown(trades) == 0


# ---------------------------------------------------------------- execution and failure rate


def test_execution_and_failure_rate_are_different_denominators() -> None:
    """A liquidation can succeed on-chain and still book a loss — win rate and failure rate
    are not complements of each other."""
    trades = [
        trade(attempted=False),                       # rejected
        booked_win(),                                  # attempted, succeeded
        booked_loss(),                                  # attempted, failed
    ]
    summary = execution_summary(trades)
    assert summary.detected == 3
    assert summary.attempted == 2
    assert summary.execution_rate_bps == 6_666
    assert summary.failure_rate_bps == 5_000


def test_execution_rate_with_no_detections_does_not_divide_by_zero() -> None:
    summary = execution_summary([])
    assert summary.execution_rate_bps == 0
    assert summary.failure_rate_bps == 0


# ---------------------------------------------------------------- daily series and ratios


def test_the_daily_series_zero_fills_gaps() -> None:
    """A quiet day is a real observation; dropping it understates volatility."""
    trades = [
        booked_win(timestamp=T0, realised_net_base=10 * UNIT),
        booked_win(timestamp=T0 + 3 * DAY, realised_net_base=20 * UNIT),
    ]
    series = daily_pnl_series(trades)
    assert series == [10 * UNIT, 0, 0, 20 * UNIT]


def test_volatility_of_a_flat_series_is_zero() -> None:
    assert volatility_base([5, 5, 5]) == 0.0


def test_sharpe_is_none_with_too_few_points() -> None:
    assert sharpe_ratio([100]) is None


def test_sharpe_is_none_when_the_series_has_no_variance() -> None:
    """A zero-variance series would divide by zero; "undefined" beats "infinite"."""
    assert sharpe_ratio([100, 100, 100]) is None


def test_sortino_ignores_upside_variance() -> None:
    """Small losing days plus one very large winning day — plausibly this strategy, given the
    measured revenue shape. Sharpe's denominator is inflated by the big *upside* day even
    though it is the entire reason the strategy is worth running; Sortino, counting only
    deviation below the minimum acceptable return (zero here), is not."""
    mostly_small_losses_one_big_win = [-5, -3, -4, -3, 500]
    sharpe = sharpe_ratio(mostly_small_losses_one_big_win)
    sortino = sortino_ratio(mostly_small_losses_one_big_win)
    assert sharpe is not None and sortino is not None
    assert sortino > sharpe


def test_sortino_is_none_with_no_downside_at_all() -> None:
    assert sortino_ratio([10, 20, 30]) is None


# ---------------------------------------------------------------- predicted vs realised


def test_divergence_sign_shows_optimism_not_just_magnitude() -> None:
    """Predicted more profit and less gas than happened: the model is optimistic, which is the
    direction that loses money quietly."""
    optimistic = booked_win(
        predicted_net_base=100 * UNIT, realised_net_base=40 * UNIT,
        predicted_gas_base=1 * UNIT, realised_gas_base=3 * UNIT,
        predicted_slippage_base=1 * UNIT, realised_slippage_base=5 * UNIT,
    )
    summary = predicted_vs_realised([optimistic])
    assert summary.mean_profit_divergence_base == 60 * UNIT
    assert summary.mean_gas_divergence_base == -2 * UNIT
    assert summary.mean_slippage_divergence_base == -4 * UNIT


def test_predicted_vs_realised_win_rate_is_the_calibration_question_in_pnl_terms() -> None:
    trades = [
        trade(attempted=True, succeeded=True, win_probability_bps=8_000),
        trade(attempted=True, succeeded=False, win_probability_bps=8_000),
    ]
    result = predicted_vs_realised_win_rate(trades)
    assert result is not None
    predicted, realised = result
    assert predicted == 0.8
    assert realised == 0.5  # claimed 80%, delivered 50% — the overconfidence D20 watches for


def test_predicted_vs_realised_win_rate_is_none_without_attempts() -> None:
    assert predicted_vs_realised_win_rate([trade(attempted=False)]) is None


# ---------------------------------------------------------------- capital


def test_return_on_capital_at_risk_uses_notional_not_owned_capital() -> None:
    """The meaningful "return" under a flash-loan model: no owned capital funds the trade."""
    trades = [booked_win(notional_base=10_000 * UNIT, realised_net_base=50 * UNIT)]
    summary = capital_summary(trades, strategy_capital_base=0)
    assert summary.total_notional_base == 10_000 * UNIT
    # 50 * UNIT net over 10,000 * UNIT notional = 0.5% = 50bps.
    assert summary.return_on_capital_at_risk_bps == 50


def test_capital_utilisation_can_exceed_one_many_times_over() -> None:
    """Flash loans mean capital is never tied up between trades — high turnover is expected,
    not a solvency warning."""
    trades = [booked_win(notional_base=10_000 * UNIT) for _ in range(20)]
    summary = capital_summary(trades, strategy_capital_base=1_000 * UNIT)
    assert summary.capital_utilisation == 200.0


def test_capital_utilisation_is_none_when_capital_is_unconfigured() -> None:
    summary = capital_summary([booked_win()], strategy_capital_base=0)
    assert summary.capital_utilisation is None
