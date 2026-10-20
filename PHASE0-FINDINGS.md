# Phase 0 findings

Read-only historical study. Data collected 2026-08-16 from public RPC endpoints.
Regenerate at any time with `liq phase0 report`, and the size-cohort competition analysis with
`liq phase0 competition`. The underlying data is in `data/phase0.sqlite`.

**Revised 2026-08-16** after recomputing competition on size cohorts. The original "fragmented,
not a cartel" conclusion did not survive that recut and has been replaced; see below.

## Coverage

| Venue | Window | Liquidations | Status |
|-------|--------|--------------|--------|
| Aave v3 / Arbitrum | 179 days (blocks 433,070,076–494,763,113) | 4,184 | complete |
| Aave v3 / Base | partial (~3 days recent + a gap-filled older range) | 6 | **incomplete — do not draw conclusions** |
| Aave v3 / Ethereum | not collected | 0 | pending |

Base and Ethereum need a paid RPC key. Public endpoints rate-limited out partway through the
Base scan; the collector is resumable, so re-running with a key continues rather than restarts.

## Arbitrum: the headline number is misleading

Raw rate is **23.4 liquidations/day**. That number should not be used for anything.

| Size of debt repaid | Share of liquidations |
|---------------------|-----------------------|
| under $1            | 53% |
| under $100          | 71% |
| under $1,000        | 80% |
| over $10,000        | 3%  |

- Median liquidation repays **$0.29**.
- The largest decile carries **87% of all value**.
- Total repaid over ~6 months: **$5.2M** (stablecoin-denominated debt, 86% of events).

**Economically meaningful rate: ~5.9/day** above $100 repaid. Above $10,000 it is closer to
**0.6/day** — roughly one worthwhile liquidation every other day.

At a 5% liquidation bonus before the protocol fee, gas, and exit slippage, a $100 liquidation
grosses about $5. The dust is not a business; it is other bots keeping the book clean.

## Competition — the pooled figures were wrong, and in the flattering direction

The first version of this document reported **208 distinct winners, HHI 730, "fragmented, not a
cartel"**, and flagged that those numbers needed recomputing on the economically meaningful
subset before they meant anything. That recut has now been done
(`liq phase0 competition`). The pooled numbers do not survive it.

All figures below are the stablecoin-denominated subset (3,611 events, 86% of the total), which
is the population that can be sized exactly. The first row is the control: same population, no
size filter, so every difference below it is attributable to size alone.

| Cohort | n | share | per day | median | winners | top-1 | top-5 | HHI | structure |
|--------|---|-------|---------|--------|---------|-------|-------|-----|-----------|
| all priceable | 3,611 | 100% | 20.2 | $0.29 | 187 | 25% | 52% | 870 | fragmented |
| ≥ $100 | 1,062 | 29% | 5.9 | $1,417 | 71 | 28% | 70% | 1,334 | fragmented |
| ≥ $1,000 | 712 | 20% | 4.0 | $2,078 | 55 | 34% | 72% | 1,611 | moderately concentrated |
| ≥ $10,000 | 93 | 2.6% | 0.52 | $18,770 | 18 | **51%** | **82%** | **2,975** | **highly concentrated** |

**Concentration rises monotonically with size.** The segment worth entering has 18 winners, not
208, and one address takes half of it.

### The two leaders are different actors, and one of them is not a competitor

| | liquidations | median | total repaid | share of <$100 | share of >$10k |
|---|---|---|---|---|---|
| `0x0d2c268a…` | 886 | $0.01 | **$35** | 34.8% | **0.0%** |
| `0xd12810b1…` | 341 | $1,756 | $2,272,373 | 1.5% | **50.5%** |

`0x0d2c268a` is the single largest "winner" in the pooled ranking at 24.5% of all liquidations.
It repaid **thirty-five dollars in six months** and has never once taken a position above $100.
It is a keeper bot cleaning dust, and counting it as a competitor is what made the market look
fragmented.

`0xd12810b1` is the actual competitor: 26% of the $100–$10k band and 50% of everything above
$10,000.

### Position in the block: tested, and the concentration is not a latency moat

