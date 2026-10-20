"""Tests for the execution layer.

Weighted towards the safety properties, because those are the ones whose failure is silent: a
liveness token that can be replayed, a nonce handed out twice, a signer backend that quietly
survives promotion to canary.
"""

from __future__ import annotations

import inspect

import pytest

from liqengine.core.types import ChainId, FinalityTier, to_address
from liqengine.execution import (
    ConfirmationMonitor,
    ExecutionIntent,
    InclusionStrategy,
    LeaseState,
    LivenessToken,
    NonceGapError,
    NonceManager,
    RefusalCode,
    ReplacementAction,
    ReplacementPolicy,
    SignerBackend,
    TokenRegistry,
    UnsafeSignerError,
    authorise,
    require_backend,
)
from liqengine.txsim import VerificationCode, VerificationResult
from liqengine.txsim.verify import Failure

EXECUTOR = to_address("0x" + "e0" * 20)
POOL = to_address("0x" + "70" * 20)
CHAIN = ChainId(42161)
NOW = 1_000_000_000_000
BLOCK = 500

PASSED = VerificationResult(failures=())
FAILED = VerificationResult(
    failures=(Failure(VerificationCode.REVERTED, "HEALTH_FACTOR_NOT_BELOW_THRESHOLD"),)
)


def intent(**overrides) -> ExecutionIntent:
    base = dict(
        intent_id="i-1",
        decision_id=42,
        venue="aave_v3_arbitrum",
        chain_id=CHAIN,
        executor=EXECUTOR,
        to=POOL,
        calldata="0xdeadbeef",
        value=0,
        gas_limit=450_000,
        max_fee_per_gas=20_000_000,
        max_priority_fee_per_gas=0,
        inclusion_strategy=InclusionStrategy.LATENCY_RACE,
        simulation_block=BLOCK,
        expires_at_block=BLOCK + 2,
        conservative_net_base=200 * 10**8,
    )
    return ExecutionIntent(**{**base, **overrides})


def token_for(target: ExecutionIntent, **overrides) -> LivenessToken:
    base = dict(
        token_id="tok-1",
        intent_hash=target.content_hash(),
        issued_monotonic_ns=NOW,
        ttl_ns=2 * 10**9,
    )
    return LivenessToken(**{**base, **overrides})


def gate(**overrides):
    target = overrides.pop("intent", None) or intent()
    base = dict(
        verification=PASSED,
        token=token_for(target),
        registry=TokenRegistry(),
        signer_address=EXECUTOR,
        signer_backend=None,
        signer_chain_id=CHAIN,
        stage="shadow",
        current_block=BLOCK,
        now_ns=NOW,
    )
    return authorise(target, **{**base, **overrides})


# ---------------------------------------------------------------- intents


def test_changing_any_field_changes_the_hash() -> None:
    """"The same intent with a higher fee" is a different transaction and needs its own token."""
    base = intent(inclusion_strategy=InclusionStrategy.PRIORITY_AUCTION, max_priority_fee_per_gas=1)
    bumped = intent(
        inclusion_strategy=InclusionStrategy.PRIORITY_AUCTION, max_priority_fee_per_gas=2
    )
    assert base.content_hash() != bumped.content_hash()


def test_the_hash_is_stable_for_identical_intents() -> None:
    assert intent().content_hash() == intent().content_hash()


def test_a_priority_fee_on_a_latency_race_venue_is_rejected() -> None:
    """There is no fee auction on FCFS; a priority fee there is paid for nothing (9a)."""
    with pytest.raises(ValueError, match="no fee auction"):
        intent(max_priority_fee_per_gas=5_000)


def test_an_intent_cannot_expire_before_it_was_simulated() -> None:
    with pytest.raises(ValueError, match="expires before"):
        intent(expires_at_block=BLOCK - 1)


def test_priority_fee_above_max_fee_is_rejected() -> None:
    with pytest.raises(ValueError, match="exceeds max fee"):
        intent(
            inclusion_strategy=InclusionStrategy.PRIORITY_AUCTION,
            max_fee_per_gas=100,
            max_priority_fee_per_gas=200,
        )


