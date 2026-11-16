#!/bin/bash
# Level 3 live alert on one clip (paths may contain spaces; quote them):
#   sbatch hprc/watch.slurm "$BRD/raw/Day 2/Cam 01/Event20261108140000006.Avi"
#SBATCH --job-name=brd-watch
#SBATCH --account=132681195956
#SBATCH --partition=gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x_%j.log
source hprc/env.sh
vllm serve "$VLM_MODEL" --served-model-name brd-vlm --port 8000 --tensor-parallel-size "${TP:-1}" \
  --max-model-len 32768 --gpu-memory-utilization "${GPU_UTIL:-0.80}" --limit-mm-per-prompt '{"image": 8}' \
  > "logs/vllm_${SLURM_JOB_ID}.log" 2>&1 &
VLLM_PID=$!
trap 'kill $VLLM_PID 2>/dev/null' EXIT
python -m brd.watch --clip "$1"
