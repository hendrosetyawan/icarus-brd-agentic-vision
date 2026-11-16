"""Shared helpers: config, clip inventory, time, the frame store, pen zones and drawing."""
from __future__ import annotations

import base64
import io
import json
import os
import re
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.path import Path as Polygon
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime(1970, 1, 1)  # times are naive local clock (CST) stored as seconds since this epoch
BEHAVIORS = ["lying", "standing", "walking", "feeding", "drinking", "ruminating_lying",
             "ruminating_standing", "grooming", "social_interaction", "other_not_visible"]
LYING = {"lying", "ruminating_lying"}
UPRIGHT = {"standing", "walking", "feeding", "drinking", "ruminating_standing"}
SIGNS = ["drooping_ears", "nasal_discharge", "head_lowered", "open_mouth_breathing",
         "eye_discharge", "coughing_posture"]
VIDEO_EXT = {".avi", ".mp4", ".mkv", ".mov"}
ANIMAL_CLASSES = [17, 18, 19, 20, 21]  # COCO horse, sheep, cow, elephant, bear: night IR cows get mislabelled


# ------------------------------------------------------------------ config
def load_config(path: str | Path | None = None) -> dict:
    path = Path(path or os.environ.get("BRD_CONFIG") or ROOT / "config.json")
    if "BRD" not in os.environ:
        raise SystemExit("Set BRD to the data root first, e.g. export BRD=$SCRATCH/brd")

    def expand(v):
        if isinstance(v, str):
            return os.path.expandvars(os.path.expanduser(v))
        if isinstance(v, dict):
            return {k: expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return v

    cfg = expand(json.loads(path.read_text()))
    for k in ("raw_dir", "work_dir", "out_dir"):
        cfg[k] = Path(cfg[k])
    for k in ("work_dir", "out_dir"):
        cfg[k].mkdir(parents=True, exist_ok=True)
    return cfg


# -------------------------------------------------------------------- time
def to_s(dt: datetime) -> float:
    return (dt - EPOCH).total_seconds()


def to_dt(s: float) -> datetime:
    return EPOCH + timedelta(seconds=float(s))


def hms(s: float) -> str:
    return to_dt(s).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- inventory
def cam_key(name: str) -> str:
    digits = re.findall(r"\d+", name)
    return f"cam{int(digits[-1]):02d}" if digits else name.lower().replace(" ", "")


def day_key(name: str) -> str:
    digits = re.findall(r"\d+", name)
    return f"day{int(digits[-1])}" if digits else name.lower().replace(" ", "")


def clip_start(path: str | Path) -> datetime:
    m = re.search(r"(\d{14})", Path(path).stem)
    if not m:
        raise ValueError(f"No YYYYMMDDHHMMSS timestamp in {path}")
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")


def find_clips(raw_dir: Path) -> pd.DataFrame:
    """Expects raw_dir/<Day N>/<Cam NN>/Event<YYYYMMDDHHMMSS>xxx.Avi."""
    rows = [dict(path=str(p), day=day_key(p.parent.parent.name), cam=cam_key(p.parent.name),
                 start=to_s(clip_start(p)))
            for p in Path(raw_dir).rglob("*") if p.suffix.lower() in VIDEO_EXT]
    if not rows:
        raise SystemExit(f"No video files under {raw_dir}")
    return pd.DataFrame(rows).sort_values(["cam", "start"]).reset_index(drop=True)


def load_clips(cfg: dict) -> pd.DataFrame:
    """clips.csv written by probe.py (path, day, cam, start, duration)."""
    f = cfg["work_dir"] / "clips.csv"
    if not f.exists():
        raise SystemExit("Run `python -m brd.probe` first.")
    return pd.read_csv(f)


def load_dets(cfg: dict) -> pd.DataFrame:
    files = sorted((cfg["work_dir"] / "dets").glob("*.csv.gz"))
    if not files:
        raise SystemExit("No detections yet. Run `python -m brd.track` first.")
    d = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    # Short per-camera track numbers (1, 2, ...): the VLM copies short ids reliably but truncates long ones
    # like 20800295 (day/segment offsets from track.py). Deterministic, so every step sees the same numbers.
    d["track"] = d.groupby("cam").track.rank(method="dense").astype(int)
    return d


def load_frames_table(cfg: dict) -> pd.DataFrame:
    """Every sampled frame (cam, t, night, n_dets) plus the day folder it came from."""
    files = sorted((cfg["work_dir"] / "dets").glob("*.frames.csv"))
    return pd.concat([pd.read_csv(f).assign(day=f.name.split("_")[0]) for f in files], ignore_index=True)


# ------------------------------------------------------------- frame store
def frame_file(frames_dir: Path, cam: str, t: float) -> Path:
    d = to_dt(t)
    return Path(frames_dir) / cam / d.strftime("%Y%m%d") / f"{d:%H%M%S}.jpg"


class Frames:
    """Index of JPEG frames saved as <dir>/<cam>/<YYYYMMDD>/<HHMMSS>.jpg."""

    def __init__(self, frames_dir: Path):
        self.times: dict[str, list[float]] = {}
        self.paths: dict[str, list[Path]] = {}
        for cam_dir in sorted(Path(frames_dir).glob("*")):
            items = sorted((to_s(datetime.strptime(p.parent.name + p.stem, "%Y%m%d%H%M%S")), p)
                           for p in cam_dir.glob("*/*.jpg"))
            self.times[cam_dir.name] = [t for t, _ in items]
            self.paths[cam_dir.name] = [p for _, p in items]

    def nearest(self, cam: str, t: float, tol: float = 3.0):
        ts = self.times.get(cam, [])
        if not ts:
            return None
        i = bisect_left(ts, t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(ts)), key=lambda j: abs(ts[j] - t))
        return (ts[best], self.paths[cam][best]) if abs(ts[best] - t) <= tol else None

    def image(self, cam: str, t: float, tol: float = 3.0):
        hit = self.nearest(cam, t, tol)
        return (hit[0], Image.open(hit[1]).convert("RGB")) if hit else (None, None)

    def between(self, cam: str, t0: float, t1: float) -> list[float]:
        ts = self.times.get(cam, [])
        return ts[bisect_left(ts, t0):bisect_left(ts, t1)]


