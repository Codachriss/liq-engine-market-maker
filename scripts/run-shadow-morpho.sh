#!/bin/bash
# Morpho shadow collector, managed by launchd (com.liqengine.shadow-morpho).
# launchd restarts this if it ever exits (crash, logout, reboot), so a dropped terminal no
# longer costs you liquidations. Edit the flags below to trade coverage speed against RPC cost:
#   --max-reads  accounts read per round (higher = faster coverage, more Alchemy CU)
#   --interval   seconds between rounds (higher = cheaper, slightly staler)
set -euo pipefail
cd /Users/I568093/Documents/liq-engine-main
# shellcheck disable=SC1091
source .venv/bin/activate
exec liq shadow run --venue morpho_base --backfill-days 180 --max-reads 30 --interval 6
