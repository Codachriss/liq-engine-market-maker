# Roadmap

The build is driven by a numbered spec supplied incrementally by the operator (~12 steps total).
Steps 1–3 are supplied and built; step 4 is supplied and deliberately deferred. This file records
them, records the corrections applied, and maps the remaining steps to what has to exist before
real capital is at risk.

`ARCHITECTURE.md` holds the *design* (decisions log, correctness traps, module map).
This file holds the *plan* (what is specified, what is built, what is next).

---

## Supplied specification

### Step 1 — Core objective (built)

Autonomous on-chain opportunity system, personal capital only. Pipeline:

```
blockchain data → real-time state → opportunity detection → wallet/behaviour intelligence
→ profit simulation → risk engine → transaction simulation → execution → P&L recording
→ performance analysis → pattern discovery → backtesting → strategy optimisation
→ controlled deployment
```

Learns from its own realised P&L. No model may mutate live trading parameters; every strategy
change passes automated validation gates. Nothing is assumed profitable from quoted prices —
gas, slippage, liquidity, execution probability, failure, protocol parameters, competition, and
worst-case execution are all priced.

Operator additions folded in at this step:

- **Pending-state awareness** — pending oracle updates, borrower txs, competitor liquidations,
  projected HF crossings. Confirmed state + pending state → prediction.
- **Execution/bidding as a separate subsystem** — detect → simulate → EV → construct → choose
  bid → submit → monitor inclusion → verify P&L. Bid is optimised, not "highest gas wins".
- **Kill switches, mandatory** — N consecutive failures, hourly/daily loss limit, RPC
  disagreement, abnormal oracle move, sim-vs-actual P&L divergence, contract change, abnormal
  gas. Risk engine can block execution regardless of what any model says.
- **Key/nonce/wallet as its own infrastructure layer** — secure signer, concurrent nonce
  manager, replacement txs, gas wallet, balance monitoring, profit sweep to cold storage.
  Execution wallet isolated from main holdings. No model ever touches keys.
- **Observability, not optional** — log every detection *and every rejection with its reason*,
  predicted vs realised profit, predicted vs actual gas and slippage. The rejections are the
  training set for the later ML system.
- **Chain choice by data** — investigate Arbitrum, Base, Morpho/isolated markets ahead of
  mainnet. Target lower competition, not maximum volume.
- **Wallet intelligence A → B → C** — borrower, then competitor, then general flow.
- **Capital model** — flash liquidity first, owned inventory later, compared on realised P&L.
- **Nodes** — professional RPC first; self-host only if latency data proves lost EV.

Corrections applied: venue ranking is by expected net capture, not
`profit ÷ competition ÷ latency` (competition and latency belong inside `P(win)`); the kill
switch is a **deadman liveness token** (a stop-signal design fails open, a required-token design
fails closed). See D2 and D8 in `ARCHITECTURE.md`.

### Step 2 — Initial strategy and protocol registry (built)

Aave-compatible liquidation mechanics first. EVM chains via adapters. Whitelisted protocols and
verified contract addresses only; never interact with arbitrary contracts. Protocol parameters
are queried or configured, never hard-coded. Registry stores chain ID, protocol, version, pool
address, oracle addresses, collateral/debt assets, liquidation thresholds, liquidation bonuses,
close factors, decimals, interest rates, reserve configuration, paused/frozen status, and
implementation/version information.

Corrections applied:

- "Ethereum first" contradicts the low-competition thesis. Reconciled as: **Aave v3 as the first
  protocol with one generic adapter** (near-identical across mainnet/Arbitrum/Base, so chain is
  configuration), **first live venue chosen by Phase 0 data**. Mainnet stays registered but
  dormant. (D1)
- Registry fields the original list was missing, each of which flips profitability or legality:
  eMode categories (different LT/bonus), close-factor nuance (HF ≤ 0.95 ⇒ 100%, a 2× size
  difference on the deepest opportunities), `liquidationProtocolFee` (your bonus is not the
  headline bonus), isolation mode / debt ceilings / siloed borrowing / caps, aToken and
  variableDebtToken addresses per reserve, flash loan premium, per-asset oracle metadata
  (aggregator, decimals, heartbeat, deviation threshold, **push vs pull type**), and the
  **L2 sequencer uptime feed + grace period** — Aave v3 on L2s blocks liquidations after
  sequencer downtime, and ignoring it produces bulk phantom profit in exactly the volatile
  windows that matter.
- "Never hard-code queryable parameters" needs a refresh and trust policy, not just storage:
  event-driven invalidation on config-change and proxy `Upgraded` events, TTL re-query as
  backstop, and pre-sign revalidation against live state. Whitelisting is by
  **address + implementation codehash**, so a proxy upgrade halts that protocol until manually
  re-verified. (D9)

### Step 3 — Real-time ingestion (built)

WebSocket subscriptions, RPC fallback, block/transaction/event/protocol/oracle/DEX-liquidity/
wallet listeners. Multiple providers, never a single dependency. Health checks, latency
monitoring, automatic failover, retry policy, rate-limit handling, stale-data detection. Block
number and timestamp recorded on every important observation.

Corrections applied:

- **Reorg handling was absent and is the biggest gap.** Canonical chain tracker via parent-hash
  linkage; finality tiers (`soft` → `confirmed` → `safe` → `finalized`, plus L1 settlement on
  L2s) on every observation; append-only reorg-aware writes that mark orphaned rather than
  mutate. Detect on soft state, account on confirmed state, never realise unfinalised
  profit. (D11)
- **Event listeners alone cannot track health factors** — this is the correctness bug most
  liquidation bots ship. Interest accrual and oracle moves degrade HF with no event on the
  position. Required model: event stream for the position set + forward index projection for
  current HF and time-to-HF=1 + periodic multicall reconciliation with a divergence alarm wired
  to the kill switch.
- **The oracle listener is a predictor, not an observer.** Reacting to a confirmed
  `AnswerUpdated` is a race you have already lost. Chainlink push ⇒ track off-chain reference
  prices, predict the deviation/heartbeat fire, pre-simulate the set it unlocks. Pyth pull ⇒
  subscribe to Hermes, submit price and liquidation atomically; no race exists. (D7)
- **Multi-provider needs quorum semantics, not just failover** — N providers agreeing on block
  hash at height H before state is trusted, disagreement halts; a named authoritative provider
  for pre-sign revalidation; provider trust tiers (archive vs full, `eth_subscribe` support,
  trace/state-override simulation support).
- WebSocket subscriptions die silently: per-subscription heartbeat, block-sequence gap
  detection, backfill before resuming live processing.

### Step 4 — Data architecture (specified; deliberately deferred)

Supplied: Postgres for core relational state, TimescaleDB or ClickHouse for high-volume event
data, Redis for low-latency temporary state and queues, object storage for large datasets and
model artifacts. Schemas for `blocks`, `positions`, `opportunities`, `executions`, `strategies`,
`wallet_profiles`.

The logical shape is right and most of it is already implemented in
`src/liqengine/store/schema.sql`. The corrections are about *when* to stand the infrastructure
up, and about six places where the field lists would produce wrong numbers.

#### 4a. Do not stand up this infrastructure yet

Four datastores is Gate B/C infrastructure. Phase 0 collected 4,184 events in six months —
SQLite holds that in a 4 MB file with zero services running, and D14 already commits to
"same logical schema, Postgres + Timescale in production". Standing up Postgres, Timescale,
Redis, and object storage before shadow mode produces volume is the same mistake D4 and D13 rule
out for RPC nodes and for a Rust rewrite: buying infrastructure ahead of demonstrated edge.

**Migration trigger, not a date:** move to Postgres when shadow mode is writing decisions
continuously and either the write rate or a concurrent reader makes SQLite's single-writer lock
a real constraint. Until then the dialect is a build artifact.

What *is* owed now: `schema.sql` claims a companion `schema_postgres.sql` that does not exist.
Either write it alongside the SQLite DDL so the two never drift, or delete the claim.

#### 4b. Timescale or ClickHouse — pick Timescale, not "where appropriate"

"Where appropriate" means two analytical systems for a solo operator to keep in sync. Timescale
is a Postgres extension: same connection, same SQL, same backups, same transactions as the
relational state. ClickHouse is faster on billion-row analytical scans, which is not the problem
here and will not be for a long time. Choose ClickHouse only if a specific query on real data is
measured to be too slow in Timescale.

#### 4c. Redis is hot-path only, and never a source of truth

Legitimate uses in the live system: nonce leases, in-flight opportunity dedupe, provider
rate-limit tokens, and the **deadman liveness token** — where Redis TTL semantics are a good
structural fit, because an expired key fails closed by default (D8). Nothing financial is ever
authoritative in Redis. If Redis is lost, the system halts and rebuilds from Postgres; it never
reasons from a partial cache.

#### 4d. `positions` as specified is the wrong shape and would not fit on disk

Two problems.

**Multi-asset.** `collateral` and `debt` are single columns, but an Aave position is *n*
collaterals and *m* debts, each with its own reserve config, eMode category, and index. It must
be `position` (one row per borrower per venue, current state) plus `position_reserve` (one row
per asset held or owed). A single-asset schema silently misprices every multi-collateral
account — which is most of the ones that matter.

**Health factor is derived, not stored per block.** Arbitrum produces a block every 250 ms. One
HF snapshot per position per block across thousands of borrowers is billions of rows a year, all
of it recomputable. Store the *inputs* — the liquidity and variable-borrow indices, the oracle
prices, and their block anchors — and recompute HF on demand. Persist a computed HF only when it
was an input to a recorded decision, where reproducing the exact number later matters.

#### 4e. Append-only and reorg discipline are missing from every table

The supplied schemas imply rows updated in place as the world changes. That breaks D11 and the
whole training signal. Every observation carries `(block_number, block_hash)` and an `orphaned`
flag; a reorg marks rows orphaned, it never edits or deletes them. What the system believed at
the time is itself data, and the divergence between belief and outcome is the thing the ML layer
is being built to learn from.

This is why `opportunities` and `executions` are correctly two tables rather than one row with
`expected_*` overwritten by `actual_*` — prediction-versus-realised has to be a **join**, not an
overwrite, because sim-vs-actual divergence is a circuit-breaker input and must never be
destroyed by an update. Already implemented as `decision` + `realised_pnl`.

#### 4f. Money is never a float, and rarely a bigint

`uint256` does not fit in `BIGINT`. Amounts are stored as exact decimal text (SQLite) or
`NUMERIC` (Postgres), with token decimals recorded separately. A float anywhere in a value column
is a rounding bug that will surface as unexplained P&L drift.

#### 4g. Fields to add

`opportunities` — `block_hash` and finality tier; `trace_id` for latency attribution across
hops; `state_snapshot` (oracle prices, reserve params, pool liquidity, simulated route) so
replay reproduces the decision exactly rather than approximating it; close factor and eMode
category actually applied; flash-liquidity source and premium; sequencer-grace status at
detection; competitor context at detection. `execution_probability` and `expected_value` are
already the right idea — that is `P(win)` from D2, and it belongs here rather than as a divisor.

`executions` — monotonic submit timestamp alongside wall clock (wall clock is unusable for
latency); inclusion latency; `tx_index` within the block (the competition metric Phase 0 used);
nonce and the replacement chain, so a replaced tx is traceable to what replaced it; decoded
revert reason; and the **finality tier at the time of accounting**, since unfinalised profit is
never realised.

`strategies` — `parameters` must be immutable per version and content-hashed, so any change is a
new version rather than an update. That is what structurally enforces "no model mutates live
trading parameters" from step 1. Add the validation-gate record (which gates passed, on what
data) and the deployment stage (`dormant` → `phase0` → `shadow` → `canary` → `live`).

