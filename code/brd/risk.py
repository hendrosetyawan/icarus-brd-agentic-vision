"""Level 2: BRD risk per cow from the Level 1 behaviour records.

    python -m brd.risk     # outputs/level2/: brd_risk.csv, features.csv, risk_cards/<cow>/ (top 3)

Per cow and day, behaviour is measured as a share of the time she was visible, then compared with
the herd (robust z-score: median and MAD) and with her own previous day. Directions follow the BRD
sickness-behaviour literature: less time and fewer visits at the bunk, more lying, less rumination,
missing herd meals, standing apart from the herd, fewer drinking bouts, and visible clinical signs
all raise the score. The score is a relative 0-1 index, not a calibrated probability: 10 cows,
2 days and no diagnoses cannot support one, and it may be that no cow is sick.
"""
from __future__ import annotations

import base64
import html
import subprocess
from pathlib import Path

import av
import matplotlib
import numpy as np
import pandas as pd

from .common import BEHAVIORS, ffmpeg_exe, hms, load_config, load_dets, video_decoder, write_json
from .pen import load_labels

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# feature: (direction, weight, plain-language name, unit, MAD floor)
FEATURES = {
    "feeding_pct": (-1, 1.5, "time at the feed bunk", "% of visible time", 3.0),
    "feeding_bouts_per_h": (-1, 1.0, "bunk visits", "per visible hour", 0.15),
    "missed_herd_meals_pct": (+1, 1.0, "herd feeding periods she missed", "%", 5.0),
    "lying_pct": (+1, 1.0, "time lying", "% of visible time", 5.0),
    "rumination_pct": (-1, 1.0, "rumination time", "% of visible time", 3.0),
    "drinking_bouts_per_h": (-1, 0.5, "drinking bouts", "per visible hour", 0.08),
    "active_pct": (-1, 0.5, "time walking or interacting", "% of visible time", 1.0),
    "isolation": (+1, 1.0, "distance to nearest herd-mate", "image units", 0.02),
    "sign_minutes": (+1, 1.5, "minutes with visible clinical signs", "min", 2.0),
    # Fewer, longer lying bouts precede BRD (feedlot accelerometer studies; longer lying in calves)
    "lying_bouts_per_24h": (-1, 0.75, "lying bouts", "per 24 visible hours", 2.0),
    "lying_bout_min": (+1, 0.5, "average lying bout", "min", 15.0),
}
CHANGES = {"feeding_pct": (-1, 1.0), "lying_pct": (+1, 0.5), "rumination_pct": (-1, 0.5)}


def visible(grid: pd.DataFrame) -> pd.DataFrame:
    return grid[~grid.source.isin(["not_visible", "no_frame"]) & (grid.behavior != "other_not_visible")]


def herd_meal_minutes(grid: pd.DataFrame) -> set:
    v = visible(grid)
    per = v.groupby("minute").agg(n=("cow", "size"), feeding=("behavior", lambda b: (b == "feeding").mean()))
    return set(per[(per.n >= 3) & (per.feeding >= 0.5)].index)


