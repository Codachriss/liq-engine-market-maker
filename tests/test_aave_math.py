"""Aave v3 liquidation math.

These tests pin the behaviours that are expensive to get wrong: interest that accrues without
an event, eMode overriding reserve parameters, the close factor stepping to 100%, and the
protocol's cut of the liquidation bonus.
"""

from __future__ import annotations

import pytest

from liqengine.config.models import CloseFactorPolicy
from liqengine.core.fixedpoint import RAY, WAD, percent_mul
from liqengine.core.types import to_address
from liqengine.protocols.aave_v3 import math as m
from liqengine.protocols.aave_v3.state import EModeCategory, ReserveState, UserAccount, UserReserve

WETH = to_address("0x0000000000000000000000000000000000000001")
USDC = to_address("0x0000000000000000000000000000000000000002")

NOW = 1_700_000_000
USD = 10**8  # oracle base unit

POLICY = CloseFactorPolicy(source_version="3.1")


def reserve(
    asset: str,
    *,
    decimals: int,
    threshold_bps: int = 8000,
    bonus_bps: int = 10500,
    protocol_fee_bps: int = 0,
    borrow_rate_ray: int = 0,
    liquidity_rate_ray: int = 0,
    last_update: int = NOW,
    liquidity_index_ray: int = RAY,
    variable_borrow_index_ray: int = RAY,
) -> ReserveState:
    return ReserveState(
        asset=to_address(asset),
        symbol=asset[-4:],
        decimals=decimals,
        ltv_bps=threshold_bps - 500,
        liquidation_threshold_bps=threshold_bps,
        liquidation_bonus_bps=bonus_bps,
        liquidation_protocol_fee_bps=protocol_fee_bps,
        liquidity_index_ray=liquidity_index_ray,
        variable_borrow_index_ray=variable_borrow_index_ray,
        current_liquidity_rate_ray=liquidity_rate_ray,
        current_variable_borrow_rate_ray=borrow_rate_ray,
        last_update_timestamp=last_update,
    )


WETH_RESERVE = reserve(WETH, decimals=18)
USDC_RESERVE = reserve(USDC, decimals=6, threshold_bps=8500)

PRICES = {WETH: 2000 * USD, USDC: 1 * USD}


def account(collateral_weth: int = 10**18, debt_usdc: int = 1000 * 10**6) -> UserAccount:
    return UserAccount(
        address=to_address("0x00000000000000000000000000000000000000ff"),
        reserves=(
            UserReserve(asset=WETH, scaled_a_token_balance=collateral_weth),
            UserReserve(asset=USDC, scaled_variable_debt=debt_usdc),
        ),
    )


RESERVES = {WETH: WETH_RESERVE, USDC: USDC_RESERVE}


def large_unhealthy_account() -> UserAccount:
    """100 WETH ($200k) against $160,100 of debt — HF just under 1.

    Deliberately far above the small-position threshold, so the close factor is decided by the
    health factor alone rather than by the "too small to leave a remnant" rule.
    """
    return account(collateral_weth=100 * 10**18, debt_usdc=160_100 * 10**6)


# --------------------------------------------------------------------------- interest accrual


def test_no_elapsed_time_means_no_growth() -> None:
    assert m.compounded_interest_ray(10**25, 0) == RAY
    assert m.linear_interest_ray(10**25, 0) == RAY
    assert m.compounded_interest_ray(10**25, -5) == RAY


def test_zero_rate_means_no_growth() -> None:
    assert m.compounded_interest_ray(0, 365 * 86400) == RAY


def test_compounding_exceeds_linear_over_a_year() -> None:
    rate = 5 * 10**25  # 5% APR in ray
    year = 365 * 86400
    assert m.compounded_interest_ray(rate, year) > m.linear_interest_ray(rate, year)


def test_compounded_interest_tracks_the_true_exponential_closely() -> None:
    """Aave's third-order binomial should sit within a basis point of e^rt at 5% for a year."""
    rate = 5 * 10**25
    year = 365 * 86400
    got = m.compounded_interest_ray(rate, year) / RAY
    assert got == pytest.approx(1.051271, rel=1e-4)


