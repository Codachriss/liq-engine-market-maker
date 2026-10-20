#!/bin/bash
# Lightweight keepalive for the Morpho shadow collector.
#
# Run ONCE from your Terminal (which has the macOS Documents permission the collector needs):
#
#     cd /Users/I568093/Documents/liq-engine-main
#     nohup ./scripts/keepalive.sh >> data/shadow-agent.log 2>&1 &
#
# What it gives you:
#   * survives closing the Terminal window        (nohup detaches it)
#   * restarts the collector if it ever crashes   (the while loop)
#   * stops idle/system sleep from pausing it      (caffeinate -is)
#
# What it does NOT survive: a full logout or reboot. After one of those, run the command again.
#
# Watch it:   tail -f data/shadow-agent.log      and      liq shadow status --venue morpho_base
# Stop it:    pkill -f 'caffeinate -is bash' ; pkill -f 'liq shadow run'
#
# Tune cost/coverage by editing the flags below (--max-reads up = faster coverage, more CU).
cd /Users/I568093/Documents/liq-engine-main
# shellcheck disable=SC1091
source .venv/bin/activate
exec caffeinate -is bash -c '
  while true; do
    echo "[keepalive] starting collector $(date)"
    liq shadow run --venue morpho_base --backfill-days 180 --max-reads 120 --interval 5 || true
    echo "[keepalive] collector exited (code $?); restarting in 15s $(date)"
    sleep 15
  done
'
