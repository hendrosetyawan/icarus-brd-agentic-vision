"""Cow identity across cameras and both days.

    python -m brd.identify gallery   # agent names the cows C01..C10 on each camera, day and night
    python -m brd.identify assign    # appearance embeddings: each track gets its most similar free cow
    python -m brd.identify audit     # agent checks/corrects every label every 30 min; saves ID-check frames
    python -m brd.identify all

After `gallery`, look at outputs/level1/gallery_sheet.jpg. If a cow is wrong, edit
work/gallery/gallery.json (cow_id of a ref) and rerun `assign` and `audit`.
"""
from __future__ import annotations

import argparse
import math
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from .common import Frames, crop, hms, is_night, load_config, load_dets, tile, write_json
from .pen import Labels, Pen, load_gallery, load_labels, save_labels
from .vlm import VLM, Agent, wait_for_server

ID_SYSTEM = ("You are an expert stockperson identifying individual Angus cows in a research pen from "
             "security-camera frames. Night frames are infrared greyscale. Be careful and literal: read ear "
             "tags when visible and never invent a tag number you cannot read.")


class Embedder:
    """DINOv2 image embeddings (L2-normalised). Swapped for a fake in tests."""

    def __init__(self, name: str):
        import torch
        from transformers import AutoImageProcessor, AutoModel
        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.proc = AutoImageProcessor.from_pretrained(name)
        self.model = AutoModel.from_pretrained(name).to(self.device).eval()

    def __call__(self, images) -> np.ndarray:
        with self.torch.no_grad():
            out = self.model(**self.proc(images=images, return_tensors="pt").to(self.device))
        v = out.pooler_output if getattr(out, "pooler_output", None) is not None else out.last_hidden_state[:, 0]
        return self.torch.nn.functional.normalize(v, dim=-1).cpu().numpy()


def valid_id(cid: str, n: int) -> bool:
    m = re.fullmatch(r"C(\d\d)", str(cid))
    return bool(m) and 1 <= int(m.group(1)) <= n


def checkpoints(pen: Pen, cam: str, every_s: float) -> list[float]:
    ts = pen.frames.times.get(cam, [])
    if not ts:
        return []
    return [t for t in np.arange(math.ceil(ts[0] / every_s) * every_s, ts[-1], every_s)
            if pen.frames.nearest(cam, t, tol=10)]


# ------------------------------------------------------------------ gallery
def track_id(v) -> int:
    """Track number from the model's answer: 2732, "2732" or "T2732"; -1 if none."""
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    return int(digits) if digits else -1


def gallery_episode(pen: Pen, vlm: VLM, cam: str, t: float, n: int, trace_dir):
    tf, img, boxes = pen.annotated(cam, t, prefer_full=True)
    night = is_night(img)
    known = bool(pen.gallery["cows"])
    task = (f"{'Night infrared' if night else 'Daytime'} frame from camera {cam} at {hms(tf)}. Tracked animals "
            f"are labelled T<number>. The pen holds {n} Angus cows.\n")
    if not known:
        task += (f"Give each visible cow a permanent ID from C01 to C{n:02d} and describe how to recognise her "
                 "again from any camera: ear tag colour/number and which ear, white markings, size and body "
                 "shape, head shape, anything distinctive. The second image has a close-up of every track; "
                 "zoom only where a tag or marking needs a closer look, then answer.")
    else:
        task += (f"Known cows so far:\n{pen.gallery_text()}\nMatch each visible cow to these IDs (use the "
                 f"reference tool to see them). Use a new ID (next free number, at most C{n:02d}) only if she "
                 "matches none of them.")
    task += ('\nSkip tracks that are not cows or are duplicate boxes. Final answer args: {"cows": [{"track": 5, '
             '"cow_id": "C01", "description": "...", "tag": "yellow tag 512, left ear"}]}')
    tracks = set(boxes.track.astype(int))

    def verify(ans):
        cows = ans.get("cows") or []
        probs = [f"T{c.get('track')} is not in this frame." for c in cows if track_id(c.get("track")) not in tracks]
        probs += [f"{c.get('cow_id')} is not a valid ID (C01..C{n:02d})." for c in cows if not valid_id(c.get("cow_id"), n)]
        ids = [c.get("cow_id") for c in cows]
        probs += [f"{cid} is used for more than one track." for cid in set(ids) if ids.count(cid) > 1]
        if len(set(pen.gallery["cows"]) | set(ids)) > n:
            probs.append(f"That would make more than {n} cows in total; some must be cows already known.")
        return probs

    sheet = pen.gallery_sheet(cam=cam) if known else None
    # Close-ups of every track up front, so the agent needn't spend one step per cow zooming (it has ~10 cows).
    _, full = pen.image(cam, tf, prefer_full=True)
    rows = list(boxes.itertuples())
    closeups = tile([crop(full, (r.x1, r.y1, r.x2, r.y2), size=320) for r in rows], cols=min(5, len(rows)),
                    cell=320, captions=[f"T{int(r.track)}" for r in rows]) if rows else None
    images = [img] + ([closeups] if closeups else []) + ([sheet] if sheet else [])
    agent = Agent(vlm, ID_SYSTEM, pen.tools(cam, tf), verify, max_steps=12)
    ans, trace = agent.run(task, images, {"kind": "gallery", "cam": cam, "t": tf, "time": hms(tf), "night": night},
                           trace_dir / f"gallery_{cam}_{'night' if night else 'day'}")
    for c in (ans or {}).get("cows", []) if trace["status"] == "success" else []:
        if valid_id(c.get("cow_id"), n) and track_id(c.get("track")) in tracks:
            pen.gallery["cows"].setdefault(c["cow_id"], {"description": c.get("description", ""), "tag": c.get("tag", "")})
            pen.gallery["refs"].append({"cow_id": c["cow_id"], "cam": cam, "t": tf, "track": track_id(c["track"]), "night": night})
    return trace["status"]


