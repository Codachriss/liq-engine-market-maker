"""W1: the batched scaled-balance reader.

The decode is checked against hand-built payloads here. It was separately checked against the
live Base deployment by cross-reading `getUserReserveData` for *every* reserve and confirming
that non-zero positions appear exactly where the `getUserConfiguration` bitmap says, and that
`scaledVariableDebt` matches word for word.
"""

from __future__ import annotations

import pytest

from liqengine.config.loader import load_registry
from liqengine.core.abi import selector
from liqengine.core.errors import HaltCondition, ProviderError
from liqengine.core.types import Address, BlockHash, BlockRef, ChainId, to_address
from liqengine.positions.book import ScaledBalanceReader
from liqengine.positions.reader import AaveScaledBalanceReader
from liqengine.protocols.aave_v3.math import HEALTH_FACTOR_INFINITE

#: The values pinned in `config/protocols/aave_v3_base.yaml`, so that the one test which goes
#: through live address resolution passes its own tripwire.
POOL = to_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")
ORACLE = to_address("0x2Cc0Fc26eD4563A5ce5e8bdcfe1A2878676Ae156")
PINNED_DATA_PROVIDER = to_address("0x0f43731eb8d45a581f4a36dd74f5f358bc90c73a")
DATA_PROVIDER = PINNED_DATA_PROVIDER

