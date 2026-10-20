"""Tests for the step-4 store surface: positions, parameters, strategies, decisions, P&L.

These pin the properties that make the data trustworthy rather than merely present — value
precision, append-only discipline, parameter immutability, and the finality rule on P&L. Each
of them is a place where a plausible-looking implementation produces numbers that are wrong in
a flattering direction.
"""

from __future__ import annotations

import sqlite3

import pytest

from liqengine.core.types import ChainId, FinalityTier, to_address
from liqengine.store import (
    SCHEMA_VERSION,
    DecisionRecord,
    EventStore,
    InclusionAttemptRecord,
    OracleUpdateRecord,
    PositionLeg,
    PositionRecord,
    RealisedOutcomeRecord,
    ReserveConfigRecord,
    WalletProfileRecord,
    params_hash,
)

VENUE = "aave_v3_arbitrum"
CHAIN = ChainId(42161)
BORROWER = to_address("0x" + "b0" * 20)
WETH = to_address("0x" + "e1" * 20)
USDC = to_address("0x" + "c2" * 20)
HASH_A = "0x" + "aa" * 32
HASH_B = "0x" + "bb" * 32


@pytest.fixture
def store(tmp_path) -> EventStore:
    s = EventStore(tmp_path / "test.sqlite")
    s.initialise()
    yield s
    s.close()


# ---------------------------------------------------------------- schema and migration


def test_fresh_store_is_at_current_version(store: EventStore) -> None:
    assert store.schema_version() == SCHEMA_VERSION


