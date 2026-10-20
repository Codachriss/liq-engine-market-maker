# Architecture

## Pipeline

```
Blockchain
  ↓
Confirmed state  +  Pending state (per venue: sequencer feed / public mempool / pull-oracle stream)
  ↓
Protocol registry  →  Position book (event stream + forward index projection + reconciliation)
  ↓
Borrower intelligence (A)  ·  Competitor intelligence (B)  ·  Flow intelligence (C, later)
  ↓
Liquidation prediction  (who crosses HF=1, at what price, when)
  ↓
Full-path profit simulation  (flash borrow → repay debt → seize collateral → swap → repay → net)
  ↓
Risk engine + circuit breakers   ── deadman token ──┐
  ↓                                                  │
Inclusion strategy (latency race | priority auction | bundle auction)
  ↓                                                  │
Pre-sign revalidation  ←──────────────────────────────┘
  ↓
Execution (isolated hot wallet; no model key access)
  ↓
Inclusion monitoring → realised P&L (finalised only)
  ↓
Event-sourced observability store
  ↓
ML / pattern discovery → backtest → shadow → canary → production
```

## Decisions log

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | First protocol: **Aave v3**, generic adapter. First *live venue* chosen by Phase 0 data, not assumed. | Mainnet is where a solo operator has least edge. Aave v3 is near-identical across mainnet/Arbitrum/Base, so chain is configuration. |
| D2 | Venue ranking by **expected net capture**, not `profit ÷ competition ÷ latency`. | Competition and latency belong inside `P(win)`, not as divisors. |
| D3 | **Flash-liquidity first**, owned inventory later, compared on realised P&L. | Less capital tied up, no directional exposure. Prefer 0-fee sources (Balancer, Morpho) over Aave's premium. |
| D4 | **Professional RPC providers first**; self-host only when latency instrumentation proves lost EV. | Don't buy infrastructure before demonstrating edge. |
| D5 | Wallet intelligence built **A → B → C** (borrower, competitor, flow). | A+B are what the liquidation engine needs. |
| D6 | Inclusion is an **abstraction with per-venue implementations**, not "bid optimisation". | Arbitrum = FCFS latency race (no bidding, reverts cost gas). Base/OP = priority-fee auction. Mainnet = bundle auction. Different physics, same interface. |
| D7 | Oracle handling is split by **push vs pull**. | Chainlink push ⇒ predict the update from off-chain reference prices and race it. Pyth pull ⇒ *we* submit the price atomically; no race exists. Pull venues are structurally friendlier to a solo operator. |
| D8 | Risk engine gates execution via **deadman liveness token**. | A stop-signal design fails open. A required-token design fails closed. |
| D9 | Whitelist by **address + implementation codehash**. | An address whitelist is meaningless for upgradeable proxies. |
| D10 | Ingestion has a **replay driver** feeding the identical downstream pipeline. | Guarantees backtest/live parity; a separate backtest implementation silently diverges. |
| D11 | Detect on soft state, **account on confirmed state**, never realise unfinalised profit. | Reorg safety. |
| D12 | "Aave-compatible" forks are **never trusted by inheritance**; each is individually verified before whitelisting. | Forks diverge in liquidation math, oracle wiring, and hooks. Low competition lives here, so we want them — but verified. |
| D13 | Stack: Python for research/Phase 0/ML/backtest. Hot path moves to Rust/Go **only if** latency data justifies it. | Phase 0 needs no latency. Premature rewrite is wasted work. |
| D14 | Phase 0 store is SQLite (zero infra). Production store is Postgres + Timescale, same logical schema. | Don't force infra before the study that decides whether there's a business. |
| D15 | Strategy parameters are **content-hashed and immutable per version**; there is no UPDATE path to a live parameter set. | "No model mutates live trading parameters" enforced by the schema, not by convention. A change is a new version, which must clear its own gates. |
| D16 | Health factors are **derived from stored inputs**, not snapshotted per position per block. | 250ms blocks make a per-block HF snapshot billions of rows a year of data already implied by scaled balances, indices and prices. |
| D17 | Simulation returns **three numbers, never one**: deterministic net, conservative net (inputs at adverse bounds), and EV (`p(win)·net − (1−p)·failure_cost`). | Subtracting a probability from a deterministic net gives a figure that is neither case and cannot be checked against a realised outcome. |
| D18 | Conservatism is expressed as **shifts on named inputs**, never a haircut on the output. A conservative case cannot be built from oracle parity, so no measured exit quote means no execution. | Each bound stays falsifiable against realised slippage and gas. Oracle parity contains no information about what happens when you actually sell. |
| D19 | `p(win)` decomposes **conditionally**: `P(first) × P(succeeds \| first)`, never as two independent factors. | A liquidation that loses the race reverts *because* it lost. Multiplying separate "succeeds" and "first" terms counts the race twice. |
| D20 | The win model returns an **interval with its sample count**, and calibration is a promotion gate. | `p(win)` multiplies into EV, so overconfidence prices every marginal trade wrong in the same direction. Insufficient data fails the gate closed. |
| D21 | Transaction simulation is **unconditional**: no simulation, no signature. A provider that cannot simulate is not eligible to authorise one. | "Whenever technically possible" would be taken exactly when things are degraded, which is when it matters most. |
| D22 | Simulated transfers and approvals are checked against an **allowlist of legal legs**, not an anomaly heuristic, and approvals must be exact and revoked. | Fee-on-transfer and rebasing tokens live in the long tail where competition is thin. A heuristic is always one token behind; an allowlist is not. |
| D23 | The liveness token is **time-bounded, bound to the intent hash, and single-use** — all three. It is consumed last, closest to the signature. | Any two of the three make it a permission slip rather than a liveness proof. Checking it first and then verifying a simulation spends the freshness it exists to provide. |
| D24 | The permitted signer backend is a **function of deployment stage, enforced at startup and at promotion**. Shadow must have no signer at all. | An env var is readable by the process, its children, and anything that can read `/proc`. Listing it beside a secret manager implies an equivalence that does not exist. |
| D25 | Nonces are **leased, not read**, and a broadcast lease is never released. | Two opportunities in one block both read N and one is discarded by the node. Releasing a broadcast nonce turns one trade into two submissions. |
| D26 | Risk budgets count **losses immediately and profits only at finality**. | Gas is burnt whatever the chain decides later; unfinalised profit is not profit. Symmetric accounting lets a reorg retroactively invalidate headroom that already authorised trades. |
| D27 | Breakers split into **windowed** (expire with their window) and **latching** (require a named acknowledgement). | A budget breach is a fact about a window. A codehash change or projection drift is the world not matching the model, and waiting does not fix that. |
| D28 | **No prohibited item — trade decisions, amounts, risk limits, key operations, contract/token addresses, slippage limits — is ever reachable from a model.** Not by policy: nothing in the call graph accepts that kind of input. | A rule enforced by discipline is a rule someone eventually skips under pressure. Structural unreachability cannot be skipped, only removed. |
| D29 | The emergency withdrawal path takes **no `RiskEngine` and no `LivenessToken`.** It still enforces signer custody and destination whitelisting, both un-relaxed. | A procedure that needs a fresh token from the risk engine to route around a hung risk engine is not an emergency procedure. |
| D30 | A single JSON-RPC provider does **no retry** (stated in its own module docstring); retry policy lives in the pool, and a rate limit is retried on the *same* provider rather than failed over. | Switching providers on a 429 does not address the cause and wastes the attempt on one that has earned no more trust. A range error and a rate limit demand opposite responses and must stay distinguishable. |
| D31 | The canary scale-up decision takes **no parameter a backtest result could occupy**. Demotion needs no sample-size floor; scaling up does. | "Never scale on the backtest alone" is unbreakable only if the function's signature makes it impossible, the same pattern as the liveness token's `issue_token`. Stopping should never wait for a large sample; scaling up should. |
| D32 | An opportunity is scored by **EV per unit of capital at risk**, never by EV multiplied by execution probability a second time. | EV already contains `p(win)` and the failure-cost term (D2, D19). A third specification independently proposed multiplying by probability again — evidence the mistake is natural, not a one-off. |
| D33 | Membership runs **behind the tip on confirmed blocks**, with a block-hash anchor re-checked per poll rather than a per-block `BlockTracker`. A reorg triggers a **re-scan**, never an unwind of our own touch set. | D11 puts membership nearer accounting than detection: a borrower existing only in an orphaned block is not a borrower. Tracking every block costs ~43k requests/day at 2s blocks, for a loop already running behind the tip. Touching is idempotent, so over-touching costs one redundant read while under-touching leaves an account stale until it next transacts — the asymmetry is total. |
| D34 | Account reads are selected by **`getUserConfiguration`** and pinned to **one explicit block number**, never `"latest"`. | The bitmap turns ~30 `scaledBalanceOf` calls per account into ~5, which is the difference between a free RPC tier coping and not. Pinning the block is also what makes a plain JSON-RPC batch a consistent snapshot, so no on-chain Multicall3 aggregator is needed for atomicity. |
| D35 | Which parameters Aave indexes is **verified against the chain**, never inferred from a signature string. | `Supply` and `Borrow` put the *caller* in the data field and the position owner in indexed `onBehalfOf`; `Repay`/`Withdraw` put a third party at topic[3]. Decoding the data field silently misses every position opened through a router or smart-contract wallet, producing a borrower set that looks plausible and is systematically incomplete. |
| D36 | Valuation uses the **`AaveOracle` price, never a market price**. An off-chain reference is for *predicting the oracle's next write* (D7), which is a different job. | Aave values collateral at whatever its own oracle says, so that is the only price at which a health factor is true. A market mid is a better estimate of worth and the wrong number for deciding whether an account can be liquidated. |
| D37 | **Unknown price freshness is not staleness.** `updated_at` is optional and a caller that needs a guarantee must demand one. | 11 of Base's 15 reserve oracle sources are capped or composite adapters that revert on `latestRoundData`, so freshness is simply unobservable for them by that route. Treating unknown as stale drops wstETH, cbETH and most of the long tail out of valuation entirely — silently removing a whole class of collateral, which is where low competition lives (and the reason `RejectReason.STALE_PRICE` can currently only fire for the four assets behind plain aggregators). |
| D38 | **Morpho Blue is the primary win-rate venue; Aave is kept only for SVR auctions that stay profitable after the bid.** Morpho's isolated markets carry their own oracles with no Chainlink SVR / OEV auction, so liquidation value is won by speed and coverage. The Morpho adapter reuses the profit simulator unchanged by expressing the market's liquidation incentive factor (LIF) as an Aave-style `liquidation_bonus_bps` with a zero protocol fee. | On Base, Chainlink SVR auctions the oracle-update liquidation value back to Aave, compressing a solo winner's margin toward ~1%; Morpho does not, so the same operator keeps more. Morpho's seizure (`repaid × price × LIF`, capped by collateral) is structurally identical to Aave's, so a second venue is an adapter, not a second engine. Full SVR-aware bidding on Aave is an inclusion-strategy concern (D6) and belongs to Gate B; in shadow it is measurement only. |
| D39 | **Conservatism about the exit is a collateral-vs-debt haircut on the swap proceeds (`exit_haircut_bps`), never independent collateral/debt price shifts.** | In an atomic liquidation seize and swap happen at one price, so a uniform price-level shift largely cancels in the net — and because the bonus is a percentage of repaid debt, bumping the debt price up actually *inflates* it, which made the "conservative" net beat the expected net on cross-asset pairs (every Morpho pair). The risk that is real is the collateral fetching less against the debt asset by the time the swap lands; expressed on the proceeds it always reduces net and stays falsifiable against realised slippage (consistent with D18). |

