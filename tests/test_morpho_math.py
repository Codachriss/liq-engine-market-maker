"""Morpho Blue liquidation and health math.

Pins the pieces that are expensive to get wrong: the liquidation-incentive-factor formula and
its cap, share<->asset rounding, the health factor, and — the load-bearing one — that Aave's
`calculate_seizure` reproduces Morpho's native seizure once the LLTV is expressed as a bonus and
the 1e36 oracle price is converted to base-currency prices. That parity is what lets the Morpho
driver reuse the whole profit simulator unchanged.
"""

from __future__ import annotations

from liqengine.core.fixedpoint import WAD
from liqengine.core.types import to_address
from liqengine.protocols.aave_v3.math import calculate_seizure
from liqengine.protocols.aave_v3.state import ReserveState
from liqengine.protocols.morpho_blue import math as m
from liqengine.protocols.morpho_blue import projection as proj
from liqengine.protocols.morpho_blue.state import MarketParams, MarketState, Position

USDC = to_address("0x0000000000000000000000000000000000000002")
CBETH = to_address("0x0000000000000000000000000000000000000003")
ORACLE = to_address("0x0000000000000000000000000000000000000004")
IRM = to_address("0x0000000000000000000000000000000000000005")

USD = 10**8  # venue base unit


def test_lif_cap_and_endpoints() -> None:
    # Full LLTV: no shortfall, factor is exactly 1.0x (no bonus).
    assert m.liquidation_incentive_factor_wad(WAD) == WAD
    # Very low LLTV: formula would exceed the cap, so it clamps to 1.15x.
    assert m.liquidation_incentive_factor_wad(10 * 10**16) == m.MAX_LIQUIDATION_INCENTIVE_FACTOR
    # 86% LLTV is Morpho's documented ~1.0438 example.
    lif = m.liquidation_incentive_factor_wad(86 * 10**16)
    assert 1_043_000_000_000_000_000 <= lif <= 1_044_000_000_000_000_000
    assert m.liquidation_bonus_bps(86 * 10**16) == 10438


def test_shares_round_trip_direction() -> None:
    # up >= down always; both use virtual shares/assets so a fresh market is well-defined.
    up = m.to_assets_up(1_000_000, 2_000_000, 1_500_000)
    down = m.to_assets_down(1_000_000, 2_000_000, 1_500_000)
    assert up >= down
    # Empty market: 1 share converts via the virtual offsets, never divides by zero.
    assert m.to_assets_up(10, 0, 0) >= 1


def _market(lltv_wad: int, *, borrow_assets: int, borrow_shares: int) -> MarketState:
    params = MarketParams(
        loan_token=USDC, collateral_token=CBETH, oracle=ORACLE, irm=IRM, lltv_wad=lltv_wad
    )
    return MarketState(
        market_id=m.compute_market_id(params),
        params=params,
        total_supply_assets=10_000_000_000,
        total_supply_shares=10_000_000_000_000,
        total_borrow_assets=borrow_assets,
        total_borrow_shares=borrow_shares,
        last_update=1_700_000_000,
        fee_wad=0,
    )


def test_health_factor_boundary() -> None:
    # cbETH ($3000) collateral, USDC loan. Oracle price (1e36) carries the 18->6 decimal gap:
    # price = 3000 * 1e36 * 1e6 / 1e18 = 3000 * 1e24.
    oracle_price = 3000 * 10**24
    lltv = 80 * 10**16  # 80%
    # Totals at a 1:1 asset:share ratio (large, so virtual offsets are negligible), so a
    # borrow-share figure reads directly as loan base units.
    market = _market(lltv, borrow_assets=10**12, borrow_shares=10**12)
    # 1 cbETH collateral -> max borrow = 3000 * 0.8 = 2400 USDC.
    collateral = 10**18
    pos = Position(market.market_id, USDC, supply_shares=0, borrow_shares=0, collateral=collateral)

    # No debt -> infinite HF, debt-free.
    val = m.value_position(pos, market, oracle_price)
    assert val.debt_free and val.health_factor_wad == m.HEALTH_FACTOR_INFINITE

    # Borrow exactly 1200 USDC -> HF ~2.0 (maxBorrow 2400 / borrowed 1200).
    borrowed_pos = Position(
        market.market_id, USDC, supply_shares=0, borrow_shares=1_200_000_000, collateral=collateral
    )
    val = m.value_position(borrowed_pos, market, oracle_price)
    assert val.max_borrow_assets == 2400 * 10**6
    assert 1_950_000_000_000_000_000 <= val.health_factor_wad <= 2_050_000_000_000_000_000
    assert not m.is_liquidatable(val)

    # Borrow 2500 USDC against a 2400 ceiling -> underwater.
    under = Position(
        market.market_id, USDC, supply_shares=0, borrow_shares=2_500_000_000, collateral=collateral
    )
    assert m.is_liquidatable(m.value_position(under, market, oracle_price))


