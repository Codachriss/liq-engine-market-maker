"""Tests for execution probability, latency measurement, and calibration.

The properties pinned here are the ones that decide whether `p(win)` can be trusted to multiply
into EV: an interval that widens when the sample is thin, backoff that refuses to believe a
0-for-2 bucket, a conditional decomposition that does not count the race twice, and a
calibration gate that fails closed on insufficient data.
"""

from __future__ import annotations

import pytest

from liqengine.core.fixedpoint import PERCENTAGE_FACTOR
from liqengine.winmodel import (
    CalibrationTracker,
    EmpiricalWinModel,
    LatencyDistribution,
    LatencyTrace,
    NonRaceHazards,
    PriorWinModel,
    RaceOutcome,
    Stage,
    WinEstimate,
    WinFeatures,
    combine,
    competition_bucket,
    size_bucket,
    wilson_interval,
)

MS = 1_000_000  # nanoseconds per millisecond
ARBITRUM = "aave_v3_arbitrum"


def features(size: str = "over_10k", competition: str = "light") -> WinFeatures:
    return WinFeatures(venue=ARBITRUM, size_bucket=size, competition_bucket=competition)


# ---------------------------------------------------------------- latency


def test_the_trace_covers_the_whole_race_not_just_our_part() -> None:
    """Own pipeline latency says how fast we were, never whether it was fast enough."""
    trace = LatencyTrace(trace_id="t1")
    trace.stamp(Stage.ORACLE_KNOWABLE, 0)
    trace.stamp(Stage.DETECTED, 5 * MS)
    trace.stamp(Stage.SIMULATED, 12 * MS)
    trace.stamp(Stage.SIGNED, 14 * MS)
    trace.stamp(Stage.SUBMITTED, 18 * MS)
    trace.stamp(Stage.SEQUENCER_ACK, 40 * MS)
    trace.stamp(Stage.INCLUDED, 300 * MS)

    spans = trace.spans_ms()
    assert spans["oracle_to_detect"] == 5.0
    assert spans["submit_to_ack"] == 22.0
    assert spans["total"] == 300.0
    assert trace.decision_to_submit_ms() == 13.0


def test_a_pull_oracle_can_produce_a_negative_span() -> None:
    """We hold the signed price before it is on chain; that is the D7 advantage, not an error."""
    trace = LatencyTrace(trace_id="pyth")
    trace.stamp(Stage.ORACLE_KNOWABLE, 100 * MS)
    trace.stamp(Stage.DETECTED, 60 * MS)
    assert trace.span_ms(Stage.ORACLE_KNOWABLE, Stage.DETECTED) == -40.0


def test_restamping_a_stage_is_an_error() -> None:
    """A retry needs a new trace; silently taking the later value hides the first attempt."""
    trace = LatencyTrace(trace_id="t1")
    trace.stamp(Stage.DETECTED, 1)
    with pytest.raises(ValueError, match="already stamped"):
        trace.stamp(Stage.DETECTED, 2)


def test_unstamped_spans_are_omitted_not_zeroed() -> None:
    """A zero span reads as instantaneous. Absent has to stay absent."""
    trace = LatencyTrace(trace_id="t1")
    trace.stamp(Stage.DETECTED, 0)
    trace.stamp(Stage.SIMULATED, 3 * MS)
    spans = trace.spans_ms()
    assert spans == {"detect_to_simulate": 3.0}
    assert trace.total_ms() is None


def test_losing_on_ordering_is_distinguishable() -> None:
    """A valid transaction that simply arrived later is a latency problem, not a strategy one."""
    lost = RaceOutcome(won=False, our_tx_index=7, winner_tx_index=2, deficit_ms=31.0)
    assert lost.beaten_by_ordering
    assert not RaceOutcome(won=True, our_tx_index=2, winner_tx_index=2).beaten_by_ordering


def test_the_tail_is_what_loses_races() -> None:
    """A 40ms mean with a 400ms p99 loses one race in a hundred for reasons the mean hides."""
    distribution = LatencyDistribution("submit_to_ack")
    distribution.extend([40.0] * 99 + [400.0])
    assert distribution.mean == pytest.approx(43.6)
    assert distribution.p50 == 40.0
    assert distribution.p99 == 40.0
    assert distribution.percentile(1.0) == 400.0


def test_an_empty_distribution_reports_nothing_rather_than_zero() -> None:
    distribution = LatencyDistribution("empty")
    assert distribution.p99 is None
    assert distribution.summary()["samples"] == 0