def isolation(cfg: dict) -> pd.DataFrame:
    """Mean distance from each cow to her nearest herd-mate on the overview camera (minutes with most cows visible)."""
    dets = load_dets(cfg)
    dets = dets[dets.cam == cfg["overview_camera"]].copy()
    dets["cow"] = load_labels(cfg).apply(dets)
    dets = dets[dets.cow != ""]
    dets["minute"] = (dets.t // 60) * 60
    dets["cx"], dets["cy"] = (dets.x1 + dets.x2) / 2, (dets.y1 + dets.y2) / 2
    pos = dets.groupby(["minute", "cow"])[["cx", "cy"]].mean().reset_index()
    rows = []
    for m, g in pos.groupby("minute"):
        if len(g) < max(3, int(0.6 * cfg["n_cows"])):
            continue
        xy = g[["cx", "cy"]].to_numpy()
        d = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
        np.fill_diagonal(d, np.inf)
        rows += [(m, c, float(v)) for c, v in zip(g.cow, d.min(1))]
    return pd.DataFrame(rows, columns=["minute", "cow", "nn_dist"])


def lying_bouts(g: pd.DataFrame, max_up_min: int = 10, min_len: int = 10) -> list[float]:
    """Lengths (min) of lying bouts in one cow-day of visible minutes (sorted by minute). A bout ends only
    when she has not been seen lying for over 10 min, so a mislabelled minute or a short stand doesn't
    split it; bouts under 10 min are dropped. Gives ~12 bouts/day of ~100 min, in the published range."""
    bouts, start, last = [], None, None
    for t, lying in zip(g.minute, g.behavior.isin(["lying", "ruminating_lying"])):
        if not lying:
            continue
        if start is None or t - last > (max_up_min + 1) * 60:
            if start is not None:
                bouts.append((last - start) / 60 + 1)
            start = t
        last = t
    if start is not None:
        bouts.append((last - start) / 60 + 1)
    return [b for b in bouts if b >= min_len]


def daily_features(cfg: dict, grid: pd.DataFrame, activity: pd.DataFrame) -> pd.DataFrame:
    v = visible(grid)
    meals = herd_meal_minutes(grid)
    iso = isolation(cfg)
    iso = iso.merge(grid[["cow", "minute", "day"]], on=["cow", "minute"]).groupby(["cow", "day"]).nn_dist.mean()
    rows = []
    for (cow, day), g in v.groupby(["cow", "day"]):
        n, hours = len(g), len(g) / 60
        share = g.behavior.value_counts() / n * 100
        a = activity[(activity.cow_id == cow) & (activity.day == day)]
        at_meal = g[g.minute.isin(meals)]
        lb = lying_bouts(g.sort_values("minute"))
        rows.append(dict(
            cow=cow, day=day, visible_h=round(hours, 1),
            feeding_pct=share.get("feeding", 0.0),
            feeding_bouts_per_h=(a.behavior == "feeding").sum() / max(hours, 0.1),
            missed_herd_meals_pct=(100 * (at_meal.behavior != "feeding").mean()) if len(at_meal) else np.nan,
            lying_pct=share.get("lying", 0.0) + share.get("ruminating_lying", 0.0),
            rumination_pct=share.get("ruminating_lying", 0.0) + share.get("ruminating_standing", 0.0),
            drinking_bouts_per_h=(a.behavior == "drinking").sum() / max(hours, 0.1),
            active_pct=share.get("walking", 0.0) + share.get("social_interaction", 0.0),
            isolation=iso.get((cow, day), np.nan),
            sign_minutes=int((g.signs.fillna("") != "").sum()),
            lying_bouts_per_24h=len(lb) / max(hours, 0.1) * 24,
            lying_bout_min=float(np.mean(lb)) if lb else np.nan,
        ))
    return pd.DataFrame(rows).round(3)


def score(feat: pd.DataFrame) -> pd.DataFrame:
    days = sorted(feat.day.unique())
    cur = feat[feat.day == days[-1]].set_index("cow")
    prev = feat[feat.day == days[-2]].set_index("cow") if len(days) > 1 else None
    contrib, reasons, strong = pd.DataFrame(index=cur.index), {c: [] for c in cur.index}, {c: 0 for c in cur.index}
    for f, (direction, w, name, unit, floor) in FEATURES.items():
        x = cur[f]
        if x.notna().sum() < 3:
            continue
        med = x.median()
        z = (x - med) / max(float((x - med).abs().median() * 1.4826), floor)
        c = (w * (direction * z).clip(-1, 3)).fillna(0)
        contrib[f] = c
        for cow in cur.index:
            if direction * z.get(cow, 0) > 2:
                strong[cow] += 1
            if c[cow] > 0.5:
                reasons[cow].append((c[cow], f"{name} {x[cow]:.3g} {unit} vs herd median {med:.3g}"))
    if prev is not None:
        for f, (direction, w) in CHANGES.items():
            delta = (cur[f] - prev[f].reindex(cur.index))
            if delta.notna().sum() < 3:
                continue
            med = delta.median()
            z = (delta - med) / max(float((delta - med).abs().median() * 1.4826), FEATURES[f][4])
            c = (w * (direction * z).clip(-1, 3)).fillna(0)
            contrib[f"change_{f}"] = c
            for cow in cur.index:
                if c[cow] > 0.5:
                    word = "down" if delta[cow] < 0 else "up"
                    reasons[cow].append((c[cow], f"{FEATURES[f][2]} {word} {abs(delta[cow]):.1f} points from "
                                                 f"{days[-2]} (herd median change {med:+.1f})"))
    total = contrib.sum(axis=1)
    risk = 1 / (1 + np.exp(-(total - 2.0) / 1.2))
    out = pd.DataFrame({"cow_id": cur.index, "risk_score": risk.round(3).to_numpy()})
    out["rank"] = out.risk_score.rank(ascending=False, method="first").astype(int)
    out["tier"] = np.select([(out.risk_score >= 0.7) & out.cow_id.map(strong).ge(2), out.risk_score >= 0.45],
                            ["inspect today", "watch closely"], "no concern")
    out["top_reasons"] = [" | ".join(t for _, t in sorted(reasons[c], reverse=True)[:3]) or "no clear deviation from the herd"
                          for c in out.cow_id]
    out["strong_deviations"] = out.cow_id.map(strong)
    return out.sort_values("rank").reset_index(drop=True)


# --------------------------------------------------------------- evidence
def video_size(path: str) -> tuple[int, int, bool]:
    """(width, height, tag_unknown). tag_unknown: FFmpeg doesn't know the codec tag, so force HEVC."""
    with av.open(path) as c:
        s = c.streams.video[0]
        dec = video_decoder(s)
        if dec.width:
            return dec.width, dec.height, False
        for p in c.demux(s):
            if p.is_keyframe:
                for f in dec.decode(p):
                    return f.width, f.height, True


def cut_clip(clips: pd.DataFrame, cam: str, t: float, box, out: Path, seconds: int = 20) -> bool:
    hit = clips[(clips.cam == cam) & (clips.start <= t) & (clips.start + clips.duration.fillna(300) > t)]
    if hit.empty:
        return False
    c = hit.iloc[0]
    w, h, forced = video_size(c.path)
    W, H = 960, int(round(960 * h / w / 2) * 2)
    x1, y1, x2, y2 = (int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H))
    cmd = [ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", f"{max(0, t - c.start - seconds / 2):.1f}",
           *(["-c:v", "hevc"] if forced else []), "-i", c.path,
           "-t", str(seconds), "-vf", f"scale={W}:{H},drawbox=x={x1}:y={y1}:w={x2 - x1}:h={y2 - y1}:color=red@0.9:t=4",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-an", str(out)]
    return subprocess.run(cmd).returncode == 0


def evidence(cfg, cow: str, grid: pd.DataFrame, meals: set, clips: pd.DataFrame, dets: pd.DataFrame, outdir: Path):
    g = visible(grid[grid.cow == cow])
    picks = []
    missed = g[g.minute.isin(meals) & (g.behavior != "feeding")].sort_values("confidence", ascending=False)
    if len(missed):
        r = missed.iloc[0]
        picks.append((r, f"Herd at the bunk, she is {r.behavior.replace('_', ' ')}"))
    signs = g[g.signs.fillna("") != ""].sort_values("confidence", ascending=False)
    if len(signs):
        r = signs.iloc[0]
        picks.append((r, f"Visible sign(s): {r.signs.replace(';', ', ').replace('_', ' ')}"))
    lying = g[g.behavior.isin(["lying", "ruminating_lying"])]
    if len(lying) and len(picks) < 3:
        run = (lying.minute.diff() != 60).cumsum()
        longest = lying.groupby(run).minute.agg(["first", "size"]).sort_values("size").iloc[-1]
        r = lying[lying.minute == longest["first"] + 60 * (longest["size"] // 2)].iloc[0]
        picks.append((r, f"Middle of her longest lying bout ({longest['size']} min)"))
    out = []
    for i, (r, why) in enumerate(picks[:3]):
        if not isinstance(r.cam, str):
            continue
        d = dets[(dets.cam == r.cam) & (dets.track == r.track) & (dets.t.between(r.minute, r.minute + 60))]
        if d.empty:
            continue
        b = d.iloc[len(d) // 2]
        name = f"clip{i + 1}.mp4"
        if cut_clip(clips, r.cam, b.t, (b.x1, b.y1, b.x2, b.y2), outdir / name):
            out.append(dict(file=name, time=hms(r.minute), cam=r.cam, behavior=r.behavior, why=why))
    return out


def ethogram(cow: str, grid: pd.DataFrame, path: Path) -> None:
    colors = {b: plt.cm.tab10(i) for i, b in enumerate(BEHAVIORS)}
    days = sorted(grid.day.unique())
    fig, axes = plt.subplots(len(days), 1, figsize=(11, 1.3 * len(days) + 0.6), squeeze=False)
    for ax, day in zip(axes[:, 0], days):
        g = grid[(grid.cow == cow) & (grid.day == day)].sort_values("minute")
        herd = visible(grid[grid.day == day]).groupby("minute").behavior.apply(lambda b: (b == "feeding").mean())
        ax.bar((g.minute % 86400) / 3600, 1, width=1 / 60, color=[colors[b] for b in g.behavior], align="edge")
        ax.plot((herd.index % 86400) / 3600, herd.to_numpy(), color="black", lw=0.8)
        ax.set(ylabel=day, yticks=[], xlim=(0, 24), ylim=(0, 1.05), xticks=range(0, 25, 3))
    axes[-1, 0].set_xlabel("clock hour (bars: her behaviour each minute; line: share of the herd feeding)")
    handles = [plt.Rectangle((0, 0), 1, 1, color=colors[b]) for b in BEHAVIORS]
    fig.legend(handles, BEHAVIORS, ncol=5, fontsize=7, loc="upper center")
    fig.tight_layout(rect=(0, 0, 1, 0.82))
    fig.savefig(path, dpi=120)
    plt.close(fig)


def card(cow: str, row, feat: pd.DataFrame, clips_info: list, outdir: Path) -> None:
    days = sorted(feat.day.unique())
    rows = []
    for f, (_, _, name, unit, _) in FEATURES.items():
        vals = [feat[(feat.cow == cow) & (feat.day == d)][f] for d in days]
        herd = feat[feat.day == days[-1]][f].median()
        rows.append(f"<tr><td>{html.escape(name)} ({unit})</td>" +
                    "".join(f"<td>{v.iloc[0]:.1f}</td>" if len(v) and pd.notna(v.iloc[0]) else "<td>-</td>" for v in vals) +
                    f"<td>{herd:.1f}</td></tr>")
    img = base64.b64encode((outdir / "ethogram.png").read_bytes()).decode()
    vids = "".join(f"<figure><video src='{c['file']}' controls width='420'></video><figcaption>{html.escape(c['why'])}"
                   f" ({c['cam']}, {c['time']})</figcaption></figure>" for c in clips_info) or "<p>No clip available.</p>"
    reasons = "".join(f"<li>{html.escape(r)}</li>" for r in str(row.top_reasons).split(" | "))
    (outdir / "card.html").write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>{cow} risk card</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;color:#222}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 8px;text-align:right}}td:first-child{{text-align:left}}
figure{{display:inline-block;margin:6px}}.tier{{font-weight:600}}</style></head><body>
<h1>{cow}: BRD risk {row.risk_score:.2f} (rank {row.rank} of herd) <span class="tier">— {row.tier}</span></h1>
<h3>Why</h3><ul>{reasons}</ul>
<h3>Behaviour vs herd</h3><table><tr><th>measure</th>{''.join(f'<th>{d}</th>' for d in days)}<th>herd median ({days[-1]})</th></tr>{''.join(rows)}</table>
<h3>Her day, minute by minute</h3><img src="data:image/png;base64,{img}" width="100%">
<h3>Video evidence</h3>{vids}
<p><small>Relative risk index from camera-observed behaviour, not a diagnosis. Behaviour is measured as a share of the
time she was visible. Any treatment decision rests with the herd veterinarian after examining her.</small></p>
</body></html>""")


def main() -> None:
    cfg = load_config()
    l1, out = cfg["out_dir"] / "level1", cfg["out_dir"] / "level2"
    out.mkdir(parents=True, exist_ok=True)
    grid = pd.read_csv(l1 / "activity_minutes.csv")
    activity = pd.read_csv(l1 / "activity.csv")
    feat = daily_features(cfg, grid, activity)
    feat.to_csv(out / "features.csv", index=False)
    risk = score(feat)
    risk.to_csv(out / "brd_risk.csv", index=False)
    print(risk[["cow_id", "risk_score", "rank", "tier", "top_reasons"]].to_string(index=False))
    clips = pd.read_csv(cfg["work_dir"] / "clips.csv")
    dets = load_dets(cfg)
    meals = herd_meal_minutes(grid)
    for row in risk.head(3).itertuples():
        d = out / "risk_cards" / row.cow_id
        d.mkdir(parents=True, exist_ok=True)
        ethogram(row.cow_id, grid, d / "ethogram.png")
        info = evidence(cfg, row.cow_id, grid, meals, clips, dets, d)
        card(row.cow_id, row, feat, info, d)
        write_json(d / "evidence.json", info)
    summary = {"herd_note": ("At least one cow deviates from the herd on two or more measures."
                             if (risk.strong_deviations >= 2).any() else
                             "No cow deviates strongly on two or more measures; the ranking reflects small differences "
                             "and may simply mean no cow is sick."),
               "days": sorted(feat.day.unique())}
    write_json(out / "risk_summary.json", summary)
    print(f"\n{summary['herd_note']}\nLevel 2 written to {out}")


if __name__ == "__main__":
    main()
