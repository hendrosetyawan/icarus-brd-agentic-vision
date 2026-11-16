#!/bin/bash
# Starts the vision-language model server (vLLM) on the GPU, then runs pipeline steps against it, in order.
#   sbatch hprc/agent.slurm identify:all behavior risk report     (":" = space; Grace's sbatch wrapper splits quoted args)
#   sbatch hprc/agent.slurm watch:--clip:/path/without/spaces.Avi:--cam:cam01
# For a model too big for one 40 GB A100 (e.g. 32B in bf16): TP=2 sbatch --gres=gpu:a100:2 hprc/agent.slurm ...
#SBATCH --job-name=brd-agent
#SBATCH --account=132681195956
#SBATCH --partition=gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH --time=07:30:00
#SBATCH --output=logs/%x_%j.log
# May be submitted early to hold a place in the A100 queue: wait until all 6 track files exist.
until [ "$(ls "${BRD:-$SCRATCH/brd}"/work/dets/*.csv.gz 2>/dev/null | wc -l)" -ge 6 ]; do sleep 60; done
echo "=== $(date +%H:%M) tracks ready"
source hprc/env.sh
vllm serve "$VLM_MODEL" --served-model-name brd-vlm --port 8000 --tensor-parallel-size "${TP:-1}" \
  --max-model-len 32768 --gpu-memory-utilization "${GPU_UTIL:-0.80}" --limit-mm-per-prompt '{"image": 8}' \
  > "logs/vllm_${SLURM_JOB_ID}.log" 2>&1 &
VLLM_PID=$!
trap 'kill $VLLM_PID 2>/dev/null' EXIT
for step in "$@"; do
  echo "=== $(date +%H:%M) python -m brd.${step//:/ }"
  python -m brd.${step//:/ } || { echo "step failed: $step"; tail -n 30 "logs/vllm_${SLURM_JOB_ID}.log"; exit 1; }
done
echo "=== $(date +%H:%M) all steps done"