Median transaction index *rises* with size — 3 in the sub-$100 band, 12 in the $100–$10k band,
10 above $10,000 — and no liquidation in any cohort lands at index 0. Zero, across all 3,611.

Two competing readings: either large liquidations queue behind an in-block oracle update, or the
absolute index is meaningless because Arbitrum blocks are simply busy. These were tested by
fetching the full contents of 60 blocks holding liquidations above $10,000.

**The oracle-queuing hypothesis is refuted.** None of the addresses that dominate the slots
before our liquidations are Chainlink feeds or Aave price sources — checked directly against
`AaveOracle.getSourceOfAsset` for every configured asset. The most common preceding address
appears in 19.3% of slots before a liquidation and 14.4% of slots after it: a 1.35× enrichment,
which is ordering noise rather than a systematic blocker.

**Position within the block is essentially uniform:**

| | position (0 = first, 1 = last) |
|---|---|
| p10 | 0.15 |
| median | 0.59 |
| p90 | 0.92 |
| **mean** | **0.54** (0.50 = uniformly random) |
| in the first quarter of the block | 22% |
| in the last quarter | 25% |

A median absolute index of 14 is what a random position looks like in a 26-transaction block.
Nothing is being queued behind, and **nobody is racing to be first.**

That matters for the decision, because it separates two very different explanations of the
concentration above. Eighteen winners with one taking half is *not* a co-location moat: the
winners are not buying position, and a new entrant does not need to outrun them to index 0. The
concentration comes from something else — coverage, capital, or simply that few operators
bothered — and those are competed with software rather than with infrastructure.

Exit behaviour is unchanged: only 2% kept the aToken rather than exiting to the underlying.

## How contested is a liquidation? Measured, and less than expected

A losing liquidation reverts and emits no event, so the loser population is invisible to the log
collector. It is not invisible in block data: a loser's transaction is still there, still has a
sender, and still has a receipt saying it failed. `liq phase0 contest` recovers them by scanning
the blocks where a liquidation succeeded for reverted transactions from other known liquidators,
and isolating the subset whose calldata names the same borrower.

Across 60 blocks holding a liquidation above $1,000:

| | count | share of blocks |
|---|---|---|
| reverted transactions from a known liquidator | 56 | 33% |
| ...naming the same borrower (**head-to-head**) | 9 | **12%** |

**Roughly one meaningful liquidation in eight shows a rival attempt on the same position.** The
losers are spread thin — eight distinct addresses, none losing more than twice — and so are the
winners of contested races.

Both numbers are bounds, and they run in opposite directions. Head-to-head **understates**:
a liquidator that has never won here is not in the known set, and a bot that always loses is
exactly the one that never appears. Any-revert **overstates**: a revert can also mean the
position self-cured on a price rebound. The truth is between 12% and 33%.

### Taken together with block position

Two independent measurements now say the same thing. Position within the block is uniform, so
nobody is paying to be first; and seven or eight out of ten meaningful liquidations have no
visible rival at all. **This is not a hot race.**

That reframes the concentration finding rather than contradicting it. One address taking 51% of
the >$10k band, in a market where most opportunities are uncontested and nobody is racing, is a
*coverage* result — they are watching and others are not. Coverage is the thing a competent bot
competes for, and it does not require winning a latency war.

It also bears directly on `p(win)`. The prior currently used is 20%, chosen pessimistically from
the 208-winner figure that has since been retired. A market where most opportunities draw no
rival supports something considerably higher for an operator that is actually present. That is
**not** a licence to assume it: the number still has to be measured in shadow mode, and the
placeholder stays marked as a placeholder until it is.

## Asset mix

Debt repaid is 86% stablecoins (USDC 33%, USDC.e 24%, DAI 15%, USDT 14%). Collateral seized is
54% WETH, then USDC.e 11%, WBTC 8%, ARB 5%.

Good news for the exit-slippage problem: WETH and WBTC have deep liquidity on Arbitrum, so the
collateral leg is unlikely to eat the bonus on the trades that matter. The long-tail collateral
risk flagged in `ARCHITECTURE.md` shows up in the 4% tail, not the core.

