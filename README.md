# liq-engine

Autonomous on-chain opportunity & liquidation system. **Personal capital only.**

Not a SaaS product, not a client platform, not a signal service, not custodial, not financial advice.

## What this is

A pipeline that discovers DeFi liquidation opportunities, prices them honestly (gas, slippage,
liquidity, flash-loan premium, execution probability, competition, worst-case fill), gates them
through a risk engine that can veto execution unconditionally, executes via a venue-appropriate
inclusion strategy, and records every decision — including rejections — into an event-sourced
store that later feeds pattern discovery, backtesting, and strategy optimisation.

The system learns from its own realised P&L. It does **not** let a model mutate live trading
parameters. Every strategy change passes automated validation gates and a staged rollout.

## Current status: Phase 0 (read-only venue study)

No keys. No signing. No execution code path exists yet. The only things built are:

- configuration / protocol registry (`config/`, `src/liqengine/config/`)
- multi-provider RPC ingestion with failover, quorum and reorg tracking (`src/liqengine/ingest/`)
- Aave v3 liquidation math (`src/liqengine/protocols/aave_v3/`)
- event-sourced observability store (`src/liqengine/store/`)
- Phase 0 historical collector + venue scorecard (`src/liqengine/phase0/`)

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and the decisions log.

## Quick start

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

copy .env.example .env     # then fill in RPC URLs

# Phase 0: collect historical liquidations for a venue
liq phase0 collect --venue aave_v3_arbitrum --days 180

# Phase 0: produce the venue scorecard
liq phase0 report
```

`liq venues` lists configured venues and their lifecycle status.

Live, read-only, nothing signed:

```powershell
liq follow --venue aave_v3_base --read 20            # W1-W3: membership, health factors, oracle prices
liq quote --venue aave_v3_base --collateral WETH --debt USDC --amount 1   # W4: measured exit quote
liq shadow run --venue aave_v3_base        # shadow mode: decide and record every round, never sign
liq shadow status --venue aave_v3_base     # is there enough data to build Gate A5's win model?
```

## Deployment stages

A venue moves through these, never skipping:

| Stage      | Keys | Signs | Description                                              |
|------------|------|-------|----------------------------------------------------------|
| `dormant`  | no   | no    | configured, not observed                                  |
| `phase0`   | no   | no    | historical study only                                     |
| `shadow`   | no   | no    | full live pipeline, decisions logged, nothing signed       |
| `canary`   | yes  | yes   | live with hard per-tx and per-day capital caps             |
| `live`     | yes  | yes   | full size, still under all circuit breakers                |

## Safety invariants

These are enforced structurally, not by convention. See `ARCHITECTURE.md` §Risk.

1. The execution layer cannot sign without a **fresh liveness token** from the risk engine
   (deadman switch). Risk engine down/hung/unreachable ⇒ no signing.
2. Contract whitelisting is by **address + implementation codehash**. A proxy upgrade halts
   that protocol until the new codehash is manually re-verified.
3. No model, agent, or research process ever has access to private keys or can write to
   live strategy parameters.
4. Unfinalised profit is never treated as realised P&L.