def pick_time(pen: Pen, cam: str, want_night: bool, every_s: float):
    best, best_score = None, -1
    for t in checkpoints(pen, cam, every_s):
        boxes = pen.boxes_at(cam, t)
        if boxes.empty:
            continue
        _, img = pen.frames.image(cam, t)
        if img is None or is_night(img) != want_night:
            continue
        area = float(((boxes.x2 - boxes.x1) * (boxes.y2 - boxes.y1)).mean())
        score = min(len(boxes), pen.cfg["n_cows"]) + area
        if score > best_score:
            best, best_score = t, score
    return best


def run_gallery(cfg, pen, vlm):
    trace_dir = cfg["work_dir"] / "traces"
    every = cfg["checkpoint_every_min"] * 60
    for night in (False, True):
        for cam in pen.cams:
            t = pick_time(pen, cam, night, every)
            if t is None:
                print(f"  no {'night' if night else 'day'} frame with cows on {cam}")
                continue
            status = gallery_episode(pen, vlm, cam, t, cfg["n_cows"], trace_dir)
            print(f"  gallery {cam} {'night' if night else 'day'} at {hms(t)}: {status}, "
                  f"{len(pen.gallery['cows'])} cows known")
    write_json(cfg["work_dir"] / "gallery" / "gallery.json", pen.gallery)
    sheet = pen.gallery_sheet()
    if sheet:
        (cfg["out_dir"] / "level1").mkdir(parents=True, exist_ok=True)
        sheet.save(cfg["out_dir"] / "level1" / "gallery_sheet.jpg", quality=90)
    print(f"Gallery: {sorted(pen.gallery['cows'])} -> check outputs/level1/gallery_sheet.jpg")


# --------------------------------------------------------- embed + assign
def embed_tracks(pen: Pen, embedder, per_track: int = 8, min_dets: int = 5):
    jobs = []  # (cam, t_frame, track, box)
    for cam, g in pen.dets.items():
        stored = np.array(pen.frames.times.get(cam, []))
        if len(stored) < 2:
            continue
        for track, gt in g.groupby("track"):
            if len(gt) < min_dets:
                continue
            j = np.clip(np.searchsorted(stored, gt.t.to_numpy()), 1, len(stored) - 1)
            near = np.where(np.abs(stored[j] - gt.t.to_numpy()) < np.abs(stored[j - 1] - gt.t.to_numpy()), j, j - 1)
            ok = np.abs(stored[near] - gt.t.to_numpy()) <= 0.6
            cand = gt[ok].assign(tf=stored[near][ok])
            if cand.empty:
                continue
            cand = cand.sort_values("conf", ascending=False).drop_duplicates("tf").sort_values("t")
            pick = cand.iloc[np.linspace(0, len(cand) - 1, min(per_track, len(cand))).astype(int)]
            jobs += [(cam, r.tf, int(track), (r.x1, r.y1, r.x2, r.y2)) for r in pick.itertuples()]
    jobs.sort(key=lambda j: (j[0], j[1]))
    print(f"  embedding {len(jobs)} crops")

    def load(group):
        (cam, tf), items = group
        _, img = pen.frames.image(cam, tf, tol=1)
        return [(cam, tr, crop(img, box, pad=0.05, size=224)) for _, _, tr, box in items] if img else []

    groups = {}
    for j in jobs:
        groups.setdefault((j[0], j[1]), []).append(j)
    crops = []
    with ThreadPoolExecutor(16) as ex:
        for part in ex.map(load, groups.items()):
            crops += part
    vecs, keys, nights = [], [], []
    for i in range(0, len(crops), 64):
        batch = crops[i:i + 64]
        vecs.append(embedder([c[2] for c in batch]))
        keys += [(c[0], c[1]) for c in batch]
        nights += [is_night(c[2]) for c in batch]
    if not vecs:
        raise SystemExit("No crops to embed: are frames and detections in work/?")
    vecs = np.concatenate(vecs)
    df = pd.DataFrame(keys, columns=["cam", "track"]).assign(night=nights)
    span = {cam: g.groupby("track").t.agg(["min", "max", "count"]) for cam, g in pen.dets.items()}
    meta, out = [], []
    for (cam, track), g in df.groupby(["cam", "track"]):
        v = vecs[g.index].mean(0)
        out.append(v / (np.linalg.norm(v) + 1e-9))
        s = span[cam].loc[track]
        meta.append(dict(cam=cam, track=int(track), t0=s["min"], t1=s["max"], n=int(s["count"]),
                         night=bool(g.night.mean() > 0.5)))
    return pd.DataFrame(meta), np.stack(out)