## Borrowers

3,370 distinct borrowers, **30% repeat rate** pooled. Recut by size, the repeat rate holds
around 27–35% through the $100 and $1,000 cohorts and then **falls to 13% above $10,000**.

That inverts the original recommendation. A borrower watchlist is worth maintaining for the mid
band, but the large liquidations — 2.6% of events and 65% of all value — are mostly one-off
borrowers who will not appear on a watchlist built from history. Note the caveat below: 93
events is a thin sample and 13% could move materially with more data.

## Where the value actually is

| Band | events | share of events | total repaid | share of value |
|------|--------|-----------------|--------------|----------------|
| under $100 | 2,549 | 71% | $11,209 | 0.2% |
| $100 – $10,000 | 969 | 27% | $1,833,085 | 35% |
| over $10,000 | 93 | 2.6% | $3,366,670 | **65%** |

Seventy-one percent of the events are two-tenths of one percent of the money.

## Revenue: 90% of six months arrived in one month

The single most important number in this document, and the one that determines whether this is a
business.

| Month | events >$100 | repaid | whole-market net bonus |
|-------|--------------|--------|------------------------|
| 2026-03 | 42 | $96,309 | $4,045 |
| 2026-04 | 53 | $149,205 | $6,267 |
| 2026-05 | 58 | $190,536 | $8,003 |
| **2026-06** | **855** | **$4,585,664** | **$192,598** |
| 2026-07 | 15 | $80,473 | $3,380 |

February and August are clipped by the collection window and excluded. The net bonus column is
the 5% headline less the protocol's 10% cut of it, less 30bps of exit cost — the pool available
to *every* liquidator on the venue, before gas.

**June was 37× the median month and 90% of the entire window.** A quiet month produces
**$5,156** of net bonus across all 55–71 participants above $100. That is the whole pie.

### What that means for capture

| Market share | quiet month | June | 11 quiet + 1 crash |
|---|---|---|---|
| 2% | $103 | $3,852 | $4,986/yr |
| 5% | $258 | $9,630 | $12,466/yr |
| 10% | $516 | $19,260 | $24,931/yr |
| 20% | $1,031 | $38,520 | $49,862/yr |

Against a running cost of roughly **$1,100–4,200/year** (paid archive RPC, VPS).

**This is a volatility option, not a revenue stream.** In a quiet month a 10% share earns about
$516 against $90–350 of infrastructure — it clears, and it is not a business. The return lives
almost entirely in the tail, and a year without a June is a year of covering costs.

### One structural reason a new entrant does better in the tail

June ran 855 events above $100 — roughly 28 a day against the 0.5–2 of a quiet month, a 17×
surge. **Capacity binds during a crash.** An incumbent running one transaction in flight at a
time cannot physically take 28 opportunities a day, however fast it is. The concentration
measured above is a quiet-market statistic; the moment flow spikes, share is available to
anyone present and able to act in parallel.

That is the strongest argument in this document for building the thing, and it points at a
specific design priority: **throughput and breadth of coverage during bursts matter more than
per-trade latency.** Which is consistent with the two competition findings — position in the
block is uniform, and most opportunities draw no rival.

## Backtest (Gate A5): per-trade economics hold up, even under stress

`liq phase0 backtest` replays every real historical liquidation as if we had executed it,
using only data already collected — no archive access, no new RPC calls. It works because of a
value identity in Aave's own seizure math: the gross bonus, in USD, is exactly
`debt_repaid × (bonus_bps − 10,000) / 10,000`, independent of the collateral's price, as long as
the liquidation was not collateral-capped. That means the debt-side of every stablecoin-debt
liquidation (86% of events) can be re-priced exactly, with no oracle history needed.

Everything else — the bonus rate, the exit fee and slippage, gas — is a **named, stated
assumption**, not a measurement, and the result is reported as a sweep across assumed win
probabilities rather than a single number, because every event in this dataset happened because
someone else won it (the same caveat as always).

### The >$100 cohort survives a stress scenario

