"""End-to-end plumbing test on synthetic video, with no GPU and no model server.

    python tests/smoke_test.py [work_dir]

Makes 2 days x 3 cameras of short fake clips (3 "cows" = blocks of distinct brightness, day in colour
and night in greyscale), then runs every stage with stand-ins: a brightness-band detector for YOLO,
a brightness-histogram embedder for DINOv2, and a scripted VLM that still drives the real agent loop
(tool calls, self-checks, traces). It checks plumbing and file formats, not model quality.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

W, H, FPS = 640, 360, 5
LUMA_COLOURS = {1: (40, 20, 20), 2: (120, 190, 230), 3: (250, 240, 170)}  # luminance ~25, ~174, ~235
BANDS = {1: (0, 60), 2: (150, 205), 3: (215, 256)}


def cow_boxes(day: int, t: float):
    """Pixel boxes per cow. Cow 3 stands on day 1 and lies (flat box) on day 2; cow 2 is at the bunk."""
    x = 40 + (t * 4) % 480
    boxes = {1: (x, 250, x + 70, 320), 2: (300, 30, 360, 85)}
    boxes[3] = (450, 150, 510, 230) if day == 1 else (430, 190, 520, 230)
    return boxes


def make_clip(path: Path, day: int, night: bool, seconds: int = 90) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="avi") as out:
        s = out.add_stream("mpeg4", rate=FPS)
        s.width, s.height, s.pix_fmt = W, H, "yuv420p"
        for i in range(seconds * FPS):
            img = np.zeros((H, W, 3), np.uint8)
            img[:] = (60, 140, 60)
            for cow, (x1, y1, x2, y2) in cow_boxes(day, i / FPS).items():
                img[int(y1):int(y2), int(x1):int(x2)] = LUMA_COLOURS[cow]
            if night:
                img = np.repeat((img @ [0.299, 0.587, 0.114]).astype(np.uint8)[..., None], 3, axis=2)
            for p in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(p)
        for p in s.encode():
            out.mux(p)


class FakeDetector:
    def __init__(self, cfg):
        pass

    def reset(self):
        pass

    def __call__(self, img):
        g = np.asarray(img.convert("L"), dtype=float)
        out = []
        for cow, (lo, hi) in BANDS.items():
            ys, xs = np.where((g >= lo) & (g < hi))
            if len(xs) > 200:
                out.append((cow, [xs.min() / g.shape[1], ys.min() / g.shape[0], (xs.max() + 1) / g.shape[1],
                                  (ys.max() + 1) / g.shape[0]], 0.9))
        return out


class FakeEmbedder:
    def __init__(self, name):
        pass

    def __call__(self, images):
        vs = []
        for im in images:
            g = np.asarray(im.convert("L"), dtype=float)
            h, w = g.shape
            centre = g[h // 4: 3 * h // 4, w // 4: 3 * w // 4]
            v = np.histogram(centre, bins=8, range=(0, 256))[0].astype(float) + 1e-3
            vs.append(v / np.linalg.norm(v))
        return np.stack(vs)


def fake_chat(self, messages, schema=None, max_tokens=700):
    """Scripted stand-in for the VLM: looks first, then answers from what the tools reported."""
    text = " ".join(p["text"] for m in messages for p in (m["content"] if isinstance(m["content"], list)
                                                          else [{"type": "text", "text": m["content"]}])
                    if p["type"] == "text")
    task = messages[1]["content"][0]["text"] if len(messages) > 1 and isinstance(messages[1]["content"], list) else text
    props = (schema or {}).get("properties", {})
    if "action" in props:  # agent step
        looked = "Observation from look" in text or "Observation from track_history" in text
        if "Decide her main behaviour" in task:
            if not looked:
                return {"thought": "check her recent posture", "action": "track_history", "args": {"track": 2, "minutes": 2}}
            return {"thought": "head in the bunk the whole minute", "action": "final",
                    "args": {"behavior": "feeding", "confidence": 0.9, "signs": [], "evidence": "head at bunk"}}
        if not looked:
            return {"thought": "see all tracks", "action": "look", "args": {"offset_s": 0}}
        obs = next(p["text"] for m in reversed(messages) if m["role"] == "user"
                   for p in m["content"] if p["type"] == "text" and "Observation from look" in p["text"])
        tracks = sorted({int(t) for t in re.findall(r"T(\d+):", obs)})
        if "Check every label" in task:
            stubborn = "cam03" in task and "night" in task  # always duplicates an ID: exercises failed_check
            first_try = "Self-check failed" not in text
            if stubborn or first_try:
                return {"thought": "first guess", "action": "final",
                        "args": {"checked": [{"track": t, "cow_id": "C01", "status": "confirmed"} for t in tracks]}}
            return {"thought": "fixed the duplicate", "action": "final",
                    "args": {"checked": [{"track": t, "cow_id": f"C{t % 100000:02d}", "status": "corrected"}
                                         for t in tracks], "notes": "ok"}}
        return {"thought": "distinct brightness", "action": "final",
                "args": {"cows": [{"track": t, "cow_id": f"C{t % 100000:02d}", "description": f"block {t % 100000}",
                                   "tag": ""} for t in tracks]}}
    if "behavior" in props:  # fast per-minute look
        b = "lying" if "low/flat" in text else ("feeding" if "zone: feed_bunk" in text else "standing")
        signs = ["drooping_ears"] if ("cow C03" in text and b == "lying") else []
        conf = 0.5 if "cow C02" in text else 0.8  # low confidence escalates C02's minutes to the agent
        return {"behavior": b, "confidence": conf, "signs": signs, "evidence": "fake"}
    if "signs" in props:  # live sign check
        return {"signs": [{"sign": "drooping_ears", "confidence": 0.8}], "evidence": "fake"}
    raise AssertionError(f"unexpected schema {schema}")


def run(module, *args):
    sys.argv = [module.__name__, *args]
    module.main()


def main() -> None:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="brd_smoke_"))
    shutil.rmtree(work, ignore_errors=True)
    raw = work / "raw"
    for day, date in ((1, "20261107"), (2, "20261108")):
        for cam in (1, 2, 3):
            for hhmm, night in (("1000", False), ("2200", True)):
                make_clip(raw / f"Day {day}" / f"Cam 0{cam}" / f"Event{date}{hhmm}00006.Avi", day, night)
    live = work / "live" / "Cam 01" / "Event20261108103000006.Avi"
    make_clip(live, 2, False, seconds=70)

    cfg = json.loads((ROOT / "config.json").read_text())
    cfg.update(n_cows=3, checkpoint_every_min=1, vlm_workers=4, yolo_weights="unused",
               zones={c: {"feed_bunk": [[0, 0], [1, 0], [1, 0.27], [0, 0.27]]} for c in ("cam01", "cam02", "cam03")})
    (work / "config.json").write_text(json.dumps(cfg))
    os.environ.update(BRD=str(work), BRD_CONFIG=str(work / "config.json"))

    from brd import behavior, identify, probe, report, risk, track, vlm, watch
    track.Detector = FakeDetector
    identify.Embedder = FakeEmbedder
    vlm.VLM.chat = fake_chat
    vlm.VLM._served_model = lambda self: "fake"
    for m in (identify, behavior, watch):
        m.wait_for_server = lambda *a, **k: None

    run(probe)
    for i in range(6):
        run(track, "--task", str(i))
    run(identify, "all")
    run(behavior)
    run(risk)
    run(report)
    run(watch, "--clip", str(live))

    out = work / "outputs"
    expect = ["level1/activity.csv", "level1/activity_minutes.csv", "level1/time_budgets.csv", "level1/time_budgets.png",
              "level1/gallery_sheet.jpg", "level1/summary.json", "level2/brd_risk.csv", "level2/features.csv",
              "level3/live_risk.csv", "level3/alerts.jsonl", "probe/contact_day1.jpg"]
    missing = [e for e in expect if not (out / e).exists()]
    assert not missing, f"missing outputs: {missing}"
    import pandas as pd
    act = pd.read_csv(out / "level1/activity.csv")
    assert set(act.cow_id) == {"C01", "C02", "C03"}, act.cow_id.unique()
    assert act.behavior.isin(json.loads(json.dumps(__import__("brd.common").common.BEHAVIORS))).all()
    assert list((out / "level1/id_check").glob("*/*.jpg")), "no ID-check frames"
    seg = pd.read_csv(work / "work/ids/segments.csv").dropna(subset=["cow_id"])
    # load_dets renumbers tracks 1..N per camera; map back to the raw ids, whose last digits are the true cow.
    raw = pd.concat([pd.read_csv(f) for f in sorted((work / "work/dets").glob("*.csv.gz"))])
    raw["short"] = raw.groupby("cam").track.rank(method="dense").astype(int)
    orig = raw.drop_duplicates(["cam", "short"]).set_index(["cam", "short"]).track
    truth = [f"C{orig[(c, k)] % 100000:02d}" for c, k in zip(seg.cam, seg.track)]
    id_ok = (seg.cow_id == truth).mean()
    print(f"ID labels matching the synthetic truth: {id_ok:.0%}")
    assert id_ok >= 0.9, seg.to_string()
    mins = pd.read_csv(out / "level1/activity_minutes.csv")
    assert (mins[mins.cow == "C02"].behavior == "feeding").mean() >= 0.5, "agent escalation should fix C02 to feeding"
    assert (mins[(mins.cow == "C03") & (mins.day == "day2")].behavior == "lying").mean() >= 0.5
    assert list((out / "level1/agent_traces").glob("*/trace.md")), "no agent traces"
    rk = pd.read_csv(out / "level2/brd_risk.csv")
    assert rk.risk_score.between(0, 1).all() and sorted(rk["rank"]) == [1, 2, 3]
    print("\nbrd_risk.csv:\n", rk[["cow_id", "risk_score", "rank", "tier", "top_reasons"]].to_string(index=False))
    assert rk.iloc[0].cow_id == "C03", "the cow that started lying should rank first"
    assert list((out / "level2/risk_cards").glob("*/card.html")), "no risk cards"
    assert (out / "level3/alerts.jsonl").read_text().strip(), "watch raised no alert"
    print(f"\nSMOKE TEST PASSED. Outputs in {out}")


if __name__ == "__main__":
    main()
