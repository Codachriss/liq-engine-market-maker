"""Tests for the position book, forward projection, and the price-trigger index.

The properties pinned here are the ones where a plausible implementation is wrong in a way that
costs money rather than crashing: a crossing price that ignores an asset held as both collateral
and debt, a drift check that compares the chain to itself, a dirty set that drops an event
arriving mid-read.
"""

from __future__ import annotations

import pytest

from liqengine.core.fixedpoint import PERCENTAGE_FACTOR, RAY, WAD
from liqengine.core.types import Address, BlockRef, ChainId, to_address
from liqengine.positions import (
    PositionBook,
    PriceTriggerIndex,
    ReconciledAccount,
    crossing_price,
    crossing_prices,
    project_account,
    rank,
    seconds_to_liquidation,
    watch_entry,
)
from liqengine.protocols.aave_v3.math import HEALTH_FACTOR_INFINITE, value_account
from liqengine.protocols.aave_v3.state import EModeCategory, ReserveState, UserAccount, UserReserve
from liqengine.store import EventStore

CHAIN = ChainId(42161)
VENUE = "aave_v3_arbitrum"
WETH = to_address("0x" + "e1" * 20)
USDC = to_address("0x" + "c2" * 20)
WBTC = to_address("0x" + "b7" * 20)
BORROWER = to_address("0x" + "a0" * 20)
OTHER = to_address("0x" + "a1" * 20)

NOW = 1_800_000_000
BLOCK = BlockRef(chain_id=CHAIN, number=1000, hash="0x" + "aa" * 32, parent_hash="0x" + "ab" * 32,
                 timestamp=NOW)

#: Oracle base currency is USD with 8 decimals, as Aave v3 uses on L2s.
PRICE_WETH = 3_000 * 10**8
PRICE_USDC = 1 * 10**8
PRICE_WBTC = 60_000 * 10**8


def reserve(
    asset: Address,
    decimals: int,
    *,
    threshold_bps: int = 8_000,
    bonus_bps: int = 10_500,
    borrow_rate_ray: int = 0,
    liquidity_rate_ray: int = 0,
    last_update: int = NOW,
) -> ReserveState:
    return ReserveState(
        asset=asset,
        symbol=asset[:6],
        decimals=decimals,
        ltv_bps=threshold_bps - 500,
        liquidation_threshold_bps=threshold_bps,
        liquidation_bonus_bps=bonus_bps,
        liquidation_protocol_fee_bps=1_000,
        liquidity_index_ray=RAY,
        variable_borrow_index_ray=RAY,
        current_liquidity_rate_ray=liquidity_rate_ray,
        current_variable_borrow_rate_ray=borrow_rate_ray,
        last_update_timestamp=last_update,
    )


RESERVES = {
    WETH: reserve(WETH, 18, threshold_bps=8_000),
    USDC: reserve(USDC, 6, threshold_bps=8_500),
    WBTC: reserve(WBTC, 8, threshold_bps=7_000),
}
PRICES = {WETH: PRICE_WETH, USDC: PRICE_USDC, WBTC: PRICE_WBTC}


def account(*legs: UserReserve, address: Address = BORROWER, emode: int = 0) -> UserAccount:
    return UserAccount(address=address, reserves=legs, emode_category_id=emode)


# ---------------------------------------------------------------- projection


def test_projection_keeps_the_per_asset_breakdown() -> None:
    projected = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=10**18),
            UserReserve(asset=USDC, scaled_variable_debt=1_000 * 10**6),
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    weth = projected.exposure(WETH)
    usdc = projected.exposure(USDC)
    assert weth is not None and usdc is not None
    assert weth.collateral_base == PRICE_WETH          # one WETH
    assert weth.debt_base == 0
    assert usdc.debt_base == 1_000 * 10**8
    assert projected.valuation.total_debt_base == 1_000 * 10**8


def test_projection_agrees_with_the_contract_valuation() -> None:
    """The health factor has one implementation. The decomposition must not become a second."""
    acct = account(
        UserReserve(asset=WETH, scaled_a_token_balance=2 * 10**18),
        UserReserve(asset=USDC, scaled_variable_debt=4_000 * 10**6),
    )
    projected = project_account(acct, RESERVES, PRICES, NOW)
    assert projected.valuation == value_account(acct, RESERVES, PRICES, NOW)


