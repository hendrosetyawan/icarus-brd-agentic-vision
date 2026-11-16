"""Detect and track cows in every clip of one camera-day; keep sampled frames for later looks.

    python -m brd.track --task 0        # 0..N-1 = (day, cam) pairs in sorted order (Slurm array)
    python -m brd.track --day day1 --cam cam01

Writes work/dets/<day>_<cam>.csv.gz (cam, t, track, x1, y1, x2, y2, conf; boxes normalised 0-1),
work/dets/<day>_<cam>.frames.csv (every sampled frame: t, night, n_dets), JPEG frames every
`store_every_s` to work/frames/, and full-resolution frames at each 30-min checkpoint to
work/frames_full/ (for reading ear tags). The tracker persists across back-to-back clips,
so a track continues through clip boundaries.
"""
from __future__ import annotations

import argparse
import math
import os

import av
import pandas as pd

from .common import ANIMAL_CLASSES, ROOT, frame_file, is_night, load_clips, load_config, video_decoder

INTRA_CODECS = {"mjpeg", "rawvideo", "png", "jpeg2000"}


class Detector:
    """YOLO + ByteTrack. Swapped for a fake in tests/smoke_test.py."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        from ultralytics import YOLO
        self.model = YOLO(self.cfg["yolo_weights"])

    def __call__(self, img):
        r = self.model.track(img, persist=True, tracker=str(ROOT / "hprc" / "bytetrack_cows.yaml"),
                             conf=self.cfg["det_conf"], classes=ANIMAL_CLASSES, imgsz=self.cfg["det_imgsz"],
                             half=True, verbose=False)[0]  # FP16: ~2x faster on GPU; ignored on CPU
        if r.boxes is None or r.boxes.id is None:
            return []
        return [(int(i), b.tolist(), float(c)) for i, b, c in
                zip(r.boxes.id.int().cpu(), r.boxes.xyxyn.cpu().numpy(), r.boxes.conf.cpu().numpy())]


def sample_frames(path: str, every_s: float, keyframes_only: bool):
    """Yield (seconds_from_clip_start, av.VideoFrame) about every `every_s` seconds."""
    with av.open(path) as c:
        s = c.streams.video[0]
        dec = video_decoder(s)
        dec.thread_type = "AUTO"
        dec.thread_count = int(os.environ.get("DECODE_THREADS", 0))  # 0 = FFmpeg picks
        intra = dec.name in INTRA_CODECS
        rate = float(s.average_rate or 25)
        next_t, i = 0.0, 0

        def frames(packet):
            nonlocal next_t, i
            for f in dec.decode(packet):
                # Time from the frame's own pts: threaded decoders return frames a few packets late.
                t = float(f.pts * s.time_base) if f.pts is not None else i / rate
                i += 1
                if t + 1e-6 < next_t:
                    continue
                next_t = t + every_s
                yield t, f

        for packet in c.demux(s):
            if packet.size == 0:
                continue
            if keyframes_only and not intra and not packet.is_keyframe:
                continue  # keyframes decode on their own; skipping the rest is ~30x less work
            if intra and packet.pts is not None and float(packet.pts * s.time_base) + 1e-6 < next_t:
                continue  # every frame is independent, so skip decoding the ones we don't need
            yield from frames(packet)
        yield from frames(None)  # flush frames still inside a threaded decoder


def run(cfg: dict, clips: pd.DataFrame, cam: str, detector, out_stem, track_base: int = 0) -> pd.DataFrame:
    """track_base keeps track numbers unique across the separately processed day files."""
    work = cfg["work_dir"]
    every, store_every = cfg["sample_every_s"], cfg["store_every_s"]
    ckpt_every = cfg["checkpoint_every_min"] * 60
    dets, frames = [], []
    prev_end, seg = None, 0
    next_ckpt = None
    for n, clip in enumerate(clips.sort_values("start").itertuples()):
        if prev_end is not None and clip.start - prev_end > cfg["max_gap_s"]:
            detector.reset()  # big gap: start fresh tracks rather than chain across it
            seg += 1
        last_store, last_t = -1e18, clip.start
        try:
            for t_rel, frame in sample_frames(clip.path, every, cfg["decode"] == "keyframes"):
                t = clip.start + t_rel
                last_t = t
                # Convert and shrink in one step: the raw frames are 12 MP, and YOLO and storage need ~1280 px.
                w = min(frame.width, cfg["frame_width"])
                img = frame.to_image(width=w, height=round(frame.height * w / frame.width))
                found = detector(img)
                if t - last_store >= store_every - 0.5:
                    f = frame_file(work / "frames", cam, t)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    img.save(f, quality=85)
                    last_store = t
                if next_ckpt is None:
                    next_ckpt = math.ceil(t / ckpt_every) * ckpt_every
                if t >= next_ckpt:
                    f = frame_file(work / "frames_full", cam, t)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    frame.to_image().save(f, quality=92)  # full resolution, for reading ear tags
                    next_ckpt = math.floor(t / ckpt_every) * ckpt_every + ckpt_every
                frames.append((cam, round(t, 2), is_night(img), len(found)))
                for tid, box, conf in found:
                    dets.append((cam, round(t, 2), track_base + seg * 100000 + tid,
                                 *(round(v, 4) for v in box), round(conf, 3)))
        except av.error.FFmpegError as e:
            print(f"  ! skipped rest of {clip.path}: {e}")
        prev_end = max(last_t, clip.start + (clip.duration if clip.duration == clip.duration else 0))
        if n % 20 == 0:
            print(f"  {cam}: clip {n + 1}/{len(clips)}  {len(dets)} detections", flush=True)
    d = pd.DataFrame(dets, columns=["cam", "t", "track", "x1", "y1", "x2", "y2", "conf"])
    d.to_csv(f"{out_stem}.csv.gz", index=False)
    pd.DataFrame(frames, columns=["cam", "t", "night", "n_dets"]).to_csv(f"{out_stem}.frames.csv", index=False)
    return d


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, help="index into the sorted (day, cam) pairs")
    ap.add_argument("--day")
    ap.add_argument("--cam")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    clips = load_clips(cfg)
    pairs = sorted(set(zip(clips.day, clips.cam)))
    day, cam = pairs[a.task] if a.task is not None else (a.day, a.cam)
    out = cfg["work_dir"] / "dets"
    out.mkdir(parents=True, exist_ok=True)
    stem = out / f"{day}_{cam}"
    if (out / f"{day}_{cam}.csv.gz").exists() and not a.force:
        print(f"{stem}.csv.gz exists; use --force to redo")
        return
    sel = clips[(clips.day == day) & (clips.cam == cam)]
    print(f"Tracking {day} {cam}: {len(sel)} clips")
    day_num = int("".join(ch for ch in day if ch.isdigit()) or 0)
    d = run(cfg, sel, cam, Detector(cfg), stem, track_base=day_num * 10_000_000)
    print(f"Done: {len(d)} detections, {d.track.nunique()} tracks -> {stem}.csv.gz")


if __name__ == "__main__":
    main()
