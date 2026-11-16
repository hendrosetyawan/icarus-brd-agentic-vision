"""Inventory the raw clips, read codec details, and make contact sheets of every camera.

    python -m brd.probe            # writes work/clips.csv and outputs/probe/*

Run this first: it tells us the codec, frame rate and keyframe spacing (which
decode mode to use) and shows what each camera sees, day and night.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import av
import numpy as np
import pandas as pd

from .common import find_clips, hms, load_config, tile, to_dt, to_s, video_decoder, write_json


def duration(path: str) -> float:
    try:
        with av.open(path) as c:
            if c.duration:
                return c.duration / av.time_base
            s = c.streams.video[0]
            if s.duration:
                return float(s.duration * s.time_base)
            return float(s.frames / (s.average_rate or 25))
    except Exception:
        return math.nan


def clip_info(path: str) -> dict:
    with av.open(path) as c:
        s = c.streams.video[0]
        dec = video_decoder(s)
        keys, size = [], None
        for p in c.demux(s):
            if p.pts is None:
                continue
            t = float(p.pts * s.time_base)
            if t > 60:
                break
            if p.is_keyframe:
                keys.append(t)
                if size is None:
                    size = next(((f.width, f.height) for f in dec.decode(p)), None)
        return dict(codec=dec.name, tag_unknown=s.codec_context is None, width=size and size[0],
                    height=size and size[1], fps=round(float(s.average_rate or 0), 2),
                    keyframe_every_s=round(float(np.diff(keys).mean()), 2) if len(keys) > 1 else None)


def grab(path: str, at_s: float):
    """The first keyframe at or after at_s - 2 s (keyframes come every ~2 s in this dataset)."""
    with av.open(path) as c:
        s = c.streams.video[0]
        dec = video_decoder(s)
        c.seek(int(max(0.0, at_s - 2) / s.time_base), stream=s, backward=True)
        for p in c.demux(s):
            if not p.is_keyframe or p.pts is None or float(p.pts * s.time_base) < at_s - 2:
                continue
            for f in list(dec.decode(p)) + list(dec.decode(None)):
                return f.to_image(width=1600, height=round(f.height * 1600 / f.width))
    return None


def main() -> None:
    cfg = load_config()
    out = cfg["out_dir"] / "probe"
    (out / "frames").mkdir(parents=True, exist_ok=True)
    clips = find_clips(cfg["raw_dir"])
    with ThreadPoolExecutor(16) as ex:
        clips["duration"] = list(ex.map(duration, clips.path))
    # If a container doesn't report its length, assume it runs until the next clip (at most 5 min).
    gap_to_next = (clips.groupby("cam").start.shift(-1) - clips.start).clip(upper=300).fillna(300)
    clips["duration"] = clips.duration.where(clips.duration > 0, gap_to_next)
    clips.to_csv(cfg["work_dir"] / "clips.csv", index=False)

    summary = []
    for (day, cam), g in clips.groupby(["day", "cam"]):
        end = g.start + g.duration.fillna(300)
        gaps = (g.start.iloc[1:].to_numpy() - end.iloc[:-1].to_numpy())
        info = clip_info(g.path.iloc[len(g) // 2])
        summary.append(dict(day=day, cam=cam, clips=len(g), first=hms(g.start.min()), last_end=hms(end.max()),
                            hours=round(g.duration.sum() / 3600, 1), gaps_over_10s=int((gaps > 10).sum()),
                            biggest_gap_min=round(float(gaps.max()) / 60, 1) if len(gaps) else 0, **info))
    summary = pd.DataFrame(summary)
    summary.to_csv(out / "summary.csv", index=False)
    print(summary.to_string(index=False))

    # Contact sheets: every camera at the same clock times, so we can map cameras and draw zones.
    for day, g in clips.groupby("day"):
        d0 = to_dt(g.start.min()).replace(hour=0, minute=0, second=0)
        cams = sorted(g.cam.unique())
        imgs, caps = [], []
        for hour in (3, 8, 12, 16, 21):
            t = to_s(d0) + hour * 3600
            for cam in cams:
                gc = g[g.cam == cam]
                hit = gc[(gc.start <= t) & (gc.start + gc.duration.fillna(300) > t)]
                img = grab(hit.path.iloc[0], t - hit.start.iloc[0]) if len(hit) else None
                if img is not None:
                    img.save(out / "frames" / f"{day}_{cam}_{hour:02d}00.jpg", quality=90)
                imgs.append(img)
                caps.append(f"{day} {cam} {hour:02d}:00" + ("" if img is not None else " (no video)"))
        tile(imgs, cols=len(cams), cell=480, captions=caps).save(out / f"contact_{day}.jpg", quality=85)
    write_json(out / "summary.json", summary.to_dict("records"))
    keyframe = summary.keyframe_every_s.dropna()
    if len(keyframe) and keyframe.max() <= 2.5:
        print(f"\nKeyframes every <= {keyframe.max()} s: set \"decode\": \"keyframes\" in config.json (one frame per keyframe; decoding all frames is ~30x slower).")
    else:
        print('\nKeep "decode": "all" (keyframes are too sparse for 1 frame/s sampling).')
    print(f"Contact sheets and frames: {out}")


if __name__ == "__main__":
    main()