| Scenario | at 10% win rate | at 50% | at 90% |
|---|---|---|---|
| Default (5% bonus, 35bps exit cost, $0.05 gas) | $21,444 | $107,432 | $193,421 |
| 10× gas ($0.50) | $20,966 | $106,954 | $192,943 |
| 3× slippage (100bps) | $17,640 | $88,414 | $159,188 |
| **Stress: $2 gas + 300bps slippage** | **$4,702** | $32,006 | $59,310 |

Totals over the full 179-day window. **Zero of the 1,062 trades in this cohort go net-negative
even under the stress scenario**, assuming they were won. Only when dust is included (the
all-priceable cohort, not the >$100 one) does the stress scenario turn marginally negative at a
10% win rate — confirming again that the >$100 floor is the right cut, not an arbitrary one.

### Still one month carrying almost everything

| Month | EV at 50% win rate |
|---|---|
| 2026-02 | $1,954 |
| 2026-03 | $1,989 |
| 2026-04 | $3,082 |
| 2026-05 | $3,936 |
| **2026-06** | **$94,749** |
| 2026-07 | $1,663 |

88% of the six-month total, at any fixed win-rate assumption, is June. The volatility-option
framing from the revenue-shape analysis holds exactly — this backtest sharpens the per-trade
economics, it does not change the shape of when the money arrives.

### The win-probability scenarios are not the same thing as market share, and that distinction matters

A scenario of "10% win probability" does **not** mean 10% market share. Market share (measured
earlier: median participant ~0.45–1.4%) is diluted by every event a competitor was never present
for. This backtest's win probability is conditional on *attempting* — and the contest
measurement already found that 67–88% of meaningful liquidations draw no rival at all. An
operator that is simply present and reasonably fast could plausibly clear a much higher win rate
*on the events it attempts* than raw market share suggests, precisely because market share
counts the events it never tried for as losses.

This is the encouraging read of the two findings together — uncontested opportunities plus
positive-even-under-stress unit economics — and it is also exactly the thing that must not be
assumed. It is a hypothesis shadow mode is built to test, not a result this backtest has earned
the right to claim.

## What this implies

1. **Do not size the opportunity from event counts.** ~0.5 to 6 meaningful liquidations a day is
   the real Arbitrum picture.
2. **The "208 competitors, fragmented" reading is retired.** Above $10,000 there are 18 winners
   and one takes half. The market is concentrated exactly where the money is, and the earlier
   figure was measuring a dust bot that made $35 in six months.
3. **The size threshold is a strategy decision, not a filter.** Below $100 there is nothing to
   win; above $10,000 there is a specialist to beat. The $100–$10,000 band is 27% of events,
   35% of value, and 55–71 participants — that is where a new entrant's question actually lies.
4. **The concentration is not a latency moat.** Block position is uniform; the winners are not
   buying position. Whatever their advantage is, it is not one that infrastructure spending
   would close, which is the more hopeful of the two possibilities — and consistent with D4.
5. **Most meaningful liquidations are uncontested** — between 67% and 88% show no rival attempt.
   Combined with uniform block position, the barrier is presence, not speed.
6. **Collect Base and Ethereum properly** with a paid key, so the comparison is real.
7. **Then decide whether to build the execution stack.** The measured opportunity has to clear
   gas, slippage, failed-attempt cost, and a named incumbent — not a diffuse field of 208.

## Caveats on the recut

- **93 events above $10,000** over six months. Concentration measured on a sample that size is
  directionally informative and not precise; the 51% top-1 share could move several points
  either way. It will not move to "fragmented".
- Sizing covers stablecoin debt only (86% of events). The remaining 14% is priced in volatile
  assets and would need historical oracle prices at each block.
- Every figure is a count of *other people's* liquidations. None of it is a profit estimate, and
  the section below still applies.

## What this study deliberately does not tell you

It contains no profit estimate. Every liquidation here happened because **somebody else won
it**, so "was it profitable" is the wrong question — the right one is "would we have won it, at
what bid, at what cost", and that requires a win model in the backtester. Treating the bonus on
these historical events as achievable revenue would overstate the edge enormously.