## Known correctness traps (encoded in code, not just prose)

- **Health factors change with no events.** Interest accrual and oracle moves silently make
  accounts liquidatable. Position book = event stream (who has debt) + forward index projection
  (what their HF is *now*) + periodic multicall reconciliation (drift correction, with a
  divergence alarm wired to the kill switch).
- **eMode changes the numbers.** eMode accounts use category LT/LTV/bonus, not reserve defaults.
- **Close factor is not always 50%.** HF ≤ 0.95 ⇒ 100%. Small debt ⇒ 100%. Dust-leftover rules
  force full liquidation. See `protocols/aave_v3/math.py`.
- **The headline bonus is not your bonus.** `liquidationProtocolFee` skims a share of it.
- **L2 sequencer downtime blocks liquidations** for a grace period. Simulating without this
  produces bulk phantom profit precisely during the volatile windows we care about.
- **Long-tail collateral is where low competition lives — and where exit slippage eats the
  bonus.** These are correlated. Simulation must price the whole atomic route including the
  collateral swap, and the risk engine may elect to *hold* collateral rather than dump it.
- **Backtests overstate edge** because the historical liquidation happened only because someone
  else won it. Backtests must model "would we have won, at what bid", not "was it profitable".
- **WebSocket subscriptions die silently.** Per-subscription heartbeat, block-sequence gap
  detection, and backfill before resuming live processing.

