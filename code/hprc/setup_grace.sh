#!/bin/bash
# One-time setup on a Grace LOGIN node (needs internet):   bash hprc/setup_grace.sh
# Creates $BRD/{raw,work,outputs,models,hf,venv}, installs packages, pre-downloads YOLO + DINOv2.
set -euo pipefail
export BRD=${BRD:-$SCRATCH/brd}
export PIP_CACHE_DIR=$BRD/cache/pip XDG_CACHE_HOME=$BRD/cache   # home allows only 10k files; keep caches on scratch
mkdir -p "$BRD"/{raw,work,outputs,models,hf,bin,cache} logs
module purge
module load GCCcore/13.2.0 Python/3.11.5    # if missing: `module spider Python`, pick 3.10-3.12, update hprc/env.sh too
python -m venv "$BRD/venv"
source "$BRD/venv/bin/activate"
pip install -U pip wheel
pip install -r requirements-grace.txt
# Compute nodes lack libGL; the headless OpenCV build avoids "libGL.so.1: cannot open shared object file".
# Both builds share the cv2/ folder, so uninstalling one breaks the other: force-reinstall headless last.
pip uninstall -y opencv-python || true
pip install --force-reinstall --no-deps opencv-python-headless
export HF_HOME=$BRD/hf
(cd "$BRD/models" && python -c "from ultralytics import YOLO; YOLO('yolo11x.pt')")
python -c "from transformers import AutoImageProcessor, AutoModel; AutoImageProcessor.from_pretrained('facebook/dinov2-small'); AutoModel.from_pretrained('facebook/dinov2-small')"
if ! command -v rclone >/dev/null && [ ! -x "$BRD/bin/rclone" ]; then
  (cd "$BRD/bin" && curl -sSfLO https://downloads.rclone.org/rclone-current-linux-amd64.zip \
     && unzip -oq rclone-current-linux-amd64.zip && cp rclone-*-linux-amd64/rclone . && rm -rf rclone-*-linux-amd64*)
fi
python -c "import vllm, ultralytics, av, torch; print('vllm', vllm.__version__, '| ultralytics', ultralytics.__version__, '| torch', torch.__version__)"
echo "Setup done. Next: put the VLM in $BRD/models and fetch the video (see README)."
