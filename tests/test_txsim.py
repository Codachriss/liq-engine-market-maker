"""Tests for pre-signature transaction verification.

The properties pinned here are the ones that make this a gate rather than a formality: a missing
simulation refuses exactly like a failed one, a stale simulation is refused, and the checks
compare against what was *predicted* rather than merely confirming success.
"""

from __future__ import annotations

import pytest

from liqengine.core.types import BlockHash, to_address
from liqengine.txsim import (
    ExpectedOutcome,
    SimulatedTransaction,
    TokenApproval,
    TokenTransfer,
    VerificationCode,
    verify,
)

EXECUTOR = to_address("0x" + "e0" * 20)
POOL = to_address("0x" + "70" * 20)
ROUTER = to_address("0x" + "71" * 20)
FLASH = to_address("0x" + "72" * 20)
BORROWER = to_address("0x" + "b0" * 20)
ATTACKER = to_address("0x" + "dd" * 20)

WETH = to_address("0x" + "e1" * 20)
USDC = to_address("0x" + "c2" * 20)
SHITCOIN = to_address("0x" + "5c" * 20)

BLOCK = 1_000
BLOCK_HASH = BlockHash("0x" + "aa" * 32)

COLLATERAL_OUT = 3_500_000_000_000_000_000  # 3.5 WETH
DEBT_IN = 10_000 * 10**6                    # 10,000 USDC
PRICE_WETH = 3_000 * 10**8


def expectation(**overrides) -> ExpectedOutcome:
    base = dict(
        executor=EXECUTOR,
        collateral_asset=WETH,
        debt_asset=USDC,
        predicted_collateral_received=COLLATERAL_OUT,
        predicted_debt_repaid=DEBT_IN,
        conservative_net_base=200 * 10**8,
        predicted_gas_base=1 * 10**8,
        gas_price_wei=20_000_000,
        native_price=PRICE_WETH,
        known_parties=frozenset({EXECUTOR, POOL, ROUTER, FLASH, BORROWER}),
        known_tokens=frozenset({WETH, USDC}),
        allowed_approvals=((USDC, POOL, DEBT_IN), (WETH, ROUTER, COLLATERAL_OUT)),
    )
    return ExpectedOutcome(**{**base, **overrides})


def happy_transfers(collateral_out: int = COLLATERAL_OUT) -> tuple[TokenTransfer, ...]:
    """The six legs of a flash liquidation, in order."""
    return (
        TokenTransfer(token=USDC, sender=FLASH, recipient=EXECUTOR, amount=DEBT_IN),
        TokenTransfer(token=USDC, sender=EXECUTOR, recipient=POOL, amount=DEBT_IN),
        TokenTransfer(token=WETH, sender=POOL, recipient=EXECUTOR, amount=collateral_out),
        TokenTransfer(token=WETH, sender=EXECUTOR, recipient=ROUTER, amount=collateral_out),
        TokenTransfer(
            token=USDC, sender=ROUTER, recipient=EXECUTOR, amount=DEBT_IN + 400 * 10**6
        ),
        TokenTransfer(token=USDC, sender=EXECUTOR, recipient=FLASH, amount=DEBT_IN),
    )


def simulation(**overrides) -> SimulatedTransaction:
    base = dict(
        success=True,
        gas_used=450_000,
        block_number=BLOCK,
        block_hash=BLOCK_HASH,
        simulated_from=EXECUTOR,
        simulator="test:eth_call",
        transfers=happy_transfers(),
        approvals=(),
    )
    return SimulatedTransaction(**{**base, **overrides})


# ---------------------------------------------------------------- the happy path


def test_a_clean_simulation_passes() -> None:
    result = verify(simulation(), expectation(), current_block=BLOCK)
    assert result.passed
    assert result.gas_used == 450_000
    assert result.collateral_divergence_bps == 0


def test_net_flow_is_computed_across_the_whole_route() -> None:
    """Whatever path the calldata takes, the end position is what was actually made."""
    sim = simulation()
    assert sim.net_flow(WETH, EXECUTOR) == 0            # seized then swapped away
    assert sim.net_flow(USDC, EXECUTOR) == 400 * 10**6  # the profit


# ---------------------------------------------------------------- the loophole, closed


def test_no_simulation_refuses_exactly_like_a_failed_one() -> None:
    """"Whenever technically possible" is the loophole every incident lives in."""
    result = verify(None, expectation(), current_block=BLOCK)
    assert not result.passed
    assert result.primary is VerificationCode.NOT_SIMULATED


def test_a_revert_reports_its_decoded_reason() -> None:
    """"Reverted" is not learnable. The reason distinguishes a self-cure from a book bug."""
    result = verify(
        simulation(success=False, revert_reason="HEALTH_FACTOR_NOT_BELOW_THRESHOLD"),
        expectation(),
        current_block=BLOCK,
    )
    assert VerificationCode.REVERTED in result.codes()
    assert "HEALTH_FACTOR_NOT_BELOW_THRESHOLD" in str(result.failures[0])