# ---------------------------------------------------------------- the prior


def test_with_no_data_the_estimate_is_a_marked_placeholder() -> None:
    """A plausible point estimate here is how a strategy passes a backtest and loses live."""
    estimate = PriorWinModel().estimate(features())
    assert estimate.is_placeholder
    assert estimate.samples == 0
    assert estimate.lower_bps == 0
    assert estimate.upper_bps == PERCENTAGE_FACTOR


def test_the_prior_is_pessimistic() -> None:
    """208 distinct winners on Arbitrum, top one at 22%. Assuming better is assuming a loss."""
    assert PriorWinModel().estimate(features()).bps <= 2_500


def test_the_conservative_case_takes_the_lower_bound() -> None:
    estimate = WinEstimate(bps=6_000, lower_bps=3_000, upper_bps=8_500, samples=25, source="e")
    assert estimate.to_win_probability().bps == 6_000
    assert estimate.to_win_probability(conservative=True).bps == 3_000


# ---------------------------------------------------------------- the empirical model


def test_a_thin_sample_produces_a_wide_interval() -> None:
    """Three for three is not a 100% win rate, and the interval has to say so."""
    point, lower, upper = wilson_interval(3, 3)
    assert point == 1.0
    assert lower < 0.5
    assert upper == 1.0


def test_the_interval_narrows_as_evidence_arrives() -> None:
    _, thin_lower, thin_upper = wilson_interval(6, 10)
    _, thick_lower, thick_upper = wilson_interval(600, 1_000)
    assert (thin_upper - thin_lower) > (thick_upper - thick_lower)


def test_zero_for_n_does_not_collapse_to_certainty() -> None:
    """The normal approximation gives a zero-width interval here. Wilson does not."""
    point, lower, upper = wilson_interval(0, 20)
    assert point == 0.0
    assert lower == 0.0
    assert upper > 0.1


def test_a_thin_bucket_backs_off_rather_than_believing_itself() -> None:
    model = EmpiricalWinModel(min_samples=30)
    model.record_many(features(competition="heavy"), wins=0, losses=2)
    estimate = model.estimate(features(competition="heavy"))
    assert estimate.is_placeholder  # not 0%
    assert estimate.samples == 0


def test_backoff_walks_from_specific_to_general() -> None:
    model = EmpiricalWinModel(min_samples=30)
    # Plenty of venue-and-size evidence, but nothing for this competition band specifically.
    model.record_many(
        WinFeatures(venue=ARBITRUM, size_bucket="over_10k", competition_bucket="light"),
        wins=20,
        losses=30,
    )
    estimate = model.estimate(features(competition="heavy"))
    assert estimate.source == "empirical"
    assert estimate.matched_key == f"{ARBITRUM}|over_10k"
    assert estimate.samples == 50


def test_the_most_specific_bucket_wins_when_it_has_evidence() -> None:
    model = EmpiricalWinModel(min_samples=30)
    model.record_many(features(competition="light"), wins=40, losses=10)
    model.record_many(features(competition="heavy"), wins=5, losses=45)
    light = model.estimate(features(competition="light"))
    heavy = model.estimate(features(competition="heavy"))
    assert light.matched_key.endswith("|light")
    assert heavy.matched_key.endswith("|heavy")
    assert light.bps > heavy.bps


def test_losses_are_recorded_or_the_model_believes_it_always_wins() -> None:
    model = EmpiricalWinModel(min_samples=10)
    for _ in range(6):
        model.record(features(), won=True)
    for _ in range(14):
        model.record(features(), won=False)
    estimate = model.estimate(features())
    assert estimate.samples == 20
    assert estimate.bps == pytest.approx(3_000, abs=1)