def test_calculate_seizure_parity_with_morpho() -> None:
    """Aave's calculate_seizure, fed the Morpho bonus and base-converted prices, must match
    Morpho's native `repaid * LIF * 1e36 / price` seizure to within bps-granularity rounding."""
    loan_decimals, collateral_decimals = 6, 18
    oracle_price = 3000 * 10**24  # cbETH in USDC, 1e36-scaled with the decimal gap baked in
    lltv = 86 * 10**16
    repaid = 500 * 10**6  # repay 500 USDC

    # Morpho native: seized = wMulDown(repaid, LIF) * 1e36 / price (floor), collateral uncapped.
    lif = m.liquidation_incentive_factor_wad(lltv)
    morpho_seized = m.mul_div_down(m.w_mul_down(repaid, lif), m.ORACLE_PRICE_SCALE, oracle_price)

    # Aave path via the shared simulator math.
    collateral_price, debt_price = m.base_prices(
        oracle_price,
        loan_decimals=loan_decimals,
        collateral_decimals=collateral_decimals,
        base_unit=USD,
    )
    bonus_bps = m.liquidation_bonus_bps(lltv)
    collateral_reserve = ReserveState(
        asset=CBETH,
        symbol="cbETH",
        decimals=collateral_decimals,
        ltv_bps=0,
        liquidation_threshold_bps=int(lltv // 10**14),
        liquidation_bonus_bps=bonus_bps,
        liquidation_protocol_fee_bps=0,
        liquidity_index_ray=10**27,
        variable_borrow_index_ray=10**27,
        current_liquidity_rate_ray=0,
        current_variable_borrow_rate_ray=0,
        last_update_timestamp=0,
    )
    debt_reserve = ReserveState(
        asset=USDC,
        symbol="USDC",
        decimals=loan_decimals,
        ltv_bps=0,
        liquidation_threshold_bps=0,
        liquidation_bonus_bps=10000,
        liquidation_protocol_fee_bps=0,
        liquidity_index_ray=10**27,
        variable_borrow_index_ray=10**27,
        current_liquidity_rate_ray=0,
        current_variable_borrow_rate_ray=0,
        last_update_timestamp=0,
    )
    seizure = calculate_seizure(
        debt_to_cover=repaid,
        debt_reserve=debt_reserve,
        collateral_reserve=collateral_reserve,
        debt_price=debt_price,
        collateral_price=collateral_price,
        user_collateral_balance=10 * 10**18,  # plenty, uncapped
        emode=None,
    )
    # No protocol fee on Morpho, so to-liquidator == seized.
    assert seizure.protocol_fee == 0
    # Match within 2 bps of the native figure (bps-granularity of the bonus + half-up rounding).
    assert abs(seizure.collateral_to_liquidator - morpho_seized) <= morpho_seized * 2 // 10_000


def test_market_id_is_deterministic_hash() -> None:
    params = MarketParams(
        loan_token=USDC, collateral_token=CBETH, oracle=ORACLE, irm=IRM, lltv_wad=86 * 10**16
    )
    mid = m.compute_market_id(params)
    assert mid.startswith("0x") and len(mid) == 66
    assert m.compute_market_id(params) == mid  # stable


def test_crossing_price_is_the_hf_one_boundary() -> None:
    lltv = 80 * 10**16
    market = _market(lltv, borrow_assets=10**12, borrow_shares=10**12)
    collateral = 10**18
    pos = Position(
        market.market_id,
        USDC,
        supply_shares=0,
        borrow_shares=1_200_000_000,
        collateral=collateral,
    )

    borrowed = m.borrowed_assets(pos, market)
    crossing = proj.crossing_price_1e36(borrowed, collateral, lltv)
    assert crossing is not None
    # At the crossing price the health factor is essentially 1 (within floor-rounding).
    assert abs(m.value_position(pos, market, crossing).health_factor_wad - WAD) <= 10**12
    # A hair below the crossing is liquidatable; a hair above is healthy.
    step = crossing // 1000
    assert m.is_liquidatable(m.value_position(pos, market, crossing - step))
    assert not m.is_liquidatable(m.value_position(pos, market, crossing + step))
    # No debt or no collateral -> there is no price-driven crossing.
    assert proj.crossing_price_1e36(0, collateral, lltv) is None
    assert proj.crossing_price_1e36(borrowed, 0, lltv) is None


def test_projected_position_distance() -> None:
    key = ("0x" + "cd" * 32, USDC)
    healthy = proj.ProjectedPosition(key=key, crossing_price_1e36=900, current_price_1e36=1_000)
    assert not healthy.liquidatable_now
    assert healthy.drop_to_cross_bps == (100 * 10_000) // 1_000  # 10% -> 1000 bps
    crossed = proj.ProjectedPosition(key=key, crossing_price_1e36=1_000, current_price_1e36=900)
    assert crossed.liquidatable_now
    assert crossed.drop_to_cross_bps == 0
