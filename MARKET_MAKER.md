# Kraken Market-Maker — Runbook

> **Scope:** This document is **separate from the liquidation engine** (see `README.md` /
> `ARCHITECTURE.md` for that). This is a **standalone two-sided market-making bot for the Kraken
> spot exchange**. It does not use the `liqengine` package — only `httpx`, `ccxt`, `python-dotenv`.
> Written for whoever runs/maintains the bot (including future-you on a VPS).

---

## 1. Honest status — read this first

This bot **works mechanically** but, on the capital and venue tested, is **net-negative/thin-edge**.
Real result over ~1 day of live running at $6–12 order sizes on Kraken: **about −$4.6 (~−5%)**.

Why (confirmed with live data, not theory):
- Kraken charges **0.16% maker fee → 0.32% per round-trip**. The spread the bot can capture on the
  pairs that qualify is barely above that, so after fees + **adverse selection** (your bid fills
  right before dips) each round-trip nets ~0 to slightly negative.
- Market-making **profits in choppy/sideways markets and loses in trends.** The small-caps that have
  a wide-enough spread to qualify also tend to trend and are volatile.
- **More capital does not create an edge** — it scales wins and losses together. The edge is in the
  per-dollar return, which is thin-to-negative here. What would help: a 0%-maker/rebate venue, a
  genuinely ranging market, a real predictive signal, or lower-toxicity pairs.

Treat this as a **learning / research tool**, not an income engine, unless the venue economics change.

(The earlier phases of this project — on-chain liquidations and cross-DEX arbitrage — were explored
and found non-viable for a small, no-infra operator; see §7 and the probe scripts.)

---

## 2. Setup

```bash
cd <repo>
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create `.env` in the repo root (git-ignored — never commit it):

```
KRAKEN_API_KEY=your_key
KRAKEN_API_SECRET=your_private_key
```

**Key permissions on Kraken:** enable *Query Funds* + *Create & Modify Orders*. **Disable withdrawal.**
That way a leaked key can only trade, never move funds out. IP-whitelist it if you can.

Scripts load `.env` by their own path, so they work from any directory.

---

## 3. Running

All commands use absolute paths; adjust `<repo>` to the repo location.

```bash
# START (detached, continuous). Pass the pair you currently hold so it re-attaches to that
# inventory and baselines P&L honestly; omit the pair only if you are flat (USD only).
nohup <repo>/.venv/bin/python -u <repo>/scripts/kraken_mm_live.py EV/USD \
      > <repo>/data/mm_live.log 2>&1 &

# WATCH
tail -f <repo>/data/mm_live.log

# STATUS (orders + balances; read-only, places nothing)
<repo>/.venv/bin/python <repo>/scripts/kraken_status.py