# ------------------------------------------------------------------- zones
def zone_of(cfg: dict, cam: str, box) -> str:
    """Name of the pen zone (feed_bunk, waterer, resting) a box touches; '' if none."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    pts = [(cx, cy), (cx, y1), (cx, y2), (x1, cy), (x2, cy)]
    for name, poly in cfg.get("zones", {}).get(cam, {}).items():
        if len(poly) >= 3 and Polygon(poly).contains_points(pts).any():
            return name
    return ""


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# ----------------------------------------------------------------- drawing
try:
    FONT = ImageFont.load_default(size=18)
    SMALL = ImageFont.load_default(size=14)
except TypeError:  # Pillow < 10.1
    FONT = SMALL = ImageFont.load_default()

PALETTE = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4", "#46f0f0",
           "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff"]


def color_for(label: str) -> str:
    m = re.search(r"C(\d+)", label)
    return PALETTE[(int(m.group(1)) - 1) % len(PALETTE)] if m else "#ffffff"


def video_decoder(stream):
    """The stream's decoder. These NVR files tag HEVC with an unknown FourCC ("GHEC"), so FFmpeg attaches
    no decoder; fall back to HEVC."""
    if stream.codec_context is not None:
        return stream.codec_context
    import av
    return av.CodecContext.create("hevc", "r")


def fit_width(img: Image.Image, width: int) -> Image.Image:
    if img.width <= width:
        return img
    return img.resize((width, round(img.height * width / img.width)))


def annotate(img: Image.Image, items, width: int = 1024, title: str = "") -> Image.Image:
    """items: [(box_norm, label)]. Draws coloured boxes with labels; colour follows the cow ID."""
    im = fit_width(img.convert("RGB"), width).copy()
    d = ImageDraw.Draw(im)
    W, H = im.size
    for box, label in items:
        x1, y1, x2, y2 = box[0] * W, box[1] * H, box[2] * W, box[3] * H
        c = color_for(label)
        d.rectangle([x1, y1, x2, y2], outline=c, width=3)
        tw = d.textlength(label, font=FONT) + 6
        d.rectangle([x1, max(0, y1 - 22), x1 + tw, max(22, y1)], fill=c)
        d.text((x1 + 3, max(0, y1 - 21)), label, fill="black", font=FONT)
    if title:
        d.rectangle([0, 0, d.textlength(title, font=FONT) + 10, 24], fill="black")
        d.text((5, 3), title, fill="white", font=FONT)
    return im


def crop(img: Image.Image, box, pad: float = 0.15, size: int = 384) -> Image.Image:
    W, H = img.size
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    b = (max(0, (x1 - pad * w) * W), max(0, (y1 - pad * h) * H),
         min(W, (x2 + pad * w) * W), min(H, (y2 + pad * h) * H))
    c = img.crop(tuple(int(v) for v in b))
    c.thumbnail((size, size))
    return c


def tile(images: list, cols: int | None = None, cell: int = 320, captions=None) -> Image.Image:
    cols = cols or len(images)
    rows = -(-len(images) // cols)
    cap_h = 20 if captions else 0
    out = Image.new("RGB", (cols * cell, rows * (cell + cap_h)), (25, 25, 25))
    d = ImageDraw.Draw(out)
    for i, im in enumerate(images):
        if im is None:
            continue
        im = im.copy()
        im.thumbnail((cell, cell))
        x, y = (i % cols) * cell, (i // cols) * (cell + cap_h)
        out.paste(im, (x + (cell - im.width) // 2, y + cap_h + (cell - im.height) // 2))
        if captions:
            d.text((x + 4, y + 2), str(captions[i])[:40], fill="white", font=SMALL)
    return out


def jpeg_b64(img: Image.Image, quality: int = 85) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def is_night(img: Image.Image) -> bool:
    """Infrared night footage is near-greyscale."""
    hsv = np.asarray(img.convert("RGB").resize((64, 36)).convert("HSV"))
    return float(hsv[..., 1].mean()) < 20


def ffmpeg_exe() -> str:
    from shutil import which
    if which("ffmpeg"):
        return "ffmpeg"
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def write_json(path: Path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, default=str))