`wallet_profiles` — `historical_pnl_estimate` is the dangerous field. A competitor's true P&L is
not observable: private orderflow, off-chain revenue, subsidised gas, and inventory positions all
sit outside what the chain shows. Keep it only if it is explicitly labelled an estimate with its
derivation method and a confidence interval, and never let it feed a bidding decision directly.
Add instead what *is* observable: `is_contract` plus implementation codehash, `first_seen`, win
rate bucketed by liquidation size, median `tx_index`, and a latency signature.

#### 4h. Tables the spec omits

- `reserve_config_history` — governance changes LT, bonus, close factor, and protocol fee over
  time. A backtest that replays 2026 events against today's parameters is quietly wrong.
- `oracle_update` — the trigger events themselves, push and pull, with the off-chain reference
  price at the time. Without this the prediction model in A2 has no training data.
- `sequencer_uptime` — grace-period windows, so backtests exclude liquidations that could not
  legally have executed.
- `inclusion_attempt` — bid, strategy, competing txs seen, won or lost. Losing is the label the
  win model learns from, and it exists nowhere else.
- `block`, `halt_event`, `provider_sample` — already built.

#### Status — schema built, infrastructure deferred

Built (schema v2, `src/liqengine/store/`):

- `schema_postgres.sql` now exists and is the logical twin of `schema.sql`. The claim in the
  SQLite header is no longer false. NUMERIC(78, 0) for chain amounts, hypertables only on the
  two tables that are pure appends with no unique key and no inbound foreign key
  (`reserve_index`, `provider_sample`) — Timescale cannot partition the rest without breaking
  either upsert or referential integrity, and the DDL says so rather than pretending.
- `position` + `position_reserve` per 4d, holding scaled balances and eMode, with `reserve_index`
  alongside. No health factor is stored outside a recorded decision.
- `reserve_config_history`, `emode_category_history`, `oracle_update`, `sequencer_uptime`,
  `inclusion_attempt` per 4h, with `reserve_config_at(venue, asset, block)` and
  `liquidations_blocked_at(chain, timestamp)` as the accessors a correct backtest has to read
  through.
- `strategy_version` / `strategy_gate` / `strategy_stage_event` / `strategy_metric` per 4g.
  Parameters are content-hashed and immutable: re-registering a version with different
  parameters raises, and `promote_strategy` refuses a stage transition whose gates have not
  passed. Performance is a snapshot table, never a mutable column on the version.
- `decision` and `realised_pnl` extended per 4g. EV is derived at write time from the stored
  inputs rather than accepted from the caller, so the number always matches D2's definition.
  P&L is booked only at `REALISATION_TIER`, and a reorg unbooks it.
- Typed records in `store/records.py` with the validation that makes bad data impossible rather
  than merely discouraged: a rejection must carry a reason, a competitor P&L estimate must carry
  its derivation method, and a partial EV returns None instead of a plausible number.
- Migration v1 to v2 is additive and verified against the real 4,184-row Phase 0 database.
  Opening a store written by a newer schema raises rather than silently ignoring columns.

Still deferred: standing up Postgres, Timescale, Redis and object storage. The trigger is 4a,
not a date. Redis has no code yet because nothing in Gate A needs a hot path.

### Step 5 — Liquidation detection

Supplied: continuously identify positions approaching liquidation; do not wait only for an
explicit liquidation event. Calculate current health factor, projected health factor, distance
to liquidation, collateral price sensitivity, debt sensitivity, liquidation amount, expected
liquidation bonus, available liquidity, expected unwind price. Prioritise by expected economic
value, not position size.

The instinct is right and the last line is the important one. Seven corrections.

#### 5a. "Projected health factor" is two different projections

Time and price are not the same axis and conflating them produces a number that means nothing:

- **Time** — index accrual. Deterministic, slow, computable exactly from the last stored index
  and the current rate. Answers "when does this account cross HF=1 if prices never move".
- **Price** — an oracle write. Stochastic, instantaneous, and the actual trigger for almost
  every liquidation worth having. Answers "what price move crosses this account".

Both are needed, separately. Only the second is actionable at the speed the race runs at.

#### 5b. The useful output is the inverse function, not the health factor

Ranking accounts by health factor is reacting. The structure that lets you *predict* is the
inverse: for each asset, the price at which each account crosses HF=1, held in a sorted index.
Then a predicted or pending oracle update is a range query — "WETH is about to print 2,910;
which accounts does that unlock" — answered before the price is on chain, which is the whole
point of D7. Reacting to a confirmed `AnswerUpdated` is a race already lost.

This subsumes "collateral price sensitivity" and "debt sensitivity" into one primitive rather
than two metrics. An asset that is collateral pushes the crossing price *down*; the same asset
as debt pushes it *up*; an account holding both nets them out. Computing the two sensitivities
separately gets that netting wrong.

#### 5c. "Continuously" cannot mean "recompute everything every block"

Arbitrum produces a block every 250ms against ~3,370 borrowers. Full revaluation every block is
both wasteful and too slow, and it is unnecessary: the inputs change on a schedule you know.
A user action dirties one account. An oracle update dirties every account holding that asset —
which the inverse index in 5b already enumerates. Accrual moves everything, but deterministically
and slowly enough to re-rank on a timer rather than a block. Recompute on dirty, not on tick.

#### 5d. Hard filters come before any of this, or the whole list is fiction

An opportunity that cannot legally execute is not an opportunity, and these produce phantom
liquidations in bulk rather than one at a time:

- **Sequencer grace period** on L2s — `liquidations_blocked_at()` exists for this.
- **Paused reserve** rejects liquidation outright. Frozen does not — the distinction matters
  and is already encoded in `ReserveState.liquidatable`.
- **Stale oracle price** — a price older than its heartbeat is not a price. Detection on a
  stale price finds accounts that crossed hours ago and were liquidated hours ago.
- **Isolation mode and siloed borrowing** constrain what can be liquidated and how.

#### 5e. "Liquidation amount" and "expected liquidation bonus" are already-solved traps

Both exist in `protocols/aave_v3/math.py` and detection must call them rather than approximate:
close factor is 50% *except* at HF ≤ 0.95 where it is 100%, plus a dust rule that forces full
repayment — a 2x sizing error on the deepest opportunities. And `liquidationProtocolFee` skims
the bonus, so the headline number is not the receipt. eMode changes both.

#### 5f. "Available liquidity" is two unrelated things

The repay leg needs **flash-loan liquidity in the debt asset** — can I borrow the repayment, and
at what premium, preferring 0-fee sources per D3. The exit leg needs **DEX liquidity in the
collateral asset** — can I sell what I seize without giving the bonus back. Different venues,
different failure modes, both required. "Expected unwind price" must be the full atomic route
priced with slippage and fees, not a spot quote — and the risk engine may elect to hold the
collateral rather than dump it, so "unwind" is a decision, not an assumption.

#### 5g. "Prioritise by expected economic value" is correct and not yet possible

EV needs `P(win)`, and there is no win model until Gate A5. Until then detection must rank by
**predicted net**, record `win_probability_bps` as an explicit placeholder rather than a guess,
and store the inputs so the backtester can score the ranking retrospectively. Shipping a
confident-looking EV built on an invented win probability is how a backtest comes out positive
and the live system does not.

#### Status — projection and ranking built

Built (`src/liqengine/positions/`):

- `projection.py` — per-asset exposure decomposition, `crossing_price()` implementing 5b
  (netting collateral and debt in one solve), and `seconds_to_liquidation()` for the accrual
  axis in 5a. Health factor itself still comes from `math.value_account`, so contract parity has
  one home.
- `watchlist.py` — `PriceTriggerIndex`, the sorted inverse index. A predicted price is a range
  query returning the accounts it unlocks.
- `book.py` — membership, dirty tracking per 5c, persistence through the step-4 tables, and the
  reconciliation drift alarm.

Not built: the multicall reader that fills scaled balances from chain (interface defined,
wiring belongs with the live ingestion loop), the hard filters in 5d as a composed gate, and
everything in 5f — which is Gate A3, simulation, not detection.

### Step 6 — Profit simulator

Supplied: before any transaction is submitted, compute gross liquidation value minus
acquisition/unwind costs, DEX fees, slippage, gas, priority fees, flash liquidity costs,
transaction failure risk, price movement risk and protocol-specific costs, to reach expected net
profit. Produce both an expected and a conservative case. Do not execute if the conservative
case is unprofitable.

The last sentence is the most valuable line in the specification so far. Six corrections.

#### 6a. That list mixes costs with risks, and they do not subtract from the same number

DEX fees, slippage, gas, priority fees, the flash premium and the protocol fee are
**deterministic subtractions** — given the inputs, they have exact values. Transaction failure
and price movement are **probabilities**, and subtracting a probability-weighted number from a
deterministic net produces a figure that is neither the expected case nor the worst case, and
cannot be checked against a realised outcome later.

The correct shape is three separate numbers:

1. **Net** — deterministic, all costs subtracted, for a given set of inputs.
2. **EV** — `p(win) × net − (1 − p(win)) × failure_cost`. This is where failure risk lives,
   and it is the same definition as D2 and the step-4 `decision` row.
3. **Conservative net** — the same deterministic calculation with the *inputs* moved to their
   adverse bounds. Price movement risk lives here, as an input shift, not as a haircut.

#### 6b. "Conservative" has to be constructed, not fudged

A flat haircut on the output is arbitrary and unfalsifiable. Each adverse bound is a named input
with a named source: worst-case slippage at the actual size from the exit quote, an adverse price
move over the inclusion window, gas at an observed high percentile. Then the conservative net is
checkable against reality after the fact, and each bound can be tightened as data arrives.

**Consequence worth stating plainly:** a conservative case cannot be computed from oracle prices
alone, because oracle parity carries no slippage information. No real exit quote means no
conservative case, which by the specification's own rule means no execution. That is the correct
behaviour and the simulator refuses rather than substituting an assumption.

#### 6c. The route is priced atomically or not at all

Flash borrow → repay debt → seize collateral → swap → repay flash → net. Pricing the bonus and
subtracting gas is the standard way to overstate this: it omits the leg where the bonus is
actually given back. Long-tail collateral is where low competition lives *and* where exit
slippage eats the bonus, and those two facts are correlated.

#### 6d. Failure cost is venue-physics, not a constant

On Arbitrum a lost race still lands on chain and still burns gas — `reverts_cost_gas: true` is
already in the chain config. On a bundle venue a losing bundle costs nothing. That is the
difference between `failure_cost = gas` and `failure_cost = 0`, and at the margin it flips the
sign of EV. It cannot be a global constant.

#### 6e. Selling the collateral is a decision, not an assumption

The risk engine may elect to hold rather than dump into thin liquidity, and Phase 0 found only
2% of liquidators kept the aToken. Holding removes the slippage and the flash premium but takes
on directional exposure and requires owned inventory to fund the repayment. It must be priced as
a distinct route whose output is flagged as unhedged, never quietly folded into a cash number.

#### 6f. Preconditions belong before the arithmetic

Sequencer grace period, paused reserve, stale oracle price, insufficient flash liquidity,
insufficient exit depth. Each makes the trade impossible rather than unprofitable, and each
should short-circuit with a named reason that lands in `decision.reject_reason` — where it
becomes the negative class the model trains on.

#### Status — built

`src/liqengine/simulate/`:

- `inputs.py` — `GasQuote`, `ExitQuote`, `FlashLoanQuote`, `VenueCosts`, and `Scenario` with
  `EXPECTED` / `CONSERVATIVE` as constructed input shifts per 6b.
- `engine.py` — `simulate()` prices the full atomic route per 6c, returns a `ProfitBreakdown`
  with every line item reconciling to net, and a `SimulationResult` holding expected and
  conservative side by side with the verdict.
