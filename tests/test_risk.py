"""Tests for the risk engine.

The property that matters most is negative: there must be no path to a token that bypasses the
evaluation. Most of the rest is about the failure modes being the *right* failure modes — a
budget that expires, a latch that does not, and a loss that counts before a profit does.
"""

from __future__ import annotations

import pytest

from liqengine.core.types import ChainId, FinalityTier, to_address
from liqengine.execution import ExecutionIntent, InclusionStrategy, LivenessToken
from liqengine.risk import (
    DEMOTION_TRIGGERS,
    SHADOW_LIMITS,
    BreakerCondition,
    Gate,
    RefusalReason,
    RiskEngine,
    RiskLimits,
    RollingSum,
    Stage,
    check_promotion,
    demotion_for,
    required_gates,
)

VENUE = "aave_v3_arbitrum"
EXECUTOR = to_address("0x" + "e0" * 20)
POOL = to_address("0x" + "70" * 20)
CHAIN = ChainId(42161)
NOW = 1_000_000_000_000_000
HOUR = 3_600_000_000_000
DAY = 24 * HOUR
UNIT = 10**8  # one dollar in the oracle's base currency


def limits(**overrides) -> RiskLimits:
    base = dict(
        stage="canary",
        max_notional_per_tx_base=10_000 * UNIT,
        max_notional_per_day_base=50_000 * UNIT,
        max_daily_loss_base=500 * UNIT,
        max_hourly_loss_base=200 * UNIT,
        max_drawdown_base=1_000 * UNIT,
        max_consecutive_failures=3,
        max_in_flight=2,
    )
    return RiskLimits(**{**base, **overrides})


def intent(**overrides) -> ExecutionIntent:
    base = dict(
        intent_id="i-1",
        decision_id=1,
        venue=VENUE,
        chain_id=CHAIN,
        executor=EXECUTOR,
        to=POOL,
        calldata="0xdead",
        value=0,
        gas_limit=450_000,
        max_fee_per_gas=20_000_000,
        max_priority_fee_per_gas=0,
        inclusion_strategy=InclusionStrategy.LATENCY_RACE,
        simulation_block=100,
        expires_at_block=102,
        conservative_net_base=200 * UNIT,
    )
    return ExecutionIntent(**{**base, **overrides})


def engine(**overrides) -> RiskEngine:
    return RiskEngine(venue=VENUE, limits=limits(**overrides))


# ---------------------------------------------------------------- the happy path


def test_a_healthy_engine_issues_a_bound_token() -> None:
    target = intent()
    token = engine().issue_token(target, 5_000 * UNIT, NOW)
    assert isinstance(token, LivenessToken)
    assert token.authorises(target)
    assert token.is_fresh_at(NOW)


def test_the_token_expires_on_its_own() -> None:
    """A hung engine stops issuing, and the last token it issued dies without intervention."""
    token = engine().issue_token(intent(), 5_000 * UNIT, NOW)
    assert not token.is_fresh_at(NOW + 3_000_000_000)


def test_there_is_no_way_to_supply_your_own_verdict() -> None:
    """A caller that could pass a verdict could pass a favourable one.

    Checks for the shape of an escape hatch rather than pinning the exact parameter list —
    inputs to the evaluation are fine and will keep being added; a way to bypass it is not.
    """
    import inspect

    signature = str(inspect.signature(RiskEngine.issue_token)).lower()
    for escape in ("force", "override", "verdict", "skip", "bypass", "allow", "approved"):
        assert escape not in signature, f"issue_token accepts {escape!r}"


# ---------------------------------------------------------------- stage and caps


def test_shadow_issues_nothing() -> None:
    """Shadow runs the whole pipeline and signs nothing. Its cap is zero, not absent."""
    result = RiskEngine(venue=VENUE, limits=SHADOW_LIMITS).issue_token(intent(), UNIT, NOW)
    assert result.reason is RefusalReason.STAGE_DOES_NOT_SIGN


def test_a_zero_cap_fails_closed_rather_than_reading_as_unlimited() -> None:
    result = engine(max_notional_per_tx_base=0).issue_token(intent(), UNIT, NOW)
    assert result.reason is RefusalReason.STAGE_DOES_NOT_SIGN


def test_the_per_transaction_cap_is_enforced() -> None:
    result = engine().issue_token(intent(), 20_000 * UNIT, NOW)
    assert result.reason is RefusalReason.NOTIONAL_ABOVE_PER_TX_CAP