## Module map

| Path | Responsibility |
|------|----------------|
| `core/` | domain primitives, finality tiers, monotonic latency tracing, structured logging |
| `config/` | typed configuration; chain / venue / protocol / oracle / whitelist models |
| `ingest/` | RPC provider pool (health, latency, retry, rate limit, failover, quorum, stale detection), canonical chain + reorg tracking, pending-state source interface |
| `protocols/` | `ProtocolAdapter` interface; Aave v3 implementation and liquidation math. **`protocols/morpho_blue/`**: Morpho Blue adapter — isolated-market state/math (LIF, share conversion, local health), events keyed by `(market_id, borrower)`, the codehash-pinned singleton reader, per-market 1e36 oracle, position reader, a membership follower + lean book, and `projection.py` (crossing-price prediction, the shadow half of A2). Reuses Aave's `calculate_seizure` and the whole simulator (D38) |
| `store/` | event-sourced observability records + writers (SQLite now, Postgres + Timescale DDL in `schema_postgres.sql`) |
| `positions/` | position book, forward projection, crossing prices, price-trigger index, reconciliation drift |
| `simulate/` | full-path profit simulation: expected and conservative net, EV, and the refusal reasons |
| `winmodel/` | `p(win)` estimation with intervals and backoff, monotonic latency tracing, calibration gate |
| `txsim/` | pre-signature transaction simulation: decoded transfers and approvals, checked against the prediction |
| `risk/` | circuit breakers, stage limits, and the sole issuer of liveness tokens |
| `execution/` | intents, liveness tokens, nonce leasing, the pre-sign gate, replacement policy, confirmation tiers, emergency withdrawal. No key material enters this package |
| `metrics/` | pure functions over resolved opportunities: P&L, performance, execution rate, predicted-vs-realised divergence, capital |
| `risk/canary.py` | the scale-up ladder: `RiskLimits` per rung, `evaluate_rung` gated on realised metrics only |
| `simulate/flash.py` | flash-source selection: cheapest source with capacity, premium always read from a live quote |
| `opportunity.py` | the generic `OpportunityDetector` protocol every future strategy type implements, and D32's `score()` |
| `phase0/` | historical liquidation collection and the venue scorecard |
| `positions/follow.py` | **W2**: the event loop maintaining book membership. Resolves each log's position owner and marks it dirty; never derives a balance from an event amount |
| `positions/reader.py` | **W1**: the batched `ScaledBalanceReader`. Scaled balances and the protocol's own health factor, every call pinned to one block |
| `protocols/aave_v3/prices.py` | **W3**: oracle prices with their freshness, and the L2 sequencer uptime gate. `usable_prices` is what keeps a stale price from valuing a position |
| `simulate/quotes.py` | **W4**: the measured exit quote (D18). Asks the venue's Uniswap v3 `QuoterV2` for a fill at size and at twice it, and returns the shortfall from oracle parity net of fee, floored at zero. `verify()` checks the quoter has code, because a codeless address reads as "no route" for every pair |
| `protocols/aave_v3/reserves.py` | The live-read slice of W6: current `ReserveState` for every reserve and v3.2 eMode categories from bitmaps, cached for the config TTL with indices projected per block. The history half of W6 (replay) stays deferred |
| `shadow/` | **Shadow mode.** `driver.py` runs the round: membership, labelling, reads, decisions. `plan.py` sizes every legal collateral/debt pair. `readiness.py` says when Gate A5 has enough labels. **`morpho_driver.py`** runs the same round over Morpho's isolated markets — no close factor (repay the whole borrow), no eMode, no sequencer-grace gate, health computed locally — and ranks the watchlist by proximity to each position's crossing price, flagging any that crossed between reads. `liq shadow run` and `liq shadow status` dispatch on the venue's protocol |

