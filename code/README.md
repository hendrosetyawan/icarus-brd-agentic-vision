# Agentic vision for Angus behaviour and BRD risk

**Team ICARUS:** Hendro Setyawan, Ruri Kaimudin, Ikhwan Fadlu.

AISFS × Merck hackathon 2026, Problem 2. The pipeline watches 2 days of video from 3 cameras in pen 6 (10 Angus cows). It tells the cows apart, labels what each one does every minute, ranks them by BRD risk with video evidence, and raises live alerts on new clips.

## How it works

| Stage | Command | What happens |
|---|---|---|
| Probe | `python -m brd.probe` | Lists the clips and reads codec, frame rate and keyframe spacing. Makes contact sheets of every camera, day and night. |
| Track (GPU) | `python -m brd.track --task N` | YOLO11 detects cows at every keyframe (about every 2 s) and ByteTrack follows them. The tracker carries on across back-to-back clips. Frames are saved every 5 s, plus a full-resolution frame every 30 min for reading ear tags. |
| Gallery (agent) | `python -m brd.identify gallery` | On each camera, day and night, the vision-language model (VLM) names the cows C01–C10, zooming in to read tags and describe markings. Later cameras are matched to the cows already named. |
| Assign | `python -m brd.identify assign` | Each track gets an appearance embedding (DINOv2) and is given the most similar cow not already in view on that camera at the same time. |
| Audit (agent) | `python -m brd.identify audit` | Every 30 min on every camera, the agent checks each label. It zooms, looks back or forward in time, checks the other cameras and the reference crops, and corrects mistakes. Self-checks reject answers that, for example, put one cow in two places. Answers that fail their checks are not applied. The ID-check frames are saved. |
| Behaviour (agent) | `python -m brd.behavior` | For each cow-minute, the camera that sees her best gives 3 crops for one quick look. Tracker evidence (posture h/w, movement, pen zone) checks that answer. Disagreement or low confidence escalates to the agent, which investigates with its tools. Minutes she can't be seen are `other_not_visible`. Gaps of up to 3 minutes between identical behaviours are filled and flagged `estimated`. |
| Risk | `python -m brd.risk` | Features per cow and day, compared with the herd and with her previous day: time and visits at the bunk, lying, rumination, missed herd meals, isolation, drinking, and visible signs. These give a 0–1 relative risk with reasons, plus cards and video clips for the top 3. |
| Report | `python -m brd.report` | Picks the three agent traces (success, failure, night) and writes the coverage summary and accuracy against your labels. |
| Live (Level 3) | `python -m brd.watch --clip X` | Runs the same pipeline on a new clip, updates each cow's risk against her own baseline, takes a close look for clinical signs, and raises an alert with a snapshot and a clip. It can also POST to a webhook. |

The agent loop lives in `brd/vlm.py`. Each turn the model returns `{"thought", "action", "args"}` and we run the tool it picked. Its final answer goes through deterministic self-checks, and failures go back to it to investigate again. Every episode is saved under `work/traces/`.

## Runbook on Grace (A100): the exact commands we ran

About 4 hours end to end on the day: ~1 h setup and copy, ~2 h tracking, ~1 h agent steps on 4 A100s.

**About the data** (from `brd.probe`): every clip is HEVC tagged with the unknown FourCC `GHEC`, so the code forces the HEVC decoder (`video_decoder()` in `brd/common.py`; `-c:v hevc` for ffmpeg). cam01 and cam03 are 4000×3000, cam02 is 2592×1944, all 30 fps with a keyframe every ~2 s, so we decode keyframes only (`"decode": "keyframes"`): one frame every ~2 s, about 30x less work than decoding every frame.

**Grace notes.** Charge jobs to the project account in the `#SBATCH --account` lines. Grace's `sbatch` is a wrapper that splits quoted arguments, so pipeline steps use `:` for spaces (`identify:all`). Compute nodes are offline: everything is installed and downloaded on a login node first.

**0. Code on Grace.** From your machine (SSH works off campus; log in once, then reuse the connection):
```bash
rsync -a --exclude __pycache__ problem2/ grace:/scratch/user/NETID/brd/code/
```
Then on Grace: `cd $SCRATCH/brd/code`.

**1. Setup (login node, ~20 min):** Python 3.11 venv, packages, YOLO11x, DINOv2, then the vision-language model.
```bash
bash hprc/setup_grace.sh
source $SCRATCH/brd/venv/bin/activate && hf download Qwen/Qwen3.5-9B --local-dir $SCRATCH/brd/models/Qwen3.5-9B
```

**2. Video (154 GB, ~15 min).** One-time rclone remote `gdrive` with `drive.readonly` (`/sw/local/bin/rclone config create gdrive drive scope=drive.readonly`, sign in with the TAMU Google account through the printed link), then:
```bash
nohup bash hprc/fetch_data.sh > logs/fetch.log 2>&1 &
```

**3. Probe (login node, ~2 min)**, then set `camera_roles`, `overview_camera` and `zones` in `config.json` from `outputs/probe/contact_day*.jpg`:
```bash
source hprc/env.sh && python -m brd.probe
```

