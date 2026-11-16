"""Behaviour of every cow for every minute of both days (Level 1 outputs).

    python -m brd.behavior                 # all cow-minutes (resumes if interrupted)
    python -m brd.behavior --limit 300     # quick trial on 300 cow-minutes

Fast path: one look per cow-minute (3 crops across the minute from the camera that sees her best)
-> behaviour, confidence, visible clinical signs. A deterministic self-check compares that answer
with tracker evidence (posture h/w, movement, pen zone). Low confidence or disagreement escalates
to the agent, which zooms, looks at other moments/cameras and the track history before deciding.

Outputs (outputs/level1/): activity.csv (one row per behaviour bout, covering every minute),
activity_minutes.csv, time_budgets.csv + .png, and a pool of crops for the accuracy check.
"""
from __future__ import annotations

import argparse
import json
import random
import threading
from concurrent.futures import ThreadPoolExecutor

import matplotlib
import numpy as np
import pandas as pd

from .common import BEHAVIORS, LYING, SIGNS, UPRIGHT, crop, hms, is_night, load_config, load_frames_table, tile, zone_of
from .identify import make_pen
from .pen import Pen, load_labels
from .vlm import VLM, Agent, user, wait_for_server

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

DEFINITIONS = (
    "Classes: lying; standing; walking (moving several steps); feeding (head in or over the feed bunk); "
    "drinking (muzzle at the waterer); ruminating_lying / ruminating_standing (cud chewing: rhythmic jaw "
    "movement or a bolus visible, or the relaxed rumination posture with jaw working); grooming (licking or "
    "scratching herself); social_interaction (licking, sniffing, butting or pushing another cow); "
    "other_not_visible (mostly hidden or too dark to judge). Also report visible clinical signs among "
    f"{', '.join(SIGNS)} (empty list if none).")
SYSTEM = ("You are an experienced cattle behaviour observer scoring Angus cows from pen camera footage. "
          "Night footage is infrared greyscale. Judge only what is visible.")
ANSWER = {"type": "object",
          "properties": {"behavior": {"type": "string", "enum": BEHAVIORS},
                         "confidence": {"type": "number"},
                         "signs": {"type": "array", "items": {"type": "string", "enum": SIGNS}},
                         "evidence": {"type": "string"}},
          "required": ["behavior", "confidence", "signs", "evidence"]}
FINAL_FORMAT = ('Final answer args: {"behavior": "<class>", "confidence": 0-1, "signs": [...], '
                '"evidence": "<= 25 words on what you saw"}')