def test_unpriced_reserve_raises_rather_than_valuing_at_zero() -> None:
    with pytest.raises(KeyError, match="no oracle price"):
        project_account(
            account(UserReserve(asset=WBTC, scaled_a_token_balance=10**8)),
            RESERVES,
            {WETH: PRICE_WETH, USDC: PRICE_USDC},
            NOW,
        )


# ---------------------------------------------------------------- crossing prices


def test_crossing_price_for_a_pure_collateral_asset() -> None:
    """One WETH at 3,000 backing 2,000 USDC of debt, threshold 80%: crosses at 2,500."""
    projected = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=10**18),
            UserReserve(asset=USDC, scaled_variable_debt=2_000 * 10**6),
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    crossing = crossing_price(projected, WETH)
    assert crossing is not None
    assert crossing.direction == "fall"
    assert crossing.price == pytest.approx(2_500 * 10**8, rel=1e-6)
    assert crossing.move_bps == pytest.approx(1_666, abs=2)  # a 16.7% fall


def test_the_crossing_price_actually_crosses() -> None:
    """The linear solve has to agree with the contract arithmetic it is approximating."""
    acct = account(
        UserReserve(asset=WETH, scaled_a_token_balance=10**18),
        UserReserve(asset=USDC, scaled_variable_debt=2_000 * 10**6),
    )
    projected = project_account(acct, RESERVES, PRICES, NOW)
    crossing = crossing_price(projected, WETH)
    assert crossing is not None

    just_above = value_account(acct, RESERVES, {**PRICES, WETH: crossing.price + 10**8}, NOW)
    just_below = value_account(acct, RESERVES, {**PRICES, WETH: crossing.price - 10**8}, NOW)
    assert just_above.health_factor_wad >= WAD
    assert just_below.health_factor_wad < WAD


def test_crossing_price_for_a_debt_asset_is_a_rise() -> None:
    """WETH borrowed against USDC collateral: it is a rise in WETH that liquidates you."""
    projected = project_account(
        account(
            UserReserve(asset=USDC, scaled_a_token_balance=10_000 * 10**6),
            UserReserve(asset=WETH, scaled_variable_debt=2 * 10**18),
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    crossing = crossing_price(projected, WETH)
    assert crossing is not None
    assert crossing.direction == "rise"
    # 10,000 USDC at 85% supports 8,500 of debt; 2 WETH reach that at 4,250 each.
    assert crossing.price == pytest.approx(4_250 * 10**8, rel=1e-6)


def test_an_asset_held_as_both_collateral_and_debt_is_netted() -> None:
    """Two separate sensitivities would each be wrong; the solve nets them in one denominator.

    One WETH of collateral against 0.1 WETH of debt is a *smaller* net long than one WETH alone,
    so the account crosses on a shallower fall — a higher crossing price, not a lower one. A
    naive collateral-only sensitivity says $1,250 when the true trigger is $1,428, which is a
    12% move of daylight in which the position is liquidatable and the watchlist says it is not.
    """
    both = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=10**18, scaled_variable_debt=10**17),
            UserReserve(asset=USDC, scaled_variable_debt=1_000 * 10**6),
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    collateral_only = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=10**18),
            UserReserve(asset=USDC, scaled_variable_debt=1_000 * 10**6),
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    netted = crossing_price(both, WETH)
    naive = crossing_price(collateral_only, WETH)
    assert netted is not None and naive is not None
    assert netted.direction == "fall"
    assert naive.price == pytest.approx(1_250 * 10**8, rel=1e-6)
    assert netted.price == pytest.approx(1_428 * 10**8, rel=1e-3)
    assert netted.price > naive.price


def test_no_crossing_when_the_asset_cannot_do_it_alone() -> None:
    """A tiny collateral leg going to zero still leaves the account healthy. Not a trigger."""
    projected = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=10**18),
            UserReserve(asset=WBTC, scaled_a_token_balance=1_000),  # dust
            UserReserve(asset=USDC, scaled_variable_debt=1_000 * 10**6),
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    assert crossing_price(projected, WBTC) is None


def test_debt_free_account_has_no_crossings() -> None:
    projected = project_account(
        account(UserReserve(asset=WETH, scaled_a_token_balance=10**18)), RESERVES, PRICES, NOW
    )
    assert projected.valuation.health_factor_wad == HEALTH_FACTOR_INFINITE
    assert crossing_prices(projected) == ()