- Verdict refuses on any 6f precondition, on a non-positive conservative net, and on a missing
  exit quote. `win_probability_bps` carries its source, and an EV built on a placeholder is
  marked as such rather than reported as a measurement.

### Step 7 — Execution probability

Supplied: estimate `P(transaction succeeds)` and, where possible, `P(transaction executes before
competing searchers)`. Record detection, simulation, signing, submission and block-inclusion
latency. Use these features in the ML system.

This is `p(win)` — the term D2 puts competition and latency inside, the term step 6's EV depends
on, and the Gate A5 blocker. Eight corrections.

#### 7a. The two probabilities are not independent, and multiplying them double-counts

You revert *because* someone else won. A liquidation that loses the race reverts with
`HEALTH_FACTOR_NOT_BELOW_THRESHOLD` — the failure and the loss are the same event, not two
events to multiply. The correct decomposition is conditional:

```
P(profit) = P(we are first) × P(succeeds | we are first)
```

The second term is small and covers genuinely independent hazards — gas limit too low, slippage
bound breached, nonce collision, the position self-curing on a price rebound. Treating "succeeds"
and "first" as separate independent factors counts the race twice and produces a `p(win)` that is
systematically too low, which reads as conservative and is actually just wrong.

#### 7b. "Where possible" is backwards for the venue we are targeting

Arbitrum is FCFS: there is no bid, only arrival order (D6). `P(first)` is not a refinement there,
it is essentially the entire estimate. Making it conditional means the model is optional on
precisely the venue Phase 0 nominated.

#### 7c. There is no data for this yet, and the model must say so

`P(first)` cannot be estimated from chain data alone. The chain records the winner; it never
records that we bid and missed. That is what `inclusion_attempt` exists for, and it stays empty
until shadow mode runs. Until then any `p(win)` is a **prior**, not an estimate, and the model
must return an interval with its sample count rather than a point estimate that looks measured.
Gate A5 is exactly the work of filling this table.

#### 7d. An accurate model is not enough; it has to be calibrated

`p(win)` multiplies straight into EV. A model that is right on average but overconfident in the
30% band prices every marginal trade wrong in the same direction, and the losses accumulate
quietly because each individual trade looked reasonable. Calibration is measurable — reliability
bins, expected calibration error, Brier score — and passing it belongs in `strategy_gate` as a
promotion condition, not in a dashboard.

#### 7e. The conservative case needs a conservative probability

Mirroring D18: the expected case uses the point estimate, the conservative case uses the **lower
confidence bound**. With ten observations a 60% win rate and a 95% win rate are barely
distinguishable, and the interval says so. Using the point estimate in both cases throws that
away exactly when the sample is thinnest.

#### 7f. The latency list is missing the measurements that decide the race

The five stages listed are the *internal* pipeline, which tells you how fast you are but not
whether that was fast enough. Also required:

- **Oracle-to-detection** — when the price became knowable versus when we saw it. On a pull
  oracle this span is *negative*: we hold the signed price before it is on chain (D7).
- **Submission-to-sequencer-acknowledgement** — on an FCFS chain this is what ordering is
  actually decided on, and it is not the same as block inclusion.
- **The counterfactual** — the winner's transaction index against ours. Without it you know your
  own latency and have no idea what it needed to be, which makes every optimisation blind.

Two properties of the recording: timestamps must be **monotonic**, because NTP steps wall clock
and a negative span in the middle of a race is not recoverable. And latency is a **distribution,
not a number** — you lose in the tail, so the p99 is the figure that matters and a mean hides it.

#### 7g. Non-race failures are distinct hazards, not one bucket

Gas limit too low, slippage bound breached, sequencer grace, self-cure on a price rebound, nonce
collision. Each has a different fix, so each is recorded separately. Collapsing them into
"P(fail)" makes the fix unknowable.

#### 7h. Before ML, there is a better estimator

A conditional empirical win rate — by venue, size bucket and observed competition, with
hierarchical backoff to a venue-level and then a global prior when a bucket is thin — is
interpretable, calibratable, and needs no training pipeline. It is also the baseline any later
model has to beat. Reaching for ML first means having nothing to compare against.

#### Status — built

`src/liqengine/winmodel/`:

- `latency.py` — monotonic `LatencyTrace` covering the full span including oracle-to-detection
  and sequencer acknowledgement, the winner counterfactual, and `LatencyDistribution` with
  percentiles. Serialises to the `decision.latency_ms` column.
- `model.py` — `WinEstimate` (point, lower and upper bound, sample count, source),
  `PriorWinModel` for the no-data case, `EmpiricalWinModel` with Wilson intervals and
  hierarchical backoff per 7h, and `combine()` implementing the conditional decomposition in 7a.
- `calibration.py` — reliability bins, expected calibration error and Brier score, as the
  promotion gate in 7d.

Not built: the shadow-mode loop that populates `inclusion_attempt`. Until it runs, every
estimate is a prior and says so.

### Step 8 — Transaction simulation

Supplied: every transaction must be simulated before execution whenever technically possible.
Simulation must verify that the transaction succeeds, the liquidation is valid, expected
collateral is received, debt repayment succeeds, collateral unwind succeeds, gas is acceptable,
profit remains positive, no unexpected token transfer occurs, and no unexpected
approval/allowance is created. Reject if simulation fails.

This is the last gate before a signature and the checks are the right ones. Eight corrections.

#### 8a. "Whenever technically possible" is the loophole every incident lives in

Make it unconditional: **no simulation, no execution.** A provider that cannot do a state-override
`eth_call` or a trace is not eligible to authorise signing — which is what the provider trust
tiers from step 3 are for. The escape hatch as written would be taken at exactly the moment it
matters, because the moment it matters is the moment things are degraded.

#### 8b. Simulating against `latest` simulates a different world

The transaction will land *after* whatever else lands first. On an FCFS venue that plausibly
includes a competitor's liquidation of the same position. Simulation runs against pending state
with overrides, and the result is valid **only for the block it was run against** — so it carries
its block and expires. This is the pre-sign revalidation gate: the simulation that authorises a
signature must have a cache age of zero.

#### 8c. A simulation that only says "success" is nearly worthless

The value is the comparison. Simulation produces *actual* deltas, and those are checked against
what step 6 predicted, within a tolerance. Divergence between predicted and simulated is a
distinct rejection — and the same divergence measured against *realised* outcomes is already a
circuit-breaker input. A boolean throws away the signal the whole pipeline is built to produce.

#### 8d. "No unexpected transfer" needs an allowlist, not an anomaly heuristic

Enumerate the legal ones: flash loan in, debt repaid to the pool, collateral seized to us, swap
in and out, flash repaid, profit to the recipient. Anything else rejects.

The trap this catches is not a hack, it is a token class: **fee-on-transfer and rebasing tokens
transfer a different amount than you asked them to.** Long-tail collateral is where low
competition lives, and it is also where these tokens live. Detecting the mismatch and refusing is
correct; treating it as noise means occasionally repaying a flash loan short and reverting.

#### 8e. Approvals must be exact and must not survive the transaction

An infinite approval left standing on a router is a permanent liability that has nothing to do
with this trade. Check both: no approval outside the expected set, and nothing left non-zero
when the transaction ends.

#### 8f. Simulate as the address that will actually sign

`msg.sender` changes the answer — balances, allowances and nonce all differ. A simulation run
from a zero address or a well-funded probe address is a simulation of somebody else's
transaction.

#### 8g. Gas from simulation replaces the estimate, and profit is recomputed at it

"Gas is acceptable" is not a standalone threshold. The check is that the step-6 **conservative**
net is still positive when recomputed at the simulated gas. A trade that survives on estimated
gas and dies on simulated gas is a trade that should not be signed.

#### 8h. A failed simulation must decode its revert reason

"Reverted" is not learnable. `HEALTH_FACTOR_NOT_BELOW_THRESHOLD` means the position self-cured
or a competitor was first; `SPECIFIED_CURRENCY_NOT_BORROWED_BY_USER` means the position book is
wrong. Different failures, different fixes, and the decoded string is what makes
`decision.reject_reason` worth training on.

One tension worth stating rather than resolving silently: on an FCFS venue every millisecond of
simulation is a millisecond later into the race. The answer is to overlap simulation with signing
and to pre-warm the provider, **not** to make the check optional — 8a exists because the latency
argument is exactly how the check gets dropped.

#### Status — built

`src/liqengine/txsim/`:

- `types.py` — `SimulatedTransaction` (decoded transfers, approvals, gas, revert reason, and the
  block it ran against), `ExpectedOutcome` (the allowlist and the step-6 predictions), and the
  `TransactionSimulator` protocol for the provider wiring.
- `verify.py` — `verify()` returning every failure rather than the first, with codes specific
  enough to be learnable.

Not built: the provider-side `eth_call` with state overrides and the trace decoder. That is
ingestion wiring against a live endpoint, and it is on the critical path — see "What is actually
between here and shadow mode" below.

### Step 9 — Execution infrastructure

Supplied: an execution abstraction layer supporting standard RPC submission, private submission
where appropriate, Flashbots-compatible Ethereum execution where appropriate, configurable
transaction replacement, nonce management, gas management and confirmation monitoring. Private
keys never reach an LLM; they live in a secure signer, environment variable or secret manager.
The execution layer only accepts structured, validated intents from the risk engine.

Nine corrections. Two of them are load-bearing safety properties the step omits.

#### 9a. "Where appropriate" is a runtime choice that must be a configured one

Same shape as 8a. Submission mode is **venue physics, decided in configuration** (D6), not
picked at execution time:

- **Arbitrum** — FCFS. There is no public mempool to protect from and no bid to place, so
  private submission and bundles are not options that happen to be unavailable, they are
  category errors. A losing attempt still lands and burns gas.
- **Base / OP** — priority-fee auction.
- **Mainnet** — bundle auction.

An executor that chooses at runtime chooses wrong under pressure, which is when it chooses.

#### 9b. The deadman token is missing, and "only accept intents from the risk engine" is not it

D8 requires the execution layer to be unable to sign without a **fresh liveness token**. An
intent is a message: it proves the risk engine spoke, not that it is *still alive and still
approving*. A risk engine that hangs after emitting an intent emits nothing further — and a
design that waits for a stop signal never receives one.