WETH = to_address("0x4200000000000000000000000000000000000006")
USDC = to_address("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
CBBTC = to_address("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf")
RESERVES = [WETH, USDC, CBBTC]

ALICE = to_address("0x" + "a1" * 20)
BOB = to_address("0x" + "b0" * 20)


def _uint(value: int) -> str:
    return f"{value:064x}"


def _addr_word(address: str) -> str:
    return "00" * 12 + address.removeprefix("0x")


def _bits(*, collateral: tuple[int, ...] = (), borrowing: tuple[int, ...] = ()) -> int:
    """Build a `getUserConfiguration` bitmap: bit 2i borrows, bit 2i+1 is collateral."""
    value = 0
    for index in borrowing:
        value |= 1 << (2 * index)
    for index in collateral:
        value |= 1 << (2 * index + 1)
    return value


def _account_data(health_factor: int) -> str:
    """Six words; `healthFactor` is the last."""
    return "0x" + "".join(
        _uint(v) for v in (1_000, 500, 100, 8_000, 7_500, health_factor)
    )


class _FakePool:
    """Dispatches `eth_call` on (to, selector). Records every block tag it was given."""

    def __init__(
        self,
        *,
        reserves: list[Address] = RESERVES,
        config: dict[Address, int] | None = None,
        health: dict[Address, int] | None = None,
        emode: dict[Address, int] | None = None,
        scaled: dict[tuple[Address, Address, str], int] | None = None,
    ) -> None:
        self.reserves = reserves
        self.config = config or {}
        self.health = health or {}
        self.emode = emode or {}
        self.scaled = scaled or {}
        self.tags: list[str] = []
        self.batches: list[int] = []
        self.batch_broken = False
        self.no_data_provider = False
        self.calls = 0
        # aToken / variableDebtToken per reserve, deterministic stand-ins.
        self.a_tokens = {a: to_address("0xaa" + a[4:]) for a in reserves}
        self.debt_tokens = {a: to_address("0xdd" + a[4:]) for a in reserves}

    async def block_by_number(self, number, *, policy=None) -> BlockRef:
        return BlockRef(
            chain_id=ChainId(8453),
            number=52_000_000 if number == "latest" else int(number),
            hash=BlockHash("0x" + "ab" * 32),
            parent_hash=BlockHash("0x" + "cd" * 32),
            timestamp=1_700_000_000,
        )

    async def call(self, method: str, params: list, *, policy=None) -> str:
        self.calls += 1
        assert method == "eth_call"
        target = to_address(str(params[0]["to"]))
        data = str(params[0]["data"])
        self.tags.append(str(params[1]))
        return self._dispatch(target, data)

    async def batch(self, calls: list) -> list[object]:
        if self.batch_broken:
            raise ProviderError("fake", "batching unsupported")
        self.batches.append(len(calls))
        out: list[object] = []
        for method, params in calls:
            out.append(await self.call(method, list(params)))
        return out

    def _dispatch(self, target: Address, data: str) -> str:
        sig = data[:10]
        arg1 = "0x" + data[34:74] if len(data) >= 74 else None
        arg2 = "0x" + data[98:138] if len(data) >= 138 else None

        # Address resolution, reached only when an address was not supplied. These return the
        # values pinned in `config/protocols/aave_v3_base.yaml`, so the tripwire passes.
        if sig == selector("getPool()"):
            return "0x" + _addr_word(POOL)
        if sig == selector("getPriceOracle()"):
            return "0x" + _addr_word(ORACLE)
        if sig == selector("getPoolDataProvider()"):
            if self.no_data_provider:
                raise ProviderError("fake", "getPoolDataProvider reverted")
            return "0x" + _addr_word(PINNED_DATA_PROVIDER)

        if sig == selector("getReservesList()"):
            return (
                "0x"
                + _uint(32)
                + _uint(len(self.reserves))
                + "".join(_addr_word(a) for a in self.reserves)
            )
        if sig == selector("getReserveTokensAddresses(address)"):
            asset = to_address(str(arg1))
            return (
                "0x"
                + _addr_word(self.a_tokens[asset])
                + _addr_word("0x" + "00" * 20)
                + _addr_word(self.debt_tokens[asset])
            )
        if sig == selector("getUserConfiguration(address)"):
            return "0x" + _uint(self.config.get(to_address(str(arg1)), 0))
        if sig == selector("getUserAccountData(address)"):
            user = to_address(str(arg1))
            return _account_data(self.health.get(user, HEALTH_FACTOR_INFINITE))
        if sig == selector("getUserEMode(address)"):
            return "0x" + _uint(self.emode.get(to_address(str(arg1)), 0))
        if sig == selector("scaledBalanceOf(address)"):
            user = to_address(str(arg1))
            for asset in self.reserves:
                if target == self.a_tokens[asset]:
                    return "0x" + _uint(self.scaled.get((user, asset, "collateral"), 0))
                if target == self.debt_tokens[asset]:
                    return "0x" + _uint(self.scaled.get((user, asset, "debt"), 0))
            raise AssertionError(f"scaledBalanceOf on unknown token {target}")
        if sig == selector("getUserReserveData(address,address)"):
            assert arg2 is not None
            return "0x" + _uint(0) * 9
        raise AssertionError(f"unexpected call {sig} to {target}")


def _reader(pool: _FakePool, **kwargs) -> AaveScaledBalanceReader:
    registry = load_registry("config", environ={})
    return AaveScaledBalanceReader(
        venue=registry.venue("aave_v3_base"),
        pool=pool,  # type: ignore[arg-type]
        pool_address=POOL,
        data_provider=DATA_PROVIDER,
        **kwargs,
    )


# --------------------------------------------------------------------------------------


async def test_a_bitmap_selects_exactly_the_reserves_the_account_uses() -> None:
    """WETH collateral (index 0), USDC debt (index 1), cbBTC untouched (index 2)."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,), borrowing=(1,))},
        health={ALICE: 2 * 10**18},
        scaled={
            (ALICE, WETH, "collateral"): 5_000,
            (ALICE, USDC, "debt"): 1_200,
        },
    )
    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])

    account = accounts[ALICE].account
    assert [r.asset for r in account.reserves] == sorted([WETH, USDC])
    assert account.reserve(WETH).scaled_a_token_balance == 5_000  # type: ignore[union-attr]
    assert account.reserve(WETH).scaled_variable_debt == 0  # type: ignore[union-attr]
    assert account.reserve(USDC).scaled_variable_debt == 1_200  # type: ignore[union-attr]
    assert account.reserve(CBBTC) is None, "an untouched reserve is absent, not zero-filled"


async def test_the_health_factor_is_read_not_computed() -> None:
    """`ReconciledAccount` exists to be the independent value the projection is checked
    against. Deriving it here would make the drift alarm compare a number to itself."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,), borrowing=(1,))},
        health={ALICE: 1_078_500_000_000_000_000},
        scaled={(ALICE, WETH, "collateral"): 1, (ALICE, USDC, "debt"): 1},
    )
    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])
    assert accounts[ALICE].health_factor_wad == 1_078_500_000_000_000_000