def test_debt_grows_with_no_event_emitted() -> None:
    """The failure mode this guards: caching a health factor and missing the crossing."""
    borrowing = reserve(USDC, decimals=6, borrow_rate_ray=10**26, last_update=NOW)  # 10% APR
    reserves = {WETH: WETH_RESERVE, USDC: borrowing}

    now = m.value_account(account(), reserves, PRICES, NOW)
    later = m.value_account(account(), reserves, PRICES, NOW + 180 * 86400)

    assert later.total_debt_base > now.total_debt_base
    assert later.health_factor_wad < now.health_factor_wad
    assert later.total_collateral_base == now.total_collateral_base  # no supply rate set


def test_projection_is_a_no_op_before_the_last_update() -> None:
    r = reserve(USDC, decimals=6, borrow_rate_ray=10**26, last_update=NOW)
    assert m.project_variable_borrow_index(r, NOW - 100) == r.variable_borrow_index_ray


# --------------------------------------------------------------------------- valuation


def test_health_factor_of_a_simple_position() -> None:
    # 1 WETH at $2,000 with an 80% threshold backing $1,000 of debt -> HF 1.6
    v = m.value_account(account(), RESERVES, PRICES, NOW)
    assert v.total_collateral_base == 2000 * USD
    assert v.total_debt_base == 1000 * USD
    assert v.health_factor_wad == 16 * 10**17
    assert not m.is_liquidatable(v)


def test_debt_free_account_is_not_liquidatable_and_has_unbounded_health() -> None:
    v = m.value_account(account(debt_usdc=0), RESERVES, PRICES, NOW)
    assert v.debt_free
    assert v.health_factor_wad == m.HEALTH_FACTOR_INFINITE
    assert not m.is_liquidatable(v)
    assert v.health_factor == float("inf")


def test_health_factor_of_exactly_one_is_not_liquidatable() -> None:
    """Aave's predicate is `< 1e18`, not `<= 1e18`. Off by one here means reverted attempts."""
    v = m.value_account(account(debt_usdc=1600 * 10**6), RESERVES, PRICES, NOW)
    assert v.health_factor_wad == WAD
    assert not m.is_liquidatable(v)


def test_a_dollar_more_debt_makes_it_liquidatable() -> None:
    v = m.value_account(account(debt_usdc=1601 * 10**6), RESERVES, PRICES, NOW)
    assert v.health_factor_wad < WAD
    assert m.is_liquidatable(v)


def test_missing_oracle_price_is_an_error_not_a_zero() -> None:
    with pytest.raises(KeyError):
        m.value_account(account(), RESERVES, {USDC: USD}, NOW)


def test_collateral_disabled_by_the_user_is_excluded() -> None:
    acct = UserAccount(
        address=to_address("0x00000000000000000000000000000000000000ff"),
        reserves=(
            UserReserve(
                asset=WETH, scaled_a_token_balance=10**18, usage_as_collateral_enabled=False
            ),
            UserReserve(asset=USDC, scaled_variable_debt=1000 * 10**6),
        ),
    )
    v = m.value_account(acct, RESERVES, PRICES, NOW)
    assert v.total_collateral_base == 0
    assert m.is_liquidatable(v)


# --------------------------------------------------------------------------- eMode


def test_emode_raises_the_threshold_and_the_health_factor() -> None:
    """An eMode account priced with reserve defaults looks liquidatable when it is not."""
    emode = EModeCategory(id=1, ltv_bps=9000, liquidation_threshold_bps=9300,
                          liquidation_bonus_bps=10200, label="ETH correlated")
    plain = m.value_account(account(), RESERVES, PRICES, NOW)
    boosted = m.value_account(account(), RESERVES, PRICES, NOW, emode=emode)

    assert boosted.average_liquidation_threshold_bps == 9300
    assert boosted.health_factor_wad > plain.health_factor_wad


def test_emode_shrinks_the_liquidation_bonus() -> None:
    """The other half of the eMode trap: correlated categories pay a much smaller bonus."""
    emode = EModeCategory(id=1, ltv_bps=9000, liquidation_threshold_bps=9300,
                          liquidation_bonus_bps=10200)
    _, _, plain_bonus = m.effective_params(WETH_RESERVE, None)
    _, _, emode_bonus = m.effective_params(WETH_RESERVE, emode)
    assert plain_bonus == 10500
    assert emode_bonus == 10200


def test_emode_zero_does_not_override() -> None:
    emode = EModeCategory(id=0, ltv_bps=1, liquidation_threshold_bps=2, liquidation_bonus_bps=3)
    assert m.effective_params(WETH_RESERVE, emode) == (7500, 8000, 10500)