def test_a_revert_does_not_also_report_trace_noise() -> None:
    """A reverted transaction has no meaningful trace; shortfalls from it are not information."""
    result = verify(
        simulation(success=False, revert_reason="reverted", transfers=()),
        expectation(),
        current_block=BLOCK,
    )
    assert result.codes() == (VerificationCode.REVERTED,)


def test_an_undecoded_revert_still_says_so() -> None:
    result = verify(simulation(success=False), expectation(), current_block=BLOCK)
    assert "no decoded reason" in str(result.failures[0])


# ---------------------------------------------------------------- freshness and identity


def test_a_stale_simulation_is_refused() -> None:
    """The state it ran against is gone. This is the pre-sign revalidation gate."""
    result = verify(simulation(), expectation(), current_block=BLOCK + 5)
    assert VerificationCode.STALE_SIMULATION in result.codes()


def test_one_block_of_age_is_allowed_by_default() -> None:
    assert verify(simulation(), expectation(), current_block=BLOCK + 1).passed


def test_a_simulation_from_the_future_is_also_refused() -> None:
    """Negative age means the node and the head disagree, which is not a state to sign on."""
    result = verify(simulation(block_number=BLOCK + 3), expectation(), current_block=BLOCK)
    assert VerificationCode.STALE_SIMULATION in result.codes()


def test_simulating_as_the_wrong_address_is_refused() -> None:
    """msg.sender changes balances, allowances and nonce. It is somebody else's transaction."""
    result = verify(
        simulation(simulated_from=to_address("0x" + "99" * 20)),
        expectation(),
        current_block=BLOCK,
    )
    assert VerificationCode.WRONG_SENDER in result.codes()


# ---------------------------------------------------------------- gas and profit


def test_gas_above_the_cap_is_refused() -> None:
    result = verify(simulation(gas_used=5_000_000), expectation(), current_block=BLOCK)
    assert VerificationCode.GAS_EXCEEDED in result.codes()


def test_profit_is_recomputed_at_the_simulated_gas() -> None:
    """A trade that survives on estimated gas and dies on simulated gas must not be signed."""
    # A ten-cent conservative net priced at five cents of gas. At 2.9M gas it costs seventeen.
    thin = expectation(conservative_net_base=10 * 10**6, predicted_gas_base=5 * 10**6)
    result = verify(simulation(gas_used=2_900_000), thin, current_block=BLOCK)
    assert VerificationCode.UNPROFITABLE_AT_SIMULATED_GAS in result.codes()
    assert result.net_base_at_simulated_gas is not None
    assert result.net_base_at_simulated_gas <= 0


def test_cheaper_than_predicted_gas_improves_the_net() -> None:
    result = verify(simulation(gas_used=100_000), expectation(), current_block=BLOCK)
    assert result.passed
    assert result.net_base_at_simulated_gas > 200 * 10**8


# ---------------------------------------------------------------- amounts


def test_a_collateral_shortfall_beyond_tolerance_is_refused() -> None:
    """Fee-on-transfer and rebasing collateral look exactly like this, and are the common case
    in the long tail where competition is thin."""
    short = COLLATERAL_OUT * 92 // 100
    sim = simulation(
        transfers=(
            TokenTransfer(token=USDC, sender=FLASH, recipient=EXECUTOR, amount=DEBT_IN),
            TokenTransfer(token=USDC, sender=EXECUTOR, recipient=POOL, amount=DEBT_IN),
            TokenTransfer(token=WETH, sender=POOL, recipient=EXECUTOR, amount=short),
        )
    )
    result = verify(sim, expectation(), current_block=BLOCK)
    assert VerificationCode.COLLATERAL_SHORTFALL in result.codes()
    assert result.collateral_divergence_bps == pytest.approx(800, abs=1)


def test_a_shortfall_inside_tolerance_passes() -> None:
    within = COLLATERAL_OUT * 9_990 // 10_000  # 10bps short
    result = verify(
        simulation(transfers=happy_transfers(collateral_out=within)),
        expectation(),
        current_block=BLOCK,
    )
    assert result.passed
    assert result.collateral_divergence_bps == 10


def test_receiving_more_than_predicted_is_not_a_failure() -> None:
    generous = COLLATERAL_OUT * 105 // 100
    result = verify(
        simulation(transfers=happy_transfers(collateral_out=generous)),
        expectation(),
        current_block=BLOCK,
    )
    assert result.passed
    assert result.collateral_divergence_bps < 0


