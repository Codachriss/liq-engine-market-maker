"""Tests for the emergency withdrawal procedure.

The properties that matter: it must work even when everything else is halted (it must not
route through the risk engine or a liveness token), and it must not relax the two things that
were never negotiable — signer custody and destination whitelisting.
"""

from __future__ import annotations

from liqengine.core.types import ChainId, to_address
from liqengine.execution import (
    EmergencyAuthorisation,
    EmergencyRefusalCode,
    EmergencyWithdrawal,
    SignerBackend,
    authorise_emergency_withdrawal,
)

CHAIN = ChainId(42161)
EXECUTOR = to_address("0x" + "e0" * 20)
COLD_STORAGE = to_address("0x" + "cc" * 20)
ATTACKER = to_address("0x" + "dd" * 20)
WETH = to_address("0x" + "e1" * 20)

WHITELIST = frozenset({COLD_STORAGE})


def withdrawal(**overrides) -> EmergencyWithdrawal:
    base = dict(
        chain_id=CHAIN,
        executor=EXECUTOR,
        destination=COLD_STORAGE,
        token=None,
        reason="oracle divergence breaker latched, moving gas float to cold storage",
    )
    return EmergencyWithdrawal(**{**base, **overrides})


def authorise(**overrides):
    base = dict(
        withdrawal=withdrawal(),
        signer_chain_id=CHAIN,
        signer_backend=SignerBackend.KMS,
        stage="canary",
        whitelisted_destinations=WHITELIST,
        acknowledged_by="operator",
    )
    merged = {**base, **overrides}
    target = merged.pop("withdrawal")
    return authorise_emergency_withdrawal(target, **merged)


def test_a_clean_withdrawal_is_authorised() -> None:
    result = authorise()
    assert isinstance(result, EmergencyAuthorisation)
    assert result.acknowledged_by == "operator"


def test_it_works_while_every_other_breaker_is_tripped() -> None:
    """Not a metaphor: this function touches no RiskEngine and no LivenessToken at all. There
    is nothing for a tripped breaker to block, which is the whole point."""
    import inspect

    signature = str(inspect.signature(authorise_emergency_withdrawal))
    assert "RiskEngine" not in signature
    assert "token" not in signature.lower()
    assert isinstance(authorise(), EmergencyAuthorisation)


def test_an_unwhitelisted_destination_is_refused() -> None:
    """An emergency is not a reason to send funds somewhere unverified."""
    result = authorise(withdrawal=withdrawal(destination=ATTACKER))
    assert result.code is EmergencyRefusalCode.UNKNOWN_DESTINATION


def test_a_withdrawal_for_the_wrong_chain_is_refused() -> None:
    result = authorise(withdrawal=withdrawal(chain_id=ChainId(8453)))
    assert result.code is EmergencyRefusalCode.WRONG_CHAIN


def test_signer_custody_is_not_relaxed_for_an_emergency() -> None:
    """An emergency does not change who is allowed to hold a key."""
    result = authorise(signer_backend=SignerBackend.ENV_VAR)
    assert result.code is EmergencyRefusalCode.UNSAFE_SIGNER


def test_an_unacknowledged_withdrawal_is_refused() -> None:
    """The one deliberately manual step: indistinguishable from a bug draining the wallet on
    its own if it could fire with nobody named."""
    result = authorise(acknowledged_by=None)
    assert result.code is EmergencyRefusalCode.NOT_ACKNOWLEDGED


def test_an_empty_acknowledgement_is_also_refused() -> None:
    result = authorise(acknowledged_by="")
    assert result.code is EmergencyRefusalCode.NOT_ACKNOWLEDGED


def test_a_token_withdrawal_is_distinct_from_a_native_sweep() -> None:
    result = authorise(withdrawal=withdrawal(token=WETH))
    assert isinstance(result, EmergencyAuthorisation)
    assert result.withdrawal.token == WETH


def test_the_reason_is_carried_through_for_the_audit_trail() -> None:
    result = authorise(withdrawal=withdrawal(reason="manual halt, sweeping before investigating"))
    assert isinstance(result, EmergencyAuthorisation)
    assert "sweeping" in result.withdrawal.reason