def test_the_daily_notional_cap_accumulates() -> None:
    risk = engine()
    for _ in range(5):
        risk.record_submission(10_000 * UNIT, NOW)
        risk.record_outcome(succeeded=True, net_base=UNIT, finality=FinalityTier.FINALIZED,
                            now_ns=NOW)
    result = risk.issue_token(intent(), 10_000 * UNIT, NOW)
    assert result.reason is RefusalReason.NOTIONAL_ABOVE_DAILY_CAP


def test_the_daily_window_slides_rather_than_resetting_at_midnight() -> None:
    """A calendar reset lets a bad run spend two days of budget in two minutes."""
    risk = engine()
    for _ in range(5):
        risk.record_submission(10_000 * UNIT, NOW)
        risk.record_outcome(succeeded=True, net_base=UNIT, finality=FinalityTier.FINALIZED,
                            now_ns=NOW)
    assert risk.issue_token(intent(), 10_000 * UNIT, NOW).reason is not None
    later = NOW + DAY + 1
    assert isinstance(risk.issue_token(intent(), 10_000 * UNIT, later), LivenessToken)


def test_an_unprofitable_intent_is_refused_even_with_headroom() -> None:
    result = engine().issue_token(intent(conservative_net_base=0), UNIT, NOW)
    assert result.reason is RefusalReason.NOT_PROFITABLE


def test_an_intent_for_another_venue_is_refused() -> None:
    result = engine().issue_token(intent(venue="aave_v3_base"), UNIT, NOW)
    assert result.reason is RefusalReason.WRONG_VENUE


def test_in_flight_transactions_are_capped() -> None:
    risk = engine(max_in_flight=1)
    risk.record_submission(UNIT, NOW)
    result = risk.issue_token(intent(), UNIT, NOW)
    assert result.reason is RefusalReason.TOO_MANY_IN_FLIGHT


def test_a_resolved_transaction_frees_its_slot() -> None:
    risk = engine(max_in_flight=1)
    risk.record_submission(UNIT, NOW)
    risk.record_outcome(succeeded=True, net_base=UNIT, finality=FinalityTier.FINALIZED, now_ns=NOW)
    assert isinstance(risk.issue_token(intent(), UNIT, NOW), LivenessToken)


# ---------------------------------------------------------------- the loss asymmetry


def test_losses_count_immediately_and_profits_only_when_final() -> None:
    """Gas is burnt whatever the chain decides later; profit is not profit until irreversible."""
    risk = engine()
    risk.record_outcome(
        succeeded=True, net_base=400 * UNIT, finality=FinalityTier.CONFIRMED, now_ns=NOW
    )
    risk.record_outcome(succeeded=False, net_base=-150 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=NOW)
    # The unfinalised gain gave no headroom, so the loss lands against an untouched budget.
    risk.record_outcome(succeeded=False, net_base=-60 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=NOW)
    assert BreakerCondition.HOURLY_LOSS_LIMIT in {
        b.condition for b in risk.active_breakers(NOW)
    }


def test_a_finalised_profit_does_build_equity() -> None:
    risk = engine()
    risk.record_outcome(
        succeeded=True, net_base=400 * UNIT, finality=FinalityTier.FINALIZED, now_ns=NOW
    )
    risk.record_outcome(
        succeeded=False, net_base=-100 * UNIT, finality=FinalityTier.CONFIRMED, now_ns=NOW
    )
    assert BreakerCondition.DRAWDOWN not in {b.condition for b in risk.active_breakers(NOW)}


# ---------------------------------------------------------------- breakers


def test_consecutive_failures_stop_trading() -> None:
    risk = engine(max_consecutive_failures=3)
    for _ in range(3):
        risk.record_outcome(succeeded=False, net_base=-UNIT, finality=FinalityTier.CONFIRMED,
                            now_ns=NOW)
    result = risk.issue_token(intent(), UNIT, NOW)
    assert result.reason is RefusalReason.BREAKER_TRIPPED
    assert "consecutive" in result.detail


def test_a_success_resets_the_failure_streak() -> None:
    risk = engine(max_consecutive_failures=3)
    for _ in range(2):
        risk.record_outcome(succeeded=False, net_base=-UNIT, finality=FinalityTier.CONFIRMED,
                            now_ns=NOW)
    risk.record_outcome(succeeded=True, net_base=UNIT, finality=FinalityTier.FINALIZED, now_ns=NOW)
    risk.record_outcome(succeeded=False, net_base=-UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=NOW)
    assert not risk.halted


