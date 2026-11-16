"""Assemble the Level 1 pieces that need choosing: three agent reasoning traces (one success, one
failure, one at night), a coverage summary, and accuracy against your hand labels (if any).

    python -m brd.report
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

from .common import load_config, write_json


def load_traces(root: Path) -> list[dict]:
    recs = []
    for p in root.glob("*/trace.json"):
        r = json.loads(p.read_text())
        r["dir"] = p.parent
        r["tool_steps"] = sum(1 for s in r["steps"] if s["action"] != "final")
        recs.append(r)
    return recs


def pick(recs: list[dict]) -> dict:
    def changed(r):  # the agent changed its mind or corrected an ID: the interesting successes
        ans = r.get("answer") or {}
        corrected = any(i.get("status") == "corrected" for i in ans.get("checked", []))
        return corrected or (r.get("first_look") or {}).get("behavior") not in (None, ans.get("behavior"))

    def checked_fail(r):  # a self-check rejected an answer at least once
        return any(isinstance(s.get("self_check"), list) for s in r["steps"])

    ok = [r for r in recs if r["status"] == "success" and r["tool_steps"] >= 1]
    day_ok = [r for r in ok if not r.get("night")]
    night_ok = [r for r in ok if r.get("night")]
    bad = [r for r in recs if r["status"] in ("failed_check", "unresolved")] or [r for r in recs if checked_fail(r)]
    key = lambda r: (changed(r), checked_fail(r), r["tool_steps"])  # noqa: E731
    chosen = {name: max(group, key=key) for name, group in
              (("success", day_ok), ("failure", bad), ("night", night_ok)) if group}
    for name in {"success", "failure", "night"} - set(chosen):
        print(f"  ! no {name} episode found to export")
    return chosen


def render(rec: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for f in rec["dir"].glob("*.jpg"):
        shutil.copy(f, out / f.name)
    L = [f"# {rec['kind']} episode: {rec.get('cam', '')} {rec.get('time', '')}"
         f"{' (night)' if rec.get('night') else ''}", "",
         f"**Status:** {rec['status']} after {rec['n_steps']} steps ({rec.get('seconds', '?')} s)", "",
         "## Task", "", rec["task"], ""] + [f"![]({i})" for i in rec.get("task_images", [])] + [""]
    if rec.get("first_look"):
        L += [f"**First quick look:** `{json.dumps(rec['first_look'])}`", ""]
    for i, s in enumerate(rec["steps"], 1):
        L += [f"## Step {i}: `{s['action']}`", "", f"*Thought:* {s['thought']}", "",
              f"*Args:* `{json.dumps(s['args'])}`", ""]
        if s.get("observation"):
            L += ["*Observation:*", "", "```", str(s["observation"])[:2000], "```", ""]
        L += [f"![]({img})" for img in s.get("images", [])]
        if s.get("self_check"):
            L += ["", f"*Self-check:* {s['self_check']}"]
        L.append("")
    L += ["## Final answer", "", "```json", json.dumps(rec.get("answer"), indent=2), "```"]
    (out / "trace.md").write_text("\n".join(L))


def accuracy(cfg: dict) -> dict | None:
    f = cfg["work_dir"] / "label_pool" / "labels.csv"
    if not f.exists():
        return None
    lab = pd.read_csv(f)
    # Score the current outputs, not the prediction saved when the minute was labelled.
    grid = pd.read_csv(cfg["out_dir"] / "level1" / "activity_minutes.csv", usecols=["cow", "minute", "behavior"])
    lab = lab.merge(grid.rename(columns={"behavior": "current"}), on=["cow", "minute"], how="left")
    lab["predicted"] = lab.current.fillna(lab.predicted)
    b = lab[lab.true_behavior.notna() & (lab.true_behavior != "can't tell")]
    ids = lab[lab.id_correct.isin(["yes", "no"])]
    res = {"labelled": len(lab),
           "behaviour_accuracy": round(float((b.predicted == b.true_behavior).mean()), 3) if len(b) else None,
           "behaviour_n": len(b),
           "id_accuracy": round(float((ids.id_correct == "yes").mean()), 3) if len(ids) else None,
           "id_n": len(ids)}
    if len(b):
        pd.crosstab(b.true_behavior, b.predicted).to_csv(cfg["out_dir"] / "level1" / "confusion_matrix.csv")
    return res


def main() -> None:
    cfg = load_config()
    out = cfg["out_dir"] / "level1"
    recs = load_traces(cfg["work_dir"] / "traces")
    chosen = pick(recs)
    for name, rec in chosen.items():
        render(rec, out / "agent_traces" / name)
    print(f"Traces: {len(recs)} episodes; saved {sorted(chosen)} to {out / 'agent_traces'}")
    grid = pd.read_csv(out / "activity_minutes.csv")
    summary = {
        "episodes": pd.Series([r["status"] for r in recs]).value_counts().to_dict(),
        "episodes_by_kind": pd.Series([r["kind"] for r in recs]).value_counts().to_dict(),
        "cow_minutes": len(grid),
        "source_share": grid.source.value_counts(normalize=True).round(3).to_dict(),
        "visible_share_by_day_night": {
            f"{d}_{'night' if n else 'day'}": round(float(v), 3) for (d, n), v in
            grid.assign(vis=~grid.source.isin(["not_visible", "no_frame"])).groupby(["day", "night"]).vis.mean().items()},
        "accuracy": accuracy(cfg),
    }
    write_json(out / "summary.json", summary)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
