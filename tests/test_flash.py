"""Tests for flash-source selection.

D3's policy in one sentence: cheapest first, skip anything without capacity. These tests are
the sentence made concrete.
"""

from __future__ import annotations

from liqengine.simulate import FlashLoanQuote, select_flash_source

BALANCER = FlashLoanQuote(source="balancer_v2", premium_bps=0, available=1_000_000)
MORPHO = FlashLoanQuote(source="morpho", premium_bps=0, available=500_000)
AAVE_SELF = FlashLoanQuote(source="aave_v3", premium_bps=5, available=10_000_000)


def test_the_cheapest_source_with_capacity_wins() -> None:
    assert select_flash_source([AAVE_SELF, BALANCER], amount=100_000) is BALANCER


def test_a_zero_fee_source_without_capacity_is_skipped() -> None:
    """Cheapest first, but only among sources that can actually cover the size."""
    result = select_flash_source([BALANCER, AAVE_SELF], amount=2_000_000)
    assert result is AAVE_SELF


def test_no_source_with_enough_capacity_returns_none() -> None:
    """Not an error — the signal simulate() already turns into
    INSUFFICIENT_FLASH_LIQUIDITY."""
    result = select_flash_source([BALANCER, MORPHO], amount=2_000_000)
    assert result is None


def test_an_empty_source_list_returns_none() -> None:
    assert select_flash_source([], amount=1) is None


def test_ties_on_premium_prefer_more_headroom() -> None:
    """Between two equally cheap sources, the one with more room to spare is one fewer venue
    to re-check if the position turns out larger than first estimated."""
    result = select_flash_source([MORPHO, BALANCER], amount=100_000)
    assert result is BALANCER  # both 0bps, Balancer has more capacity


def test_premium_is_never_assumed_only_read_from_the_quote() -> None:
    """"Always queried" is enforced by construction: there is no path to select a source
    without a premium already attached to it."""
    import inspect

    signature = str(inspect.signature(select_flash_source))
    assert "premium" not in signature  # the caller cannot pass one in; it comes from the quote