# STOP (cleanly cancels the bot's own resting orders on the way out)
pkill -f kraken_mm_live
```

**Rules:**
- Run **only one instance** at a time (two fight over orders/balance).
- When restarting **while holding a token**, pass that token's pair (e.g. `EV/USD`) so the net
  baseline is honest and it manages the inventory. Flat (USD only) → omit the pair (it rotates to
  the best pair).
- The net counter **resets to 0 each start** — it measures P&L from that launch, not lifetime.

---

## 4. How it works

- **Two-sided quoting:** each cycle it places a bid below mid and an ask above mid (at `LEVEL_FRACS`),
  re-centred on the live mid. It buys dips and sells rips — capturing the spread both directions.
- **Inventory skew:** order sizes revert the position toward ~50% token / 50% USD, bounded so total
  token exposure never exceeds `ALLOC_USD`.
- **Hold-and-quote (no panic stop):** like Hummingbot, it does **not** market-sell at a loss. In a
  dip it holds the inventory and keeps quoting, waiting for the ask to fill on a rebound. The only
  loss path removed deliberately (an earlier stop-loss version crystallised losses on every dip).
- **Pair selection:** `mm_bot.py` scans all USD-quoted Kraken pairs and keeps those with
  spread > 2× maker fee, real 24h volume/trades, an affordable minimum order, and **not trending
  down** (price not far below its 24h VWAP). It prefers the highest-volume qualifier.
- **Min-order aware:** sizes every order to clear the pair's minimum; if selling would leave a
  sub-minimum tail it sells the whole holding instead (avoids getting stuck unable to sell).

---

## 5. Parameters (top of `scripts/kraken_mm_live.py`)

| Constant | Meaning | Current |
|---|---|---|
| `ALLOC_USD` | Max token inventory per pair = **max exposure (your risk cap)** | 16 |
| `LOT_USD` | Size of each quote | 12 |
| `LEVEL_FRACS` | How far from mid to quote (fraction). Wider = fewer, higher-margin fills, less fee churn | `[0.005, 0.009]` |
| `HALF_SPREAD_BPS` | Informational only (LEVEL_FRACS drive the quotes) | 50 |
| `SLEEP_S` | Seconds between re-quote cycles | 20 |
| `MIN_LOT_USD` | Don't place a quote smaller than this | 2 |
| `KRAKEN_RUN_SECONDS` (env) | Runtime cap; `0`/unset = continuous | 0 |

Pair-selection thresholds live in `RiskConfig` in `scripts/mm_bot.py` (min volume, min trades,
max spread, max downtrend, etc.).

**Tuning intuition:** widen `LEVEL_FRACS` and raise `SLEEP_S` to trade *less but better* (fewer fills,
each clearing fees comfortably — reduces the fee bleed). Tighten them for more fills (more churn,
more fee drag). On a 0.16%-maker venue, wider is generally safer.

---

## 6. Reliability / operational notes

- The bot is a **local process**. If the machine **sleeps, powers off, or loses network**, it
  stops (and is not built to auto-recover a network outage — it will likely exit).
- On a **clean exit** (`pkill`, Ctrl-C, or an error) it **cancels its own orders**. On a **hard
  power-off** it may leave 1–2 resting orders on Kraken that could fill while it's off (bounded by
  `ALLOC_USD`). Before shutting the machine down, `pkill -f kraken_mm_live` first, then check
  `kraken_status.py`.
- For true **24/7**, run it on an always-on host (a small cloud VPS) with a process supervisor
  (systemd/`Restart=always`). A laptop that sleeps will keep interrupting it.

---

## 7. Script index

**Market maker (standalone, Kraken):**
- `scripts/kraken_mm_live.py` — the live two-sided market maker.
- `scripts/mm_bot.py` — pair-universe scanner + `RiskConfig` + selection (public API, no keys).
- `scripts/kraken_status.py` — read-only orders + balances.
- `scripts/kraken_account.py` — read-only key/balance check.
- `scripts/kraken_live_test.py` — one far-from-market postOnly order, then cancel (validates the
  live path with ~zero fill risk).
- `scripts/kraken_mm_scan.py` — one-shot MM-viability scan (which pairs clear spread > 2× fee now).

**On-chain research probes (dead-ends, kept for the record — need `websockets`/`brotli` + the
`liqengine` package / an RPC in `.env`):**
- `scripts/arb_scan.py` — Base cross-DEX (Uniswap v3 vs Aerodrome) round-trip arb scanner.
- `scripts/oracle_latency_probe.py` — classifies Morpho market oracles, reconstructs the
  oracle-update → liquidation back-run pattern.
- `scripts/mempool_latency_harness.py` — measures Base mempool pre-inclusion visibility (found: none).
- `scripts/flashblocks_probe.py` — Base Flashblocks stream analysis.
- `scripts/submit_latency_probe.py` — sequencer submit-latency floor.
- `scripts/_verify_aero.py`, `scripts/_ws_pending_check.py` — small verification helpers.

**Conclusion of the on-chain research:** same-chain/liquidation/arb MEV on Base is a colocated,
latency-gated, or capital-gated game that a small no-infra operator cannot win profitably; that is
why the project pivoted to the (also thin-edge, but at least runnable) CEX market maker documented
here.
