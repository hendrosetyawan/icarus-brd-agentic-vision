"""BRD sign evidence per cow, organised like the pen-rider DART check (Depression, Appetite, Respiration,
Temperature) and the Wisconsin scoring chart, from the Level 1 and Level 2 outputs.

    python brief/brd_evidence.py OUTPUTS_DIR OUT_DIR      # writes brd_signs.csv and brd_signs.png

Each indicator is the cow's day-2 value (the latest day), compared with the herd by a robust z-score and
signed so that positive = more BRD-like (e.g. less bunk time, longer lying bouts, farther from herd-mates).
Temperature cannot be seen on RGB/IR video, so it is reported as not observed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

# (column, group, label, direction (+1: higher = more BRD-like), noise floor, format)
INDICATORS = [
    ("active_pct", "D", "walking +\nsocial %", -1, 1.0, "{:.1f}"),
    ("lying_pct", "D", "lying\n% of time", +1, 5.0, "{:.0f}"),
    ("lying_bout_min", "D", "lying bout\n(min)", +1, 15.0, "{:.0f}"),
    ("lying_bouts_per_24h", "D", "lying bouts\nper day", -1, 2.0, "{:.1f}"),
    ("isolation", "D", "distance to\nherd-mate", +1, 0.02, "{:.2f}"),
    ("low_head_min", "D", "head low,\nnot eating", +1, 2.0, "{:.0f}"),
    ("feeding_pct", "A", "bunk time\n% of time", -1, 3.0, "{:.1f}"),
    ("feeding_bouts_per_h", "A", "bunk visits\nper hour", -1, 0.15, "{:.2f}"),
    ("missed_herd_meals_pct", "A", "herd meals\nmissed %", +1, 5.0, "{:.0f}"),
    ("rumination_pct", "A", "rumination\n% (weak)", -1, 3.0, "{:.1f}"),
    ("resp_min", "R", "mouth /\nnasal min", +1, 1.0, "{:.0f}"),
    ("lying_still_pct", "T", "lying\nstill %", +1, 5.0, "{:.0f}"),
    ("fever_index", "T", "fever-like\nindex", +1, 1.0, "{:+.1f}"),
    ("d_feeding", "Δ", "Δ bunk time\nvs herd", -1, 2.0, "{:+.1f}"),
    ("d_lying", "Δ", "Δ lying\nvs herd", +1, 3.0, "{:+.1f}"),
]
GROUPS = {"D": "D · Depression / lethargy", "A": "A · Appetite", "R": "R · Resp.", "T": "T · Fever proxy",
          "Δ": "Day 1 → 2"}
# Behaviours reported in febrile cattle (endotoxin and BRD challenge studies, 39.5-41.3 °C): less feeding, more
# inactive lying, more lying, less activity and rumination, faster breathing. Weights favour the most consistent.
FEVER_WEIGHTS = {"feeding_pct": 1.0, "lying_still_pct": 1.0, "d_feeding": 0.75, "lying_pct": 0.5, "active_pct": 0.5,
                 "resp_min": 0.5, "rumination_pct": 0.25}
INK, MUTED, GRID = "#1f2933", "#52514e", "#e4e3df"
DIVERGING = LinearSegmentedColormap.from_list("brd", ["#256abf", "#9ec5f4", "#f0efec", "#f4a3a2", "#c73534"])


def evidence(out: Path) -> pd.DataFrame:
    feat = pd.read_csv(out / "level2" / "features.csv")
    risk = pd.read_csv(out / "level2" / "brd_risk.csv")
    grid = pd.read_csv(out / "level1" / "activity_minutes.csv")
    days = sorted(feat.day.unique())
    cur = feat[feat.day == days[-1]].set_index("cow")
    prev = feat[feat.day == days[0]].set_index("cow")
    signs = grid[(grid.day == days[-1]) & grid.signs.notna() & (grid.signs != "")]
    low = signs[(signs.behavior != "feeding") & signs.signs.str.contains("head_lowered|drooping_ears")]
    resp = grid[grid.signs.fillna("").str.contains("open_mouth|nasal")]
    cur["low_head_min"] = low.groupby("cow").size().reindex(cur.index, fill_value=0)
    cur["resp_min"] = resp.groupby("cow").size().reindex(cur.index, fill_value=0)
    vis = grid[(grid.day == days[-1]) & (grid.behavior != "other_not_visible")
               & ~grid.source.isin(["not_visible", "no_frame"])]
    still = vis.assign(s=vis.behavior.eq("lying") & (vis.move.fillna(0) < 0.1)).groupby("cow").s.mean() * 100
    cur["lying_still_pct"] = still.reindex(cur.index)
    for col, name in (("feeding_pct", "d_feeding"), ("lying_pct", "d_lying")):
        d = cur[col] - prev[col]
        cur[name] = d - d.median()
    rows = []
    for cow in cur.index:
        r = {"cow_id": cow}
        for col, grp, label, direction, floor, fmt in INDICATORS:
            if col == "fever_index":
                continue
            x = cur[col]
            med = x.median()
            scale = max(float((x - med).abs().median() * 1.4826), floor)
            r[col] = round(float(x[cow]), 3)
            r[col + "_z"] = round(float(np.clip(direction * (x[cow] - med) / scale, -3, 3)), 2)
        rows.append(r)
    ev = pd.DataFrame(rows)
    ev["fever_index"] = sum(w * ev[c + "_z"] for c, w in FEVER_WEIGHTS.items()) / sum(FEVER_WEIGHTS.values())
    ev["fever_index"] = ev.fever_index.round(2)
    ev["fever_index_z"] = (ev.fever_index * 1.5).clip(-3, 3).round(2)
    ev = ev.merge(risk[["cow_id", "risk_score", "rank", "tier"]], on="cow_id")
    ev["n_signs_z_over_1"] = ev[[c + "_z" for c, *_ in INDICATORS]].gt(1).sum(axis=1)
    return ev.sort_values("rank")


def heatmap(ev: pd.DataFrame, png: Path, width: float = 7.3, height: float = 2.5) -> None:
    cols = [c for c, *_ in INDICATORS]
    z = ev[[c + "_z" for c in cols]].to_numpy()
    fig = plt.figure(figsize=(width, height), dpi=300)
    ax = fig.add_axes([0.17, 0.04, 0.80, 0.70])
    ax.imshow(z, cmap=DIVERGING, vmin=-3, vmax=3, aspect="auto")
    for i, (_, r) in enumerate(ev.iterrows()):
        for j, (c, grp, label, direction, floor, fmt) in enumerate(INDICATORS):
            v = r[c]
            ax.text(j, i, fmt.format(v), ha="center", va="center", fontsize=4.8,
                    color="white" if abs(r[c + "_z"]) > 2.0 else INK)
    ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    # gaps between groups and rows (surface-colour separators)
    for j in range(1, len(cols)):
        ax.axvline(j - 0.5, color="white", lw=1.6 if INDICATORS[j][1] != INDICATORS[j - 1][1] else 0.6)
    for i in range(1, len(ev)):
        ax.axhline(i - 0.5, color="white", lw=0.6)
    # column labels and DART group headers
    for j, (c, grp, label, *_r) in enumerate(INDICATORS):
        ax.text(j, -0.75, label, ha="center", va="bottom", fontsize=4.7, color=MUTED, linespacing=1.15)
    starts = {}
    for j, (c, grp, *_r) in enumerate(INDICATORS):
        starts.setdefault(grp, [j, j])[1] = j
    for grp, (a, b) in starts.items():
        ax.plot([a - 0.42, b + 0.42], [-2.25, -2.25], color={"D": "#2a78d6", "A": "#eb6834", "R": "#1baf7a",
                                                             "T": "#6250d6", "Δ": "#9aa5b1"}[grp], lw=2, solid_capstyle="round",
                clip_on=False)
        ax.text((a + b) / 2, -2.45, GROUPS[grp], ha="center", va="bottom", fontsize=5.6, color=INK,
                weight="bold", clip_on=False)
    # row labels: cow, risk, tier (tier in words, never colour alone)
    for i, (_, r) in enumerate(ev.iterrows()):
        tier = "" if r.tier == "no concern" else f"  {r.tier}"
        ax.text(-0.62, i, f"{r.cow_id}  {r.risk_score:.2f}{tier}", ha="right", va="center", fontsize=5.2,
                color=INK, weight="bold" if r.tier == "inspect today" else "normal")
    ax.text(-0.62, -0.9, "cow · risk", ha="right", va="bottom", fontsize=4.8, color=MUTED)
    # legend
    cax = fig.add_axes([0.17, 0.935, 0.16, 0.022])
    cb = fig.colorbar(plt.cm.ScalarMappable(cmap=DIVERGING, norm=plt.Normalize(-3, 3)), cax=cax,
                      orientation="horizontal")
    cb.set_ticks([-3, 0, 3]); cb.set_ticklabels(["healthier than herd", "herd", "more BRD-like"])
    cb.ax.tick_params(labelsize=4.6, length=0, colors=MUTED); cb.outline.set_visible(False)
    fig.text(0.35, 0.94, "Cell = day-2 value; colour = distance from the herd in the BRD direction (robust z)", fontsize=4.8, color=MUTED, va="center")
    fig.savefig(png, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    outputs, dest = Path(sys.argv[1]), Path(sys.argv[2])
    ev = evidence(outputs)
    ev.to_csv(dest / "brd_signs.csv", index=False)
    heatmap(ev, dest / "brd_signs.png")
    print(ev[["cow_id", "risk_score", "tier", "n_signs_z_over_1"]].to_string(index=False))