def reference_vectors(gallery: dict, meta: pd.DataFrame, vecs: np.ndarray) -> dict:
    """{cow: [(cam, night, vector)]} from the gallery's reference tracks."""
    key = {(r.cam, r.track): i for i, r in enumerate(meta.itertuples())}
    refs = {}
    for r in gallery["refs"]:
        if (r["cam"], r["track"]) in key:
            refs.setdefault(r["cow_id"], []).append((r["cam"], r["night"], vecs[key[(r["cam"], r["track"])]]))
    if not refs:
        raise SystemExit("Gallery has no usable references; run `python -m brd.identify gallery` first.")
    return refs


def assign(pen: Pen, meta: pd.DataFrame, vecs: np.ndarray, refs: dict, ref_track: dict | None = None) -> Labels:
    """Longest tracks first, each takes the most similar cow not already busy in that camera at that time."""
    key = {(r.cam, r.track): i for i, r in enumerate(meta.itertuples())}
    ref_track = ref_track or {}
    labels = Labels()
    for cam in pen.cams:
        busy = {cow: [] for cow in refs}
        rows = meta[meta.cam == cam].sort_values("n", ascending=False)
        for r in rows.itertuples():
            v = vecs[key[(cam, r.track)]]
            sims = {}
            for cow, rs in refs.items():
                same = [x for x in rs if x[0] == cam and x[1] == r.night] or [x for x in rs if x[0] == cam] or rs
                sims[cow] = max(float(v @ x[2]) for x in same)
            order = sorted(sims, key=sims.get, reverse=True)
            if (cam, r.track) in ref_track:
                order = [ref_track[(cam, r.track)]] + order
            cow = next((c for c in order if not any(a < r.t1 - 2 and r.t0 + 2 < b for a, b in busy[c])), "")
            if cow:
                busy[cow].append((r.t0, r.t1))
            source = "gallery" if (cam, r.track) in ref_track else "embedding"
            labels.set(cam, r.track, r.t0, r.t1, cow, source, round(sims.get(cow, 0.0), 3))
    return labels


# -------------------------------------------------------------------- audit
def audit_episode(pen: Pen, vlm: VLM, cam: str, t: float, n: int, trace_dir):
    tf, img, boxes = pen.annotated(cam, t, prefer_full=True)
    if img is None or boxes.empty:
        return None
    night = is_night(img)
    tracks = set(boxes.track.astype(int))
    task = (f"Camera {cam} at {hms(tf)} ({'night, infrared' if night else 'daytime'}). Each animal is labelled "
            f"'T<track> <cow ID>' by the tracker ('?' = unknown).\nReference cows:\n{pen.gallery_text()}\n"
            "Check every label. Zoom on any cow you are unsure about and compare with the reference crops; look "
            "a few minutes back or forward, or at the other cameras, when a cow is hidden or ambiguous. A cow ID "
            'can appear only once in this frame. Final answer args: {"checked": [{"track": 12, "cow_id": "C03", '
            '"status": "confirmed" | "corrected" | "unsure"}], "notes": "..."}. Include every track; use cow_id '
            '"none" for a box that is not a cow.')

    def verify(ans):
        items = ans.get("checked") or []
        seen = {track_id(i.get("track")) for i in items}
        probs = [f"T{tr} is missing from your answer." for tr in sorted(tracks - seen)]
        probs += [f"{i.get('cow_id')} is not a valid ID." for i in items
                  if i.get("cow_id") not in ("none", "unknown") and not valid_id(i.get("cow_id"), n)]
        ids = [i.get("cow_id") for i in items if valid_id(i.get("cow_id"), n)]
        probs += [f"{cid} is given to more than one track; one cow cannot be in two places." for cid in set(ids)
                  if ids.count(cid) > 1]
        return probs

    sheet = pen.gallery_sheet(cam=cam, night=night)
    agent = Agent(vlm, ID_SYSTEM, pen.tools(cam, tf), verify)
    ans, trace = agent.run(task, [img] + ([sheet] if sheet else []),
                           {"kind": "audit", "cam": cam, "t": tf, "time": hms(tf), "night": night},
                           trace_dir / f"audit_{cam}_{int(tf)}")
    return cam, tf, ans, trace["status"]


