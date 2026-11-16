#!/bin/bash
# Copy the hackathon video (~190 GB) from Google Drive to $BRD/raw. Run on a login node inside tmux:
#   tmux new -s fetch      then   bash hprc/fetch_data.sh
# Needs a one-time `rclone config` remote named "gdrive" (README step 3). Safe to re-run: it resumes.
set -euo pipefail
export BRD=${BRD:-$SCRATCH/brd}
RCLONE=$(command -v rclone || echo "$BRD/bin/rclone")
FOLDER_ID=1r9dUk5tj-Tsslb9gHSE6aPkhHWdEOdBW   # Drive folder "Problem 2: BRD Agentic Vision" (dataset, brief, pen map; ~153 GiB)
"$RCLONE" copy gdrive: "$BRD/raw" --drive-root-folder-id "$FOLDER_ID" \
  --transfers 12 --checkers 16 --stats 60s --stats-one-line
du -sh "$BRD/raw"/*