def test_inclusion_strategy_knows_its_own_physics() -> None:
    assert not InclusionStrategy.LATENCY_RACE.supports_bidding
    assert InclusionStrategy.LATENCY_RACE.reverts_cost_gas
    assert InclusionStrategy.BUNDLE_AUCTION.supports_private_submission
    assert not InclusionStrategy.BUNDLE_AUCTION.reverts_cost_gas


# ---------------------------------------------------------------- the deadman token


def test_a_clean_authorisation_succeeds_and_burns_the_token() -> None:
    registry = TokenRegistry()
    target = intent()
    result = gate(intent=target, registry=registry)
    assert result.token_id == "tok-1"
    assert registry.is_spent(token_for(target))


def test_no_token_refuses() -> None:
    """The deadman switch working, not an error. A stop-signal design would sign here."""
    result = gate(token=None)
    assert result.code is RefusalCode.NO_LIVENESS_TOKEN


def test_an_expired_token_refuses() -> None:
    """A hung risk engine's last token expires without anyone noticing it hung."""
    result = gate(now_ns=NOW + 3 * 10**9)
    assert result.code is RefusalCode.TOKEN_EXPIRED


def test_a_token_for_a_different_intent_refuses() -> None:
    """Binding to the hash is what stops one approval authorising a different transaction."""
    other = intent(intent_id="i-2", calldata="0xcafe")
    result = gate(token=token_for(other))
    assert result.code is RefusalCode.TOKEN_MISMATCH


def test_a_token_cannot_be_spent_twice() -> None:
    registry = TokenRegistry()
    target = intent()
    assert gate(intent=target, registry=registry).token_id == "tok-1"
    second = gate(intent=target, registry=registry)
    assert second.code is RefusalCode.TOKEN_ALREADY_SPENT


def test_burning_a_spent_token_raises_rather_than_passing_quietly() -> None:
    registry = TokenRegistry()
    token = token_for(intent())
    registry.burn(token)
    with pytest.raises(ValueError, match="already been spent"):
        registry.burn(token)


def test_a_token_from_the_future_is_not_fresh() -> None:
    """A clock fault must not let a stale approval pass as a live one."""
    assert not token_for(intent(), issued_monotonic_ns=NOW + 10**9).is_fresh_at(NOW)


# ---------------------------------------------------------------- gate ordering and checks


def test_the_token_is_checked_last_so_its_freshness_is_maximal() -> None:
    """A failing simulation must not consume a token; the token is the closest thing to the
    signature and spending it on a check that fails wastes the freshness it exists to prove."""
    registry = TokenRegistry()
    target = intent()
    result = gate(intent=target, registry=registry, verification=FAILED)
    assert result.code is RefusalCode.SIMULATION_FAILED
    assert not registry.is_spent(token_for(target))


def test_an_expired_intent_refuses_before_anything_else() -> None:
    result = gate(current_block=BLOCK + 10)
    assert result.code is RefusalCode.INTENT_EXPIRED


def test_a_chain_id_mismatch_refuses_before_anything_else() -> None:
    """An intent built for the wrong chain is almost always a config error, not an attack —
    exactly the kind of error a deterministic gate exists to catch before it becomes a signed
    transaction on a network nobody meant to touch."""
    result = gate(signer_chain_id=ChainId(1))
    assert result.code is RefusalCode.WRONG_CHAIN


def test_signing_for_someone_elses_address_refuses() -> None:
    result = gate(signer_address=to_address("0x" + "99" * 20))
    assert result.code is RefusalCode.WRONG_EXECUTOR


def test_a_missing_simulation_refuses() -> None:
    assert gate(verification=None).code is RefusalCode.SIMULATION_MISSING


def test_the_failure_detail_carries_the_revert_reason() -> None:
    result = gate(verification=FAILED)
    assert "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in result.detail


# ---------------------------------------------------------------- key custody


