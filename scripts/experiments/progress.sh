#!/usr/bin/env bash
# Progress of the experiment runs: stage log, metrics per finished epoch, live batch of the current epoch.
# Usage: scripts/experiments/progress.sh [rid2|airs]   (default rid2); add -f to refresh every 30 s.
set -euo pipefail
cd "$(dirname "$0")/../.."
EXP=${1:-rid2}
[ "$EXP" = "-f" ] && EXP=rid2
LOG_DIR=data/interim/external/$EXP
STAGE_LOG=$LOG_DIR/train.log
[ "$EXP" = "airs" ] && STAGE_LOG=$LOG_DIR/night.log

show() {
  echo "== stages ($STAGE_LOG)"
  tail -n 6 "$STAGE_LOG" 2>/dev/null || echo "(no log yet)"
  for csv in $(ls -t models/runs/*"$EXP"*/results.csv 2>/dev/null | head -2); do
    echo
    echo "== $(basename "$(dirname "$csv")") — finished epochs (mask metrics on our val)"
    awk -F, 'NR==1{printf "%6s %7s %6s %6s %7s %10s\n","epoch","min","P","R","mAP50","mAP50-95"; next}
             {printf "%6d %7.0f %6.3f %6.3f %7.3f %10.3f\n",$1,$2/60,$12,$13,$14,$15}' "$csv"
  done
  echo
  echo "== current epoch (live)"
  tail -c 4000 "$LOG_DIR/nohup.out" 2>/dev/null | tr '\r' '\n' | grep -E "[0-9]+/[0-9]+ .*it/s|s/it" | tail -n 1 | cut -c1-140 || true
  pgrep -f "scripts/experiments/.*_(train|night).py" >/dev/null && echo "(running)" || echo "(not running)"
}

if [ "${2:-}" = "-f" ] || [ "${1:-}" = "-f" ]; then
  while true; do clear; date '+%H:%M:%S'; show; sleep 30; done
else
  show
fi
