#!/bin/bash
# Detection + tracking: one array task per (day, camera) = 6 tasks.   sbatch hprc/track.slurm
#SBATCH --job-name=brd-track
#SBATCH --account=132681195956
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1                  # any GPU type (T4/RTX/A100): YOLO fits all, and A100s are scarce
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=05:00:00
#SBATCH --array=0-5
#SBATCH --output=logs/%x_%A_%a.log
source hprc/env.sh
python -m brd.track --task "$SLURM_ARRAY_TASK_ID"