**4. Hold one A100 and track (~2 h).** `hprc/hold.slurm` keeps a GPU so steps can be (re)started inside it without queueing again. Tracking runs the 6 camera-days in parallel on it (or use `sbatch hprc/track.slurm` for one GPU per camera-day):
```bash
sbatch hprc/hold.slurm                                          # note its JOBID
srun --jobid=JOBID --overlap -n 1 -c 16 bash hprc/track_in_job.sh
```

**5. Vision-model servers.** One in the held job, plus extra GPUs: `brd/vlm.py` spreads requests over every server registered in `work/vlm_servers/`.
```bash
srun --jobid=JOBID --overlap -n 1 -c 6 bash hprc/serve.sh &     # 127.0.0.1:8000
sbatch hprc/vlm_server.slurm                                    # repeat for more GPUs; scancel when done
```

**6. Agent steps and Level 3 demo (~1 h on 4 A100s):**
```bash
srun --jobid=JOBID --overlap -n 1 -c 16 bash hprc/steps.sh identify:all behavior risk report \
  watch:--clip:$SCRATCH/brd/demo/Event20261108140145006.Avi:--cam:cam01
```
Check `outputs/level1/gallery_sheet.jpg`. `watch` takes any clip (`--clip`, path without spaces: we link it into `demo/`) or follows a folder (`--follow`).

**7. Accuracy check.** Copy `outputs/` and `work/label_pool/` to a machine with a browser, run `streamlit run app.py`, label about 50 cow-minutes in **Label & accuracy**, copy `work/label_pool/labels.csv` back and rerun `python -m brd.report`.

**8. BRD sign evidence, brief and package.** `brief/brd_evidence.py` scores every cow on the BRD signs pen riders use (DART: depression, appetite, respiration; temperature is not visible on video, so a fever-like index averages the behaviours febrile cattle show in challenge studies: less feeding, more lying still, less activity) against the herd and writes `brd_signs.csv` and its heatmap. `brief/make_brief.py` builds that, the pipeline diagram (`brief/pipeline_diagram.py`) and the 2-page brief:
```bash
python brief/brd_evidence.py $SCRATCH/brd/outputs $SCRATCH/brd/outputs/level2
python brief/make_brief.py brief/numbers.json $SCRATCH/brd/outputs brief/technical_brief.pdf
TEAM=icarus bash hprc/package.sh brief/technical_brief.pdf
```
The ZIP holds the code and all outputs, never raw video or extracted frames.

## Outputs mapped to the brief

| Brief asks for | File(s) |
|---|---|
| activity.csv (bouts, every cow, every minute) | `outputs/level1/activity.csv` (per minute: `activity_minutes.csv`) |
| ID-check frames every 30 min per camera | `outputs/level1/id_check/<cam>/*.jpg`, gallery: `gallery_sheet.jpg` |
| Per-cow time budgets | `outputs/level1/time_budgets.csv` / `.png` |
| 3 agent traces (success, failure, night) | `outputs/level1/agent_traces/{success,failure,night}/trace.md` |
| brd_risk.csv + visualisation tool | `outputs/level2/brd_risk.csv`, `app.py` |
| Risk card for top 3 with 2–3 clips | `outputs/level2/risk_cards/<cow>/card.html` + `clip*.mp4` |
| Live alert with video evidence | `outputs/level3/alerts/<clip>/<cow>.{json,jpg,mp4}`, `live_risk.csv` |

## What is observed, estimated or assumed

- **Observed:** detections, tracks and the VLM's per-minute labels (`source` = `fast` or `agent_*`).
- **Estimated:** short gaps filled from neighbouring minutes (`source = estimated`).
- **Not observed:** minutes when a cow was not visible (`not_visible`).
- **Assumed:** the risk score's directions and weights, taken from the BRD sickness-behaviour literature. The score is a relative index for 10 cows over 2 days, not a probability. No diagnoses exist to calibrate it, and possibly no cow is sick.

## Models and outside resources

| Item | Use | Licence |
|---|---|---|
| Ultralytics YOLO11x (COCO-pretrained), ByteTrack | detection, tracking | AGPL-3.0 |
| DINOv2-small (`facebook/dinov2-small`) | appearance embeddings for re-ID | Apache-2.0 |
| VLM: `Qwen/Qwen3.5-9B` (revision `c202236235762e1c871ad0ccb60c8ee5ba337b9a`), thinking off, served with vLLM 0.33.0 | agent and behaviour labels | Apache-2.0 (check the model card) |
| PyAV / FFmpeg | decoding and clip cutting | LGPL |

Versions used: Python 3.11.5, torch 2.14.0 (CUDA 13), vLLM 0.33.0, Ultralytics 8.4.214, Transformers 5.20.2, PyAV 18.1.0, lap 0.5.14, opencv-python-headless 5.0.0.93, pandas 3.1.1. No outside cattle data was used.

**AI tools:** code drafted with Claude Code (Anthropic) and reviewed and run by the team.

## Testing

`python tests/smoke_test.py` runs every stage on synthetic video. It uses stand-ins for YOLO, DINOv2 and the VLM, while the real agent loop, self-checks, traces and outputs still run. It checks plumbing, not model quality.