def apply_audit(pen: Pen, cam: str, tf: float, ans: dict | None, half_window: float, n: int) -> list[str]:
    """Write the agent's confirmed/corrected IDs onto each track for ±half_window around the checkpoint."""
    statuses = []
    for item in (ans or {}).get("checked", []):
        cid, st = item.get("cow_id"), item.get("status", "unsure")
        statuses.append(st)
        if st == "unsure":
            continue
        cow = cid if valid_id(cid, n) else ""
        pen.labels.set(cam, track_id(item.get("track")), tf - half_window, tf + half_window, cow, f"audit_{st}")
    return statuses


def run_audit(cfg, pen: Pen, vlm: VLM) -> None:
    every = cfg["checkpoint_every_min"] * 60
    todo = [(cam, t) for cam in pen.cams for t in checkpoints(pen, cam, every)]
    print(f"  {len(todo)} audits")
    with ThreadPoolExecutor(cfg["vlm_workers"]) as ex:
        results = [r for r in ex.map(lambda ct: audit_episode(pen, vlm, *ct, cfg["n_cows"],
                                                              cfg["work_dir"] / "traces"), todo) if r]
    stats = {"success": 0, "failed_check": 0, "unresolved": 0, "corrected": 0, "confirmed": 0, "unsure": 0}
    for cam, tf, ans, status in sorted(results, key=lambda r: r[1]):
        stats[status] = stats.get(status, 0) + 1
        if status != "success":  # an answer that failed its self-check is not trusted; keep the previous labels
            continue
        for st in apply_audit(pen, cam, tf, ans, every / 2, cfg["n_cows"]):
            stats[st] = stats.get(st, 0) + 1
    save_labels(cfg, pen.labels)
    write_json(cfg["out_dir"] / "level1" / "audit_stats.json", stats)
    print(f"  audit: {stats}")
    out = cfg["out_dir"] / "level1" / "id_check"
    for cam, t in todo:
        tf, img, _ = pen.annotated(cam, t, prefer_full=True, width=1280)
        if img is not None:
            (out / cam).mkdir(parents=True, exist_ok=True)
            img.save(out / cam / f"{hms(tf)[:16].replace(' ', '_').replace(':', '')}.jpg", quality=85)
    print(f"  ID-check frames -> {out}")


def make_pen(cfg, labels=None) -> Pen:
    return Pen(cfg, load_dets(cfg), Frames(cfg["work_dir"] / "frames"), Frames(cfg["work_dir"] / "frames_full"),
               labels, load_gallery(cfg))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["gallery", "assign", "audit", "all"])
    a = ap.parse_args()
    cfg = load_config()
    steps = ["gallery", "assign", "audit"] if a.step == "all" else [a.step]
    pen = make_pen(cfg, load_labels(cfg))
    vlm = None
    if {"gallery", "audit"} & set(steps):
        wait_for_server(cfg["vlm_url"])
        vlm = VLM(cfg["vlm_url"], cfg["vlm_model"])
    if "gallery" in steps:
        run_gallery(cfg, pen, vlm)
    if "assign" in steps:
        meta, vecs = embed_tracks(pen, Embedder(cfg["embedder"]))
        np.save(cfg["work_dir"] / "ids_track_vecs.npy", vecs)
        meta.to_csv(cfg["work_dir"] / "ids_track_meta.csv", index=False)
        ref_track = {(r["cam"], r["track"]): r["cow_id"] for r in pen.gallery["refs"]}
        pen.labels = assign(pen, meta, vecs, reference_vectors(pen.gallery, meta, vecs), ref_track)
        print(f"  assigned: {save_labels(cfg, pen.labels)}")
    if "audit" in steps:
        run_audit(cfg, pen, vlm)


if __name__ == "__main__":
    main()
