#!/bin/bash
# Run pipeline steps in order; ':' stands for a space inside a step (identify:all -> brd.identify all).
source hprc/env.sh
for step in "$@"; do
  echo "=== $(date +%H:%M) python -m brd.${step//:/ }"
  python -m brd.${step//:/ } || { echo "step failed: $step"; exit 1; }
done
echo "=== $(date +%H:%M) all steps done"
