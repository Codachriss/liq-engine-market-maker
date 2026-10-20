"""Morpho event decoding, reader ABI decode, and the position book.

These pin the wire-format paths: the per-event owner topic index, the Liquidate and CreateMarket
decoders, the `market()`/`position()` word layout, and the book's dirty-tracking and composite
key round-trip. Getting any of these wrong produces a silently incomplete borrower set or
mis-valued positions, so they are exercised against hand-built payloads.
"""

from __future__ import annotations

from liqengine.core.abi import encode_address, encode_uint
from liqengine.core.types import to_address, to_hash32
from liqengine.protocols.morpho_blue import events as ev
from liqengine.protocols.morpho_blue.book import MorphoPositionBook, decode_key, encode_key
from liqengine.protocols.morpho_blue.reader import _decode_market, _decode_position
from liqengine.protocols.morpho_blue.state import MarketParams

MID = to_hash32("0x" + "ab" * 32)
LOAN = to_address("0x0000000000000000000000000000000000000002")
COLL = to_address("0x0000000000000000000000000000000000000003")
ORACLE = to_address("0x0000000000000000000000000000000000000004")
IRM = to_address("0x0000000000000000000000000000000000000005")
BORROWER = to_address("0x00000000000000000000000000000000000000bb")
CALLER = to_address("0x00000000000000000000000000000000000000cc")


def _topic_addr(addr: str) -> str:
    return "0x" + "00" * 12 + addr[2:]


def _data(*words: str) -> str:
    return "0x" + "".join(words)


def _log(topics: list[str], data: str = "0x", **over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "topics": topics,
        "data": data,
        "blockNumber": "0x10",
        "blockHash": "0x" + "11" * 32,
        "transactionHash": "0x" + "22" * 32,
        "transactionIndex": "0x1",
        "logIndex": "0x2",
    }
    base.update(over)
    return base


def test_owner_topic_index_per_event() -> None:
    # Borrow: onBehalf at topic[2].
    log = _log([ev.TOPIC_BORROW, MID, _topic_addr(BORROWER), _topic_addr(CALLER)])
    assert ev.market_and_owner(log) == (MID, BORROWER)
    # Repay: onBehalf at topic[3].
    log = _log([ev.TOPIC_REPAY, MID, _topic_addr(CALLER), _topic_addr(BORROWER)])
    assert ev.market_and_owner(log) == (MID, BORROWER)
    # SupplyCollateral: onBehalf at topic[3].
    log = _log([ev.TOPIC_SUPPLY_COLLATERAL, MID, _topic_addr(CALLER), _topic_addr(BORROWER)])
    assert ev.market_and_owner(log) == (MID, BORROWER)
    # WithdrawCollateral: onBehalf at topic[2].
    log = _log([ev.TOPIC_WITHDRAW_COLLATERAL, MID, _topic_addr(BORROWER), _topic_addr(CALLER)])
    assert ev.market_and_owner(log) == (MID, BORROWER)
    # Unrecognised topic0 -> None, never a raise.
    assert ev.market_and_owner(_log(["0x" + "ff" * 32, MID])) is None


def test_decode_liquidation() -> None:
    data = _data(
        encode_uint(500_000_000),  # repaid assets
        encode_uint(500_000_000_000),  # repaid shares
        encode_uint(10**17),  # seized
        encode_uint(0),  # bad debt assets
        encode_uint(0),  # bad debt shares
    )
    log = _log([ev.TOPIC_LIQUIDATE, MID, _topic_addr(CALLER), _topic_addr(BORROWER)], data)
    liq = ev.decode_liquidation(log)
    assert liq.market_id == MID
    assert liq.liquidator == CALLER
    assert liq.borrower == BORROWER
    assert liq.repaid_assets == 500_000_000
    assert liq.seized_assets == 10**17
    assert liq.uid == f"{liq.transaction_hash}:{liq.log_index}"


def test_decode_create_market_round_trips_to_id() -> None:
    from liqengine.protocols.morpho_blue.math import compute_market_id

    params = MarketParams(
        loan_token=LOAN, collateral_token=COLL, oracle=ORACLE, irm=IRM, lltv_wad=86 * 10**16
    )
    real_id = compute_market_id(params)
    data = _data(
        encode_address(LOAN),
        encode_address(COLL),
        encode_address(ORACLE),
        encode_address(IRM),
        encode_uint(86 * 10**16),
    )
    log = _log([ev.TOPIC_CREATE_MARKET, real_id], data)
    decoded_id, decoded = ev.decode_create_market(log)
    assert decoded_id == real_id
    assert decoded == params


def test_reader_decode_market_and_position() -> None:
    params = MarketParams(
        loan_token=LOAN, collateral_token=COLL, oracle=ORACLE, irm=IRM, lltv_wad=10**18
    )
    market_raw = _data(
        encode_uint(1_000),  # total supply assets
        encode_uint(2_000),  # total supply shares
        encode_uint(3_000),  # total borrow assets
        encode_uint(4_000),  # total borrow shares
        encode_uint(1_700_000_000),  # last update
        encode_uint(0),  # fee
    )
    market = _decode_market(MID, params, market_raw)
    assert market is not None
    assert market.total_borrow_assets == 3_000
    assert market.total_borrow_shares == 4_000
    assert market.last_update == 1_700_000_000

    # A never-created market reads as all zeros (lastUpdate 0) -> skipped.
    assert _decode_market(MID, params, _data(*([encode_uint(0)] * 6))) is None

    pos_raw = _data(encode_uint(111), encode_uint(222), encode_uint(333))
    pos = _decode_position(MID, BORROWER, pos_raw)
    assert pos is not None
    assert (pos.supply_shares, pos.borrow_shares, pos.collateral) == (111, 222, 333)


def test_book_key_round_trip_and_dirty_ordering() -> None:
    key = (MID, BORROWER)
    assert decode_key(encode_key(key)) == key

    book = MorphoPositionBook(venue="morpho_base")
    other = (MID, CALLER)
    book.touch(other, at_block=5)  # no debt hint
    book.touch(key, likely_debt=True, at_block=10)
    # Likely-debt keys come first.
    assert book.dirty_ordered()[0] == key
    assert set(book.dirty) == {key, other}

    entries = dict((k, (p, q)) for k, p, q in book.dirty_entries())
    assert entries[encode_key(key)] == (1, 10)
    assert entries[encode_key(other)] == (0, 5)

    # A read marks the key clean and caches its HF / debt-free status.
    book.note_read(key, health_factor_wad=2 * 10**18, at_block=11, debt_free=False)
    assert key not in book.dirty
    assert book.reconciled_health_factor(key) == 2 * 10**18
    assert book.read_block(key) == 11

    book.note_read(other, health_factor_wad=0, at_block=11, debt_free=True)
    assert book.known_debt_free(other)