def test_size_buckets_follow_the_phase0_distribution() -> None:
    """71% of Arbitrum liquidations repay under $100; pooling them swamps the contested range."""
    unit = 10**8
    assert size_bucket(29 * unit // 100) == "under_1"      # the $0.29 median
    assert size_bucket(50 * unit) == "under_100"
    assert size_bucket(5_000 * unit) == "under_10k"
    assert size_bucket(50_000 * unit) == "over_10k"


def test_competition_buckets_handle_the_unobserved_case() -> None:
    assert competition_bucket(None) == "unknown"
    assert competition_bucket(0) == "uncontested"
    assert competition_bucket(5) == "heavy"


# ---------------------------------------------------------------- the decomposition


def test_the_decomposition_is_conditional_not_a_second_independent_factor() -> None:
    """You revert because someone else won; those are one event, not two to multiply."""
    first = WinEstimate(bps=4_000, lower_bps=3_000, upper_bps=5_000, samples=100, source="e")
    combined = combine(first, non_race_failure_bps=500)
    assert combined.bps == 3_800   # 40% x 95%
    assert combined.lower_bps == 2_850
    assert combined.samples == 100
    assert combined.source == "e"


def test_no_conditional_failure_leaves_the_estimate_untouched() -> None:
    first = WinEstimate(bps=4_000, lower_bps=3_000, upper_bps=5_000, samples=100, source="e")
    assert combine(first, 0) == first


def test_hazards_compose_as_a_union_not_a_sum() -> None:
    """Summing overflows past certainty once any single hazard gets large."""
    hazards = NonRaceHazards(gas_limit_too_low_bps=3_000, slippage_bound_breached_bps=3_000)
    assert hazards.total_bps() == 5_100  # 1 - 0.7*0.7, not 6,000
    assert hazards.total_bps() < PERCENTAGE_FACTOR


def test_hazards_are_named_so_the_fix_is_knowable() -> None:
    """One aggregate P(fail) cannot tell you whether to retune gas or requote the exit."""
    hazards = NonRaceHazards(self_cured_bps=200, nonce_collision_bps=50)
    assert hazards.total_bps() == pytest.approx(249, abs=1)
    assert NonRaceHazards().total_bps() == 0


def test_an_out_of_range_hazard_is_rejected() -> None:
    with pytest.raises(ValueError, match="out of range"):
        NonRaceHazards(other_bps=20_000).total_bps()
    with pytest.raises(ValueError, match="out of range"):
        combine(WinEstimate(1, 1, 1, 1, "e"), -5)


# ---------------------------------------------------------------- calibration


def _feed(tracker: CalibrationTracker, predicted_bps: int, wins: int, losses: int) -> None:
    for _ in range(wins):
        tracker.record(predicted_bps, True)
    for _ in range(losses):
        tracker.record(predicted_bps, False)


def test_a_calibrated_model_passes() -> None:
    """A stated 30% that wins 30% of the time, across bands."""
    tracker = CalibrationTracker(min_samples=100)
    _feed(tracker, 3_000, wins=30, losses=70)
    _feed(tracker, 7_000, wins=70, losses=30)
    report = tracker.report()
    assert report.passed
    assert report.ece_bps < 100


def test_an_overconfident_model_fails_and_says_where() -> None:
    """Right on average, wrong in the band the marginal trades live in."""
    tracker = CalibrationTracker(min_samples=100)
    _feed(tracker, 8_000, wins=40, losses=60)   # claims 80%, delivers 40%
    _feed(tracker, 2_000, wins=60, losses=40)   # claims 20%, delivers 60%
    report = tracker.report()
    assert not report.passed
    assert report.ece_bps > 3_000
    assert len(report.overconfident_bins) == 1
    assert report.overconfident_bins[0].gap_bps == 4_000
    assert "worst_band" in report.detail()


def test_insufficient_data_fails_closed() -> None:
    """An unmeasured gate that defaults to open is not a gate."""
    tracker = CalibrationTracker(min_samples=100)
    _feed(tracker, 5_000, wins=5, losses=5)
    report = tracker.report()
    assert not report.measurable
    assert not report.passed
    assert "need 100" in report.detail()


def test_ece_weights_bands_by_how_often_they_are_used() -> None:
    """Being wrong where the strategy never trades matters less than where it lives."""
    rarely_wrong = CalibrationTracker(min_samples=10)
    _feed(rarely_wrong, 5_000, wins=50, losses=50)   # 100 samples, perfect
    _feed(rarely_wrong, 9_000, wins=0, losses=2)     # 2 samples, badly wrong

    mostly_wrong = CalibrationTracker(min_samples=10)
    _feed(mostly_wrong, 5_000, wins=50, losses=50)
    _feed(mostly_wrong, 9_000, wins=0, losses=100)

    assert rarely_wrong.report().ece_bps < mostly_wrong.report().ece_bps


def test_brier_rewards_confident_correctness() -> None:
    confident = CalibrationTracker(min_samples=10)
    _feed(confident, 9_500, wins=95, losses=5)

    hedging = CalibrationTracker(min_samples=10)
    _feed(hedging, 5_000, wins=95, losses=5)

    assert confident.report().brier_bps < hedging.report().brier_bps


def test_a_probability_outside_the_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="out of range"):
        CalibrationTracker().record(12_000, True)