# ------------------------------------------------------------ observations
def observations(pen: Pen) -> pd.DataFrame:
    """One row per (cow, minute): the best camera's view of her and the tracker evidence."""
    dets = pd.concat(pen.dets.values(), ignore_index=True)
    dets["cow"] = pen.labels.apply(dets)
    dets = dets[dets.cow != ""].copy()
    if dets.empty:
        raise SystemExit("No detections carry a cow ID. Run `python -m brd.identify all` first.")
    dets["minute"] = (dets.t // 60) * 60
    dets["area"] = (dets.x2 - dets.x1) * (dets.y2 - dets.y1)
    dets["cx"], dets["cy"] = (dets.x1 + dets.x2) / 2, (dets.y1 + dets.y2) / 2
    hw = {cam: pen.aspect(cam, (0, 0, 1, 1)) for cam in pen.cams}
    dets["aspect"] = (dets.y2 - dets.y1) / (dets.x2 - dets.x1).clip(lower=1e-6) * dets.cam.map(hw)
    g = dets.sort_values("t").groupby(["cow", "minute", "cam", "track"])
    obs = g.agg(n=("t", "size"), area=("area", "mean"), conf=("conf", "mean"), aspect=("aspect", "median"),
                cx0=("cx", "first"), cy0=("cy", "first"), cx1=("cx", "last"), cy1=("cy", "last"),
                t_mid=("t", "median"), x1=("x1", "median"), y1=("y1", "median"), x2=("x2", "median"),
                y2=("y2", "median")).reset_index()
    obs["move"] = np.hypot(obs.cx1 - obs.cx0, obs.cy1 - obs.cy0) / np.sqrt(obs.area).clip(lower=1e-3)
    ref = obs.groupby(["cow", "cam"]).aspect.quantile(0.9).rename("ref_aspect")
    obs = obs.join(ref, on=["cow", "cam"])
    obs["posture_rel"] = obs.aspect / obs.ref_aspect
    obs["score"] = obs.n * obs.area * obs.conf
    best = obs.sort_values("score", ascending=False).drop_duplicates(["cow", "minute"])
    best["zone"] = [zone_of(pen.cfg, r.cam, (r.x1, r.y1, r.x2, r.y2)) for r in best.itertuples()]
    return best.sort_values(["cow", "minute"]).reset_index(drop=True)


def minute_strip(pen: Pen, r) -> object:
    imgs = []
    for t in np.linspace(r.minute + 8, r.minute + 52, 3):
        tf, img = pen.frames.image(r.cam, t, tol=10)
        if img is None:
            continue
        box = pen.boxes_at(r.cam, tf, tol=1.5)
        box = box[box.track == r.track]
        if not box.empty:
            b = box.iloc[0]
            imgs.append(crop(img, (b.x1, b.y1, b.x2, b.y2), size=360))
    return tile(imgs, cell=360) if imgs else None


def hints(r) -> str:
    posture = "low/flat (lying-like)" if r.posture_rel < 0.72 else "upright"
    return (f"tracker posture {posture} (h/w {r.aspect:.2f}, {r.posture_rel:.0%} of her standing height), "
            f"moved {r.move:.1f} body-lengths this minute, zone: {r.zone or 'none'}")


def rule_problems(ans: dict, r, cfg: dict) -> list[str]:
    b, probs = ans.get("behavior"), []
    if float(ans.get("confidence", 0)) < 0.55:
        probs.append("confidence is low")
    if cfg.get("zones", {}).get(r.cam):
        if b == "feeding" and r.zone != "feed_bunk":
            probs.append("feeding, but the tracker box is not at the feed bunk")
        if b == "drinking" and r.zone != "waterer":
            probs.append("drinking, but the tracker box is not at the waterer")
    if cfg["camera_roles"].get(r.cam) != "top":
        if b in LYING and r.posture_rel > 0.85:
            probs.append("a lying class, but her box is as tall as when she stands")
        if b in UPRIGHT and r.posture_rel < 0.6:
            probs.append("an upright class, but her box is flat like a lying cow")
    if b in LYING and r.move >= 0.2:
        probs.append(f"a lying class, but she moved {r.move:.1f} body-lengths this minute")
    if b == "walking" and r.move < 0.3:
        probs.append("walking, but she barely moved this minute")
    return probs


# ---------------------------------------------------------------- classify
def classify(pen: Pen, vlm: VLM, r, use_agent: bool, trace_dir) -> dict:
    strip = minute_strip(pen, r)
    base = dict(cow=r.cow, minute=r.minute, cam=r.cam, track=int(r.track), zone=r.zone,
                posture_rel=round(float(r.posture_rel), 2), move=round(float(r.move), 2))
    if strip is None:
        return {**base, "behavior": "other_not_visible", "confidence": 0.0, "signs": [], "evidence": "no frame",
                "source": "no_frame", "night": None}
    night = is_night(strip)
    prompt = (f"Three crops of cow {r.cow} about 20 s apart during {hms(r.minute)[11:16]}-{hms(r.minute + 60)[11:16]} "
              f"({'night, infrared' if night else 'daytime'}, camera {r.cam}). Tracker evidence: {hints(r)}.\n"
              f"What is her main behaviour this minute? {DEFINITIONS}\nAnswer as JSON with behavior, confidence "
              "(0-1), signs, evidence.")
    try:
        ans = vlm.chat([{"role": "system", "content": SYSTEM}, user(prompt, [strip])], schema=ANSWER, max_tokens=250)
    except RuntimeError as e:
        ans = {"behavior": "other_not_visible", "confidence": 0.0, "signs": [], "evidence": str(e)[:80]}
    probs = rule_problems(ans, r, pen.cfg)
    if not probs or not use_agent:
        return {**base, **ans, "source": "fast", "night": night}
    task = (f"Cow {r.cow} is track T{int(r.track)} in camera {r.cam}. Decide her main behaviour during "
            f"{hms(r.minute)}-{hms(r.minute + 60)[11:]} (offset_s between -30 and +30 stays inside this minute). "
            f"A quick first look said {ans.get('behavior')} (confidence {ans.get('confidence')}), but: "
            f"{'; '.join(probs)}. Tracker evidence: {hints(r)}. Check with zoom, look, other_cameras or "
            f"track_history before deciding.\n{DEFINITIONS}\n{FINAL_FORMAT}")

    def verify(a):
        if a.get("behavior") not in BEHAVIORS:
            return [f"behavior must be one of {BEHAVIORS}"]
        if float(a.get("confidence", 0)) >= 0.8:  # a confident, investigated answer may overrule the tracker
            return []
        return [p for p in rule_problems(a, r, pen.cfg) if p != "confidence is low"]

    agent = Agent(vlm, SYSTEM, pen.tools(r.cam, r.minute + 30), verify, max_steps=6)
    final, trace = agent.run(task, [strip], {"kind": "behavior", "cow": r.cow, "cam": r.cam, "t": r.minute,
                                             "time": hms(r.minute), "night": night, "first_look": ans},
                             trace_dir / f"behavior_{r.cow}_{int(r.minute)}")
    if final and final.get("behavior") in BEHAVIORS:
        return {**base, **{k: final.get(k, ans.get(k)) for k in ANSWER["required"]}, "source": f"agent_{trace['status']}",
                "night": night}
    return {**base, **ans, "source": "agent_unresolved", "night": night}


# ----------------------------------------------------------------- outputs
def full_grid(cfg: dict, done: pd.DataFrame, cows: list[str]) -> pd.DataFrame:
    frames = load_frames_table(cfg)
    frames["minute"] = (frames.t // 60) * 60
    video = frames.groupby("minute").agg(day=("day", "first"), night=("night", "mean")).reset_index()
    grid = pd.MultiIndex.from_product([cows, video.minute], names=["cow", "minute"]).to_frame(index=False)
    grid = grid.merge(video, on="minute").merge(done.drop(columns=["night"], errors="ignore"), on=["cow", "minute"], how="left")
    grid["night"] = grid.night > 0.5
    miss = grid.behavior.isna()
    grid.loc[miss, ["behavior", "source", "confidence"]] = ["other_not_visible", "not_visible", 0.0]
    # Short gaps between two identical observed behaviours are estimated as that behaviour.
    gap = cfg["fill_gap_min"]
    for cow, g in grid.groupby("cow"):
        obs = g[g.source != "not_visible"]
        for a, b in zip(obs.index[:-1], obs.index[1:]):
            close = grid.at[b, "minute"] - grid.at[a, "minute"] <= (gap + 1) * 60
            if b - a > 1 and close and grid.at[a, "behavior"] == grid.at[b, "behavior"] != "other_not_visible":
                grid.loc[a + 1:b - 1, ["behavior", "source"]] = [grid.at[a, "behavior"], "estimated"]
    grid["time"] = grid.minute.map(hms)
    grid["signs"] = grid.signs.apply(lambda s: ";".join(s) if isinstance(s, list) else "")
    return grid


def bouts(grid: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cow, g in grid.sort_values("minute").groupby("cow"):
        new = (g.behavior != g.behavior.shift()) | (g.minute.diff() != 60)
        for _, b in g.groupby(new.cumsum()):
            rows.append(dict(cow_id=cow, day=b.day.iloc[0], behavior=b.behavior.iloc[0], start=hms(b.minute.iloc[0]),
                             end=hms(b.minute.iloc[-1] + 60), minutes=len(b),
                             observed_minutes=int((~b.source.isin(["not_visible", "estimated", "no_frame"])).sum()),
                             estimated_minutes=int((b.source == "estimated").sum()),
                             mean_confidence=round(float(b.confidence.fillna(0).mean()), 2),
                             cameras=";".join(sorted(b.cam.dropna().unique())), night=bool(b.night.mean() > 0.5)))
    return pd.DataFrame(rows)


def time_budgets(grid: pd.DataFrame, out) -> pd.DataFrame:
    tb = grid.pivot_table(index=["cow", "day"], columns="behavior", values="minute", aggfunc="size", fill_value=0)
    tb = tb.reindex(columns=BEHAVIORS, fill_value=0)
    seen = tb.drop(columns="other_not_visible").sum(axis=1)
    share = tb.drop(columns="other_not_visible").div(seen.clip(lower=1), axis=0)
    hours = (tb / 60).round(2).add_suffix("_h")
    res = hours.join((share * 100).round(1).add_suffix("_pct_of_visible")).assign(visible_h=(seen / 60).round(2))
    res.to_csv(out / "time_budgets.csv")
    fig, ax = plt.subplots(figsize=(12, 0.45 * len(share) + 1.5))
    left = np.zeros(len(share))
    labels = [f"{c} {d}" for c, d in share.index]
    for i, b in enumerate(share.columns):
        ax.barh(labels, share[b], left=left, label=b, color=plt.cm.tab10(i))
        left += share[b].to_numpy()
    ax.set(xlabel="share of time she was visible", xlim=(0, 1), title="Per-cow time budgets")
    ax.invert_yaxis()
    ax.legend(fontsize=8, loc="center left", bbox_to_anchor=(1.01, 0.5))
    fig.tight_layout()
    fig.savefig(out / "time_budgets.png", dpi=130)
    plt.close(fig)
    return res


def label_pool(pen: Pen, done: pd.DataFrame, obs: pd.DataFrame, cfg: dict, n: int = 150) -> None:
    """Random cow-minutes saved as crops for the human accuracy check in the app."""
    pool = cfg["work_dir"] / "label_pool"
    pool.mkdir(parents=True, exist_ok=True)
    rows = obs.merge(done[["cow", "minute", "behavior"]], on=["cow", "minute"])
    meta = []
    for r in rows.sample(min(n, len(rows)), random_state=0).itertuples():
        strip = minute_strip(pen, r)
        if strip is None:
            continue
        name = f"{r.cow}_{int(r.minute)}.jpg"
        strip.save(pool / name, quality=85)
        meta.append(dict(file=name, cow=r.cow, minute=r.minute, time=hms(r.minute), cam=r.cam, predicted=r.behavior))
    (pool / "pool.json").write_text(json.dumps(meta, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only this many cow-minutes (trial run)")
    ap.add_argument("--no-agent", action="store_true", help="fast path only")
    a = ap.parse_args()
    cfg = load_config()
    pen = make_pen(cfg, load_labels(cfg))
    wait_for_server(cfg["vlm_url"])
    vlm = VLM(cfg["vlm_url"], cfg["vlm_model"])
    obs = observations(pen)
    out = cfg["out_dir"] / "level1"
    out.mkdir(parents=True, exist_ok=True)
    store = cfg["work_dir"] / "behavior_minutes.jsonl"
    done_keys = set()
    if store.exists():
        done_keys = {(d["cow"], d["minute"]) for d in map(json.loads, store.read_text().splitlines())}
    todo = [r for r in obs.itertuples() if (r.cow, r.minute) not in done_keys]
    if a.limit:
        todo = random.Random(0).sample(todo, min(a.limit, len(todo)))
    print(f"{len(obs)} observed cow-minutes, {len(todo)} to classify")
    lock, count = threading.Lock(), [0]

    def work(r):
        res = classify(pen, vlm, r, not a.no_agent, cfg["work_dir"] / "traces")
        with lock, store.open("a") as f:
            f.write(json.dumps(res, default=str) + "\n")
            count[0] += 1
            if count[0] % 500 == 0:
                print(f"  {count[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(cfg["vlm_workers"]) as ex:
        list(ex.map(work, todo))
    done = pd.DataFrame(map(json.loads, store.read_text().splitlines())).drop_duplicates(["cow", "minute"], keep="last")
    cows = sorted(set(pen.gallery["cows"]) or set(obs.cow))
    grid = full_grid(cfg, done, cows)
    grid.to_csv(out / "activity_minutes.csv", index=False)
    bouts(grid).to_csv(out / "activity.csv", index=False)
    time_budgets(grid, out)
    label_pool(pen, done, obs, cfg)
    src = grid.source.value_counts().to_dict()
    print(f"Level 1 written to {out}: {src}")


if __name__ == "__main__":
    main()