The token must be time-bounded (a hung risk engine's last token expires on its own), **bound to
the intent hash** (so it cannot authorise a different transaction), and **single-use** (so it
cannot authorise the same one twice). All three, or it is a permission slip rather than a
liveness proof.

#### 9c. Key custody: the rule is a process boundary, not an audience

"Never give keys to an LLM" is true and far too narrow. The invariant is that **nothing except
the signer ever holds key material** — not the strategy layer, not the researcher, not a log
line, not an exception traceback, not a crash dump. The model is one of many things on that
list and not the most likely to leak.

And the three storage options given are not equivalent. An environment variable is readable by
any code in the process, by anything that can read `/proc`, and by every subprocess that
inherits it. It is fine for `shadow`, where there is nothing to steal, and it is not acceptable
for `canary` or `live`. The permitted backend is therefore a function of deployment stage and
enforced rather than recommended.

#### 9d. Nonce management is where concurrency turns into lost money

Two opportunities detected in the same block must not both take nonce N. That needs **leases,
not reads**: a single owner hands out a nonce and records it as in flight until it confirms.

It also needs the failure modes handled explicitly, because each one is different:

- A nonce leased but never broadcast is **released** and reused. Skipping it stalls everything
  behind it.
- A nonce broadcast and stuck **blocks every higher nonce**, however good those opportunities
  are. Detecting the head-of-line block is what turns a mysterious outage into a known one.
- Retrying a submission must be **idempotent by nonce**, or a network timeout becomes two
  liquidations.

#### 9e. Replacement is a different action on a latency-race venue

Bumping the fee is meaningless where there is no fee auction. On Arbitrum, replacing a stuck
transaction is starting a **new race** with a new cost, not improving the position of the
existing one. The policy is therefore per-strategy: on an auction venue, replace when the
improved bid still clears EV; on FCFS, replace only when the original is genuinely stuck, and
price the replacement as a fresh attempt including its own failure cost.

#### 9f. "Gas management" is two separate things owned by two components

The **gas limit** comes from simulation (step 8). The **fee** is a bid, owned by the inclusion
strategy, and on an auction venue it is the thing being optimised. Folding them into one
"gas management" concern hides the bid inside plumbing.

#### 9g. Confirmation monitoring is finality-tiered, not binary

One confirmation is not done. D11 already ties this to the store: `booked_at` is set only at
`REALISATION_TIER`, and on an L2 the only genuinely irreversible tier is L1 settlement. The
monitor advances a tier at a time and the accounting follows it.

#### 9h. Pre-sign revalidation is the last thing before the signature, in a specific order

Substantive checks first, liveness token last. The token is a freshness proof, so it should be
consumed as close to the signature as possible; checking it first and then spending 200ms
verifying a simulation wastes exactly the freshness it exists to provide.

#### 9i. There is no kill path in the spec

A circuit breaker firing must stop the executor *and* deal with what is already in flight. That
means in-flight transactions are enumerable, and cancellation is a no-op self-transfer at the
same nonce — because on EVM there is no cancel, only replace.

#### Status — built

`src/liqengine/execution/`:

- `intent.py` — `ExecutionIntent`, content-hashed and expiring, and `LivenessToken` with the
  three properties in 9b. `TokenRegistry` burns tokens on use.
- `nonce.py` — `NonceManager` with leases, release, broadcast tracking, head-of-line block
  detection and chain reconciliation per 9d.
- `gate.py` — `authorise()`, composing simulation verification, intent freshness, executor
  identity, backend policy and the liveness token, in the order 9h requires.
- `signer.py` — the `Signer` protocol and `SignerBackend` with the stage policy from 9c. No
  function in this package accepts or returns key material, and a test asserts it.
- `replacement.py` — per-strategy replacement policy per 9e.
- `confirm.py` — the finality-tier state machine for 9g.

Not built: the transport itself (RPC submission, bundle relay clients) and a concrete signer.
Both are IO against endpoints and neither can be written honestly without them.

### Risk engine (Gate B1) — built ahead of a numbered step

Not in the supplied specification as a step of its own, but required by D8 and by step 9's
liveness token, which is inert without an issuer. `src/liqengine/risk/`.

The mechanism is the one D8 names: **the engine does not tell the executor to halt, it stops
issuing tokens.** A crashed, hung, or input-starved engine produces the same outcome as one that
has decided to stop, which is the only property that makes it worth relying on. There is no
`force` argument and no path that accepts a caller-supplied verdict — `issue_token` runs the
evaluation itself, because a caller that could pass a verdict could pass a favourable one.

Three design points worth recording:

- **Losses count immediately, profits only at finality.** Gas is burnt whatever the chain
  decides afterwards; profit is not profit until irreversible (D11). Counting them symmetrically
  lets a reorg retroactively invalidate the headroom that authorised later trades.
- **Windowed breakers expire, latching ones do not.** A loss budget is a fact about a window. A
  codehash change, an oracle divergence or a position-book drift is a fact about the world not
  matching the model, and waiting does not fix it — those need a named acknowledgement, because
  an anonymous clear is indistinguishable from an automatic one.
- **Zero means no trading, not unlimited.** A misconfigured cap that reads as infinity is the
  most expensive possible typo, so shadow's caps are explicitly zero rather than absent.

### Step 10 — Wallet intelligence

Supplied: continuously profile wallets, classify by observable behaviour into liquidator /
market maker / arbitrageur / whale / fund / retail / bot / protocol / exchange / unknown, extract
behavioural features, do not blindly copy, and establish whether wallet behaviour is predictive.

The last two lines are the correct framing and rarer than they look. Five corrections.

#### 10a. Most of that taxonomy is not reachable from this data and is not needed

The category list is a general wallet-classification schema. What this system needs is D5's A
and B: **borrowers** (who is about to be liquidatable) and **competitors** (who wins, how fast,
at what size). "Market maker", "fund/treasury", "exchange" and "retail" are neither observable
from Aave events nor load-bearing for a liquidation decision. Classifying into categories the
engine will never branch on is work that produces a dashboard.

Keep: `liquidator`, `borrower`, `both`, `unknown` — which is what `wallet_profile.wallet_type`
already stores — plus `is_contract` and the implementation codehash, which *are* observable and
do change how a competitor behaves.

#### 10b. Competitor P&L is not observable, and the schema already refuses to pretend

Covered in 4g and enforced in `WalletProfileRecord`: private orderflow, off-chain revenue,
subsidised gas and inventory all sit outside what the chain shows. An estimate is kept only with
its derivation method and a confidence, and never feeds a bid directly.

#### 10c. "Reaction latency" is not measurable for other people

We can measure our own latency precisely (step 7). For a competitor, the chain gives block
position and inclusion, not when they saw the price. The observable proxy is **transaction index
within the block**, which Phase 0 already used, plus the distribution of that index — and it is
a proxy, not a latency.

#### 10d. Every metric must be computed on the economically meaningful subset

This is the single highest-value pending analysis, already flagged in `PHASE0-FINDINGS.md`. The
208 distinct winners, the HHI of 730, the "58% land beyond index 3" and the 30% borrower repeat
rate are all computed across a dataset that is 71% sub-$100 dust nobody contests. Those figures
may be artifacts of the dust rather than facts about the market. **Recomputing them above $100
needs no new data and changes what the competitive picture means.**

#### 10e. Clustering needs a stated purpose or it is unfalsifiable

"Wallet clustering" as a feature produces groups. What decision changes based on which group a
competitor is in? If the answer is `p(win)`'s competition bucket (step 7), then the clustering
target is defined and testable: does conditioning on the cluster improve calibration? If there
is no such answer, the clusters are decoration.

#### Status — reachable now, partly

`wallet_profile` and its writer exist (step 4). The competitor and borrower analysis in 10d can
run against the real 4,184-row Phase 0 dataset **today**, with no new wiring — it is the next
piece of work I would do. Everything requiring our own outcomes (win rate by competitor, latency
signature, whether behaviour is predictive) is blocked until shadow mode fills
`inclusion_attempt`.

### Step 11 — Pattern discovery engine

Supplied: automatically discover patterns from historical data and realised P&L across a long
feature list, statistical methods and ML where appropriate, interpretable models before complex
ones, and no LLM as the primary numerical predictor.

"Interpretable before complex" and "no LLM as the numerical model" are both right and I would not
change them. Four corrections.

#### 11a. This is blocked on realised P&L, and realised P&L does not exist

The step says "from historical data **and realised P&L**". There is no realised P&L: nothing has
executed. Phase 0 contains other people's liquidations, and treating their outcomes as our
returns is precisely the overstatement `PHASE0-FINDINGS.md` was written to prevent. Roughly half
the listed features — execution latency, competing transactions, DEX route, and profitability
itself — are unmeasurable until shadow mode runs.

#### 11b. The baseline already exists and should not be rebuilt as ML

`EmpiricalWinModel` (step 7) is a conditional rate estimator with intervals and backoff. It is
the interpretable baseline, and any gradient-boosted model has to beat it on **calibration**, not
on accuracy — `p(win)` multiplies into EV, so a model that classifies well and is overconfident
in the marginal band is worse than the simple one it replaced (D20).

#### 11c. Feature discovery on a few hundred observations finds noise

Eighteen candidate features against the volume Phase 0 measured — roughly six meaningful
liquidations a day on Arbitrum — is a few hundred rows in the first months. That is a regime
where feature-importance rankings are unstable and "discovered patterns" are mostly the
particular sample. The honest sequencing is: fix the feature set in advance, report effect sizes
with intervals, and require out-of-sample confirmation before anything changes a parameter.

#### 11d. Nothing discovered here may change a live parameter automatically

Already structural: `strategy_version` parameters are content-hashed and immutable, and a change
is a new version that must clear its gates (D15). Pattern discovery proposes a new version; it
cannot edit the running one. Worth restating because "automatically discover patterns" and "do
not require manual inspection" read as though discovery flows straight to production, and the
schema is built so it cannot.

#### Status — deferred, correctly

The measurement scaffolding exists (`decision` with its full state snapshot, `inclusion_attempt`,
`realised_pnl`, the calibration tracker). What is missing is data, and the fix for that is shadow
mode, not more modelling code. Building the discovery engine now would mean fitting it to Phase 0
and inheriting exactly the bias the study was written to warn about.

### Step 12 — Automatic pattern discovery

Supplied: generate statements automatically ("opportunities on X with gross profit > Y and
liquidity > Z have a 78% historical execution rate", "gas above X gwei makes this strategy's EV
negative", "collateral Z has positive theoretical and negative realised P&L because of unwind
slippage"), and store every discovered pattern.

The example statements are the right *shape* — each is falsifiable and each names a threshold.
Four corrections.

#### 12a. Same blocker as step 11: four of the five examples need realised P&L

"78% historical execution rate", "less profitable over the last N blocks", "negative realised
P&L because of unwind slippage" — all require outcomes we have not produced. The exception is the
gas one, which is mechanical rather than statistical: `simulate()` already recomputes the
conservative net at any gas price, so "above X gwei this is negative" is a solve, not a
discovery. It needs no data and no model.

#### 12b. A stored pattern needs the numbers that make it falsifiable, or it is a sentence

"Protocol Y has become less profitable" is unfalsifiable as written. A stored pattern is a typed
record carrying sample size, effect size, a confidence interval, the exact window it was measured
over, and whether it has survived out-of-sample. Without those it cannot be re-tested, cannot
expire, and cannot be told apart from the one that was true last month.

#### 12c. Eighteen features against a few hundred rows will produce patterns by chance

"Wallet cluster A frequently precedes profitable conditions" is exactly the shape of finding that
appears when enough comparisons are run. At Phase 0's measured rate the first months yield a few
hundred meaningful events. Discovery needs multiple-comparison correction, and no pattern is
*stored as a pattern* until it has held out of sample. Anything else is stored overfitting with a
timestamp.

#### 12d. Patterns must expire

A market fact from six months ago is a hypothesis today. Stored patterns need a re-test cadence
and a `superseded_by` link, so the set is a current view rather than an accumulating pile.

**Status: deferred with step 11.** The one piece worth doing early is the gas-threshold solve in
12a, which belongs on the simulator rather than in a discovery engine.

### Step 13 — Backtesting

Supplied: a historical replay engine that reconstructs protocol state, prices and liquidity,
estimates gas, simulates the liquidation, estimates execution probability and computes
theoretical P&L, compared against observed outcomes; no look-ahead bias; separated
training/validation/test/live data; walk-forward testing.

**This is Gate A5 and it is the single thing standing between the system and a defensible
number.** The corrections are about honesty limits, not the design, which is right.

#### 13a. Look-ahead prevention should be structural, not procedural

"Never use future information" as a rule is followed until it is not. D10 already gives the
mechanism: the replay driver feeds the *identical* downstream pipeline, and the pipeline reads
state through accessors that take a block. `reserve_config_at()` and `reserve_index_at()` (step 4)
are already shaped this way. The remaining leak is reconstructing prices and liquidity from a
current snapshot because the historical one is inconvenient — the usual way a backtest becomes
optimistic without anyone deciding to make it so.

#### 13b. "Compare against actual observed outcomes" is not the test

Every liquidation in the dataset happened because somebody else won it. Comparing our simulated
P&L against their realised outcome measures whether we can price *their* trade, not whether we
would have had it. The output is not a P&L figure — it is P&L **as a function of the win-rate
assumption**, with the assumption stated.

Newly relevant: the block-position result shows the winners are not buying position, so `p(win)`
is not bounded by latency spend. That widens the plausible range rather than narrowing it, which
is a reason to report the curve rather than pick a point on it.

#### 13c. Fidelity is bounded by archive access, and that is a precondition not a discovery

Historical DEX reserves, historical reserve indices and gas *used* all need archive state at the
replayed block, and public endpoints do not serve it. So either the backtest has archive access,
or its exit leg is assumed — and D18 already says an assumed exit cannot authorise a trade. The
same rule applies here: a backtest with an assumed exit produces a number that must be labelled,
not quietly averaged in.

#### 13d. Splits must be by time, and the fold arithmetic needs stating up front

Random train/test splits leak: two liquidations in the same block, or the same borrower
liquidated twice, land on both sides. Splits are chronological.

The arithmetic is uncomfortable and better faced now. At ~4 meaningful liquidations a day above
$1,000, six months is roughly 700 events; above $10,000 it is 93. Walk-forward with several folds
leaves tens of events per test fold. That does not make the exercise pointless, but the honest
output is wide intervals, and a fold-level result quoted as a point estimate is noise.

**Status: a debt-exact version built; the collateral-side, archive-dependent version still
blocked as described above.** `src/liqengine/backtest/replay.py` found a path that needs no
archive access at all: Aave's seizure math defines the gross bonus, in USD, as
`debt_repaid × (bonus_bps − 10,000) / 10,000` — a value identity independent of the collateral's
price, valid whenever the liquidation was not collateral-capped. Every stablecoin-debt
liquidation (86% of Arbitrum's events) can be re-priced exactly on the debt side with zero new
data. The bonus rate, exit cost and gas remain named assumptions (13c's rule applied, not
evaded), and the output is a sweep across assumed win probabilities (13b), not a point estimate.
`liq phase0 backtest` runs it; results in `PHASE0-FINDINGS.md`.

What this version cannot do: verify the identity holds where a liquidation was collateral-capped
(undetectable from the event alone — a stated source of possible overstatement), and it cannot
touch the 14% of events with volatile-asset debt, which still need W3/W6 and archive access for
a true price-and-liquidity reconstruction. Walk-forward folding (13d) was not attempted for the
reason already given: the >$10k cohort's 93 events would produce fold sizes too thin to trust,
and the six-month window is dominated by one month regardless of how it is split.

### Step 14 — Strategy optimisation

Supplied: candidates may be proposed but never auto-deployed; generate → backtest →
out-of-sample → walk-forward → paper execution → risk check → canary → live monitoring → full
deployment; every change versioned; never overwrite a previous version.

**Most of this was already built.** `strategy_version` is immutable and content-hashed,
re-registering a version with different parameters raises, `promote_strategy` refuses a stage
transition whose required gates have not passed, stage changes are appended events rather than a
mutable column, and performance is a snapshot table (D15, step 4). Three gaps.

#### 14a. The mechanism existed; the required sequence was never declared

`promote_strategy` takes `required_gates` from its caller. Nothing said what those gates *are*
for a given stage, so the policy lived in whichever call site was invoked — and a gate a
different call site can skip is not a gate. The ladder has to be data, checked in one place.

#### 14b. There was no demotion path

The supplied sequence runs one way. "Live monitoring" with no defined way back means the decision
to stop gets made while losing money, which is the worst moment to design it. Demotion criteria
belong beside promotion criteria and are declared before the stage is entered.

#### 14c. "Paper execution" is shadow mode

The same thing under a second name. One term throughout keeps the deployment ladder in
`README.md` as the single description of where a strategy is.

**Status: gaps closed.** `risk/promotion.py` declares the ordered ladder, the gates each stage
requires — including the calibration gate from 7d and the backtest gates from step 13 — and the
demotion triggers.

### Step 15 — Risk management

Supplied: hard controls the AI cannot override, with initial defaults — 0.25% of capital per
opportunity, 1% daily and 3% weekly realised loss, 10% protocol and 5% asset exposure, minimum
profit/gas ratio 5x, minimum execution probability 90%, positive conservative case, hard caps on
per-transaction loss and gas, and a failed-transaction rate limit. All configurable, changed
only outside the model.

Almost all of this was a real gap and is now implemented. **One default is wrong**, and the
measurements above are why.

#### 15a. The 90% minimum execution probability is backwards and would prevent all trading

A floor on `p(win)` selects for **uncontested** opportunities, and Phase 0 has now measured what
those are. The largest pooled winner on Arbitrum took 24.5% of all liquidations at a median of
$0.01 and made **$35 in six months**. Nobody contests those, so their win rate is near certain —
and their value is nil. Meanwhile the >$10k band holds 65% of all value and has an incumbent
taking half of it.

It would also make the system inert regardless: the win model returns a *prior* of 20% with a
wide interval until shadow mode produces data, so a 90% floor is a permanent no-trade.

The right floor is on **EV**, which already contains `p(win)` and weighs it against the size of
the prize and the cost of losing (D2). A 30% chance at a large edge with a small failure cost is
a good trade; a 95% chance at a two-dollar edge is not. `min_expected_value_base` and
`min_profit_to_gas_ratio` express that directly. There is deliberately no `min_win_probability`
field, and `limits.py` records why so it is not added back later as an oversight.

#### 15b. Percentage and absolute caps are both needed, and the tighter binds

A percentage of capital scales as capital grows. An absolute cap still holds when the capital
figure itself is wrong, which is the failure a sizing bug produces. `opportunity_cap_base()`
returns the tighter of the two, and unset capital falls back to the absolute rather than reading
as unlimited.

#### 15c. Concentration limits were missing entirely, and needed a ledger not a counter

Protocol and asset exposure are now tracked as *open* exposure — added on submission, released
when the transaction resolves — so they measure capital genuinely at risk rather than cumulative
turnover. Both are needed separately: two protocols holding the same collateral asset is
diversified by venue and concentrated by risk.

#### 15d. A failure *rate* is not a failure *streak*

The engine had a consecutive-failure counter. A bot failing every third attempt never trips one
and is still broken, so a rolling per-hour rate now runs alongside it.

#### 15e. "Cannot be overridden by the AI" is already structural

`issue_token` runs its own evaluation and there is no `force`, `override` or `verdict`
parameter; a test asserts the absence by name rather than pinning the signature. Limits are a
frozen dataclass loaded from configuration, and nothing in the model path can construct one.

**Also added:** weekly loss window alongside hourly and daily, hard per-transaction loss cap
(breaching it trips the sim-versus-realised breaker, because a loss that size means the sizing
model is wrong rather than that the day went badly), and a per-transaction gas cap in base
currency rather than gas units — gas units are meaningless without a price.

### Step 16 — Kill switches

Supplied: global, per-chain, per-protocol, per-strategy and per-asset kill switches, with
automatic pause on daily loss, stale RPC, anomalous oracle, unexpected contract change,
simulation failures, execution failure rate, slippage deviating from model, invalid gas
assumptions, deteriorating model calibration, and unexpected token behaviour.

The condition list was better than mine and three items were genuinely missing. Two corrections.

#### 16a. Scope was missing, and the blast radius belongs to the condition

The engine was venue-scoped only. An oracle behaving strangely is a *chain* problem, a proxy
upgrade is a *protocol* problem, a token transferring the wrong amount is an *asset* problem,
and a miscalibrated model is a *strategy* problem. Halting the wrong scope either stops far more
than necessary or leaves the actual hazard running. `Scope` and `DEFAULT_SCOPE` now attach a
natural blast radius to each condition, so it is a property of the hazard rather than of
whichever caller fired it.

#### 16b. Three conditions were missing, and all three latch

- `SIMULATION_FAILURE_RATE` — simulations starting to fail means the model of the protocol has
  drifted from the protocol.
- `CALIBRATION_DEGRADED` — ties to 7d/D20. `p(win)` multiplies into EV, so a decaying model
  prices every marginal trade wrong in the same direction. Demotes to shadow rather than
  halting: the fix is more data, and shadow is where data comes from without spending.
- `UNEXPECTED_TOKEN_BEHAVIOUR` — ties to D22. A fee-on-transfer or rebasing token breaks the
  accounting, not just the trade. Halts.

Plus `SLIPPAGE_MODEL_BROKEN`, distinct from the general sim-versus-realised divergence because
a broken exit model invalidates every quote issued since, not just the trade that revealed it.

Each new latching condition also required a declared demotion target — a test asserts that
every latching breaker has one, on the grounds that a latch with no declared response is a
decision made under pressure.

**Status: built.** Remaining gap: the scope hierarchy is declared but the engine is still one
instance per venue, so a chain-scoped halt has to be applied by the supervisor that owns the
engines. That supervisor does not exist yet and arrives with the live loop.

### Step 17 — Model monitoring

Supplied: monitor prediction accuracy, probability calibration, false positives and negatives,
realised versus predicted profit / gas / slippage, model drift and feature drift. On
deterioration: pause the strategy, fall back to deterministic rules, generate a diagnostic
report.

The fallback ladder is the good part and is not in any other step. Three corrections.

#### 17a. "False positives and negatives" is classification language for something that is not a classifier

`p(win)` is not deciding a class, it is producing a number that multiplies into EV. Accuracy
against a 0/1 outcome is close to meaningless for it — a model that says 30% and is right 30% of
the time is "wrong" on every single prediction by a classification metric and is exactly correct
by the metric that matters. The right pair is:

- **Calibration** (already built, 7d/D20): does a stated 30% win 30% of the time?
- **Regret**: EV forgone by rejections that would have been profitable, and EV lost on
  acceptances that were not. That is the decision-quality measure, and it is denominated in
  money rather than in counts.

#### 17b. The realised-versus-predicted trio is already wired, and the schema shape is why

`decision` holds the predicted line items and `realised_pnl` holds the outcomes, deliberately as
separate rows so the comparison is a join rather than an overwrite (4e). Monitoring is a query
over that join, not new plumbing. `SIM_REALISED_DIVERGENCE` and `SLIPPAGE_MODEL_BROKEN` are
already breaker conditions.

#### 17c. "Fall back to deterministic rules" needs the deterministic rules to exist

Naming a fallback that has never been defined means discovering at the worst moment that there
is nothing to fall back to. The deterministic strategy is: the conservative net from step 6 with
`p(win)` pinned at its **lower bound** rather than its point estimate, no learned adjustment, and
a tightened EV floor. That is a real strategy that can be run, backtested and promoted like any
other — so it should be a registered `strategy_version`, not a code path.

Feature drift is only meaningful once the feature set is fixed (11c), so it belongs with step 11.

**Status: partly built.** Calibration, the divergence breakers and the demotion ladder exist.
Regret accounting and the registered fallback strategy do not.

### Step 18 — Memory system

Supplied: persistent machine memory rather than conversational memory, storing opportunities,
executions, failures, strategy and parameter versions, wallet profiles, protocol metadata,
discovered patterns, model performance, risk events, anomalies and deployment history. It must
answer "what happened the last 10 times this exact type of opportunity occurred?" and "why did
the system reject this opportunity?".

**This is almost entirely already built** — it is what step 4 is. Mapping the list: opportunities
and rejections → `decision`; executions and failures → `inclusion_attempt` and `realised_pnl`;
strategy and parameter versions → `strategy_version` (immutable, content-hashed);
wallet profiles → `wallet_profile`; protocol metadata → `reserve_config_history` and
`emode_category_history`; model performance → `strategy_metric`; risk events → `halt_event`;
deployment history → `strategy_stage_event`. Discovered patterns is the one table that does not
exist, and step 12 explains why.

Two gaps against the two questions it must answer.

#### 18a. "The last 10 times this type of opportunity occurred" needs a type

There is no opportunity *fingerprint* — a stable key over the dimensions that make two
opportunities the same kind of thing (venue, collateral asset, debt asset, size bucket, close
factor branch, eMode). Without one the question can only be answered by ad-hoc filtering, which
means it is answered differently each time it is asked.

#### 18b. "Why did the system reject this" is answerable but not renderable

`reject_reason` and the full `state_snapshot` are stored. What is missing is the thing that turns
a row into an explanation, which is step 19.

The framing "do not rely on conversational memory" is right and already structural: nothing in
the decision path reads from a conversation, and every input to a decision is a row.

### Step 19 — Explainability

Supplied: a structured explanation for every rejected and executed opportunity, with the worked
example showing gross profit, gas, slippage, execution probability and conservative expected
profit leading to a decision.

The example is close to exactly what `ProfitBreakdown` already holds, and the reasoning it shows
— conservative expected profit negative, therefore do not execute — is D17 and D18. Two
corrections.

#### 19a. "Competitor latency risk: high" is not a measurable field

We have now measured this and it is not the discriminator: block position is uniform, nobody is
racing to be first, and most opportunities draw no rival at all. An explanation line that reads
"latency risk: high" would be confidently wrong. Replace with what is actually observable — the
**contested rate** for this opportunity type, and the number of competitors seen at detection,
both of which are already columns.

#### 19b. The explanation must be generated from the stored row, not written alongside it

An explanation composed at decision time and stored as text will drift from the numbers it
claims to describe the first time a field changes. Render it from `decision` plus
`realised_pnl`, so it cannot disagree with the data, and so historical decisions can be
re-explained after the renderer improves.

**Status: not built, and small.** Every field exists; what is missing is the renderer and the
fingerprint from 18a. Both are a short piece of work and are the natural next thing after the
current measurement run.

### Step 20 — Security

Supplied: no private keys in source or logs, no unrestricted contract calls, whitelist
contracts and tokens, explicit chain ID validation, transaction simulation, maximum approval
limits, nonce protection, replay protection, balance checks, allowance checks, emergency
withdrawal, separate hot wallet from long-term capital, minimal wallet balance, automated
monitoring for unexpected transactions. Never let an LLM construct or submit a transaction
without deterministic validation.

Most of this list is already structural, built for other reasons across steps 4–9. Mapping it
rather than re-building it, then closing the three real gaps.

Already built: no keys in source (D24, `SignerBackend`/`require_backend`); whitelist contracts
and tokens (D9, D12, address+codehash pinning, `known_parties`/`known_tokens` in step 8);
transaction simulation (step 8, unconditional per D21); maximum approvals (D22,
`allowed_approvals` + `LINGERING_APPROVAL`); nonce protection (D25, leasing); balance and
allowance checks (`txsim` reads the actual post-transaction state, not an assumption); hot
wallet separated from capital (the signer only ever holds gas float, never the liquidation
notional, because flash loans supply that — see the capital discussion above).

#### 20a. "No private keys in logs" needed an explicit check, not an assumption

Structurally true because no function anywhere accepts or returns key material (a test asserts
this for `execution/`), but a log line elsewhere in the codebase could still stringify an object
that happens to contain one. Belongs as a lint-time grep in CI once there is a CI, not as
runtime code — noted rather than built, since building a runtime scanner for a category of bug
the type system already prevents would be solving a problem that does not exist here.

#### 20b. Chain ID was never actually checked

`ExecutionIntent` carries a `chain_id` field and nothing read it. An intent built for the wrong
chain — a config error, not an attack — would have been signed and submitted regardless. Fixed:
`authorise()` now takes the executor's chain and refuses a mismatch.

#### 20c. Replay protection is a property of the token, and is now stated as one

D23's liveness token is single-use and bound to the intent hash — that already is replay
protection at the application layer; EIP-155's chain ID in the signed transaction is the
network-level layer underneath it. Both were true; only the second was previously named
explicitly (fixed in 20b, and now cross-referenced).

#### 20d. "Minimal wallet balance" and "emergency withdrawal" did not exist

Both are new, and both matter more than their one-line mentions suggest. A hot wallet running
low on gas mid-burst fails silently — the risk engine has no way to know why nothing is landing.
And a halt with no exit path means capital sits in a wallet the operator can watch drain but not
move.

#### 20e. "Automated monitoring for unexpected transactions" already exists under a different name

`NonceManager.sync()` raises `NonceGapError` when the chain's nonce is ahead of everything this
process leased — which is exactly "a transaction we did not authorise came from this wallet".
Cross-referenced rather than rebuilt.

**Status: built.** `execution/emergency.py` — `EmergencyWithdrawal`, requiring the same signer
custody rules plus an explicit operator acknowledgement (mirroring D27's latch-clearing), sweeping
only to a configured, whitelisted cold-storage address. `risk/limits.py` and `risk/engine.py` —
`WALLET_BALANCE_LOW`, checked against a configured floor. `execution/gate.py` — chain ID
validation added to `authorise()`.

### Step 21 — AI architecture

Supplied: use AI for code generation, protocol research, anomaly investigation, hypothesis
generation, natural-language performance analysis, relationship discovery, governance
interpretation. Never let an LLM be the sole source of trade decisions, amounts, risk limits,
key operations, contract or token addresses, or slippage limits — those must be
deterministic/validated.

This is already true throughout, by construction, and was never stated as a standing invariant
in one place. One correction and one thing to name explicitly.

#### 21a. This describes what has already been built, not a step still to take

`RiskEngine.issue_token` runs its own evaluation with no override (D8); `Signer` takes an intent
and returns a signature with no path for raw key material (D24); `authorise()` composes
deterministic checks only; addresses live in pinned, codehash-verified configuration (D9), never
inferred. Every one of the prohibited items is already unreachable from a model — not by
policy, but because nothing in the call graph accepts that kind of input.

#### 21b. "Hypothesis generation" needs a landing spot or it is a slogan

Step 12's discovery engine and step 22's research environment are that landing spot. A generated
hypothesis becomes a candidate `strategy_version`, which must clear the same gates as any other
(D15, `risk/promotion.py`) — generation is cheap and unconstrained; adoption is exactly as
constrained as it already is for a human-proposed change.

**Status: recorded as an architectural boundary, not new code.** Written into
`ARCHITECTURE.md` as D28 so it is checked against future modules rather than re-derived.

### Step 22 — Quant research subsystem

Supplied: a Python research environment (pandas/polars, numpy, scipy, scikit-learn, statsmodels,
matplotlib, Jupyter, experiment tracking), model versioning, and a record per experiment
(dataset, period, features, parameters, model, results, seed, validation, conclusion). Prevent
leakage from future information.

#### 22a. This is premature ahead of the backtester, and building it now would be busywork

There is nothing to research yet. Step 13 (Gate A5) has not produced a single backtest, so an
experiment-tracking system would track zero experiments. Building the harness before there is
anything to run through it is the same mistake steps 11/12 were deferred to avoid — fitting
infrastructure to data that does not exist yet.

#### 22b. "Prevent leakage from future information" is already a structural requirement, restated

D10's replay driver and the block-parameterised accessors (`reserve_config_at`,
`reserve_index_at`, step 13a) exist for exactly this. A research environment sitting on top of
them inherits the guarantee; a research environment built independently of them would have to
reinvent it and could get it wrong.

#### 22c. Every field in the experiment record already has a home

Dataset and period → the venue key plus a block range; features → a fixed set once 11c is
settled; parameters and model → `strategy_version.parameters`, content-hashed and immutable
(D15); results and validation → `strategy_metric` and `strategy_gate`; conclusion → the
promotion decision itself, in `strategy_stage_event`. A bespoke experiment-tracking table would
duplicate a schema that already exists for the same reason (auditability of what changed and
why) — the record is `strategy_version` plus its gates, not a new artifact.

**Status: deferred until Gate A5 produces something to research.** `pyproject.toml`'s
`research` extra already carries pandas, numpy and matplotlib; scikit-learn, statsmodels, scipy
and a notebook environment are added now since declaring them costs nothing, but nothing imports
them yet and nothing should until there is a backtest to read.

### Step 23 — Metrics

Supplied: total/realised/unrealised P&L and its breakdown by opportunity, protocol, asset and
strategy; win rate, average win/loss, expectancy, drawdown, volatility, Sharpe, Sortino, profit
factor; execution and failure rate; average gas and slippage; predicted-versus-realised profit
and execution probability; capital utilisation and return on capital at risk.

The list is right and needs no correction — it is a query layer over data that already exists
(4e's deliberate join-not-overwrite shape is what makes every predicted-vs-realised item on this
list possible at all). One thing worth stating rather than silently doing.

#### 23a. Sharpe and Sortino need a per-period return series, which liquidation P&L does not naturally have

Liquidations arrive as discrete, irregularly-timed events, not a daily mark-to-market series.
Computing a ratio built for continuous returns requires first choosing a bucketing period and a
treatment for empty buckets (zero return, or excluded) — a choice that changes the number
non-trivially and has to be stated alongside it, not buried in the formula. Implemented with
daily buckets and zero-filled gaps, named as an assumption on the result rather than left
implicit.

**Status: built.** `src/liqengine/metrics/` computes every listed figure from `realised_pnl`
and `decision` rows — pure functions over sequences the caller supplies, so they are usable
against Phase 0-style historical data, shadow-mode decisions, or eventual live P&L without
three implementations.

### Step 24 — Paper trading

Supplied: live data with paper execution, behaving as close to real execution as possible,
recording the hypothetical transaction, simulated gas, slippage, inclusion and P&L. Do not move
to real capital until configurable paper-trading criteria pass.

**This is shadow, exactly.** `Stage.SHADOW` in `risk/promotion.py`, `SHADOW_LIMITS` with every
cap at zero so nothing can sign, and the full pipeline — detection, `simulate()`, `txsim`,
`winmodel` — runs unmodified because none of those modules branch on stage. "As close to real
execution as possible" is the reason `txsim` exists at all: a paper trade that only ran
`simulate()` would skip the exact failure modes (fee-on-transfer collateral, an approval left
standing) that step 8 exists to catch. The configurable criteria are
`SHADOW_DIVERGENCE_WITHIN_TOLERANCE`, already a named gate required for promotion to canary.

**Status: built, under one name.** The only action taken here is confirming there is no second
implementation to build — a "paper execution" path built independently of shadow would be a
second pipeline to keep in sync with the first, which is the exact duplication steps 17c and
22c both warn against elsewhere.

### Step 25 — Canary deployment

Supplied: deploy passing strategies with very small capital, scaling
£100→£250→£500→£1,000→scale, where scaling depends on realised P&L, drawdown, execution rate,
model calibration and failure rate — never on the backtest alone.

The scaling *criteria* are exactly the right set, and the closing rule is the most important
sentence in the step. Three corrections, all about turning a fixed sequence of numbers into a
mechanism.

#### 25a. The ladder is in the wrong currency and the wrong place

Every number in this system is base-currency (USD, per Aave's oracle) integer units — `£` is a
display-layer concern, and the actual caps already have a home: `RiskLimits.max_notional_per_tx_base`
and `strategy_capital_base` (step 15). A canary ladder is a *sequence of `RiskLimits`*, not a
separate concept — rung 1 is a `RiskLimits` with a small cap, rung 2 is the same limits object
with a larger one, promoted via the exact mechanism already built (D27's `demotion_for`, and the
promotion side this step is asking for).

#### 25b. "Never scale solely because the backtest looks good" has to be enforced, not stated

A function that scaling flows through and that *accepts* a backtest result can be called with
one. The only way to make the rule unbreakable is to give the scale-up decision no parameter
that a backtest could occupy — it takes realised metrics and nothing else, the same pattern D8
uses for the liveness token (no `force`, no override) and the same pattern step 9's `issue_token`
test asserts by inspecting the signature.

#### 25c. A criterion needs a threshold and a sample-size floor, or it decides nothing

"Execution rate" and "failure rate" are metrics (step 23 already computes them); a scaling rule
needs to say what value passes and how many trades must back it up first. Three wins is not a
track record — `min_trades_before_scaling` gates every other check, because a 100% execution
rate over two trades and over two hundred mean entirely different things, and conflating them is
how a canary scales up on noise.

#### Status — built

`src/liqengine/risk/canary.py`:

- `CanaryLadder` — a sequence of `RiskLimits`, default rungs the example numbers become
  ($100/$250/$500/$1,000, in base currency, configurable).
- `evaluate_rung(realised: PerformanceSummary, execution: ExecutionSummary, calibration:
  CalibrationReport, trades_at_rung: int, ...) -> CanaryDecision` — `HOLD` / `SCALE_UP` /
  `DEMOTE`, with a reason. No parameter of any type a backtest result could satisfy; a test
  asserts this by inspecting the signature, mirroring the one on `issue_token`.
- Demotion criteria reuse `DEMOTION_TRIGGERS` (D27) directly rather than duplicating them —
  scaling down was already solved, this step only needed the scale-*up* half.

### Step 26 — Flash liquidity

Supplied: investigate flash-loan execution where it improves capital efficiency; do not assume
it is automatically profitable; price liquidation profit, flash fee, gas, DEX fees, slippage and
failure probability; only use it when atomically profitable under conservative assumptions.

**Almost all of this is already built**, because it is D3 and step 6 under a different name.
`simulate()` already prices exactly this list — `FlashLoanQuote.premium_bps` is a real cost line
in `ProfitBreakdown`, the conservative case is what execution is decided on (D18), and `combine()`
folds failure probability into EV rather than into net (7a). One real gap.

#### 26a. Source *selection* was never built — only source *pricing*

`LiquidationRequest.flash` takes one `FlashLoanQuote`; nothing chose it. D3 says "cheapest
first, premiums always queried" as a policy, but the actual algorithm — try the zero-fee sources
in order, skip any without capacity for this size, fall back to Aave's own — did not exist as
code. `simulate/flash.py` — `select_flash_source(sources, amount)` returns the cheapest source
with enough capacity, or `None` if every source is too small, which `simulate()` already turns
into `INSUFFICIENT_FLASH_LIQUIDITY`.

#### 26b. "Where it genuinely improves capital efficiency" is a comparison the metrics module already answers

D3's compare-on-realised-P&L clause needs no new mechanism: tag `strategy_id` by route (`flash`
vs `owned_inventory`) and `metrics.pnl_by_strategy` (step 23) is the comparison. Noted rather
than built twice.

**Status: the one gap closed.** `simulate/flash.py` added; everything else cross-referenced to
where it already lives.

### Step 27 — Protocol discovery

Supplied: after the initial engine works, scan supported protocols for lending markets,
liquidation mechanics and incentives, collateral types, health-factor and oracle mechanisms,
available liquidity, protocol risk, upgradeability and unusual mechanics; rank by expected
profit / competition / execution risk / capital requirement; never auto-deploy — require
verification and explicit whitelist approval.

#### 27a. "After the initial engine works" — it does not yet, so this is correctly not started

The Aave v3 engine is not in shadow mode. A protocol-discovery subsystem surveying for the
*next* venue before the first one has produced a single shadow-mode decision is solving a
problem that does not exist yet — there is nothing to compare a second protocol against.

#### 27b. The ranking formula repeats D2's exact mistake, generalised to protocols

Competition and execution risk divided into expected profit is the same error D2 corrected for
venues: both belong inside the EV term (via `p(win)` and failure cost), not as divisors sitting
outside it. A protocol scored `profit ÷ competition ÷ risk` would rank a low-risk, low-reward
protocol above a high-EV one for the same reason a venue ranked that way would — the formula
double-penalises risk that a properly-computed EV already prices once.

#### 27c. The requirement is already structurally satisfied, by the mechanism a discovery tool would feed

"Never auto-deploy; require verification and whitelist approval" is D9 and D12: address *and*
implementation codehash pinning, and no Aave-compatible fork trusted by inheritance — every one
individually verified. A discovery subsystem, whenever built, is a research tool that
*proposes* candidates into that same `liq registry verify` + manual config-authoring path Aave
v3 itself went through. It has no mechanism of its own to add a whitelist entry, because that
mechanism deliberately has no automated door.

**Status: correctly deferred.** When it is time, the ranking should be a per-protocol EV
estimate (same shape as `winmodel`, not a fresh formula) and the discovery output should be a
*draft* config for `config/protocols/`, not a live entry.

### Step 28 — Long-tail opportunity discovery

Supplied: a generic `OpportunityDetector` interface — `detect()`, `simulate()`, `score()`,
`risk_check()`, `execute()`, `record_result()` — with `LiquidationDetector` as the first
implementation, and the architecture left open for cross-DEX arbitrage, other protocols'
liquidations, unusual collateral events, solver opportunities and rebalancing later. Do not
build them all at once.

The interface shape is right and "do not build all strategies simultaneously" is the correct
discipline. Two corrections, both about what the interface should be built *from*.

#### 28a. The six methods already exist, scattered across six modules that were never asked to share a contract

`detect` is `positions.rank` / `PriceTriggerIndex.triggered_by`. `simulate` is
`simulate.simulate`. `risk_check` is `RiskEngine.issue_token`. `execute` is
`execution.authorise` plus a `Signer`. `record_result` is `EventStore.record_realised_pnl`.
Building `OpportunityDetector` from scratch would mean re-solving problems steps 4–15 already
solved; building it as a thin `Protocol` over the existing pieces means a *second* detector only
has to supply the parts that are actually strategy-specific.

#### 28b. `score()` needs D31's correction before it is written, not after

Scoring an opportunity by raw predicted profit is exactly the "theoretical P&L" step 31 rules
out. `score()` returns EV — which already contains `p(win)` and failure cost (D2, D19) — divided
by capital at risk, so a second detector type is comparable against a liquidation on the same
axis without knowing anything about liquidations.

#### Status — built

`src/liqengine/opportunity.py`:

- `SimulatedOpportunity` — the minimal `Protocol` any strategy's simulation output must satisfy:
  a conservative net, an expected value, a win probability, whether it is executable. Not a new
  type — `simulate.SimulationResult` gained two convenience properties
  (`conservative_net_base`, `win_probability_bps`) so the existing liquidation pipeline satisfies
  it structurally, with no wrapper class.
- `OpportunityDetector[OpportunityT, SimulatedT]` — a `Generic` `Protocol` over the six methods,
  each annotated with which built module a liquidation implementation delegates to.
  `record_result` takes and `execute` returns `RealisedOutcomeRecord` (step 4) rather than a new
  result type, so recording a second strategy's outcome needs no second schema.
- `score()` — the D31-corrected ratio, usable by any detector.

**Not built:** a concrete `LiquidationDetector` class. Writing one now would be untestable glue
sitting on top of a live loop that does not exist yet (W1–W6) — exactly the premature strategy
count this step warns against, just aimed at plumbing instead of a second strategy. It is
deferred to the same milestone as the wiring it would wrap.

### Step 29 — Development order

Supplied: repository → ingestion → protocol state → liquidation detection → profit simulator →
historical database → replay/backtesting → paper execution → risk engine → wallet intelligence
→ ML → real transaction simulation → small-capital execution → performance feedback →
automated research → controlled optimisation. Never skip to live trading.

The discipline — never skip to live trading — is exactly right and nothing in this build has
violated it. The specific phase *order*, though, is not what actually happened, because the
specification did not arrive in this order and the dependencies that actually exist pulled
things together differently. Worth reconciling honestly rather than pretending the plan was
followed to the letter.

#### 29a. What actually happened, against this list

| Their phase | What it asks for | What actually happened |
|---|---|---|
| 1–2 | Repo, architecture, ingestion | Built first, matches |
| 3, 6 | Protocol state/indexing, *then* historical database, five phases apart | Arrived together as step 4 — one unified store, because splitting "current state" from "historical record" into two schemas one phase apart would have meant migrating the first into the second later. The position book (phase 3's actual indexing logic) came two steps after, in step 5, once the schema existed to hold it. |
| 4 | Liquidation detection | Step 5, after the store — detection needs somewhere to persist a decision before it needs anything else |
| 5, 12 | Profit simulator, *then* real transaction simulation, seven phases apart | Steps 6 and 8, adjacent — `simulate()` prices the trade and `txsim` checks the calldata actually does what was priced; building the second without the first still fresh would have meant re-deriving what "correct" means twice |
| 9 | Risk engine | Arrived *after* execution infrastructure (step 9 in this build), not before, because the liveness token step 9 introduced had no issuer until the engine existed — risk engine and execution are more tightly coupled than five sequential phases implies |
| 7 | Replay/backtesting | **Not built.** This is Gate A5, and it is blocked on real prices and reserve history (W3, W6), not on any phase before it in their list |
| 10, 11 | Wallet intelligence, ML | Both deferred with reasons already recorded (steps 10, 11) — blocked on data volume, not on build order |
| 8 | Paper execution | This is shadow mode (step 24), and the mechanism (stage ladder, zero caps) exists; the live loop that would actually run it does not (W1–W6) |
| 13–16 | Small-capital execution onward | Not reached; correctly gated behind the backtester |

#### 29b. The honest current position on their numbering

Sitting at the boundary of their phases 6/7: the historical database (6) exists in full, and
constructing the replay engine (7) is the next thing that is not either wiring (W1–W6) or a
deferred, data-gated step. Everything from phase 9 onward that has *content* — the risk engine,
the canary ladder, the calibration gate — was built ahead of its number because a later step's
specification (execution's liveness token, the canary's demotion criteria) needed it to exist,
not because the ordering discipline was abandoned. Nothing has been signed, and nothing skips to
live trading — the one rule that mattered.

### Step 30 — Engineering standards

Supplied: type safety, unit and integration tests, property-based tests where useful,
deterministic simulations, structured logging, metrics, tracing, error handling, retry logic,
circuit breakers, configuration management, secrets management, Docker, a reproducible dev
environment. Every financial calculation tested; every contract interaction integration-tested.

Most of this list is already true and the rest splits cleanly into "buildable now" and
"needs the live loop first" — the same split W1–W6 already draws for functionality, applied here
to tooling.

**Already true, cross-referenced:** type safety (`mypy --strict`, clean throughout); unit tests
(497 and counting, one file per module); deterministic simulation (every `simulate`/`txsim`/
`winmodel` function is pure — no clock, no randomness, no network); structured logging
(`core/logging.py`, `structlog`); retry logic and circuit breakers (D30, `ingest/provider.py` +
`pool.py`); configuration management (`config/`, typed and validated).

**Real gaps, closed now — buildable with no live system to depend on:**

- **Property-based tests.** The fixed-point math (`core/fixedpoint.py`,
  `protocols/aave_v3/math.py`) is exactly what property-based testing is for: pure integer
  functions with algebraic invariants (rounding direction, monotonicity, round-trip identities)
  that example-based tests can only sample a few points of. Added via `hypothesis`.
- **Docker.** A reproducible way to run `liq phase0 collect`/`report` without a local Python
  setup. Added: a `Dockerfile` for the CLI.

**Deferred with the same reasoning as W1–W6 — need the live system to be worth building:**

- **Integration tests against a real contract.** Meaningful only against a forked chain (anvil/
  Hardhat) or a real archive endpoint, and the honest version of this needs the archive access
  Gate A already requires (13c). Building a fork-based harness now, before the backtester that
  is the actual reason to want one, is tooling ahead of its user.
- **Tracing and live metrics export.** Nothing runs continuously yet to trace. This lands with
  the live loop, not before it — the same reasoning that deferred W1–W6's wiring.
- **Secrets management, concretely.** D24 already specifies *which* backend each stage
  requires; the KMS/hardware client that actually implements `Signer` is Gate B work, and
  writing one now means integrating against a key that does not yet need protecting.
- **A fully reproducible dev environment (lockfile, devcontainer).** `pyproject.toml` plus a
  venv has been sufficient for everyone who has touched this so far — exactly one operator.
  Worth doing before a second contributor, not before then.

### Step 31 — Important implementation principle

Supplied: never optimise for theoretical P&L. Optimise for `realistic EV × execution probability
÷ capital at risk`. A strategy with a large theoretical profit at 10% execution probability is
inferior to one with a small profit at 95%.

The principle is exactly right and is the reason this entire build looks the way it does. The
formula as written has the error D2 corrected in the first session and D19 corrected again for
the same reason — worth naming precisely, because the example given only proves the writer's
own point once the fix is applied.

#### 31a. `EV × execution probability` double-counts the one thing D2 already fixed

If "EV" here means expected value in the sense this system uses it, `p(win)` is already inside
it — `EV = p(win) × net − (1 − p(win)) × failure_cost` (D2, D19). Multiplying that EV by
execution probability again is applying `p(win)` twice. If instead "EV" means the *naive*
undiscounted profit (what step 6 calls gross or predicted net, before the win model), then the
formula is reconstructing D2's EV from its two pieces — but incompletely, because it omits the
failure-cost term entirely. Neither reading survives contact with the worked example.

#### 31b. The example is only true once the omitted failure-cost term is put back

£1,000 at 10% naive-multiplied is £100. £100 at 95% is £95. By the formula as literally written,
the first is *better*. The claim that the second is superior is only true once a real failure
cost is included: at a plausible failure cost of, say, £50 per lost attempt, the first strategy
nets `0.10 × 1,000 − 0.90 × 50 = £55` and the second nets `0.95 × 100 − 0.05 × 50 = £92.50` — now
the second wins, which is the example's actual point. The intuition was correct; the formula
supporting it was missing the exact term this system has spent D2 through D19 insisting on.

#### 31c. Capital at risk as the divisor is right, and already built

Dividing by capital at risk is a genuine, separate, correct idea — a capital-efficiency
ranking, not a repetition of the win-probability point. It is exactly
`metrics.capital_summary.return_on_capital_at_risk_bps` (step 23) and exactly what step 28's
`score()` computes for opportunity ranking. No correction needed here; it was already right and
already built.

**Status: the corrected formula is the one already in force throughout this codebase** — D2,
restated for a third time because a third specification independently arrived at the same
mistake from a different angle, which is itself evidence the mistake is a natural one to make
and worth this much repetition.

---

## What is actually between here and shadow mode

The numbered steps are not the long pole. Every module built so far is a pure, deterministic
core with no chain connection — which is why they are all testable and all fast, and also why
none of them can yet be pointed at Arbitrum and left running.

**Wiring that no numbered step covers, and that Gate A cannot run without:**

| # | Component | Feeds |
|---|-----------|-------|
| W1 | Multicall reader behind `ScaledBalanceReader` — batched `getUserReserveData` / `scaledBalanceOf` / `getUserAccountData` | position book reconciliation |
| W2 | Event subscription loop — Supply/Withdraw/Borrow/Repay/LiquidationCall/UserEModeSet dirtying accounts | position book membership |
| W3 | Oracle price feed — Chainlink aggregator subscription plus an off-chain reference for the deviation, or the Pyth Hermes stream | prediction, valuation, staleness |
| W4 | DEX quote source — a real router quote for the exit leg, at size. **Built**: `simulate/quotes.py`, Uniswap v3 `QuoterV2`, single-hop, verified on Base with `liq quote` | the simulator, which refuses without one (D18) |
| W5 | `eth_call` with state overrides plus trace decoding | step 8 |
| W6 | Reserve config loader writing `reserve_config_history` on config events and TTL | correct replay |

W1–W4 and the shadow driver are built and verified against live Base. `liq shadow status`
reports Gate A5 readiness as four counts, not a duration: 100 race labels at $100+, 30 in one
size bucket, 60% detection coverage of real liquidations, and a 7-day span. Two things W4 taught:

- **A venue address must be checked for code, not just format.** The first QuoterV2 address
  recorded was mistyped in its last four characters. An `eth_call` to a codeless address returns
  empty rather than reverting, so every pair read as "no route at any tier" — indistinguishable
  from a venue with no liquidity. `UniswapV3Quoter.verify()` now refuses a codeless quoter, the
  same way D12 refuses an unverified codehash.
- **Single-hop is a real limit on Base.** WETH→USDC quotes at a few bps from parity. cbETH and
  wstETH against stables fill at ~1% of parity, because their depth is against WETH. Multi-hop
  needs a packed `bytes` path, which `encode_call` deliberately cannot encode yet. The quote is
  still returned as measured and the simulator's net rejects it, which is correct, but it means
  the long-tail collateral Phase 0 flagged as uncontested is currently unexecutable here.

**RESOLVED — the conservative scenario was not conservative on price.** `CONSERVATIVE` used to
raise the debt price 1% and drop the collateral price 1%. The bonus is a percentage of the repaid
debt, so in base currency a dearer debt token made the bonus *larger*, and on live cbBTC→USDC
pairs the conservative net came out above the expected net — so every decision was effectively
made on the expected case. Restated per D39: conservatism about the exit is now a single
`exit_haircut_bps` (the collateral falling against the debt asset before the swap lands), applied
to the swap proceeds only, so it always reduces net and stays falsifiable against realised
slippage. The two independent price shifts default to zero. Same-asset pairs opt out of the
haircut (no swap leg). This changed every recorded conservative figure, so it landed before any
shadow data is used for A5. Every Morpho pair is cross-asset, so the Morpho driver depends on it.

**Morpho Blue venue + the shadow half of A2 built (2026-10-14).** `protocols/morpho_blue/` and
`shadow/morpho_driver.py` add Morpho as the primary win-rate venue (D38): isolated markets with
no SVR/OEV auction, reusing the simulator via LIF-as-bonus. Health is computed locally (loan
asset as base currency, stablecoin-loan markets only for the MVP); gas is valued through a
configured Chainlink native/USD feed. `projection.py` adds the shadow-relevant half of A2 — the
crossing price at which each position reaches HF=1 — so the watchlist is ranked by proximity to
that price and a cheap per-round oracle poll flags positions that crossed between reads for an
immediate priority read (protecting the A5 detection-coverage gate). The other half of A2 —
predicting the oracle's next on-chain write from an off-chain reference and racing it — stays
deferred: it only pays off at execution (Gate B) and carries an external market-data dependency.
The venue ships at `phase0` with the singleton codehash pinned; it needs a market allowlist
(`liq registry markets`) and promotion to `shadow` before it runs. SVR-aware bidding on Aave is
Gate B; in shadow it is measurement only.

Those six are what turn a set of correct components into a system that observes. They are
roughly the same size as the remaining numbered steps, and they are the reason a "how many steps
left" count understates the work.

**Then Gate A completes with the backtester (A5)**, which needs W1–W6 to have produced data.

Honest sequencing to a first return:

1. Step 9 (execution infrastructure) and the risk engine — Gate B components, but they gate
   nothing in shadow, so they can be built in parallel with the wiring.
2. W1–W4 and the shadow driver, then shadow mode runs and `shadow_outcome` starts filling.
   **Corrected:** W5 is pre-signature simulation and belongs to Gate B. W6's replay history is
   for the backtester; shadow needs only W6's live-read slice (`protocols/aave_v3/reserves.py`).
   Shadow fills `shadow_outcome`, not `inclusion_attempt`: with nothing submitted there is no
   attempt to win or lose, only the counterfactual of who took it and how many blocks later.
3. A5 backtester, once there is data to backtest against.
4. Gate B promotion, subject to the calibration gate in 7d and the strategy gates in D15.

---

## What stands between here and a first real return

Phase 0 is done (`PHASE0-FINDINGS.md`). The remaining work splits into three gates.

### Gate A — shadow (no keys, nothing signed)

Everything needed to prove the system finds and prices real opportunities correctly, with zero
capital at risk. This is the honest definition of "start testing".

| # | Component | Why it gates |
|---|-----------|--------------|
| A1 | **Position book** — event stream + forward index projection + multicall reconciliation | Without it there is no set of accounts to evaluate |
| A2 | **Prediction** — who crosses HF=1, at what price, when; oracle-update prediction | Reacting to confirmed state is a lost race |
| A3 | **Full-path profit simulation** — flash borrow → repay → seize → swap → repay → net, with gas, premium, slippage, sequencer grace period | The number that decides everything |
| A4 | **Decision recording** — every detection and rejection with reason, predicted vs realised | The ML training set, and the only way to audit A3 |
| A5 | **Backtester with a win model** — "would we have won it, at what bid, at what cost" | Phase 0 deliberately contains no profit estimate because every historical liquidation was won by someone else. Without this you cannot size the edge, only imagine it. |

At the end of Gate A the system runs live, logs what it *would* have done, and you compare
predicted P&L against what actually happened on chain. If predicted capture does not clearly
clear gas, slippage, failed-attempt cost, and 208 competitors, **stop here** — that is the gate
doing its job, and it costs nothing but time.

### Gate B — canary (keys, signing, hard caps)

| # | Component | Why it gates |
|---|-----------|--------------|
| B1 | **Risk engine + deadman liveness token** | Execution cannot sign without a fresh token. Must exist before any key does. |
| B2 | **Key / nonce / wallet infrastructure** — isolated signer, concurrent nonce manager, replacement txs, gas wallet, balance monitoring, cold-storage sweep | First point at which real money is reachable |
| B3 | **Inclusion strategy** — per-venue: Arbitrum FCFS latency race, Base/OP priority auction, mainnet bundle auction | Different physics, one interface (D6) |
| B4 | **Pre-sign revalidation** | Cache-age zero at signing, or the trade is stale |
| B5 | **Execution + inclusion monitoring + realised P&L on finalised state only** | Closes the loop; unfinalised profit is never realised (D11) |

**Gate B is the minimum for a first real return** — roughly six more specified steps, of which
B1 and B2 must land before B5 regardless of what order you send them in.

### Gate C — scale (only after canary shows positive realised P&L)

Wallet intelligence B and C, ML pattern discovery, strategy optimisation, staged rollout
automation, additional protocols and forks (each individually verified, never trusted by
inheritance — D12), owned-inventory comparison, and a Rust/Go hot path if and only if latency
instrumentation proves lost EV (D13).

---

## Direct answer to "how many steps before I can test for return?"

- **Testing without money at risk** starts after Gate A — five components, and A5 is the one
  most people skip and then lose money on.
- **First real return** needs Gate B on top — five more, of which the risk engine and key
  infrastructure are non-negotiable prerequisites to signing.
- **Roughly six to eight of your remaining nine steps**, then. Steps that map to Gate C — ML,
  pattern discovery, strategy optimisation, controlled deployment — are real, but none of them
  are between you and your first dollar.

The sequencing constraint that matters more than the count: **A5 (backtester with a win model)
comes before Gate B.** Phase 0 measured the market, not the edge. Skipping straight to execution
means finding out the edge does not exist by losing gas on failed races, which is the expensive
way to learn something the backtester tells you for free.