def test_emode_does_not_apply_to_an_asset_outside_the_category() -> None:
    emode = EModeCategory(
        id=1, ltv_bps=9000, liquidation_threshold_bps=9300, liquidation_bonus_bps=10200,
        collateral_assets=frozenset({USDC}),
    )
    assert m.effective_params(WETH_RESERVE, emode) == (7500, 8000, 10500)


# --------------------------------------------------------------------------- ranking


def test_uniform_drop_to_liquidation_matches_the_health_factor() -> None:
    # HF 1.6 -> prices must fall to 1/1.6 = 62.5% of spot, a 37.5% drop.
    v = m.value_account(account(), RESERVES, PRICES, NOW)
    assert m.uniform_collateral_drop_to_liquidate_bps(v) == 3750


def test_already_liquidatable_accounts_need_no_drop() -> None:
    v = m.value_account(account(debt_usdc=2000 * 10**6), RESERVES, PRICES, NOW)
    assert m.uniform_collateral_drop_to_liquidate_bps(v) == 0


def test_debt_free_accounts_can_never_be_dropped_into_liquidation() -> None:
    v = m.value_account(account(debt_usdc=0), RESERVES, PRICES, NOW)
    assert m.uniform_collateral_drop_to_liquidate_bps(v) == 10_000


def test_single_asset_drop_needs_more_than_a_uniform_drop() -> None:
    """One asset falling alone must fall further than all of them falling together."""
    acct = UserAccount(
        address=to_address("0x00000000000000000000000000000000000000ff"),
        reserves=(
            UserReserve(asset=WETH, scaled_a_token_balance=10**18),
            UserReserve(asset=USDC, scaled_a_token_balance=2000 * 10**6),
            UserReserve(asset=USDC, scaled_variable_debt=1000 * 10**6),
        ),
    )
    v = m.value_account(acct, RESERVES, PRICES, NOW)
    uniform = m.uniform_collateral_drop_to_liquidate_bps(v)
    weth_only = m.single_asset_drop_to_liquidate_bps(v, 2000 * USD, 8000)
    assert weth_only > uniform


def test_single_asset_that_cannot_trigger_liquidation_alone_returns_full() -> None:
    v = m.value_account(account(), RESERVES, PRICES, NOW)
    assert m.single_asset_drop_to_liquidate_bps(v, 1 * USD, 8000) == 10_000


# --------------------------------------------------------------------------- close factor


def test_close_factor_is_half_for_a_shallow_liquidation() -> None:
    hf = 96 * 10**16  # 0.96
    assert m.close_factor_bps(hf, 50_000 * USD, 60_000 * USD, POLICY) == 5000


def test_close_factor_is_full_below_the_health_threshold() -> None:
    """HF <= 0.95 doubles the size of the deepest opportunities. Missing this halves them."""
    hf = 95 * 10**16
    assert m.close_factor_bps(hf, 50_000 * USD, 60_000 * USD, POLICY) == 10_000


def test_close_factor_is_full_for_a_small_position() -> None:
    hf = 99 * 10**16
    assert m.close_factor_bps(hf, 1_500 * USD, 60_000 * USD, POLICY) == 10_000


def test_close_factor_policy_can_disable_the_small_position_rule() -> None:
    policy = CloseFactorPolicy(source_version="3.0", min_base_full_close=0, min_leftover_base=0)
    assert m.close_factor_bps(99 * 10**16, 10 * USD, 10 * USD, policy) == 5000


def test_small_positions_are_fully_liquidatable_even_when_barely_unhealthy() -> None:
    """A $1,601 position is below the $2,000 threshold, so the full close factor applies."""
    v = m.value_account(account(debt_usdc=1601 * 10**6), RESERVES, PRICES, NOW)
    sizing = m.max_debt_to_cover(1601 * 10**6, USDC_RESERVE, USD, v, POLICY)
    assert sizing.close_factor_bps == 10_000
    assert sizing.max_debt_to_cover == 1601 * 10**6


def test_max_debt_to_cover_applies_the_close_factor() -> None:
    v = m.value_account(large_unhealthy_account(), RESERVES, PRICES, NOW)
    assert v.health_factor_wad < WAD  # unhealthy...
    assert v.health_factor_wad > POLICY.close_factor_hf_threshold_wad  # ...but not deeply

    sizing = m.max_debt_to_cover(10_000 * 10**6, USDC_RESERVE, USD, v, POLICY)
    assert sizing.close_factor_bps == 5000
    assert sizing.max_debt_to_cover == 5_000 * 10**6
    assert not sizing.forced_full_by_dust