async def test_a_debt_free_account_reports_the_infinite_sentinel() -> None:
    """Aave returns `type(uint256).max`, which is exactly `HEALTH_FACTOR_INFINITE`, so the
    chain's value passes straight through without a special case."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,))},
        scaled={(ALICE, WETH, "collateral"): 9},
    )
    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])
    assert accounts[ALICE].health_factor_wad == HEALTH_FACTOR_INFINITE


async def test_the_collateral_bit_becomes_the_usage_flag() -> None:
    """Borrowing USDC without using it as collateral. Aave consults this same bit when
    valuing the account, so it is the flag rather than a proxy for it."""
    pool = _FakePool(
        config={ALICE: _bits(borrowing=(1,), collateral=(0,))},
        health={ALICE: 3 * 10**18},
        scaled={(ALICE, WETH, "collateral"): 7, (ALICE, USDC, "debt"): 11},
    )
    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])
    account = accounts[ALICE].account

    assert account.reserve(USDC).usage_as_collateral_enabled is False  # type: ignore[union-attr]
    assert account.reserve(WETH).usage_as_collateral_enabled is True  # type: ignore[union-attr]


async def test_emode_category_is_carried_through() -> None:
    """eMode swaps in category LT/LTV/bonus. An account read without it is mispriced."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,))},
        emode={ALICE: 1},
        scaled={(ALICE, WETH, "collateral"): 1},
    )
    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])
    assert accounts[ALICE].account.emode_category_id == 1


async def test_an_account_using_nothing_yields_no_legs() -> None:
    pool = _FakePool(config={ALICE: 0})
    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])
    assert accounts[ALICE].account.reserves == ()


async def test_every_call_is_pinned_to_one_block() -> None:
    """A configuration read at block N with balances at N+1 is a snapshot of neither. This is
    also what makes a plain JSON-RPC batch a consistent view without a Multicall3 aggregator.
    """
    pool = _FakePool(
        config={
            ALICE: _bits(collateral=(0,), borrowing=(1,)),
            BOB: _bits(collateral=(2,)),
        },
        health={ALICE: 2 * 10**18},
        scaled={(ALICE, WETH, "collateral"): 1, (ALICE, USDC, "debt"): 1},
    )
    await _reader(pool).read_accounts("aave_v3_base", [ALICE, BOB], block=52_000_123)

    pinned = [t for t in pool.tags if t != "latest"]
    assert pinned, "the account reads must name a block"
    assert set(pinned) == {hex(52_000_123)}, "all of them, and the same one"


async def test_duplicate_borrowers_are_read_once() -> None:
    pool = _FakePool(config={ALICE: 0})
    await _reader(pool).read_accounts("aave_v3_base", [ALICE, ALICE, ALICE])
    # Three header calls for one account, plus the priming reads.
    assert sum(1 for t in pool.tags if t != "latest") == 3


async def test_priming_is_cached_across_reads() -> None:
    """Reserve listings change only when governance lists a reserve; re-reading them on every
    pass would triple the cost of a loop that runs continuously."""
    pool = _FakePool(config={ALICE: 0})
    reader = _reader(pool)
    await reader.read_accounts("aave_v3_base", [ALICE])
    priming_calls = sum(1 for t in pool.tags if t == "latest")

    await reader.read_accounts("aave_v3_base", [ALICE])
    assert sum(1 for t in pool.tags if t == "latest") == priming_calls