def test_the_hourly_loss_breaker_expires_with_its_window() -> None:
    risk = engine()
    risk.record_outcome(succeeded=False, net_base=-250 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=NOW)
    assert risk.halted
    assert not risk.active_breakers(NOW + HOUR + 1)


def test_abnormal_gas_is_a_model_problem_not_a_cost_problem() -> None:
    """Every downstream estimate was fitted in ordinary conditions."""
    risk = engine()
    risk.observe_gas(current_wei=600, baseline_wei=100, now_ns=NOW)
    assert BreakerCondition.ABNORMAL_GAS in {b.condition for b in risk.active_breakers(NOW)}


def test_normal_gas_does_not_trip() -> None:
    risk = engine()
    risk.observe_gas(current_wei=200, baseline_wei=100, now_ns=NOW)
    assert not risk.halted


def test_drawdown_from_peak_stops_trading() -> None:
    risk = engine(max_drawdown_base=300 * UNIT)
    risk.record_outcome(succeeded=True, net_base=500 * UNIT, finality=FinalityTier.FINALIZED,
                        now_ns=NOW)
    risk.record_outcome(succeeded=False, net_base=-310 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=NOW)
    assert BreakerCondition.DRAWDOWN in {b.condition for b in risk.active_breakers(NOW)}


# ---------------------------------------------------------------- latching


def test_a_latching_breaker_does_not_expire() -> None:
    """A codehash change is not a budget. Waiting does not make the contract familiar again."""
    risk = engine()
    risk.trip(BreakerCondition.CODEHASH_CHANGED, "pool proxy upgraded", NOW)
    assert risk.active_breakers(NOW + 30 * DAY)


def test_clearing_a_latch_requires_a_named_acknowledgement() -> None:
    risk = engine()
    risk.trip(BreakerCondition.ORACLE_DIVERGENCE, "3% off reference", NOW)
    with pytest.raises(ValueError, match="requires an explicit acknowledged_by"):
        risk.clear(BreakerCondition.ORACLE_DIVERGENCE)
    risk.clear(BreakerCondition.ORACLE_DIVERGENCE, acknowledged_by="operator")
    assert not risk.halted


def test_a_windowed_breaker_can_be_cleared_without_ceremony() -> None:
    risk = engine()
    risk.trip(BreakerCondition.ABNORMAL_GAS, "spike", NOW)
    risk.clear(BreakerCondition.ABNORMAL_GAS)
    assert not risk.halted


def test_manual_halt_latches() -> None:
    risk = engine()
    risk.trip(BreakerCondition.MANUAL_HALT, "operator stopped it", NOW)
    assert risk.issue_token(intent(), UNIT, NOW).reason is RefusalReason.BREAKER_TRIPPED
    assert risk.active_breakers(NOW + DAY)


def test_which_conditions_latch_is_explicit() -> None:
    assert BreakerCondition.CODEHASH_CHANGED.latches
    assert BreakerCondition.SIM_REALISED_DIVERGENCE.latches
    assert BreakerCondition.POSITION_BOOK_DRIFT.latches
    assert not BreakerCondition.DAILY_LOSS_LIMIT.latches
    assert not BreakerCondition.ABNORMAL_GAS.latches


# ---------------------------------------------------------------- feeds


def test_a_feed_that_never_reported_stops_trading() -> None:
    """Otherwise a price source that was never connected is never missed."""
    risk = engine()
    risk.require_feeds("oracle", "gas")
    result = risk.issue_token(intent(), UNIT, NOW)
    assert result.reason is RefusalReason.BREAKER_TRIPPED
    assert "never reported" in result.detail


def test_a_stale_feed_stops_trading_and_recovers_when_it_returns() -> None:
    risk = engine(max_feed_age_ns=5_000_000_000)
    risk.require_feeds("oracle")
    risk.feed_heartbeat("oracle", NOW)
    assert isinstance(risk.issue_token(intent(), UNIT, NOW), LivenessToken)

    stale = NOW + 10_000_000_000
    assert risk.issue_token(intent(), UNIT, stale).reason is RefusalReason.BREAKER_TRIPPED

    risk.feed_heartbeat("oracle", stale)
    assert isinstance(risk.issue_token(intent(), UNIT, stale), LivenessToken)


# ---------------------------------------------------------------- rolling window


# ---------------------------------------------------------------- the added limits