def test_crossings_are_ordered_by_nearest_move() -> None:
    projected = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=10**18),
            UserReserve(asset=WBTC, scaled_a_token_balance=5 * 10**7),
            UserReserve(asset=USDC, scaled_variable_debt=4_000 * 10**6),
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    moves = [c.move_bps for c in crossing_prices(projected)]
    assert moves == sorted(moves)


def test_emode_moves_the_crossing_price() -> None:
    """An eMode account uses its category threshold; using the reserve default misprices it."""
    acct = account(
        UserReserve(asset=WETH, scaled_a_token_balance=10**18),
        UserReserve(asset=USDC, scaled_variable_debt=2_000 * 10**6),
        emode=1,
    )
    emode = EModeCategory(
        id=1, ltv_bps=9_000, liquidation_threshold_bps=9_300, liquidation_bonus_bps=10_100
    )
    default = crossing_price(project_account(acct, RESERVES, PRICES, NOW), WETH)
    boosted = crossing_price(project_account(acct, RESERVES, PRICES, NOW, emode), WETH)
    assert default is not None and boosted is not None
    assert boosted.price < default.price  # a higher threshold survives a deeper fall


# ---------------------------------------------------------------- accrual axis


def test_accrual_alone_can_reach_liquidation() -> None:
    """No oracle move, no event: debt grows into the collateral. Nobody is racing for these."""
    reserves = {
        WETH: reserve(WETH, 18, threshold_bps=8_000),
        USDC: reserve(USDC, 6, threshold_bps=8_500, borrow_rate_ray=RAY // 5),  # 20% APR
    }
    acct = account(
        UserReserve(asset=WETH, scaled_a_token_balance=10**18),
        UserReserve(asset=USDC, scaled_variable_debt=2_350 * 10**6),
    )
    seconds = seconds_to_liquidation(acct, reserves, PRICES, NOW)
    assert seconds is not None and 0 < seconds < 365 * 24 * 3600

    before = value_account(acct, reserves, PRICES, NOW + seconds - 1)
    after = value_account(acct, reserves, PRICES, NOW + seconds)
    assert before.health_factor_wad >= WAD
    assert after.health_factor_wad < WAD


def test_already_liquidatable_is_zero_seconds() -> None:
    acct = account(
        UserReserve(asset=WETH, scaled_a_token_balance=10**18),
        UserReserve(asset=USDC, scaled_variable_debt=2_900 * 10**6),
    )
    assert seconds_to_liquidation(acct, RESERVES, PRICES, NOW) == 0


def test_no_accrual_never_arrives() -> None:
    """Zero borrow rate: the account is exactly as healthy in a year as it is now."""
    acct = account(
        UserReserve(asset=WETH, scaled_a_token_balance=10**18),
        UserReserve(asset=USDC, scaled_variable_debt=1_000 * 10**6),
    )
    assert seconds_to_liquidation(acct, RESERVES, PRICES, NOW) is None


# ---------------------------------------------------------------- ranking


def _healthy(address: Address, debt_usdc: int) -> UserAccount:
    return account(
        UserReserve(asset=WETH, scaled_a_token_balance=10**18),
        UserReserve(asset=USDC, scaled_variable_debt=debt_usdc * 10**6),
        address=address,
    )


def test_ranking_is_by_proximity_not_size() -> None:
    """A whale ten percent away is worth less attention than a minnow a basis point away."""
    close_small = project_account(_healthy(BORROWER, 2_390), RESERVES, PRICES, NOW)
    far_large = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=100 * 10**18),
            UserReserve(asset=USDC, scaled_variable_debt=100_000 * 10**6),
            address=OTHER,
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    ordered = rank([far_large, close_small])
    assert [e.borrower for e in ordered] == [BORROWER, OTHER]
    assert far_large.valuation.total_debt_base > close_small.valuation.total_debt_base


def test_already_liquidatable_accounts_come_first() -> None:
    crossed = project_account(_healthy(OTHER, 2_900), RESERVES, PRICES, NOW)
    near = project_account(_healthy(BORROWER, 2_390), RESERVES, PRICES, NOW)
    assert [e.borrower for e in rank([near, crossed])] == [OTHER, BORROWER]


