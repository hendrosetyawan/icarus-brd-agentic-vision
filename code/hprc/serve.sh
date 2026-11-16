#!/bin/bash
# Vision-model server on this node's GPU at 127.0.0.1:8000 (the pipeline's default vlm_url).
source hprc/env.sh
exec vllm serve "$VLM_MODEL" --served-model-name brd-vlm --port 8000 --tensor-parallel-size "${TP:-1}" \
  --max-model-len 32768 --gpu-memory-utilization "${GPU_UTIL:-0.80}" --limit-mm-per-prompt '{"image": 8}'
