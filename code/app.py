"""Viewer for the pipeline outputs: risk ranking and cards, time budgets, each cow's day, ID checks,
agent traces, live alerts, and a labelling page to measure accuracy.

    export BRD=$SCRATCH/brd      # same data root the pipeline used
    streamlit run app.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from brd.common import BEHAVIORS, load_config

st.set_page_config(page_title="Pen 6 BRD watch", page_icon="🐄", layout="wide")
cfg = load_config()
L1, L2, L3 = (cfg["out_dir"] / f"level{i}" for i in (1, 2, 3))


def csv(p: Path):
    return pd.read_csv(p) if p.exists() else None


st.title("Pen 6: behaviour and BRD risk")
tabs = st.tabs(["Risk ranking", "Time budgets", "Cow timeline", "ID checks", "Agent traces", "Live alerts",
                "Label & accuracy"])

with tabs[0]:
    risk = csv(L2 / "brd_risk.csv")
    if risk is None:
        st.info("Run `python -m brd.risk` first.")
    else:
        note = L2 / "risk_summary.json"
        if note.exists():
            st.caption(json.loads(note.read_text())["herd_note"])
        st.dataframe(risk, hide_index=True, width="stretch",
                     column_config={"risk_score": st.column_config.ProgressColumn("Risk", min_value=0.0, max_value=1.0,
                                                                                   format="%.2f"),
                                    "top_reasons": st.column_config.TextColumn("Why", width="large")})
        have = {c.parent.name for c in (L2 / "risk_cards").glob("*/card.html")}
        if have:
            pick = st.selectbox("Risk card (highest risk first)", [c for c in risk.cow_id if c in have])
            d = L2 / "risk_cards" / pick
            st.image(str(d / "ethogram.png"))
            ev = json.loads((d / "evidence.json").read_text()) if (d / "evidence.json").exists() else []
            cols = st.columns(max(1, len(ev)))
            for col, e in zip(cols, ev):
                col.video(str(d / e["file"]))
                col.caption(f"{e['why']} ({e['cam']}, {e['time']})")
            st.caption(f"Full card: {d / 'card.html'}")
        feat = csv(L2 / "features.csv")
        if feat is not None:
            with st.expander("All features per cow and day"):
                st.dataframe(feat, hide_index=True, width="stretch")

with tabs[1]:
    tb = csv(L1 / "time_budgets.csv")
    if tb is None:
        st.info("Run `python -m brd.behavior` first.")
    else:
        st.image(str(L1 / "time_budgets.png"))
        st.dataframe(tb, hide_index=True, width="stretch")

with tabs[2]:
    grid = csv(L1 / "activity_minutes.csv")
    if grid is not None:
        cow = st.selectbox("Cow", sorted(grid.cow.unique()))
        g = grid[grid.cow == cow].copy()
        g["time"] = pd.to_datetime(g.time)
        st.bar_chart(g.assign(n=1).pivot_table(index=g.time.dt.floor("h"), columns="behavior", values="n",
                                               aggfunc="sum", fill_value=0).reindex(columns=BEHAVIORS, fill_value=0))
        st.dataframe(csv(L1 / "activity.csv").query("cow_id == @cow"), hide_index=True, width="stretch")

with tabs[3]:
    cams = sorted(p.name for p in (L1 / "id_check").glob("*")) if (L1 / "id_check").exists() else []
    if not cams:
        st.info("Run `python -m brd.identify audit` first.")
    else:
        if (L1 / "gallery_sheet.jpg").exists():
            with st.expander("Reference gallery"):
                st.image(str(L1 / "gallery_sheet.jpg"))
        cam = st.radio("Camera", cams, horizontal=True)
        files = sorted((L1 / "id_check" / cam).glob("*.jpg"))
        i = st.slider("Checkpoint", 0, len(files) - 1, 0) if len(files) > 1 else 0
        st.image(str(files[i]), caption=files[i].stem)

with tabs[4]:
    traces = sorted((L1 / "agent_traces").glob("*/trace.md")) if (L1 / "agent_traces").exists() else []
    if not traces:
        st.info("Run `python -m brd.report` first.")
    for t in traces:
        with st.expander(t.parent.name):
            for line in t.read_text().splitlines():
                if line.startswith("![]("):
                    st.image(str(t.parent / line[4:-1]), width=520)
                else:
                    st.markdown(line)

with tabs[5]:
    live = csv(L3 / "live_risk.csv")
    if live is not None:
        st.dataframe(live, hide_index=True, width="stretch")
    log = L3 / "alerts.jsonl"
    alerts = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    if not alerts:
        st.info("No alerts yet. Run `python -m brd.watch --clip <video>`.")
    for a in reversed(alerts):
        with st.container(border=True):
            st.markdown(f"**{a['message']}**")
            d = next(L3.glob(f"alerts/*/{a['cow_id']}.json"), None)
            if d is not None:
                c1, c2 = st.columns(2)
                if a.get("snapshot"):
                    c1.image(str(d.parent / a["snapshot"]))
                if a.get("clip"):
                    c2.video(str(d.parent / a["clip"]))

with tabs[6]:
    pool = cfg["work_dir"] / "label_pool"
    items = json.loads((pool / "pool.json").read_text()) if (pool / "pool.json").exists() else []
    if not items:
        st.info("The behaviour step saves a labelling pool; run it first.")
    else:
        lab_file = pool / "labels.csv"
        labels = pd.read_csv(lab_file) if lab_file.exists() else pd.DataFrame(
            columns=["file", "cow", "minute", "predicted", "true_behavior", "id_correct"])
        todo = [x for x in items if x["file"] not in set(labels.file)]
        st.write(f"{len(labels)} labelled, {len(todo)} to go. Label what you see; skip with \"can't tell\".")
        if todo:
            x = todo[0]
            st.image(str(pool / x["file"]), caption=f"{x['cow']} · {x['cam']} · {x['time']}")
            if (L1 / "gallery_sheet.jpg").exists():
                with st.expander(f"Reference crops (is this {x['cow']}?)"):
                    st.image(str(L1 / "gallery_sheet.jpg"))
            with st.form("label"):
                tb_ = st.radio("What is she doing?", BEHAVIORS + ["can't tell"], horizontal=True, index=None)
                idc = st.radio(f"Is this {x['cow']}?", ["yes", "no", "can't tell"], horizontal=True, index=None)
                if st.form_submit_button("Save and next") and tb_ and idc:
                    row = dict(file=x["file"], cow=x["cow"], minute=x["minute"], predicted=x["predicted"],
                               true_behavior=tb_, id_correct=idc)
                    pd.concat([labels, pd.DataFrame([row])]).to_csv(lab_file, index=False)
                    st.rerun()
        b = labels[~labels.true_behavior.isin(["can't tell"])]
        if len(b):
            st.metric("Behaviour accuracy", f"{(b.predicted == b.true_behavior).mean():.0%}", help=f"n = {len(b)}")
            ids = labels[labels.id_correct.isin(["yes", "no"])]
            if len(ids):
                st.metric("Cow ID accuracy", f"{(ids.id_correct == 'yes').mean():.0%}", help=f"n = {len(ids)}")