def test_the_binding_cap_is_the_tighter_of_percentage_and_absolute() -> None:
    """Percentage scales with capital; absolute still holds when the capital figure is wrong."""
    tight_pct = limits(
        max_notional_per_tx_base=10_000 * UNIT,
        strategy_capital_base=1_000_000 * UNIT,
        max_risk_per_opportunity_bps=25,  # 0.25% = 2,500
    )
    assert tight_pct.opportunity_cap_base() == 2_500 * UNIT

    tight_abs = limits(
        max_notional_per_tx_base=1_000 * UNIT,
        strategy_capital_base=1_000_000 * UNIT,
        max_risk_per_opportunity_bps=25,
    )
    assert tight_abs.opportunity_cap_base() == 1_000 * UNIT


def test_unset_capital_falls_back_to_the_absolute_cap() -> None:
    """A percentage of an unknown total is not a limit, and must not read as unlimited."""
    assert limits(strategy_capital_base=0).opportunity_cap_base() == 10_000 * UNIT


def test_a_trade_that_barely_beats_its_gas_is_refused() -> None:
    """A rounding error taking real execution risk."""
    risk = engine(min_profit_to_gas_ratio=5)
    result = risk.issue_token(
        intent(conservative_net_base=2 * UNIT), UNIT, NOW, gas_cost_base=1 * UNIT
    )
    assert result.reason is RefusalReason.PROFIT_TO_GAS_TOO_LOW


def test_a_trade_clearing_the_gas_ratio_passes() -> None:
    risk = engine(min_profit_to_gas_ratio=5)
    assert isinstance(
        risk.issue_token(intent(conservative_net_base=20 * UNIT), UNIT, NOW, gas_cost_base=UNIT),
        LivenessToken,
    )


def test_gas_above_the_per_transaction_cap_is_refused() -> None:
    risk = engine(max_gas_per_tx_base=5 * UNIT)
    result = risk.issue_token(intent(), UNIT, NOW, gas_cost_base=50 * UNIT)
    assert result.reason is RefusalReason.GAS_ABOVE_CAP


#: Per-opportunity sizing is not what these two tests are about, so it is opened up to let the
#: concentration limit be the binding one. In the presets the sizing cap binds long first.
CONCENTRATION_ONLY = dict(
    strategy_capital_base=100_000 * UNIT, max_risk_per_opportunity_bps=10_000
)


def test_protocol_exposure_is_capped() -> None:
    """Missing this is how a diversified-looking book turns out to be one bet."""
    risk = engine(**CONCENTRATION_ONLY, max_protocol_exposure_bps=1_000)  # 10% = 10,000
    risk.record_submission(9_000 * UNIT, NOW, intent_id="a", protocol="aave_v3", asset="USDC")
    result = risk.issue_token(intent(), 5_000 * UNIT, NOW, protocol="aave_v3", asset="WETH")
    assert result.reason is RefusalReason.PROTOCOL_EXPOSURE


def test_asset_exposure_is_capped_separately() -> None:
    """Two protocols, one asset: diversified by venue and concentrated by risk."""
    risk = engine(**CONCENTRATION_ONLY, max_asset_exposure_bps=500)  # 5% = 5,000
    risk.record_submission(4_000 * UNIT, NOW, intent_id="a", protocol="aave_v3", asset="WETH")
    result = risk.issue_token(intent(), 2_000 * UNIT, NOW, protocol="compound", asset="WETH")
    assert result.reason is RefusalReason.ASSET_EXPOSURE


def test_the_tighter_of_sizing_and_concentration_binds() -> None:
    """The 0.25% default is far tighter than the 10% protocol cap, and should win."""
    risk = engine(strategy_capital_base=100_000 * UNIT)  # 0.25% = 250
    result = risk.issue_token(intent(), 5_000 * UNIT, NOW, protocol="aave_v3", asset="WETH")
    assert result.reason is RefusalReason.NOTIONAL_ABOVE_PER_TX_CAP


def test_exposure_is_released_when_the_transaction_resolves() -> None:
    """It measures capital genuinely at risk, not cumulative turnover."""
    risk = engine(strategy_capital_base=100_000 * UNIT)
    risk.record_submission(9_000 * UNIT, NOW, intent_id="a", protocol="aave_v3", asset="USDC")
    protocols, _ = risk.exposure()
    assert protocols["aave_v3"] == 9_000 * UNIT

    risk.record_outcome(
        succeeded=True, net_base=UNIT, finality=FinalityTier.FINALIZED, now_ns=NOW, intent_id="a"
    )
    protocols, assets = risk.exposure()
    assert protocols["aave_v3"] == 0
    assert assets["USDC"] == 0