def test_dust_positions_are_filtered_before_simulation() -> None:
    """Phase 0: 71% of liquidations repay under $100. Their bonus is smaller than the gas."""
    dust = project_account(
        account(
            UserReserve(asset=WETH, scaled_a_token_balance=10**15),
            UserReserve(asset=USDC, scaled_variable_debt=2 * 10**6),
            address=OTHER,
        ),
        RESERVES,
        PRICES,
        NOW,
    )
    real = project_account(_healthy(BORROWER, 2_390), RESERVES, PRICES, NOW)
    ordered = rank([dust, real], min_debt_base=100 * 10**8)
    assert [e.borrower for e in ordered] == [BORROWER]


# ---------------------------------------------------------------- price trigger index


def test_a_predicted_price_returns_the_accounts_it_unlocks() -> None:
    """The query a predicted oracle update becomes, answered before the price is on chain."""
    near = watch_entry(project_account(_healthy(BORROWER, 2_390), RESERVES, PRICES, NOW))
    far = watch_entry(project_account(_healthy(OTHER, 1_000), RESERVES, PRICES, NOW))
    index = PriceTriggerIndex.build([near, far])

    # BORROWER crosses at $2,987.5; OTHER not until $1,250.
    assert index.triggered_by(WETH, 2_980 * 10**8) == [BORROWER]
    assert set(index.triggered_by(WETH, 1_200 * 10**8)) == {BORROWER, OTHER}
    assert index.triggered_by(WETH, 2_990 * 10**8) == []
    assert index.triggered_by(WETH, PRICE_WETH) == []


def test_the_index_holds_rises_and_falls_apart() -> None:
    faller = watch_entry(project_account(_healthy(BORROWER, 2_390), RESERVES, PRICES, NOW))
    riser = watch_entry(
        project_account(
            account(
                UserReserve(asset=USDC, scaled_a_token_balance=10_000 * 10**6),
                UserReserve(asset=WETH, scaled_variable_debt=2 * 10**18),
                address=OTHER,
            ),
            RESERVES,
            PRICES,
            NOW,
        )
    )
    index = PriceTriggerIndex.build([faller, riser])

    assert index.triggered_by(WETH, 2_980 * 10**8) == [BORROWER]  # a fall
    assert index.triggered_by(WETH, 5_000 * 10**8) == [OTHER]     # a rise
    assert index.accounts_for(WETH) == {BORROWER, OTHER}


def test_the_index_gives_the_dirty_set_for_an_oracle_update() -> None:
    """Recomputing only the exposed accounts is what makes 250ms blocks affordable."""
    entries = [
        watch_entry(project_account(_healthy(BORROWER, 2_390), RESERVES, PRICES, NOW)),
        watch_entry(
            project_account(
                account(
                    UserReserve(asset=WBTC, scaled_a_token_balance=10**8),
                    UserReserve(asset=USDC, scaled_variable_debt=30_000 * 10**6),
                    address=OTHER,
                ),
                RESERVES,
                PRICES,
                NOW,
            )
        ),
    ]
    index = PriceTriggerIndex.build(entries)
    assert index.accounts_for(WETH) == {BORROWER}
    assert index.accounts_for(WBTC) == {OTHER}


# ---------------------------------------------------------------- the book


def test_dirty_set_is_claimed_not_cleared() -> None:
    """An event arriving mid-read must survive; clearing after the read would drop it."""
    book = PositionBook(venue=VENUE, chain_id=CHAIN)
    book.touch(BORROWER)
    taken = book.take_dirty()
    assert taken == {BORROWER}
    assert book.dirty == frozenset()

    book.touch(OTHER)  # arrives while the read for BORROWER is in flight
    assert book.take_dirty() == {OTHER}


def test_reconciliation_reports_drift_against_the_state_we_were_using() -> None:
    """Measured before adopting the new balances, or the check compares the chain to itself."""
    book = PositionBook(venue=VENUE, chain_id=CHAIN, drift_tolerance_bps=50)
    stale = _healthy(BORROWER, 1_000)
    book.accounts[BORROWER] = stale

    truth = _healthy(BORROWER, 2_000)
    on_chain_hf = value_account(truth, RESERVES, PRICES, NOW).health_factor_wad
    report = book.apply_reconciliation(
        ReconciledAccount(account=truth, health_factor_wad=on_chain_hf, at_block=BLOCK),
        RESERVES,
        PRICES,
    )
    assert report is not None
    assert not report.within_tolerance
    assert report.divergence_bps > 50
    assert book.accounts[BORROWER] is truth


