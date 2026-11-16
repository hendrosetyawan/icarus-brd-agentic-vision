#!/bin/bash
# Build the submission ZIP: code + Level 1/2/3 outputs + brief. Never includes raw video or extracted frames.
#   bash hprc/package.sh  [brief.pdf]
set -euo pipefail
export BRD=${BRD:-$SCRATCH/brd}
NAME=${TEAM:-team}_brd_agentic_vision
STAGE=$(mktemp -d)/$NAME
mkdir -p "$STAGE"
rsync -a --exclude '__pycache__' --exclude 'logs' --exclude '*.pyc' ./ "$STAGE/code/"
for lvl in level1 level2 level3; do
  [ -d "$BRD/outputs/$lvl" ] && rsync -a "$BRD/outputs/$lvl" "$STAGE/outputs/"
done
[ -d "$BRD/outputs/probe" ] && rsync -a --exclude frames "$BRD/outputs/probe" "$STAGE/outputs/"
[ -n "${1:-}" ] && cp "$1" "$STAGE/"
(cd "$(dirname "$STAGE")" && zip -qr "$OLDPWD/$NAME.zip" "$NAME")
ls -lh "$NAME.zip"