def test_v1_store_migrates_additively(tmp_path) -> None:
    """A v1 store keeps every row and gains the new columns.

    Phase 0 data is real chain history that took hours of rate-limited scanning to collect.
    A migration that rewrites or drops rows is not an option.
    """
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL);
        CREATE TABLE decision (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venue TEXT NOT NULL, trace_id TEXT NOT NULL, observed_at TEXT NOT NULL,
            block_number INTEGER NOT NULL, block_hash TEXT NOT NULL,
            borrower TEXT NOT NULL, collateral_asset TEXT, debt_asset TEXT,
            outcome TEXT NOT NULL, reject_reason TEXT,
            health_factor_wad TEXT, debt_to_cover TEXT, predicted_gross_base TEXT,
            predicted_gas_base TEXT, predicted_slippage_base TEXT, predicted_net_base TEXT,
            win_probability_bps INTEGER, state_snapshot TEXT, latency_ms TEXT,
            orphaned INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE realised_pnl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL REFERENCES decision (id),
            venue TEXT NOT NULL, tx_hash TEXT, block_number INTEGER, block_hash TEXT,
            finality INTEGER NOT NULL DEFAULT 0, succeeded INTEGER NOT NULL,
            gas_used INTEGER, gas_cost_base TEXT, gross_base TEXT, net_base TEXT,
            recorded_at TEXT NOT NULL, orphaned INTEGER NOT NULL DEFAULT 0);
        INSERT INTO schema_version VALUES (1, '2026-08-15T00:00:00+00:00');
        INSERT INTO decision (venue, trace_id, observed_at, block_number, block_hash,
                              borrower, outcome)
        VALUES ('v', 't', '2026-08-15T00:00:00+00:00', 100, '0xdead', '0xbeef', 'detected');
        """
    )
    conn.commit()
    conn.close()

    with EventStore(path) as store:
        store.initialise()
        assert store.schema_version() == SCHEMA_VERSION
        row = store._conn.execute("SELECT * FROM decision WHERE id = 1").fetchone()
        assert row["trace_id"] == "t"          # the old row survived
        assert row["opportunity_uid"] == ""    # new NOT NULL column got its default
        assert row["sequencer_grace_active"] == 0
        assert store._has_column("realised_pnl", "booked_at")
        # New tables come from the DDL itself, not the migration list.
        assert store.reserve_config_at("v", "0x0", 1) is None


def test_migration_is_idempotent(tmp_path) -> None:
    path = tmp_path / "twice.sqlite"
    with EventStore(path) as store:
        store.initialise()
        store.initialise()
        assert store.schema_version() == SCHEMA_VERSION


def test_store_from_the_future_is_refused(tmp_path) -> None:
    """Older code opening a newer store would silently ignore columns it cannot see."""
    path = tmp_path / "future.sqlite"
    with EventStore(path) as store:
        store.initialise()
        store._conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2099-01-01')",
            (SCHEMA_VERSION + 1,),
        )
    with EventStore(path) as store, pytest.raises(RuntimeError, match="Refusing to open"):
        store.initialise()


# ---------------------------------------------------------------- positions


def test_position_is_multi_asset(store: EventStore) -> None:
    """Two collaterals and two debts on one account, which a single-asset schema cannot hold."""
    store.upsert_position(
        PositionRecord(
            venue=VENUE,
            chain_id=CHAIN,
            borrower=BORROWER,
            as_of_block=1000,
            as_of_block_hash=HASH_A,
            emode_category=1,
            legs=(
                PositionLeg(asset=WETH, kind="collateral", scaled_amount=5 * 10**18),
                PositionLeg(asset=USDC, kind="collateral", scaled_amount=2000 * 10**6),
                PositionLeg(asset=USDC, kind="variable_debt", scaled_amount=1500 * 10**6),
                PositionLeg(asset=WETH, kind="stable_debt", scaled_amount=10**17),
            ),
        )
    )
    legs = store.position_legs(VENUE, BORROWER)
    assert len(legs) == 4
    assert {(row["asset"], row["kind"]) for row in legs} == {
        (WETH, "collateral"),
        (USDC, "collateral"),
        (USDC, "variable_debt"),
        (WETH, "stable_debt"),
    }
    assert store.borrowers_with_debt(VENUE) == [BORROWER]


def test_repaid_leg_disappears_on_refresh(store: EventStore) -> None:
    """Merging instead of replacing would leave a repaid debt in the book forever."""
    base = dict(venue=VENUE, chain_id=CHAIN, borrower=BORROWER, as_of_block_hash=HASH_A)
    store.upsert_position(
        PositionRecord(
            **base,
            as_of_block=1000,
            legs=(
                PositionLeg(asset=WETH, kind="collateral", scaled_amount=10**18),
                PositionLeg(asset=USDC, kind="variable_debt", scaled_amount=500 * 10**6),
            ),
        )
    )
    store.upsert_position(
        PositionRecord(
            **base,
            as_of_block=1001,
            legs=(PositionLeg(asset=WETH, kind="collateral", scaled_amount=10**18),),
        )
    )
    assert [row["kind"] for row in store.position_legs(VENUE, BORROWER)] == ["collateral"]
    assert store.borrowers_with_debt(VENUE) == []


def test_projection_cannot_erase_on_chain_reconciliation(store: EventStore) -> None:
    """The gap between reconciled and projected HF is the drift alarm.

    A projection-sourced write that cleared the reconciled value would destroy the only signal
    that says the projection has gone wrong.
    """
    store.upsert_position(
        PositionRecord(
            venue=VENUE,
            chain_id=CHAIN,
            borrower=BORROWER,
            as_of_block=1000,
            as_of_block_hash=HASH_A,
            reconciled_hf_wad=1_020_000_000_000_000_000,
            reconciled_at_block=1000,
        )
    )
    store.upsert_position(
        PositionRecord(
            venue=VENUE,
            chain_id=CHAIN,
            borrower=BORROWER,
            as_of_block=1001,
            as_of_block_hash=HASH_B,
        )
    )
    row = store._conn.execute(
        "SELECT * FROM position WHERE venue = ? AND borrower = ?", (VENUE, BORROWER)
    ).fetchone()
    assert row["reconciled_hf_wad"] == "1020000000000000000"
    assert row["reconciled_at_block"] == 1000
    assert row["as_of_block"] == 1001


def test_scaled_amounts_survive_uint256(store: EventStore) -> None:
    """2^200 has no business fitting in a 64-bit column, and must round-trip exactly."""
    huge = 2**200
    store.upsert_position(
        PositionRecord(
            venue=VENUE,
            chain_id=CHAIN,
            borrower=BORROWER,
            as_of_block=1,
            as_of_block_hash=HASH_A,
            legs=(PositionLeg(asset=WETH, kind="variable_debt", scaled_amount=huge),),
        )
    )
    assert int(store.position_legs(VENUE, BORROWER)[0]["scaled_amount"]) == huge


def test_position_leg_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        PositionLeg(asset=WETH, kind="collatoral", scaled_amount=1)


# ---------------------------------------------------------------- protocol parameters


def test_reserve_config_reads_as_of_the_replayed_block(store: EventStore) -> None:
    """Governance loosened the threshold at block 2000; a replay of 1500 must not see it."""
    for block, threshold in ((1000, 8000), (2000, 8500)):
        store.record_reserve_config(
            ReserveConfigRecord(
                venue=VENUE,
                chain_id=CHAIN,
                asset=WETH,
                block_number=block,
                block_hash=HASH_A,
                source="event",
                liquidation_threshold_bps=threshold,
                liquidation_bonus_bps=10_500,
            )
        )
    assert store.reserve_config_at(VENUE, WETH, 1500)["liquidation_threshold_bps"] == 8000
    assert store.reserve_config_at(VENUE, WETH, 2000)["liquidation_threshold_bps"] == 8500
    assert store.reserve_config_at(VENUE, WETH, 999) is None


def test_reserve_config_source_is_constrained() -> None:
    with pytest.raises(ValueError, match="source must be"):
        ReserveConfigRecord(
            venue=VENUE,
            chain_id=CHAIN,
            asset=WETH,
            block_number=1,
            block_hash=HASH_A,
            source="guessed",
        )


def test_emode_parameters_are_versioned_too(store: EventStore) -> None:
    store.record_emode_category(
        VENUE, 1, 1000, HASH_A, label="ETH correlated", liquidation_threshold_bps=9500
    )
    assert store.emode_category_at(VENUE, 1, 1200)["liquidation_threshold_bps"] == 9500
    assert store.emode_category_at(VENUE, 1, 900) is None


# ---------------------------------------------------------------- triggers


def test_oracle_update_records_the_off_chain_reference(store: EventStore) -> None:
    """The deviation between chain price and off-chain mid is what predicts the next write."""
    store.record_oracle_updates(
        [
            OracleUpdateRecord(
                chain_id=CHAIN,
                uid="0xfeed:3",
                asset=WETH,
                oracle_type="chainlink_push",
                price=3_000_00000000,
                price_decimals=8,
                block_number=500,
                reference_price=3_015_00000000,
                reference_source="binance:ETHUSDT",
                deviation_bps=50,
            )
        ]
    )
    row = store.last_oracle_update(CHAIN, WETH)
    assert row["deviation_bps"] == 50
    assert row["reference_source"] == "binance:ETHUSDT"


def test_oracle_type_is_constrained() -> None:
    with pytest.raises(ValueError, match="oracle_type must be one of"):
        OracleUpdateRecord(
            chain_id=CHAIN, uid="x", asset=WETH, oracle_type="chainlink", price=1, price_decimals=8
        )


def test_grace_period_blocks_liquidations_after_the_sequencer_returns(store: EventStore) -> None:
    """Simulating through this window is how a backtest invents profit during the volatility."""
    store.record_sequencer_outage(CHAIN, "0xseq", down_at_timestamp=1000, grace_period_seconds=3600)
    assert store.liquidations_blocked_at(CHAIN, 2000) is True  # still down

    store.record_sequencer_outage(
        CHAIN, "0xseq", down_at_timestamp=1000, grace_period_seconds=3600, up_at_timestamp=5000
    )
    assert store.liquidations_blocked_at(CHAIN, 5001) is True   # inside the grace period
    assert store.liquidations_blocked_at(CHAIN, 8601) is False  # grace expired
    assert store.liquidations_blocked_at(CHAIN, 999) is False   # before it went down


# ---------------------------------------------------------------- strategies


def test_strategy_parameters_are_immutable(store: EventStore) -> None:
    """A changed parameter under the same version is a live mutation wearing a version number."""
    store.register_strategy_version("liq", 1, "baseline", {"min_ev": 100}, "operator")
    store.register_strategy_version("liq", 1, "baseline", {"min_ev": 100}, "operator")  # no-op

    with pytest.raises(ValueError, match="immutable"):
        store.register_strategy_version("liq", 1, "baseline", {"min_ev": 50}, "operator")

    store.register_strategy_version("liq", 2, "baseline", {"min_ev": 50}, "operator")
    assert store.current_stage("liq", 2) is None


def test_parameter_hash_ignores_key_order() -> None:
    """Otherwise a dict reordering registers a 'new version' that is identical in meaning."""
    assert params_hash({"a": 1, "b": 2}) == params_hash({"b": 2, "a": 1})


def test_promotion_requires_its_gates(store: EventStore) -> None:
    store.register_strategy_version("liq", 1, "baseline", {"min_ev": 100}, "operator")
    gates = ("backtest_positive_ev", "shadow_divergence_within_tolerance")

    with pytest.raises(ValueError, match="gates not passed"):
        store.promote_strategy("liq", 1, "canary", required_gates=gates)

    store.record_strategy_gate("liq", 1, "backtest_positive_ev", True)
    with pytest.raises(ValueError, match="shadow_divergence_within_tolerance"):
        store.promote_strategy("liq", 1, "canary", required_gates=gates)

    store.record_strategy_gate("liq", 1, "shadow_divergence_within_tolerance", True)
    store.promote_strategy("liq", 1, "canary", required_gates=gates)
    assert store.current_stage("liq", 1) == "canary"


def test_a_failed_gate_supersedes_an_earlier_pass(store: EventStore) -> None:
    """Re-running a gate must be able to revoke a promotion path, not just add to it."""
    store.register_strategy_version("liq", 1, "baseline", {}, "operator")
    store.record_strategy_gate("liq", 1, "backtest_positive_ev", True)
    store.record_strategy_gate("liq", 1, "backtest_positive_ev", False, "EV went negative")
    with pytest.raises(ValueError, match="gates not passed"):
        store.promote_strategy("liq", 1, "live", required_gates=("backtest_positive_ev",))


# ---------------------------------------------------------------- decisions


def _decision(**overrides) -> DecisionRecord:
    base = dict(
        opportunity_uid="opp-1",
        venue=VENUE,
        trace_id="trace-1",
        block_number=1000,
        block_hash=HASH_A,
        borrower=BORROWER,
        outcome="simulated",
    )
    return DecisionRecord(**{**base, **overrides})


def test_expected_value_prices_the_lost_race(store: EventStore) -> None:
    """On Arbitrum a lost race still lands and still burns gas, so it subtracts from EV."""
    record = _decision(
        predicted_net_base=1_000_000,
        failure_cost_base=200_000,
        win_probability_bps=3_000,
    )
    # 0.30 * 1_000_000 - 0.70 * 200_000 = 160_000
    assert record.expected_value() == 160_000

    decision_id = store.record_decision(record)
    row = store._conn.execute("SELECT * FROM decision WHERE id = ?", (decision_id,)).fetchone()
    assert row["expected_value_base"] == "160000"


def test_expected_value_is_none_without_its_inputs() -> None:
    """A partial EV is worse than none: it still looks like a number."""
    assert _decision(predicted_net_base=1_000_000).expected_value() is None
    assert _decision(win_probability_bps=5_000).expected_value() is None


def test_expected_value_can_be_negative() -> None:
    record = _decision(
        predicted_net_base=100_000, failure_cost_base=500_000, win_probability_bps=1_000
    )
    assert record.expected_value() == -440_000


def test_a_rejection_must_say_why() -> None:
    """The rejection reason is the label; a rejection without one is a lost training example."""
    with pytest.raises(ValueError, match="reject_reason"):
        _decision(outcome="rejected")


def test_rejections_are_stored_like_any_other_decision(store: EventStore) -> None:
    store.record_decision(_decision(outcome="rejected", reject_reason="below_min_ev"))
    row = store._conn.execute(
        "SELECT * FROM decision WHERE outcome = 'rejected'"
    ).fetchone()
    assert row["reject_reason"] == "below_min_ev"


def test_decision_defaults_to_soft_finality(store: EventStore) -> None:
    """Detection runs on soft state by design; a tier-0 decision is expected, not a fault."""
    decision_id = store.record_decision(_decision())
    row = store._conn.execute(
        "SELECT finality FROM decision WHERE id = ?", (decision_id,)
    ).fetchone()
    assert row["finality"] == FinalityTier.SOFT


def test_decision_snapshot_round_trips(store: EventStore) -> None:
    snapshot = {"prices": {WETH: "3000"}, "route": ["uniswap_v3"]}
    decision_id = store.record_decision(
        _decision(state_snapshot=snapshot, latency_ms={"rpc": 12.5})
    )
    row = store._conn.execute(
        "SELECT state_snapshot, latency_ms FROM decision WHERE id = ?", (decision_id,)
    ).fetchone()
    assert '"route"' in row["state_snapshot"]
    assert '"rpc"' in row["latency_ms"]


# ---------------------------------------------------------------- inclusion and P&L


def test_losing_the_race_is_recorded(store: EventStore) -> None:
    """The chain shows the winner's transaction, never the fact that we bid and missed."""
    decision_id = store.record_decision(_decision(outcome="submitted"))
    store.record_inclusion_attempt(
        InclusionAttemptRecord(
            decision_id=decision_id,
            venue=VENUE,
            strategy="latency_race",
            outcome="lost",
            our_tx_index=7,
            winner=to_address("0x" + "11" * 20),
            winner_tx_index=2,
            competitors_seen=3,
            inclusion_latency_ms=84.0,
        )
    )
    row = store._conn.execute("SELECT * FROM inclusion_attempt").fetchone()
    assert row["outcome"] == "lost"
    assert row["winner_tx_index"] == 2
    assert row["our_tx_index"] == 7