def test_an_environment_variable_signer_cannot_reach_canary() -> None:
    """Readable by the process, its children, and anything that can read /proc."""
    with pytest.raises(UnsafeSignerError, match="requires kms or stronger"):
        require_backend("canary", SignerBackend.ENV_VAR)


def test_kms_and_hardware_are_acceptable_for_live() -> None:
    require_backend("live", SignerBackend.KMS)
    require_backend("live", SignerBackend.HARDWARE)


def test_shadow_must_not_have_a_signer_configured_at_all() -> None:
    """Shadow never signs. A key present there is a key that can leak for no benefit."""
    require_backend("shadow", None)
    with pytest.raises(UnsafeSignerError, match="never signs"):
        require_backend("shadow", SignerBackend.KMS)


def test_an_unknown_stage_fails_closed() -> None:
    with pytest.raises(UnsafeSignerError):
        require_backend("experimental", SignerBackend.KMS)


def test_backends_know_whether_the_key_lands_in_process_memory() -> None:
    assert SignerBackend.ENV_VAR.key_enters_process_memory
    assert SignerBackend.SECRET_MANAGER.key_enters_process_memory
    assert not SignerBackend.KMS.key_enters_process_memory


def test_the_gate_refuses_an_unsafe_backend() -> None:
    result = gate(stage="canary", signer_backend=SignerBackend.ENV_VAR)
    assert result.code is RefusalCode.UNSAFE_SIGNER


def test_no_function_in_the_execution_package_handles_key_material() -> None:
    """An invariant nobody checks is a comment.

    Scans every public callable's signature and return annotation for anything that would carry
    a private key. The rule is structural: keys live behind the signer, and there is no path
    through this package by which one becomes a value.
    """
    import liqengine.execution as package

    banned = ("private_key", "privkey", "secret_key", "mnemonic", "seed_phrase", "keystore")
    for name in package.__all__:
        obj = getattr(package, name)
        if not callable(obj):
            continue
        try:
            signature = inspect.signature(obj)
        except (TypeError, ValueError):
            continue
        text = f"{signature}".lower()
        for term in banned:
            assert term not in text, f"{name} mentions {term} in its signature"


# ---------------------------------------------------------------- nonces


def test_two_intents_in_one_block_get_different_nonces() -> None:
    """Reading the chain nonce twice gives both of them N, and the node discards one."""
    manager = NonceManager(address=EXECUTOR, next_nonce=7)
    first = manager.lease("i-1", NOW)
    second = manager.lease("i-2", NOW)
    assert (first.nonce, second.nonce) == (7, 8)


def test_an_unbroadcast_nonce_is_released_for_reuse() -> None:
    """Skipping it would stall every later transaction for no reason."""
    manager = NonceManager(address=EXECUTOR, next_nonce=7)
    lease = manager.lease("i-1", NOW)
    manager.release(lease.nonce)
    assert manager.lease("i-2", NOW).nonce == 7


def test_a_broadcast_nonce_cannot_be_released() -> None:
    """Handing it to a second transaction while the first is live is how one trade becomes two."""
    manager = NonceManager(address=EXECUTOR, next_nonce=7)
    manager.lease("i-1", NOW)
    manager.mark_broadcast(7, "0xaaa", NOW)
    with pytest.raises(ValueError, match="only an unbroadcast lease"):
        manager.release(7)


def test_rebroadcasting_the_same_hash_is_idempotent() -> None:
    """A network timeout must not become two liquidations, nor reset stuck-detection."""
    manager = NonceManager(address=EXECUTOR, next_nonce=0)
    manager.lease("i-1", NOW)
    manager.mark_broadcast(0, "0xaaa", NOW)
    manager.mark_broadcast(0, "0xaaa", NOW + 5_000_000_000)
    assert manager.lease_for(0).broadcast_at_ns == NOW
    assert manager.lease_for(0).superseded == ()


