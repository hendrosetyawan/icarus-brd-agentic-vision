# Sourced by every job:  source hprc/env.sh
export BRD=${BRD:-$SCRATCH/brd}
module purge
module load GCCcore/13.2.0 Python/3.11.5   # must match what setup_grace.sh used
source "$BRD/venv/bin/activate"
export HF_HOME=$BRD/hf
export HF_HUB_OFFLINE=1                      # compute nodes have no internet; everything is pre-downloaded
export XDG_CACHE_HOME=$BRD/cache VLLM_CACHE_ROOT=$BRD/cache/vllm TRITON_CACHE_DIR=$BRD/cache/triton  # home allows only 10k files
export VLM_MODEL=${VLM_MODEL:-$BRD/models/Qwen3.5-9B}
export PYTHONUNBUFFERED=1
module load CUDA/13.0.0 2>/dev/null           # nvcc, in case FlashInfer compiles a kernel at startup
export VLLM_USE_FLASHINFER_SAMPLER=0          # its sampler needs nvcc to JIT-compile; PyTorch's sampler doesn't
export VLM_SERVERS_DIR=$BRD/work/vlm_servers  # extra vLLM servers from hprc/vlm_server.slurm register here
mkdir -p logs