def test_inclusion_outcome_is_constrained() -> None:
    with pytest.raises(ValueError, match="outcome must be one of"):
        InclusionAttemptRecord(decision_id=1, venue=VENUE, strategy="latency_race", outcome="meh")


def test_profit_is_not_realised_until_finalised(store: EventStore) -> None:
    """Confirmed is not final. Booking at confirmation is how reorged profit becomes real."""
    decision_id = store.record_decision(_decision(outcome="included"))
    realised_id = store.record_realised_pnl(
        RealisedOutcomeRecord(
            decision_id=decision_id,
            venue=VENUE,
            succeeded=True,
            block_number=1000,
            block_hash=HASH_A,
            net_base=42_000,
        )
    )
    assert store.book_realised_pnl(realised_id, FinalityTier.CONFIRMED) is False
    assert store.booked_pnl(VENUE) == 0

    assert store.book_realised_pnl(realised_id, FinalityTier.FINALIZED) is True
    assert store.booked_pnl(VENUE) == 42_000


def test_a_reorg_unbooks_profit(store: EventStore) -> None:
    """Booked profit anchored to a reorged block is not profit. The reversal must be visible."""
    decision_id = store.record_decision(_decision(outcome="included"))
    realised_id = store.record_realised_pnl(
        RealisedOutcomeRecord(
            decision_id=decision_id,
            venue=VENUE,
            succeeded=True,
            block_number=1000,
            block_hash=HASH_A,
            net_base=42_000,
        )
    )
    store.book_realised_pnl(realised_id, FinalityTier.FINALIZED)
    assert store.booked_pnl(VENUE) == 42_000

    store.mark_orphaned(CHAIN, [HASH_A])
    assert store.booked_pnl(VENUE) == 0
    row = store._conn.execute("SELECT * FROM realised_pnl WHERE id = ?", (realised_id,)).fetchone()
    assert row["orphaned"] == 1
    assert row["booked_at"] is None
    assert row["net_base"] == "42000"  # marked, never deleted


