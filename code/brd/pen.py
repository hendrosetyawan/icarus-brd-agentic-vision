"""Everything the agent can look at: detections, frames, cow-ID labels, the reference gallery,
and the tools built on them (zoom, look back/forward in time, other cameras, track history)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image

from .common import annotate, crop, hms, tile, zone_of


class Labels:
    """Cow ID per (camera, track) over time intervals: {(cam, track): [[t0, t1, cow, source, score], ...]}."""

    def __init__(self, df: pd.DataFrame | None = None):
        self.segs: dict[tuple, list] = {}
        if df is not None:
            for r in df.sort_values("t0").itertuples():
                self.segs.setdefault((r.cam, int(r.track)), []).append(
                    [r.t0, r.t1, r.cow_id if isinstance(r.cow_id, str) else "", r.source, r.score])

    def cow(self, cam: str, track: int, t: float) -> str:
        for t0, t1, cow, *_ in self.segs.get((cam, int(track)), []):
            if t0 - 1 <= t <= t1 + 1:
                return cow
        return ""

    def set(self, cam: str, track: int, a: float, b: float, cow: str, source: str, score: float = 1.0) -> None:
        """Relabel [a, b] of a track, splitting existing segments around it."""
        existing = self.segs.get((cam, int(track)), [])
        lo = min((s[0] for s in existing), default=a)  # never extend a label beyond the track's own span
        hi = max((s[1] for s in existing), default=b)
        out = []
        for t0, t1, c, s, sc in existing:
            if t1 < a or t0 > b:
                out.append([t0, t1, c, s, sc])
                continue
            if t0 < a:
                out.append([t0, a, c, s, sc])
            if t1 > b:
                out.append([b, t1, c, s, sc])
        out.append([max(a, lo), min(b, hi), cow, source, score])
        self.segs[(cam, int(track))] = sorted(out)

    def frame(self) -> pd.DataFrame:
        rows = [(cam, tr, *s) for (cam, tr), segs in self.segs.items() for s in segs]
        return pd.DataFrame(rows, columns=["cam", "track", "t0", "t1", "cow_id", "source", "score"])

    def apply(self, dets: pd.DataFrame) -> pd.Series:
        """Cow ID for every detection row ('' when unknown)."""
        out = pd.Series("", index=dets.index, dtype=object)
        for (cam, track), g in dets.groupby(["cam", "track"]):
            for t0, t1, cow, *_ in self.segs.get((cam, int(track)), []):
                out.loc[g.index[(g.t >= t0 - 1) & (g.t <= t1 + 1)]] = cow
        return out


class Pen:
    def __init__(self, cfg: dict, dets: pd.DataFrame, frames, full=None, labels: Labels | None = None,
                 gallery: dict | None = None):
        self.cfg, self.frames, self.full = cfg, frames, full
        self.labels = labels or Labels()
        self.gallery = gallery or {"cows": {}, "refs": []}
        self.cams = sorted(dets.cam.unique())
        self.empty = dets.iloc[[]]
        self.dets = {cam: g.reset_index(drop=True) for cam, g in dets.groupby("cam")}
        self.index = {cam: g.groupby("t").indices for cam, g in self.dets.items()}
        self.times = {cam: np.array(sorted(ix)) for cam, ix in self.index.items()}
        self.hw = {}

    # ---------------------------------------------------------------- data
    def boxes_at(self, cam: str, t: float, tol: float = 2.0) -> pd.DataFrame:
        ts = self.times.get(cam)
        if ts is None or not len(ts):
            return self.empty
        i = int(np.searchsorted(ts, t))
        j = min((k for k in (i - 1, i) if 0 <= k < len(ts)), key=lambda k: abs(ts[k] - t))
        if abs(ts[j] - t) > tol:
            return self.empty
        return self.dets[cam].iloc[self.index[cam][ts[j]]]

    def track_rows(self, cam: str, track: int, t0: float, t1: float) -> pd.DataFrame:
        g = self.dets[cam]
        return g[(g.track == track) & (g.t >= t0) & (g.t <= t1)]

    def aspect(self, cam: str, box) -> float:
        """Box height/width in pixels (lying cows are flatter)."""
        if cam not in self.hw:
            hit = self.frames.nearest(cam, self.times[cam][0], tol=1e9)
            size = Image.open(hit[1]).size if hit else (16, 9)
            self.hw[cam] = size[1] / size[0]
        return (box[3] - box[1]) / max(box[2] - box[0], 1e-6) * self.hw[cam]

    def image(self, cam: str, t: float, prefer_full: bool = False):
        if prefer_full and self.full is not None:
            tf, img = self.full.image(cam, t, tol=4)
            if img is not None:
                return tf, img
        return self.frames.image(cam, t, tol=4)

    def annotated(self, cam: str, t: float, prefer_full: bool = False, width: int = 1024):
        tf, img = self.image(cam, t, prefer_full)
        if img is None:
            return None, None, self.boxes_at(cam, t).iloc[[]]
        boxes = self.boxes_at(cam, tf)
        items = [((r.x1, r.y1, r.x2, r.y2), f"T{r.track} {self.labels.cow(cam, r.track, tf) or '?'}")
                 for r in boxes.itertuples()]
        return tf, annotate(img, items, width, title=f"{cam} {hms(tf)}"), boxes

    def describe_boxes(self, cam: str, t: float, boxes: pd.DataFrame) -> str:
        parts = []
        for r in boxes.itertuples():
            box = (r.x1, r.y1, r.x2, r.y2)
            z = zone_of(self.cfg, cam, box)
            parts.append(f"T{r.track}: label {self.labels.cow(cam, r.track, t) or '?'}, "
                         f"h/w {self.aspect(cam, box):.2f}{', zone ' + z if z else ''}")
        return "; ".join(parts) or "no cows detected"

    # ------------------------------------------------------------- gallery
    def gallery_text(self) -> str:
        return "\n".join(f"{cid}: {c.get('description', '')} (tag: {c.get('tag') or 'unreadable'})"
                         for cid, c in sorted(self.gallery["cows"].items())) or "(gallery is empty)"

    def gallery_sheet(self, cow_ids=None, cam: str | None = None, night: bool | None = None, cell: int = 220):
        refs = [r for r in self.gallery["refs"] if (not cow_ids or r["cow_id"] in cow_ids)]
        if cam:  # prefer this camera's references, fall back to any
            refs = [r for r in refs if r["cam"] == cam] or refs
        if night is not None:
            refs = [r for r in refs if r["night"] == night] or refs
        imgs, caps = [], []
        for r in sorted(refs, key=lambda r: r["cow_id"]):
            _, img = self.image(r["cam"], r["t"], prefer_full=True)
            row = self.boxes_at(r["cam"], r["t"])
            row = row[row.track == r["track"]]
            if img is None or row.empty:
                continue
            b = row.iloc[0]
            imgs.append(crop(img, (b.x1, b.y1, b.x2, b.y2), size=cell * 2))
            caps.append(f"{r['cow_id']} {r['cam']} {'night' if r['night'] else 'day'}")
        return tile(imgs, cols=min(5, max(1, len(imgs))), cell=cell, captions=caps) if imgs else None

    # --------------------------------------------------------------- tools
    def tools(self, cam: str, t: float) -> dict:
        """Agent tools anchored at camera `cam`, time `t`."""

        def zoom(track, offset_s=0, **_):
            tq = t + float(offset_s)
            tf, img = self.image(cam, tq, prefer_full=True)
            row = self.boxes_at(cam, tf if tf else tq)
            row = row[row.track == int(track)]
            if img is None or row.empty:
                return f"T{track} is not visible in {cam} at {hms(tq)}.", []
            b = row.iloc[0]
            box = (b.x1, b.y1, b.x2, b.y2)
            z = zone_of(self.cfg, cam, box)
            return (f"T{track} in {cam} at {hms(tf)}: label {self.labels.cow(cam, int(track), tf) or '?'}, "
                    f"h/w {self.aspect(cam, box):.2f}{', zone ' + z if z else ''}."), [crop(img, box, size=640)]

        def look(offset_s=0, camera=None, **_):
            c = camera if camera and camera != "current" else cam  # the tool spec says camera defaults to "current"
            tf, img, boxes = self.annotated(c, t + float(offset_s))
            if img is None:
                return f"No frame for {c} near {hms(t + float(offset_s))}.", []
            return f"{c} at {hms(tf)}: {self.describe_boxes(c, tf, boxes)}", [img]

        def other_cameras(offset_s=0, **_):
            imgs, texts = [], []
            for c in self.cams:
                if c == cam:
                    continue
                tf, img, boxes = self.annotated(c, t + float(offset_s), width=768)
                if img is not None:
                    imgs.append(img)
                    texts.append(f"{c}: {self.describe_boxes(c, tf, boxes)}")
            return ("\n".join(texts) or "No other camera has a frame then."), ([tile(imgs, cell=768)] if imgs else [])

        def reference(cow_ids=None, **_):
            sheet = self.gallery_sheet(cow_ids, cam=cam)
            return self.gallery_text(), ([sheet] if sheet else [])

        def track_history(track, minutes=10, **_):
            rows = self.track_rows(cam, int(track), t - 60 * float(minutes), t + 60 * float(minutes))
            if rows.empty:
                return f"No history for T{track} in {cam} within ±{minutes} min.", []
            lines = []
            for m, g in rows.groupby((rows.t // 60) * 60):
                b = g.iloc[len(g) // 2]
                box = (b.x1, b.y1, b.x2, b.y2)
                cx = ((g.x1 + g.x2) / 2).to_numpy()
                cy = ((g.y1 + g.y2) / 2).to_numpy()
                move = float(np.hypot(cx[-1] - cx[0], cy[-1] - cy[0]) / max(np.sqrt((b.x2 - b.x1) * (b.y2 - b.y1)), 1e-3))
                lines.append(f"{hms(m)[11:16]} label {self.labels.cow(cam, int(track), m + 30) or '?'}, "
                             f"h/w {self.aspect(cam, box):.2f}, moved {move:.1f} body-lengths, "
                             f"zone {zone_of(self.cfg, cam, box) or '-'}")
            return "\n".join(lines), []

        return {
            "zoom": ("args {track:int, offset_s:int=0}. Close-up of one tracked cow (read ear tags, head, ears, nose).", zoom),
            "look": ("args {offset_s:int=0, camera:str=current}. Whole annotated frame at another time and/or camera.", look),
            "other_cameras": ("args {offset_s:int=0}. The other cameras at the same moment.", other_cameras),
            "reference": ("args {cow_ids:list=all}. Reference crops and descriptions of the known cows C01..C10.", reference),
            "track_history": ("args {track:int, minutes:int=10}. Per-minute label, posture (h/w), movement and zone of a track.", track_history),
        }


def load_gallery(cfg: dict) -> dict:
    f = cfg["work_dir"] / "gallery" / "gallery.json"
    return json.loads(f.read_text()) if f.exists() else {"cows": {}, "refs": []}


def load_labels(cfg: dict, name: str = "segments.csv") -> Labels:
    f = cfg["work_dir"] / "ids" / name
    return Labels(pd.read_csv(f)) if f.exists() else Labels()


def save_labels(cfg: dict, labels: Labels, name: str = "segments.csv") -> Path:
    f = cfg["work_dir"] / "ids" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    labels.frame().to_csv(f, index=False)
    return f