def test_a_replacement_records_the_chain_it_superseded() -> None:
    manager = NonceManager(address=EXECUTOR, next_nonce=0)
    manager.lease("i-1", NOW)
    manager.mark_broadcast(0, "0xaaa", NOW)
    manager.mark_broadcast(0, "0xbbb", NOW + 10**9)
    assert manager.lease_for(0).superseded == ("0xaaa",)
    assert manager.lease_for(0).tx_hash == "0xbbb"


def test_head_of_line_blocking_is_visible() -> None:
    """Otherwise a single stuck transaction presents as "we stopped winning" with no cause."""
    manager = NonceManager(address=EXECUTOR, next_nonce=0, stuck_after_ns=10**9)
    for index in range(3):
        manager.lease(f"i-{index}", NOW)
        manager.mark_broadcast(index, f"0x{index}", NOW)
    assert manager.head_of_line().nonce == 0
    assert not manager.is_blocked(NOW)
    assert manager.is_blocked(NOW + 2 * 10**9)
    assert len(manager.stuck(NOW + 2 * 10**9)) == 3


def test_an_abandoned_nonce_still_blocks() -> None:
    """There is no cancel on EVM; the nonce is only cleared by something landing at it."""
    manager = NonceManager(address=EXECUTOR, next_nonce=0)
    manager.lease("i-1", NOW)
    manager.mark_broadcast(0, "0xaaa", NOW)
    manager.abandon(0)
    assert manager.head_of_line().nonce == 0
    assert manager.lease_for(0).state is LeaseState.ABANDONED


def test_in_flight_is_enumerable_for_the_kill_path() -> None:
    manager = NonceManager(address=EXECUTOR, next_nonce=0)
    for index in range(3):
        manager.lease(f"i-{index}", NOW)
        manager.mark_broadcast(index, f"0x{index}", NOW)
    manager.confirm(0, "0x0")
    assert [lease.nonce for lease in manager.in_flight()] == [1, 2]


def test_a_chain_nonce_ahead_of_us_is_a_halt() -> None:
    """Another signer is using this account: a second instance, or a compromised key."""
    manager = NonceManager(address=EXECUTOR, next_nonce=5)
    with pytest.raises(NonceGapError, match="another signer"):
        manager.sync(9)


def test_sync_confirms_everything_the_chain_has_passed() -> None:
    manager = NonceManager(address=EXECUTOR, next_nonce=0)
    for index in range(3):
        manager.lease(f"i-{index}", NOW)
        manager.mark_broadcast(index, f"0x{index}", NOW)
    manager.sync(2)
    assert manager.lease_for(0).state is LeaseState.CONFIRMED
    assert manager.lease_for(1).state is LeaseState.CONFIRMED
    assert manager.lease_for(2).state is LeaseState.BROADCAST
    assert manager.prune_confirmed() == 2


# ---------------------------------------------------------------- replacement


FCFS = ReplacementPolicy(strategy=InclusionStrategy.LATENCY_RACE, stuck_after_ns=10**10)
AUCTION = ReplacementPolicy(strategy=InclusionStrategy.PRIORITY_AUCTION, stuck_after_ns=10**10)


def _decide(policy: ReplacementPolicy, **overrides):
    base = dict(
        age_ns=10**9,
        current_max_fee=100_000,
        current_priority_fee=10_000,
        target_priority_fee=20_000,
        remaining_net_base=100 * 10**8,
        replacement_cost_base=1 * 10**8,
        opportunity_still_valid=True,
    )
    return policy.decide(**{**base, **overrides})


def test_a_slow_transaction_on_an_fcfs_venue_is_left_alone() -> None:
    """No auction to improve into. Replacing is a new race that pays gas twice for one trade."""
    decision = _decide(FCFS)
    assert decision.action is ReplacementAction.HOLD
    assert "new race" in decision.reason


def test_a_genuinely_stuck_fcfs_transaction_is_replaced() -> None:
    decision = _decide(FCFS, age_ns=2 * 10**10)
    assert decision.action is ReplacementAction.REPLACE
    assert decision.new_max_fee_per_gas == 110_000  # the node's 10% minimum bump