def test_exposure_limits_are_skipped_when_capital_is_unknown() -> None:
    """Stated rather than silently permissive: capital is required configuration to sign."""
    risk = engine(strategy_capital_base=0)
    risk.record_submission(9_000 * UNIT, NOW, intent_id="a", protocol="aave_v3", asset="USDC")
    assert isinstance(
        risk.issue_token(intent(), UNIT, NOW, protocol="aave_v3", asset="USDC"), LivenessToken
    )


def test_a_scattered_failure_rate_trips_even_without_a_streak() -> None:
    """A bot failing every third attempt never trips a consecutive counter and is still broken."""
    risk = engine(max_consecutive_failures=99, max_failures_per_hour=3)
    for _ in range(3):
        risk.record_outcome(succeeded=False, net_base=-UNIT, finality=FinalityTier.CONFIRMED,
                            now_ns=NOW)
        risk.record_outcome(succeeded=True, net_base=UNIT, finality=FinalityTier.FINALIZED,
                            now_ns=NOW)
    assert BreakerCondition.FAILURE_RATE in {b.condition for b in risk.active_breakers(NOW)}


def test_the_weekly_loss_limit_outlives_the_daily_one() -> None:
    """Seven bad days inside the daily limit is still a bad week."""
    risk = engine(max_daily_loss_base=500 * UNIT, max_weekly_loss_base=900 * UNIT)
    risk.record_outcome(succeeded=False, net_base=-400 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=NOW)
    later = NOW + 2 * DAY
    risk.record_outcome(succeeded=False, net_base=-400 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=later)
    third = NOW + 4 * DAY
    risk.record_outcome(succeeded=False, net_base=-200 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=third)
    conditions = {b.condition for b in risk.active_breakers(third)}
    assert BreakerCondition.WEEKLY_LOSS_LIMIT in conditions
    assert BreakerCondition.DAILY_LOSS_LIMIT not in conditions


def test_a_single_catastrophic_loss_says_the_sizing_model_is_wrong() -> None:
    """The percentage limits scale with capital; this backstop does not."""
    risk = engine(max_loss_per_tx_base=100 * UNIT)
    risk.record_outcome(succeeded=False, net_base=-5_000 * UNIT, finality=FinalityTier.CONFIRMED,
                        now_ns=NOW)
    assert BreakerCondition.SIM_REALISED_DIVERGENCE in {
        b.condition for b in risk.active_breakers(NOW)
    }


def test_there_is_no_minimum_win_probability_setting() -> None:
    """A p(win) floor selects for uncontested dust.

    Phase 0: the largest pooled winner took a quarter of all liquidations at a median of $0.01
    and made $35 in six months. Nobody contests those, so their win rate is near certain and
    their value is nil. EV already contains p(win) and weighs it against the prize (D2).
    """
    assert not any("win_probability" in f for f in RiskLimits.__dataclass_fields__)
    assert not any("execution_probability" in f for f in RiskLimits.__dataclass_fields__)


def test_a_low_wallet_balance_stops_trading() -> None:
    """A hot wallet running dry mid-burst otherwise fails silently — nothing in the trade data
    explains why the system stopped winning anything."""
    risk = engine()
    risk.limits = limits(min_wallet_balance_wei=10**16)  # 0.01 ETH
    risk.observe_wallet_balance(5 * 10**15, NOW)  # 0.005 ETH
    assert BreakerCondition.WALLET_BALANCE_LOW in {b.condition for b in risk.active_breakers(NOW)}


def test_topping_up_the_wallet_clears_the_breaker_without_a_human() -> None:
    """A breaker only a human could clear turns "send ETH" into "send ETH, then remember to
    also run an unrelated command"."""
    risk = engine()
    risk.limits = limits(min_wallet_balance_wei=10**16)
    risk.observe_wallet_balance(5 * 10**15, NOW)
    assert risk.halted
    risk.observe_wallet_balance(2 * 10**16, NOW)
    assert not risk.halted


def test_the_balance_check_is_disabled_by_default() -> None:
    """Zero disables it — appropriate for shadow, where the wallet holds nothing."""
    risk = engine()
    risk.observe_wallet_balance(0, NOW)
    assert not risk.halted