def test_a_reorg_marks_every_block_anchored_table(store: EventStore) -> None:
    store.record_oracle_updates(
        [
            OracleUpdateRecord(
                chain_id=CHAIN,
                uid="0xfeed:1",
                asset=WETH,
                oracle_type="chainlink_push",
                price=1,
                price_decimals=8,
                block_number=1000,
                block_hash=HASH_A,
            )
        ]
    )
    store.record_reserve_config(
        ReserveConfigRecord(
            venue=VENUE,
            chain_id=CHAIN,
            asset=WETH,
            block_number=1000,
            block_hash=HASH_A,
            source="event",
        )
    )
    store.upsert_position(
        PositionRecord(
            venue=VENUE,
            chain_id=CHAIN,
            borrower=BORROWER,
            as_of_block=1000,
            as_of_block_hash=HASH_A,
            legs=(PositionLeg(asset=WETH, kind="variable_debt", scaled_amount=1),),
        )
    )
    store.mark_orphaned(CHAIN, [HASH_A])

    assert store.last_oracle_update(CHAIN, WETH) is None
    assert store.reserve_config_at(VENUE, WETH, 1000) is None
    assert store.position_legs(VENUE, BORROWER) == []
    assert store.borrowers_with_debt(VENUE) == []


def test_losses_are_stored_as_losses(store: EventStore) -> None:
    """A clamp at zero would hide losses in the one table built to expose them."""
    decision_id = store.record_decision(_decision(outcome="reverted"))
    realised_id = store.record_realised_pnl(
        RealisedOutcomeRecord(
            decision_id=decision_id,
            venue=VENUE,
            succeeded=False,
            revert_reason="HEALTH_FACTOR_NOT_BELOW_THRESHOLD",
            block_number=1000,
            block_hash=HASH_A,
            net_base=-3_500,
        )
    )
    store.book_realised_pnl(realised_id, FinalityTier.FINALIZED)
    assert store.booked_pnl(VENUE) == -3_500