Not yet built: `eth_call` state overrides for pre-signature simulation (W5, a Gate B tool, since
shadow signs nothing); the reserve config *history* needed for correct replay (W6's other half);
inclusion strategies, execution, P&L reconciliation, and ML.

W4 is single-hop only. `core.abi.encode_call` refuses dynamic arguments by design, and a
multi-hop `quoteExactInput` needs a packed `bytes` path. On Base that bites: cbETH and wstETH
have their depth against WETH, so a direct hop to a stable debt asset fills at about 1% of
parity. The quote is still measured and returned; the simulator's net rejects it. Extending
`encode_call` to dynamic types is the next W4 increment if long-tail collateral matters, and
Phase 0 says that is where the uncontested liquidations live.

W3's prediction half is deliberately deferred: the off-chain reference price that makes
oracle-update prediction possible (D7) needs an external market-data dependency with its own
failure modes, and ranking accounts by distance to a *predicted* price is worth nothing until
they can be valued at the current one. Pull venues (Pyth, Redstone) need no prediction at all —
we would submit the price ourselves — so that path is per-venue work gated on a pull venue
being chosen.

The position book is now fed live by W1, W2 and W3, so the system observes membership, reads
health factors, prices the assets behind them, and knows whether the sequencer gate is open.
W4 prices the exit leg, so the simulator has every input it refuses to run without, and the
shadow driver now runs it live on every newly underwater account and records the decision. It cannot
yet project health locally (needs reserve indices and thresholds, W6) or predict a crossing. See `ROADMAP.md` for the specified steps, the corrections
applied to each, and the gates between here and a first return.