async def test_a_provider_that_cannot_batch_still_produces_a_full_read() -> None:
    """Free tiers commonly reject batching. The position book becoming un-reconcilable on
    exactly the endpoints a solo operator starts with would be the wrong failure."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,), borrowing=(1,))},
        health={ALICE: 2 * 10**18},
        scaled={(ALICE, WETH, "collateral"): 5_000, (ALICE, USDC, "debt"): 1_200},
    )
    pool.batch_broken = True

    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])

    assert pool.batches == [], "nothing was batched"
    assert accounts[ALICE].account.reserve(USDC).scaled_variable_debt == 1_200  # type: ignore[union-attr]


async def test_batches_are_bounded() -> None:
    pool = _FakePool(config=dict.fromkeys([ALICE, BOB], 0))
    await _reader(pool, batch_size=2).read_accounts("aave_v3_base", [ALICE, BOB])
    assert pool.batches and max(pool.batches) <= 2


async def test_an_unreadable_account_does_not_discard_the_others() -> None:
    """It stays dirty and is retried next pass. Adopting a partial account as truth is the
    alternative, and it is worse."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,)), BOB: _bits(collateral=(0,))},
        scaled={(ALICE, WETH, "collateral"): 42, (BOB, WETH, "collateral"): 7},
    )
    original = pool._dispatch

    def _fail_bobs_config(target: Address, data: str) -> str:
        if data[:10] == selector("getUserConfiguration(address)") and BOB[2:] in data:
            raise ProviderError("fake", "reverted")
        return original(target, data)

    pool._dispatch = _fail_bobs_config  # type: ignore[method-assign]
    pool.batch_broken = True  # force the per-call path so one failure is isolated

    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE, BOB])

    assert ALICE in accounts
    assert BOB not in accounts


async def test_no_data_provider_halts_rather_than_guessing_token_addresses() -> None:
    """aToken addresses are derivable only from the data provider. Guessing them would read
    balances off the wrong contract and report them as truth."""
    pool = _FakePool(config={ALICE: 0})
    pool.no_data_provider = True
    reader = _reader(pool)
    reader.data_provider = None

    with pytest.raises(HaltCondition, match="PoolDataProvider"):
        await reader.read_accounts("aave_v3_base", [ALICE])


async def test_the_result_satisfies_what_the_book_consumes() -> None:
    """The reader exists to feed `PositionBook.apply_reconciliation`, which needs an account,
    an independently-read health factor, and a block anchor."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,))},
        scaled={(ALICE, WETH, "collateral"): 3},
    )
    reconciled = (await _reader(pool).read_accounts("aave_v3_base", [ALICE]))[ALICE]

    assert reconciled.account.address == ALICE
    assert reconciled.at_block.number == 52_000_000
    assert reconciled.at_block.hash
    assert isinstance(reconciled.health_factor_wad, int)


async def test_isolation_mode_is_unset_and_that_is_deliberate() -> None:
    """It needs each reserve's debt ceiling, which is W6. It constrains what debt an account
    may take on and does not enter the health factor, so leaving it unset cannot corrupt the
    reconciliation — but simulation will need it."""
    pool = _FakePool(
        config={ALICE: _bits(collateral=(0,))},
        scaled={(ALICE, WETH, "collateral"): 1},
    )
    accounts = await _reader(pool).read_accounts("aave_v3_base", [ALICE])
    assert accounts[ALICE].account.isolated_collateral is None


async def test_no_borrowers_makes_no_calls() -> None:
    pool = _FakePool()
    assert await _reader(pool).read_accounts("aave_v3_base", []) == {}
    assert pool.calls == 0, "not even priming — there is nothing to prime for"


def test_the_reader_satisfies_the_scaled_balance_reader_protocol() -> None:
    """Structural conformance, so `PositionBook` consumes it without knowing a chain exists
    and a replay driver can substitute identically (D10).

    The annotation is the assertion: `ScaledBalanceReader` is a plain `Protocol`, so mypy
    rejects this assignment if the signature drifts. An `isinstance` check would need the
    protocol marked `@runtime_checkable`, which would only verify the method *name* exists.
    """
    reader: ScaledBalanceReader = _reader(_FakePool())
    assert callable(reader.read_accounts)
