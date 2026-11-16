#!/bin/bash
# Holds one A100 so we can launch and restart steps inside it without re-queueing:
#   sbatch hprc/hold.slurm
#   srun --jobid=<JOBID> --overlap -c 6  bash hprc/serve.sh                 # vision-model server on :8000
#   srun --jobid=<JOBID> --overlap -c 16 bash hprc/steps.sh identify:all behavior risk report
# (Grace's sbatch wrapper splits quoted arguments, so steps use ':' in place of spaces.)
#SBATCH --job-name=brd-hold
#SBATCH --account=132681195956
#SBATCH --partition=gpu
#SBATCH --gres=gpu:a100:1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=24
#SBATCH --mem=120G
#SBATCH --time=07:15:00
#SBATCH --output=logs/%x_%j.log
echo "holding $(hostname) from $(date +%H:%M)"
sleep infinity
