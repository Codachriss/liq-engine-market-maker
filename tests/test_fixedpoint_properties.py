"""Property-based tests for the fixed-point arithmetic every financial calculation sits on.

Example-based tests sample a handful of points; the properties here are true at every point,
and a violation at some untried input is exactly the failure mode a health-factor calculation
cannot afford — the fifth decimal is where liquidation eligibility is decided (fixedpoint.py's
own module docstring). `hypothesis` is the tool for exactly this: pure integer functions with
algebraic invariants (rounding direction, monotonicity, round-trip identity) that a fixed set of
examples can only sample a few points of.

Every function under test here is deterministic and side-effect free, so shrinking a failure
to a minimal counterexample is safe to do freely.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from liqengine.core.fixedpoint import (
    HALF_PERCENTAGE_FACTOR,
    PERCENTAGE_FACTOR,
    RAY,
    WAD,
    percent_div,
    percent_mul,
    ray_div,
    ray_mul,
    ray_to_wad,
    wad_div,
    wad_mul,
    wad_to_ray,
)

# uint256 in practice never approaches its true ceiling for a token amount, but the rounding
# arithmetic itself has to hold across many orders of magnitude, so the range is wide.
amounts = st.integers(min_value=0, max_value=2**200)
positive_amounts = st.integers(min_value=1, max_value=2**200)
percentages = st.integers(min_value=1, max_value=50_000)  # up to 500.00%, covers real bonuses


# --------------------------------------------------------------------------------------
# ray/wad mul-div: rounding is half-up, and half-up has a direction
# --------------------------------------------------------------------------------------


@given(a=amounts, b=amounts)
def test_ray_mul_rounds_no_further_than_one_unit_from_true_division(a: int, b: int) -> None:
    """Half-up rounding means the integer result is within one unit of the exact quotient —
    never more, in either direction. A larger error would mean the rounding rule was not
    actually applied, just some other arithmetic that happens to look similar."""
    exact = a * b
    result = ray_mul(a, b)
    assert abs(result * RAY - exact) <= RAY // 2 + 1


@given(a=amounts, b=amounts)
def test_wad_mul_rounds_no_further_than_one_unit_from_true_division(a: int, b: int) -> None:
    exact = a * b
    result = wad_mul(a, b)
    assert abs(result * WAD - exact) <= WAD // 2 + 1


@given(a=amounts, b=positive_amounts)
def test_ray_div_then_ray_mul_recovers_the_original_within_rounding(a: int, b: int) -> None:
    """Not an exact round trip — two roundings compose. `ray_div`'s own rounding error is at
    most half a ray-unit of quotient; multiplying that back by `b` amplifies it by `b / RAY`,
    so the bound has to scale with `b`'s size relative to `RAY` rather than sit at a fixed
    constant — a fixed constant is exactly the wrong shape of property for a scale-dependent
    error and is what a first attempt at this test got wrong."""
    recovered = ray_mul(ray_div(a, b), b)
    assert abs(recovered - a) <= b // RAY + 2


@given(a=amounts, b=positive_amounts)
def test_wad_div_then_wad_mul_recovers_the_original_within_rounding(a: int, b: int) -> None:
    recovered = wad_mul(wad_div(a, b), b)
    assert abs(recovered - a) <= b // WAD + 2


@given(a=positive_amounts)
def test_ray_mul_by_one_ray_is_the_identity(a: int) -> None:
    assert ray_mul(a, RAY) == a


@given(a=positive_amounts)
def test_wad_mul_by_one_wad_is_the_identity(a: int) -> None:
    assert wad_mul(a, WAD) == a


@given(a=amounts, b=amounts)
def test_ray_mul_is_commutative(a: int, b: int) -> None:
    """`a * b` and `b * a` must round to the same integer result, or which operand is passed
    first would silently change a computed health factor."""
    assert ray_mul(a, b) == ray_mul(b, a)


@given(a=amounts, b=amounts)
def test_wad_mul_is_commutative(a: int, b: int) -> None:
    assert wad_mul(a, b) == wad_mul(b, a)


@given(a=amounts, b=positive_amounts, c=st.integers(min_value=1, max_value=10**6))
def test_ray_mul_is_monotonic_in_its_first_argument(a: int, b: int, c: int) -> None:
    """A larger balance must never produce a smaller scaled result — monotonicity is what
    makes "more collateral is safer" true of the arithmetic, not just the intent behind it."""
    assert ray_mul(a, b) <= ray_mul(a + c, b)


# --------------------------------------------------------------------------------------
# ray <-> wad conversion
# --------------------------------------------------------------------------------------


@given(a=amounts)
def test_wad_to_ray_then_ray_to_wad_is_exact(a: int) -> None:
    """wad -> ray loses no information (ray has more precision), so converting back must be
    exact — any drift here would mean the conversion, not just a mul/div, is lossy."""
    assert ray_to_wad(wad_to_ray(a)) == a


@given(a=amounts)
def test_ray_to_wad_rounds_no_further_than_one_unit(a: int) -> None:
    # Integer arithmetic throughout: float division loses precision above 2**53, and
    # Hypothesis found a ray (1.22e25) where the float "exact" value was itself two units off
    # while ray_to_wad was exact. |wad * ratio - a| <= ratio is "within one wad unit", exactly.
    ratio = RAY // WAD
    assert abs(ray_to_wad(a) * ratio - a) <= ratio


# --------------------------------------------------------------------------------------
# percentage math: what the liquidation bonus and close factor are computed in
# --------------------------------------------------------------------------------------


@given(value=amounts, pct=percentages)
def test_percent_mul_rounds_no_further_than_one_unit(value: int, pct: int) -> None:
    exact = value * pct
    result = percent_mul(value, pct)
    assert abs(result * PERCENTAGE_FACTOR - exact) <= HALF_PERCENTAGE_FACTOR + 1


@given(value=amounts, pct=percentages)
def test_percent_mul_at_100_percent_is_the_identity(value: int, pct: int) -> None:
    assert percent_mul(value, PERCENTAGE_FACTOR) == value


@given(value=positive_amounts, pct=percentages)
def test_percent_div_then_percent_mul_recovers_the_original_within_rounding(
    value: int, pct: int
) -> None:
    """Same scale-dependent bound as the ray/wad round trips: the amplification factor here is
    `pct / PERCENTAGE_FACTOR` rather than `b / RAY`, but it is the same phenomenon."""
    recovered = percent_mul(percent_div(value, pct), pct)
    assert abs(recovered - value) <= pct // PERCENTAGE_FACTOR + 2


@given(value=amounts, low=percentages, high=percentages)
def test_percent_mul_is_monotonic_in_the_percentage(value: int, low: int, high: int) -> None:
    """A larger liquidation bonus must never compute a smaller seized amount — this is the
    property that makes calculate_seizure's bonus arithmetic trustworthy at every value, not
    just the handful the example-based tests happen to try."""
    assume(low <= high)
    assert percent_mul(value, low) <= percent_mul(value, high)


@given(value=amounts)
def test_percent_mul_of_zero_percentage_is_zero(value: int) -> None:
    assert percent_mul(value, 0) == 0


# --------------------------------------------------------------------------------------
# division by zero is a decision, not an accident
# --------------------------------------------------------------------------------------


@given(a=amounts)
def test_ray_div_by_zero_raises_rather_than_returning_a_wrong_number(a: int) -> None:
    import pytest

    with pytest.raises(ZeroDivisionError):
        ray_div(a, 0)


@given(a=amounts)
def test_percent_div_by_zero_raises_rather_than_returning_a_wrong_number(a: int) -> None:
    import pytest

    with pytest.raises(ZeroDivisionError):
        percent_div(a, 0)
