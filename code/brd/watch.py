"""Level 3: live early warning. Watches new video, updates each cow's risk, and raises alerts with evidence.

    python -m brd.watch --clip "/path/Day 2/Cam 01/Event20261108140000006.Avi"
    python -m brd.watch --follow /path/to/incoming --cam cam01      # keep watching a folder

For each new clip: track the cows, identify them against the gallery (embeddings + one agent
audit), score each cow-minute, take a close look for clinical signs (drooping ears, nasal discharge,
lowered head, open-mouth breathing), and compare her behaviour with her own Level 1 baseline at
the same time of day. Risk is updated from her Level 2 score (or her last live score). An alert
fires when the clip adds evidence (a reason) and risk is at or above the threshold, or a sign is seen with confidence; it carries a snapshot and
a short clip. Set ALERT_WEBHOOK to also POST each alert as JSON (e.g. a Slack/Teams/ntfy webhook).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import behavior, identify, track
from .common import (SIGNS, Frames, annotate, cam_key, clip_start, crop, hms, load_config, to_s, tile,
                     write_json)
from .pen import Pen, load_gallery
from .risk import cut_clip
from .vlm import VLM, user, wait_for_server

SIGN_SCHEMA = {"type": "object",
               "properties": {"signs": {"type": "array", "items": {
                   "type": "object", "properties": {"sign": {"type": "string", "enum": SIGNS},
                                                     "confidence": {"type": "number"}},
                   "required": ["sign", "confidence"]}},
                   "evidence": {"type": "string"}},
               "required": ["signs", "evidence"]}


def sign_check(pen: Pen, vlm: VLM, cam: str, cow: str, rows: pd.DataFrame) -> dict:
    """Close look (full-resolution crops) at one cow for visible clinical signs."""
    crops = []
    for t in rows.t.iloc[np.linspace(0, len(rows) - 1, min(3, len(rows))).astype(int)]:
        tf, img = pen.image(cam, t, prefer_full=True)
        b = rows.iloc[(rows.t - t).abs().argmin()]
        if img is not None:
            crops.append(crop(img, (b.x1, b.y1, b.x2, b.y2), pad=0.1, size=512))
    if not crops:
        return {"signs": [], "evidence": "not visible"}
    prompt = ("Close-ups of one Angus cow. Look carefully at ears, eyes, nose, mouth, head carriage and flank. "
              f"Report only signs you can actually see, each with a confidence 0-1, among: {', '.join(SIGNS)}. "
              "Empty list if none. JSON: {signs: [{sign, confidence}], evidence}.")
    try:
        return vlm.chat([user(prompt, [tile(crops, cell=512)])], schema=SIGN_SCHEMA, max_tokens=250)
    except RuntimeError as e:
        return {"signs": [], "evidence": f"check failed: {e}"}


def baseline(grid: pd.DataFrame, cow: str, hours: set) -> dict:
    g = behavior_visible(grid[(grid.cow == cow) & grid.hour.isin(hours)])
    if g.empty:
        return {"feeding": np.nan, "lying": np.nan}
    return {"feeding": (g.behavior == "feeding").mean(), "lying": g.behavior.isin(["lying", "ruminating_lying"]).mean()}


def behavior_visible(g: pd.DataFrame) -> pd.DataFrame:
    return g[~g.source.isin(["not_visible", "no_frame"]) & (g.behavior != "other_not_visible")]


def process_clip(cfg: dict, vlm: VLM, path: Path, cam: str) -> list[dict]:
    try:
        start = to_s(clip_start(path))
    except ValueError:
        start = time.time() - 300
    work = cfg["work_dir"] / "watch" / path.stem
    c2 = deepcopy(cfg)
    c2["work_dir"], c2["checkpoint_every_min"] = work, 1  # full-resolution frame every minute for sign checks
    (work / "dets").mkdir(parents=True, exist_ok=True)
    clips = pd.DataFrame([dict(path=str(path), day="live", cam=cam, start=start, duration=np.nan)])
    d = track.run(c2, clips, cam, track.Detector(c2), work / "dets" / f"live_{cam}")
    if d.empty:
        print(f"{path.name}: no cows detected")
        return []
    gallery = load_gallery(cfg)
    pen = Pen(c2, d, Frames(work / "frames"), Frames(work / "frames_full"), gallery=gallery)

    # Identify: embeddings against the main run's reference tracks, then one agent audit mid-clip.
    main_meta = pd.read_csv(cfg["work_dir"] / "ids_track_meta.csv")
    main_vecs = np.load(cfg["work_dir"] / "ids_track_vecs.npy")
    refs = identify.reference_vectors(gallery, main_meta, main_vecs)
    meta, vecs = identify.embed_tracks(pen, identify.Embedder(cfg["embedder"]), min_dets=3)
    pen.labels = identify.assign(pen, meta, vecs, refs)
    mid = float(d.t.median())
    res = identify.audit_episode(pen, vlm, cam, mid, cfg["n_cows"], work / "traces")
    if res and res[3] == "success":
        identify.apply_audit(pen, cam, res[1], res[2], 3600, cfg["n_cows"])

    obs = behavior.observations(pen)
    minutes = pd.DataFrame([behavior.classify(pen, vlm, r, True, work / "traces") for r in obs.itertuples()])
    dets = d.assign(cow=pen.labels.apply(d))

    grid = pd.read_csv(cfg["out_dir"] / "level1" / "activity_minutes.csv")
    grid["hour"] = pd.to_datetime(grid.time).dt.hour
    live_file = cfg["out_dir"] / "level3" / "live_risk.csv"
    prior = pd.read_csv(cfg["out_dir"] / "level2" / "brd_risk.csv").set_index("cow_id").risk_score.to_dict()
    if live_file.exists():
        prior.update(pd.read_csv(live_file).set_index("cow_id").risk_score.to_dict())
    hours = {pd.Timestamp(hms(start)).hour, pd.Timestamp(hms(start + 300)).hour}
    herd_feeding = (minutes.behavior == "feeding").mean() if len(minutes) else 0.0
    alerts, live = [], []
    out = cfg["out_dir"] / "level3" / "alerts" / path.stem
    for cow, m in minutes.groupby("cow"):
        m = behavior_visible(m.assign(source=m.source.fillna("fast")))
        if m.empty:
            continue
        now = {"feeding": (m.behavior == "feeding").mean(), "lying": m.behavior.isin(["lying", "ruminating_lying"]).mean()}
        base = baseline(grid, cow, hours)
        rows = dets[dets.cow == cow]
        signs = sign_check(pen, vlm, cam, cow, rows) if len(rows) else {"signs": []}
        sign_conf = max([s.get("confidence", 0) for s in signs.get("signs", [])] or [0])
        reasons, delta = [], -0.3
        usual = base["feeding"] if pd.notna(base["feeding"]) else 0.0
        # Less than her own usual, or hardly at all while the herd eats (a cow who seldom eats never looks "unusual")
        if herd_feeding >= 0.3 and (now["feeding"] <= 0.1 or now["feeding"] < usual - 0.2):
            delta += 2 * (max(usual, herd_feeding / 2) - now["feeding"])
            reasons.append(f"not at the bunk while {herd_feeding:.0%} of the herd feeds "
                           f"(she feeds {now['feeding']:.0%} of this window vs her usual {usual:.0%})")
        if pd.notna(base["lying"]) and now["lying"] > base["lying"] + 0.2:
            delta += 1.5 * (now["lying"] - base["lying"])
            reasons.append(f"lying {now['lying']:.0%} of this window vs her usual {base['lying']:.0%} at this hour")
        if sign_conf >= 0.5:
            delta += 1.5 * sign_conf
            reasons.append("visible sign(s): " + ", ".join(f"{s['sign'].replace('_', ' ')} ({s['confidence']:.0%})"
                                                           for s in signs["signs"] if s.get("confidence", 0) >= 0.5))
        p0 = float(np.clip(prior.get(cow, 0.2), 0.01, 0.99))
        p1 = float(1 / (1 + np.exp(-(np.log(p0 / (1 - p0)) + delta))))
        live.append(dict(cow_id=cow, risk_score=round(p1, 3), updated=hms(start), clip=path.name))
        # Alert on new evidence in this clip (a reason) with risk at or above the threshold, or on a clear sign.
        # A cow that is high only from Level 2, with nothing new here, stays on the watch list (live_risk.csv).
        if not ((reasons and p1 >= cfg["alert_threshold"]) or sign_conf >= 0.7):
            continue
        out.mkdir(parents=True, exist_ok=True)
        b = rows.iloc[len(rows) // 2]
        tf, img = pen.image(cam, b.t, prefer_full=True)
        snap = None
        if img is not None:
            snap = f"{cow}.jpg"
            tile([annotate(img, [((b.x1, b.y1, b.x2, b.y2), cow)], width=1024),
                  crop(img, (b.x1, b.y1, b.x2, b.y2), size=512)], cell=640).save(out / snap, quality=85)
        clip_name = f"{cow}.mp4" if cut_clip(clips.assign(duration=300.0), cam, b.t, (b.x1, b.y1, b.x2, b.y2),
                                              out / f"{cow}.mp4", seconds=15) else None
        alert = dict(cow_id=cow, camera=cam, time=hms(b.t), risk_before=round(p0, 2), risk_now=round(p1, 2),
                     reasons=reasons or ["risk above threshold"], snapshot=snap, clip=clip_name,
                     message=(f"Check {cow} in pen 6 ({cam}, {hms(b.t)[11:16]}): " + "; ".join(reasons or ["risk above threshold"])
                              + f". Risk {p0:.2f} -> {p1:.2f}. For the herd veterinarian to assess."))
        write_json(out / f"{cow}.json", alert)
        alerts.append(alert)
        print("ALERT:", alert["message"])
        if os.environ.get("ALERT_WEBHOOK"):
            try:
                requests.post(os.environ["ALERT_WEBHOOK"], json={"text": alert["message"], **alert}, timeout=10)
            except requests.RequestException as e:
                print(f"  webhook failed: {e}")
    if live:
        old = pd.read_csv(live_file) if live_file.exists() else pd.DataFrame(columns=["cow_id"])
        new = pd.concat([old[~old.cow_id.isin([x["cow_id"] for x in live])], pd.DataFrame(live)])
        live_file.parent.mkdir(parents=True, exist_ok=True)
        new.sort_values("risk_score", ascending=False).to_csv(live_file, index=False)
    with (cfg["out_dir"] / "level3" / "alerts.jsonl").open("a") as f:
        for a in alerts:
            f.write(json.dumps(a) + "\n")
    print(f"{path.name}: {len(minutes)} cow-minutes scored, {len(alerts)} alert(s)")
    return alerts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", type=Path)
    ap.add_argument("--follow", type=Path, help="folder to watch for new clips")
    ap.add_argument("--cam", help="camera key (default: from the parent folder name, e.g. 'Cam 01' -> cam01)")
    ap.add_argument("--every", type=int, default=30, help="seconds between folder checks")
    a = ap.parse_args()
    cfg = load_config()
    (cfg["out_dir"] / "level3").mkdir(parents=True, exist_ok=True)
    wait_for_server(cfg["vlm_url"])
    vlm = VLM(cfg["vlm_url"], cfg["vlm_model"])
    if a.clip:
        process_clip(cfg, vlm, a.clip, a.cam or cam_key(a.clip.parent.name))
        return
    if not a.follow:
        raise SystemExit("Give --clip or --follow")
    seen = set()
    print(f"Watching {a.follow} every {a.every}s (Ctrl+C to stop)")
    while True:
        for p in sorted(a.follow.rglob("*")):
            if p.suffix.lower() in {".avi", ".mp4"} and p not in seen and time.time() - p.stat().st_mtime > 10:
                seen.add(p)
                process_clip(cfg, vlm, p, a.cam or cam_key(p.parent.name))
        time.sleep(a.every)


if __name__ == "__main__":
    main()