def test_debt_never_leaving_the_executor_is_refused() -> None:
    """The liquidation did not happen; something else consumed the flash loan."""
    sim = simulation(
        transfers=(
            TokenTransfer(token=USDC, sender=FLASH, recipient=EXECUTOR, amount=DEBT_IN),
            TokenTransfer(token=WETH, sender=POOL, recipient=EXECUTOR, amount=COLLATERAL_OUT),
        )
    )
    result = verify(sim, expectation(), current_block=BLOCK)
    assert VerificationCode.DEBT_MISMATCH in result.codes()


# ---------------------------------------------------------------- the allowlist


def test_an_unexpected_token_is_refused() -> None:
    """A route that quietly picks up an intermediate hop is not the route that was priced."""
    sim = simulation(
        transfers=(
            *happy_transfers(),
            TokenTransfer(token=SHITCOIN, sender=ROUTER, recipient=EXECUTOR, amount=1),
        )
    )
    result = verify(sim, expectation(), current_block=BLOCK)
    assert VerificationCode.UNEXPECTED_TOKEN in result.codes()


def test_an_unexpected_counterparty_is_refused() -> None:
    sim = simulation(
        transfers=(
            *happy_transfers(),
            TokenTransfer(token=USDC, sender=EXECUTOR, recipient=ATTACKER, amount=10**6),
        )
    )
    result = verify(sim, expectation(), current_block=BLOCK)
    assert VerificationCode.UNEXPECTED_COUNTERPARTY in result.codes()
    assert ATTACKER in str(result.failures[0]) or any(ATTACKER in str(f) for f in result.failures)


def test_an_empty_allowlist_disables_the_check_rather_than_rejecting_everything() -> None:
    """An unconfigured allowlist must not be indistinguishable from a hostile trace."""
    result = verify(
        simulation(),
        expectation(known_tokens=frozenset(), known_parties=frozenset()),
        current_block=BLOCK,
    )
    assert result.passed


# ---------------------------------------------------------------- approvals


def test_an_unrequested_approval_is_refused() -> None:
    sim = simulation(
        approvals=(TokenApproval(token=WETH, owner=EXECUTOR, spender=ATTACKER, amount=0),)
    )
    result = verify(sim, expectation(), current_block=BLOCK)
    assert VerificationCode.UNEXPECTED_APPROVAL in result.codes()


def test_an_unlimited_approval_is_refused_even_to_a_known_spender() -> None:
    """An infinite allowance on a router is a permanent liability unrelated to this trade."""
    sim = simulation(
        approvals=(TokenApproval(token=WETH, owner=EXECUTOR, spender=ROUTER, amount=2**256 - 1),)
    )
    result = verify(sim, expectation(), current_block=BLOCK)
    assert VerificationCode.UNEXPECTED_APPROVAL in result.codes()
    assert "unlimited" in str(result.failures[0])


def test_an_approval_left_standing_is_refused() -> None:
    sim = simulation(
        approvals=(
            TokenApproval(token=WETH, owner=EXECUTOR, spender=ROUTER, amount=COLLATERAL_OUT),
        )
    )
    result = verify(sim, expectation(), current_block=BLOCK)
    assert VerificationCode.LINGERING_APPROVAL in result.codes()


def test_an_approval_granted_and_revoked_passes() -> None:
    sim = simulation(
        approvals=(
            TokenApproval(token=WETH, owner=EXECUTOR, spender=ROUTER, amount=COLLATERAL_OUT),
            TokenApproval(token=WETH, owner=EXECUTOR, spender=ROUTER, amount=0),
        )
    )
    assert verify(sim, expectation(), current_block=BLOCK).passed


def test_approvals_by_other_owners_are_not_ours_to_police() -> None:
    """The router approving its own pool is its business, not a finding about our transaction."""
    sim = simulation(
        approvals=(TokenApproval(token=WETH, owner=ROUTER, spender=POOL, amount=2**256 - 1),)
    )
    assert verify(sim, expectation(), current_block=BLOCK).passed


# ---------------------------------------------------------------- reporting


def test_every_failure_is_reported_not_just_the_first() -> None:
    """Fixing three problems one rejection at a time wastes three cycles of a race."""
    sim = simulation(
        gas_used=5_000_000,
        transfers=(
            *happy_transfers(),
            TokenTransfer(token=SHITCOIN, sender=ATTACKER, recipient=EXECUTOR, amount=1),
        ),
        approvals=(TokenApproval(token=WETH, owner=EXECUTOR, spender=ROUTER, amount=10**30),),
    )
    codes = set(verify(sim, expectation(), current_block=BLOCK + 9).codes())
    assert {
        VerificationCode.STALE_SIMULATION,
        VerificationCode.GAS_EXCEEDED,
        VerificationCode.UNEXPECTED_TOKEN,
        VerificationCode.UNEXPECTED_COUNTERPARTY,
        VerificationCode.UNEXPECTED_APPROVAL,
        VerificationCode.LINGERING_APPROVAL,
    } <= codes