def test_reconciliation_of_a_matching_projection_is_within_tolerance() -> None:
    book = PositionBook(venue=VENUE, chain_id=CHAIN)
    acct = _healthy(BORROWER, 2_000)
    book.accounts[BORROWER] = acct
    on_chain_hf = value_account(acct, RESERVES, PRICES, NOW).health_factor_wad

    report = book.apply_reconciliation(
        ReconciledAccount(account=acct, health_factor_wad=on_chain_hf, at_block=BLOCK),
        RESERVES,
        PRICES,
    )
    assert report is not None
    assert report.divergence_bps == 0
    assert report.within_tolerance


def test_a_newly_discovered_account_has_nothing_to_diverge_from() -> None:
    book = PositionBook(venue=VENUE, chain_id=CHAIN)
    acct = _healthy(BORROWER, 2_000)
    report = book.apply_reconciliation(
        ReconciledAccount(account=acct, health_factor_wad=WAD, at_block=BLOCK), RESERVES, PRICES
    )
    assert report is None
    assert book.accounts[BORROWER] is acct


def test_debt_free_versus_indebted_is_total_disagreement() -> None:
    """The worst case: the book says no debt, the chain says otherwise. Not a small divergence."""
    book = PositionBook(venue=VENUE, chain_id=CHAIN)
    book.accounts[BORROWER] = account(
        UserReserve(asset=WETH, scaled_a_token_balance=10**18), address=BORROWER
    )
    report = book.apply_reconciliation(
        ReconciledAccount(
            account=_healthy(BORROWER, 2_000), health_factor_wad=WAD, at_block=BLOCK
        ),
        RESERVES,
        PRICES,
    )
    assert report is not None
    assert report.divergence_bps == PERCENTAGE_FACTOR
    assert not report.within_tolerance


def test_projection_skips_unpriced_accounts_rather_than_the_whole_book() -> None:
    """One unpriced long-tail reserve must not blind the book to everything else."""
    book = PositionBook(venue=VENUE, chain_id=CHAIN)
    book.accounts[BORROWER] = _healthy(BORROWER, 2_000)
    book.accounts[OTHER] = account(
        UserReserve(asset=WBTC, scaled_a_token_balance=10**8),
        UserReserve(asset=USDC, scaled_variable_debt=1_000 * 10**6),
        address=OTHER,
    )
    projected = book.project(RESERVES, {WETH: PRICE_WETH, USDC: PRICE_USDC}, NOW)
    assert [p.borrower for p in projected] == [BORROWER]


def test_book_round_trips_through_the_store(tmp_path) -> None:
    with EventStore(tmp_path / "book.sqlite") as store:
        store.initialise()
        book = PositionBook(venue=VENUE, chain_id=CHAIN)
        acct = UserAccount(
            address=BORROWER,
            reserves=(
                UserReserve(asset=WETH, scaled_a_token_balance=10**18),
                UserReserve(
                    asset=USDC, scaled_a_token_balance=500 * 10**6,
                    scaled_variable_debt=2_000 * 10**6,
                ),
            ),
            emode_category_id=1,
        )
        book.apply_reconciliation(
            ReconciledAccount(account=acct, health_factor_wad=WAD, at_block=BLOCK),
            RESERVES,
            PRICES,
        )
        assert book.persist(store) == 1

        reloaded = PositionBook(venue=VENUE, chain_id=CHAIN)
        assert reloaded.load(store) == 1
        restored = reloaded.accounts[BORROWER]
        assert restored.emode_category_id == 1
        assert restored.reserve(WETH).scaled_a_token_balance == 10**18
        assert restored.reserve(USDC).scaled_variable_debt == 2_000 * 10**6
        assert restored.reserve(USDC).scaled_a_token_balance == 500 * 10**6
        assert reloaded.borrowers_with_debt() == [BORROWER]


def test_unanchored_accounts_are_not_persisted(tmp_path) -> None:
    """A position stored without a block anchor is a fact a reorg cannot unwind."""
    with EventStore(tmp_path / "anchor.sqlite") as store:
        store.initialise()
        book = PositionBook(venue=VENUE, chain_id=CHAIN)
        book.accounts[BORROWER] = _healthy(BORROWER, 1_000)  # set directly, no reconciliation
        assert book.persist(store) == 0
