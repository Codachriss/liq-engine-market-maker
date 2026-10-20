"""ABI codec and Aave v3 log decoding."""

from __future__ import annotations

import pytest

from liqengine.core import abi
from liqengine.core.types import to_address
from liqengine.protocols.aave_v3 import events


def word(value: int) -> str:
    return f"{value:064x}"


def addr_word(address: str) -> str:
    return f"{int(address, 16):064x}"


# --------------------------------------------------------------------------- selectors


def test_known_selector_matches_the_canonical_value() -> None:
    """`transfer(address,uint256)` is the most widely published selector there is."""
    assert abi.selector("transfer(address,uint256)") == "0xa9059cbb"


def test_known_event_topic_matches_the_canonical_value() -> None:
    assert abi.event_topic("Transfer(address,address,uint256)") == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )


def test_selectors_are_derived_not_pasted() -> None:
    """A wrong pasted constant reads as 'this function does not exist'. Derive it."""
    assert abi.selector("getPool()") == events.SELECTOR_GET_POOL
    assert abi.event_topic(events.SIG_LIQUIDATION_CALL) == events.TOPIC_LIQUIDATION_CALL


# --------------------------------------------------------------------------- words


def test_words_splits_on_32_byte_boundaries() -> None:
    assert len(abi.words("0x" + word(1) + word(2))) == 2


def test_partial_trailing_word_is_rejected() -> None:
    with pytest.raises(ValueError, match="whole number of words"):
        abi.words("0x" + word(1) + "abcd")


def test_odd_length_hex_is_rejected() -> None:
    with pytest.raises(ValueError, match="odd-length"):
        abi.strip_hex("0xabc")


def test_address_word_must_be_left_padded() -> None:
    dirty = "ff" * 12 + "00" * 20
    with pytest.raises(ValueError, match="left-padded"):
        abi.word_to_address(bytes.fromhex(dirty))


def test_bool_word_rejects_non_canonical_values() -> None:
    with pytest.raises(ValueError, match="canonical bool"):
        abi.word_to_bool(bytes.fromhex(word(2)))


def test_signed_words_decode_negative_answers() -> None:
    """Chainlink answers are int256 and are legitimately negative for some feeds."""
    minus_one = b"\xff" * 32
    assert abi.word_to_signed_int(minus_one) == -1
    assert abi.word_to_int(minus_one) == 2**256 - 1


# --------------------------------------------------------------------------- encoding


def test_encode_call_lays_out_static_arguments() -> None:
    encoded = abi.encode_call(
        "getUserAccountData(address)", "0x00000000000000000000000000000000000000aa"
    )
    assert encoded.startswith(abi.selector("getUserAccountData(address)"))
    assert encoded.endswith("aa")
    assert len(encoded) == 2 + 8 + 64


def test_encode_call_rejects_bools_to_avoid_silent_coercion() -> None:
    with pytest.raises(TypeError, match="bools"):
        abi.encode_call("f(uint256)", True)


def test_encode_uint_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="out of range"):
        abi.encode_uint(2**256)


# --------------------------------------------------------------------------- strings


def test_decode_dynamic_string() -> None:
    payload = "0x" + word(32) + word(4) + "55534443".ljust(64, "0")
    assert abi.decode_string(payload) == "USDC"


def test_decode_bytes32_string() -> None:
    """MKR and other early tokens declare symbol() as bytes32, not string."""
    payload = "0x" + "4d4b52".ljust(64, "0")
    assert abi.decode_string(payload) == "MKR"


def test_decode_empty_string() -> None:
    assert abi.decode_string("0x") == ""


# --------------------------------------------------------------------------- liquidation logs


COLLATERAL = "0x0000000000000000000000000000000000000011"
DEBT = "0x0000000000000000000000000000000000000022"
BORROWER = "0x0000000000000000000000000000000000000033"
LIQUIDATOR = "0x0000000000000000000000000000000000000044"


def liquidation_log(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "blockNumber": "0x64",
        "blockHash": "0xABC",
        "transactionHash": "0xDEF",
        "transactionIndex": "0x2",
        "logIndex": "0x7",
        "topics": [
            events.TOPIC_LIQUIDATION_CALL,
            addr_word(COLLATERAL),
            addr_word(DEBT),
            addr_word(BORROWER),
        ],
        "data": "0x" + word(1000 * 10**6) + word(5 * 10**17) + addr_word(LIQUIDATOR) + word(0),
    }
    return base | overrides


def test_decode_liquidation_call() -> None:
    decoded = events.decode_liquidation_call(liquidation_log())
    assert decoded.block_number == 100
    assert decoded.transaction_index == 2
    assert decoded.collateral_asset == to_address(COLLATERAL)
    assert decoded.debt_asset == to_address(DEBT)
    assert decoded.user == to_address(BORROWER)
    assert decoded.liquidator == to_address(LIQUIDATOR)
    assert decoded.debt_to_cover == 1000 * 10**6
    assert decoded.liquidated_collateral_amount == 5 * 10**17
    assert decoded.receive_a_token is False


def test_decoded_hashes_are_lowercased() -> None:
    decoded = events.decode_liquidation_call(liquidation_log())
    assert decoded.block_hash == "0xabc"
    assert decoded.transaction_hash == "0xdef"


def test_uid_is_stable_across_rescans() -> None:
    a = events.decode_liquidation_call(liquidation_log())
    b = events.decode_liquidation_call(liquidation_log())
    assert a.uid == b.uid == "0xdef:7"


def test_receive_atoken_flag_decodes() -> None:
    data = "0x" + word(1) + word(1) + addr_word(LIQUIDATOR) + word(1)
    assert events.decode_liquidation_call(liquidation_log(data=data)).receive_a_token is True


def test_wrong_topic_is_rejected() -> None:
    log = liquidation_log()
    log["topics"] = ["0x" + "11" * 32, addr_word(COLLATERAL), addr_word(DEBT), addr_word(BORROWER)]
    with pytest.raises(ValueError, match="unexpected topic0"):
        events.decode_liquidation_call(log)


def test_wrong_topic_count_is_rejected() -> None:
    log = liquidation_log()
    log["topics"] = [events.TOPIC_LIQUIDATION_CALL, addr_word(COLLATERAL)]
    with pytest.raises(ValueError, match="4 topics"):
        events.decode_liquidation_call(log)


def test_wrong_data_length_is_rejected() -> None:
    """A short payload means the wrong contract or a decoder bug — never a partial record."""
    with pytest.raises(ValueError, match="4 words"):
        events.decode_liquidation_call(liquidation_log(data="0x" + word(1)))


def test_config_invalidating_topics_include_proxy_upgrades() -> None:
    assert events.TOPIC_UPGRADED in events.CONFIG_INVALIDATING_TOPICS
    assert events.TOPIC_COLLATERAL_CONFIG_CHANGED in events.CONFIG_INVALIDATING_TOPICS