def test_booked_pnl_sums_beyond_int64(store: EventStore) -> None:
    """Ten wins of 5 ETH each pass 2^63 in wei; SQL SUM would silently wrap."""
    for _ in range(10):
        decision_id = store.record_decision(_decision(outcome="included"))
        realised_id = store.record_realised_pnl(
            RealisedOutcomeRecord(
                decision_id=decision_id,
                venue=VENUE,
                succeeded=True,
                block_number=1000,
                block_hash=HASH_B,
                net_base=5 * 10**18,
            )
        )
        store.book_realised_pnl(realised_id, FinalityTier.FINALIZED)
    assert store.booked_pnl(VENUE) == 50 * 10**18
    assert store.booked_pnl(VENUE) > 2**63


# ---------------------------------------------------------------- wallet intelligence


def test_competitor_pnl_estimate_requires_its_method() -> None:
    """An unattributed competitor P&L figure later reads as a measurement. It is not one."""
    with pytest.raises(ValueError, match="pnl_estimate_method"):
        WalletProfileRecord(
            chain_id=CHAIN,
            wallet=BORROWER,
            window_start_block=1,
            window_end_block=2,
            wallet_type="liquidator",
            pnl_estimate_base=1_000_000,
        )


def test_wallet_profile_keeps_win_rate_by_size(store: EventStore) -> None:
    """One aggregate win rate hides the distinction that matters: most only win dust."""
    store.record_wallet_profile(
        WalletProfileRecord(
            chain_id=CHAIN,
            wallet=BORROWER,
            window_start_block=1,
            window_end_block=1000,
            wallet_type="liquidator",
            is_contract=True,
            win_rate_bps_by_size={"under_100": 4_200, "over_10000": 300},
            median_tx_index=4.0,
            pnl_estimate_base=1_000_000,
            pnl_estimate_method="bonus_minus_gas_upper_bound",
            pnl_estimate_confidence=0.3,
        )
    )
    row = store.latest_wallet_profile(CHAIN, BORROWER)
    assert '"over_10000": 300' in row["win_rate_bps_by_size"]
    assert row["pnl_estimate_method"] == "bonus_minus_gas_upper_bound"
    assert row["is_contract"] == 1