def test_a_stuck_fcfs_transaction_worth_nothing_is_cancelled_not_replaced() -> None:
    decision = _decide(FCFS, age_ns=2 * 10**10, remaining_net_base=10**7)
    assert decision.action is ReplacementAction.CANCEL


def test_an_auction_venue_rebids_immediately() -> None:
    decision = _decide(AUCTION)
    assert decision.action is ReplacementAction.REPLACE
    assert decision.new_max_priority_fee_per_gas == 20_000


def test_a_rebid_below_the_minimum_bump_is_raised_to_it() -> None:
    """Below the node's threshold it silently keeps the original, which looks like a no-op."""
    decision = _decide(AUCTION, target_priority_fee=10_500)
    assert decision.new_max_priority_fee_per_gas == 11_000


def test_a_rebid_that_does_not_pay_for_itself_is_refused() -> None:
    decision = _decide(AUCTION, remaining_net_base=10**7, replacement_cost_base=10**8)
    assert decision.action is ReplacementAction.HOLD


def test_a_dead_opportunity_is_not_chased() -> None:
    """Someone else took it. A replacement spends real gas on a certainty of losing."""
    decision = _decide(AUCTION, opportunity_still_valid=False)
    assert decision.action is ReplacementAction.HOLD
    assert "let it resolve" in decision.reason


def test_a_dead_opportunity_blocking_the_nonce_is_cancelled() -> None:
    decision = _decide(FCFS, opportunity_still_valid=False, age_ns=2 * 10**10)
    assert decision.action is ReplacementAction.CANCEL
    assert "blocking" in decision.reason


def test_the_bump_always_increases_even_on_a_tiny_fee() -> None:
    """Integer truncation on a small fee produces a bump equal to the original."""
    assert AUCTION._bumped(1) == 2
    assert AUCTION._bumped(0) == 1


# ---------------------------------------------------------------- confirmation


def test_profit_is_not_bookable_until_finalised() -> None:
    monitor = ConfirmationMonitor()
    monitor.track("0xaaa", nonce=0, decision_id=1, now_ns=NOW)
    monitor.advance("0xaaa", FinalityTier.CONFIRMED, NOW + 10**9, block_number=100)
    assert not monitor.get("0xaaa").bookable
    assert len(monitor.pending()) == 1

    monitor.advance("0xaaa", FinalityTier.FINALIZED, NOW + 60 * 10**9)
    assert monitor.get("0xaaa").bookable
    assert monitor.pending() == ()


def test_the_ladder_never_goes_down() -> None:
    """A lagging provider reporting an older tier must not un-confirm anything."""
    monitor = ConfirmationMonitor()
    monitor.track("0xaaa", nonce=0, decision_id=1, now_ns=NOW)
    monitor.advance("0xaaa", FinalityTier.FINALIZED, NOW + 10**9)
    monitor.advance("0xaaa", FinalityTier.CONFIRMED, NOW + 2 * 10**9)
    assert monitor.get("0xaaa").tier is FinalityTier.FINALIZED


def test_a_reorg_makes_a_finalised_transaction_unbookable() -> None:
    monitor = ConfirmationMonitor()
    monitor.track("0xaaa", nonce=0, decision_id=1, now_ns=NOW)
    monitor.advance("0xaaa", FinalityTier.FINALIZED, NOW + 10**9)
    monitor.mark_reorged("0xaaa")
    assert not monitor.get("0xaaa").bookable
    assert monitor.bookable() == ()


def test_time_to_finality_is_measurable() -> None:
    """How long profit sits real-on-screen and not-yet-real-in-the-ledger."""
    monitor = ConfirmationMonitor()
    monitor.track("0xaaa", nonce=0, decision_id=1, now_ns=NOW)
    monitor.advance("0xaaa", FinalityTier.CONFIRMED, NOW + 10**9)
    monitor.advance("0xaaa", FinalityTier.FINALIZED, NOW + 61 * 10**9)
    assert monitor.get("0xaaa").time_to_tier_ns(FinalityTier.FINALIZED) == 61 * 10**9