def test_the_rolling_sum_evicts_by_time() -> None:
    window = RollingSum(window_ns=HOUR)
    window.add(100, NOW)
    window.add(50, NOW + HOUR // 2)
    assert window.total(NOW + HOUR // 2) == 150
    assert window.total(NOW + HOUR + 1) == 50
    assert window.total(NOW + 2 * HOUR) == 0


# ---------------------------------------------------------------- the promotion ladder


ALL_GATES = set(Gate)


def test_a_venue_cannot_skip_a_rung() -> None:
    """"Never skipping" enforced by convention is skipped the first time someone is in a hurry."""
    check = check_promotion(Stage.SHADOW, Stage.LIVE, ALL_GATES)
    assert not check.permitted
    assert "cannot skip" in check.detail()
    assert "canary" in check.detail()


def test_a_fully_gated_promotion_is_permitted() -> None:
    assert check_promotion(Stage.SHADOW, Stage.CANARY, ALL_GATES).permitted


def test_a_missing_gate_names_itself() -> None:
    """"Gate 4 failed" sends someone to a document. The named gate does not."""
    passed = ALL_GATES - {Gate.WIN_MODEL_CALIBRATED}
    check = check_promotion(Stage.SHADOW, Stage.CANARY, passed)
    assert not check.permitted
    assert check.missing_gates == (Gate.WIN_MODEL_CALIBRATED,)
    assert "win_model_calibrated" in check.detail()


def test_canary_requires_a_measured_backtest_exit() -> None:
    """D18 again: an assumed exit cannot authorise real money in a backtest either."""
    passed = ALL_GATES - {Gate.BACKTEST_EXIT_MEASURED}
    assert not check_promotion(Stage.SHADOW, Stage.CANARY, passed).permitted


def test_shadow_carries_no_financial_gates_but_does_require_the_right_contracts() -> None:
    """A shadow run against the wrong pool produces confident, worthless data."""
    assert required_gates(Stage.SHADOW) == (Gate.CONTRACTS_VERIFIED,)
    assert check_promotion(Stage.PHASE0, Stage.SHADOW, {Gate.CONTRACTS_VERIFIED}).permitted
    assert not check_promotion(Stage.PHASE0, Stage.SHADOW, set()).permitted


def test_live_additionally_requires_canary_to_have_made_money() -> None:
    assert Gate.CANARY_PNL_POSITIVE in required_gates(Stage.LIVE)
    assert Gate.CANARY_PNL_POSITIVE not in required_gates(Stage.CANARY)


def test_gate_requirements_are_cumulative_up_the_ladder() -> None:
    """Reading one line has to answer the whole question, not just what the rung adds."""
    assert set(required_gates(Stage.CANARY)) <= set(required_gates(Stage.LIVE))


def test_stopping_is_never_obstructed() -> None:
    """Demotion may jump any distance and needs no gates."""
    assert check_promotion(Stage.LIVE, Stage.SHADOW, set()).permitted
    assert check_promotion(Stage.LIVE, Stage.HALTED, set()).permitted
    assert check_promotion(Stage.CANARY, Stage.DORMANT, set()).permitted


def test_promoting_to_the_current_stage_is_refused() -> None:
    check = check_promotion(Stage.CANARY, Stage.CANARY, ALL_GATES)
    assert not check.permitted
    assert "already at this stage" in check.detail()


def test_a_model_wrong_breaker_halts_rather_than_demotes() -> None:
    """Continuing to trade on a model known to be wrong is the decision this table pre-makes."""
    assert demotion_for(BreakerCondition.CODEHASH_CHANGED) is Stage.HALTED
    assert demotion_for(BreakerCondition.SIM_REALISED_DIVERGENCE) is Stage.HALTED


def test_a_bad_day_falls_back_to_shadow_rather_than_stopping() -> None:
    """The pipeline keeps running and producing data without spending anything."""
    assert demotion_for(BreakerCondition.DRAWDOWN) is Stage.SHADOW
    assert demotion_for(BreakerCondition.DAILY_LOSS_LIMIT) is Stage.SHADOW


def test_an_ordinary_breaker_does_not_change_stage() -> None:
    """Abnormal gas stops trading through the engine; it is not a deployment decision."""
    assert demotion_for(BreakerCondition.ABNORMAL_GAS) is None
    assert demotion_for(BreakerCondition.STALE_FEED) is None


def test_every_latching_breaker_has_a_declared_demotion() -> None:
    """A latching breaker with no declared response is a decision made under pressure."""
    latching = {c for c in BreakerCondition if c.latches}
    assert latching <= set(DEMOTION_TRIGGERS)