def test_dust_rule_forces_a_full_repayment() -> None:
    """Leaving $250 behind is illegal, so the only legal larger option is to repay everything.

    The account is large (so the close factor is 50%) but this particular debt reserve holds
    only $500 of it — the multi-debt case, where the dust rule actually bites.
    """
    v = m.value_account(large_unhealthy_account(), RESERVES, PRICES, NOW)
    user_debt = 500 * 10**6  # 50% would leave $250, below the $1,000 minimum leftover
    sizing = m.max_debt_to_cover(user_debt, USDC_RESERVE, USD, v, POLICY)
    assert sizing.close_factor_bps == 5000
    assert sizing.forced_full_by_dust
    assert sizing.max_debt_to_cover == user_debt


def test_no_dust_problem_when_the_remainder_is_large() -> None:
    v = m.value_account(large_unhealthy_account(), RESERVES, PRICES, NOW)
    sizing = m.max_debt_to_cover(10_000 * 10**6, USDC_RESERVE, USD, v, POLICY)
    assert not sizing.forced_full_by_dust
    assert sizing.max_debt_to_cover == 5_000 * 10**6


# --------------------------------------------------------------------------- seizure


def test_seizure_pays_the_bonus_in_collateral() -> None:
    q = m.calculate_seizure(
        debt_to_cover=1000 * 10**6,
        debt_reserve=USDC_RESERVE,
        collateral_reserve=WETH_RESERVE,
        debt_price=USD,
        collateral_price=2000 * USD,
        user_collateral_balance=10**18,
    )
    assert q.collateral_at_par == 5 * 10**17          # $1,000 of WETH at $2,000
    assert q.collateral_seized == 525 * 10**15        # +5% bonus
    assert q.protocol_fee == 0
    assert q.collateral_to_liquidator == 525 * 10**15
    assert q.gross_bonus == 25 * 10**15
    assert not q.collateral_capped


def test_protocol_fee_is_charged_on_the_bonus_not_the_principal() -> None:
    """A 10% protocol fee on a 5% bonus leaves 4.5%, not 4.5 percentage points off the whole."""
    weth = reserve(WETH, decimals=18, protocol_fee_bps=1000)
    q = m.calculate_seizure(
        debt_to_cover=1000 * 10**6,
        debt_reserve=USDC_RESERVE,
        collateral_reserve=weth,
        debt_price=USD,
        collateral_price=2000 * USD,
        user_collateral_balance=10**18,
    )
    assert q.protocol_fee == 25 * 10**14                     # 10% of the 0.025 WETH bonus
    assert q.collateral_to_liquidator == 5225 * 10**14
    assert q.gross_bonus == 225 * 10**14                     # 4.5% of $1,000, in WETH


def test_seizure_is_capped_by_the_users_collateral() -> None:
    q = m.calculate_seizure(
        debt_to_cover=1000 * 10**6,
        debt_reserve=USDC_RESERVE,
        collateral_reserve=WETH_RESERVE,
        debt_price=USD,
        collateral_price=2000 * USD,
        user_collateral_balance=10**17,  # only 0.1 WETH = $200
    )
    assert q.collateral_capped
    assert q.collateral_seized == 10**17
    # $200 of collateral buys off $200/1.05 = $190.48 of debt
    assert q.debt_to_cover_actual == 190_476_190
    assert q.debt_to_cover_actual < q.debt_to_cover_requested


def test_emode_bonus_applies_to_the_seizure() -> None:
    emode = EModeCategory(id=1, ltv_bps=9000, liquidation_threshold_bps=9300,
                          liquidation_bonus_bps=10200)
    q = m.calculate_seizure(
        debt_to_cover=1000 * 10**6,
        debt_reserve=USDC_RESERVE,
        collateral_reserve=WETH_RESERVE,
        debt_price=USD,
        collateral_price=2000 * USD,
        user_collateral_balance=10**18,
        emode=emode,
    )
    assert q.collateral_seized == percent_mul(5 * 10**17, 10200)
    assert q.gross_bonus == 10**16  # 2%, not 5%


def test_seizure_rejects_a_non_positive_repayment() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        m.calculate_seizure(
            debt_to_cover=0,
            debt_reserve=USDC_RESERVE,
            collateral_reserve=WETH_RESERVE,
            debt_price=USD,
            collateral_price=2000 * USD,
            user_collateral_balance=10**18,
        )
