"""Tests for recovering the loser population from block data.

The classifier is pure — receipts are passed in rather than fetched — so the logic that decides
what counts as a lost race is testable without a chain. Which matters, because that logic is
the difference between a measurement and a number.
"""

from __future__ import annotations

from liqengine.phase0.contest import (
    BlockContest,
    ContestReport,
    candidate_hashes,
    classify_block,
)

WINNER = "0x" + "11" * 20
RIVAL = "0x" + "22" * 20
RIVAL_CONTRACT = "0x" + "33" * 20
STRANGER = "0x" + "99" * 20
BORROWER = "0x" + "bb" * 20
OTHER_BORROWER = "0x" + "cc" * 20

KNOWN = frozenset({WINNER, RIVAL, RIVAL_CONTRACT})


def tx(
    tx_hash: str,
    sender: str,
    *,
    to: str | None = None,
    borrower: str = BORROWER,
) -> dict[str, str]:
    """A transaction whose calldata names a borrower, as a liquidation attempt's would."""
    return {
        "hash": tx_hash,
        "from": sender,
        "to": to or RIVAL_CONTRACT,
        "input": "0x1cff79cd" + borrower.removeprefix("0x") + "00" * 32,
    }


def classify(transactions: list[dict[str, str]], reverted: set[str]) -> BlockContest:
    return classify_block(
        block_number=1000,
        transactions=transactions,
        winner_tx_hash="0xwin",
        winner=WINNER,
        borrower=BORROWER,
        known=KNOWN,
        reverted=frozenset(reverted),
    )


# ---------------------------------------------------------------- the head-to-head test


def test_a_rival_reverting_on_the_same_borrower_is_a_measured_loss() -> None:
    """Same block, same target, one winner. That is attributable to both parties."""
    result = classify([tx("0xa", RIVAL)], reverted={"0xa"})
    assert result.head_to_head == (RIVAL,)
    assert result.contested


def test_a_rival_reverting_on_a_different_borrower_is_not_head_to_head() -> None:
    """Still evidence of activity, but not evidence that this position was contested."""
    result = classify([tx("0xa", RIVAL, borrower=OTHER_BORROWER)], reverted={"0xa"})
    assert result.reverted_known == (RIVAL,)
    assert result.head_to_head == ()
    assert not result.contested


def test_a_successful_rival_transaction_is_not_a_loss() -> None:
    """Two liquidations can succeed in one block against different positions."""
    result = classify([tx("0xa", RIVAL)], reverted=set())
    assert result.reverted_known == ()


def test_the_winners_own_transaction_is_never_counted() -> None:
    winning = {"hash": "0xWIN", "from": WINNER, "to": None, "input": "0x"}
    assert classify([winning], reverted={"0xwin"}).reverted_known == ()


def test_a_stranger_reverting_is_ignored() -> None:
    """The known set is what makes a revert attributable; without it this is Arbitrum noise."""
    result = classify([tx("0xa", STRANGER, to=STRANGER)], reverted={"0xa"})
    assert result.reverted_known == ()


def test_a_rival_is_matched_by_the_contract_it_calls_not_just_the_sender() -> None:
    """Liquidators call through contracts: the event names the contract, the tx names the EOA."""
    result = classify([tx("0xa", STRANGER, to=RIVAL_CONTRACT)], reverted={"0xa"})
    assert result.head_to_head == (STRANGER,)


def test_several_rivals_in_one_block_are_all_counted() -> None:
    result = classify(
        [tx("0xa", RIVAL), tx("0xb", STRANGER, to=RIVAL_CONTRACT)], reverted={"0xa", "0xb"}
    )
    assert len(result.head_to_head) == 2


def test_borrower_matching_is_case_insensitive() -> None:
    """Checksummed addresses in calldata would otherwise silently never match."""
    upper = {
        "hash": "0xa",
        "from": RIVAL,
        "to": RIVAL_CONTRACT,
        "input": ("0x1cff79cd" + BORROWER.removeprefix("0x") + "00" * 32).upper(),
    }
    assert classify([upper], reverted={"0xa"}).head_to_head == (RIVAL,)


# ---------------------------------------------------------------- receipt budget


def test_only_plausible_rivals_cost_a_receipt_call() -> None:
    """Arbitrum blocks average tens of transactions and almost none are liquidations."""
    transactions = [
        tx("0xa", RIVAL),
        tx("0xb", STRANGER, to=STRANGER),
        tx("0xc", STRANGER, to=RIVAL_CONTRACT),
        {"hash": "0xWIN", "from": WINNER, "to": None, "input": "0x"},
    ]
    assert candidate_hashes(transactions, "0xwin", KNOWN) == ["0xa", "0xc"]


# ---------------------------------------------------------------- aggregation


def block(
    number: int, *, head_to_head: tuple[str, ...], reverted: tuple[str, ...] = ()
) -> BlockContest:
    return BlockContest(
        block_number=number,
        winner=WINNER,
        borrower=BORROWER,
        transactions=30,
        reverted_known=reverted or head_to_head,
        head_to_head=head_to_head,
    )


def test_the_report_separates_the_two_bounds() -> None:
    """Head-to-head understates contest; any-revert overstates it. Both are reported."""
    report = ContestReport(
        venue="v",
        min_usd=1000,
        blocks=(
            block(1, head_to_head=(RIVAL,)),
            block(2, head_to_head=(), reverted=(RIVAL,)),
            block(3, head_to_head=()),
            block(4, head_to_head=()),
        ),
    )
    assert report.contested_share == 0.25
    assert report.any_revert_share == 0.5
    assert report.head_to_head_losses == 1


def test_an_empty_sample_reports_zero_rather_than_dividing_by_it() -> None:
    report = ContestReport(venue="v", min_usd=1000, blocks=())
    assert report.contested_share == 0.0
    assert report.any_revert_share == 0.0


def test_losers_and_contested_winners_are_ranked() -> None:
    report = ContestReport(
        venue="v",
        min_usd=1000,
        blocks=(
            block(1, head_to_head=(RIVAL, STRANGER)),
            block(2, head_to_head=(RIVAL,)),
        ),
    )
    assert list(report.losers()) == [RIVAL, STRANGER]
    assert report.losers()[RIVAL] == 2
    assert report.contested_winners() == {WINNER: 2}
