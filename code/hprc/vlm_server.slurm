#!/bin/bash
# Extra vision-model server on another GPU. brd/vlm.py spreads requests over the main job's server plus
# every server registered in $VLM_SERVERS_DIR, so each extra job adds throughput. Submit as many as useful:
#   sbatch hprc/vlm_server.slurm            (scancel them when the agent steps are done)
#SBATCH --job-name=brd-vlm
#SBATCH --account=132681195956
#SBATCH --partition=gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=06:00:00
#SBATCH --output=logs/%x_%j.log
source hprc/env.sh
PORT=$((8100 + SLURM_JOB_ID % 800))   # two server jobs may share a node
REG="$VLM_SERVERS_DIR/$SLURM_JOB_ID"
mkdir -p "$VLM_SERVERS_DIR"
trap 'exit' TERM
trap 'rm -f "$REG"; kill $VLLM_PID 2>/dev/null' EXIT
vllm serve "$VLM_MODEL" --served-model-name brd-vlm --host 0.0.0.0 --port "$PORT" \
  --max-model-len 32768 --gpu-memory-utilization 0.90 --limit-mm-per-prompt '{"image": 8}' \
  > "logs/vllm_${SLURM_JOB_ID}.log" 2>&1 &
VLLM_PID=$!
until curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null; do
  kill -0 $VLLM_PID 2>/dev/null || { echo "vLLM exited"; tail -n 30 "logs/vllm_${SLURM_JOB_ID}.log"; exit 1; }
  sleep 10
done
echo "$(hostname):$PORT" > "$REG"
echo "=== $(date +%H:%M) serving at $(hostname):$PORT"
wait $VLLM_PID
