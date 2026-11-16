"""Two-page technical brief (PDF), organised around the BRD signs the challenge asks about.

    python brief/make_brief.py brief/numbers.json OUTPUTS_DIR OUT.pdf

Builds the pipeline diagram (pipeline_diagram.py) and the per-cow BRD sign evidence (brd_evidence.py)
next to OUT.pdf, then lays out: BRD signs -> what the video measures, the pipeline, the evidence per cow,
Level 1 quality, limitations and references. NUMBERS.json holds the quoted figures and the per-cow reading.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import brd_evidence
import pipeline_diagram

INK, MUTED, ACCENT = colors.HexColor("#1f2933"), colors.HexColor("#52606d"), colors.HexColor("#0b6e4f")
RULE, HEAD = colors.HexColor("#d9dee4"), colors.HexColor("#e9f1f9")


def styles():
    s = getSampleStyleSheet()
    base = ParagraphStyle("b", parent=s["Normal"], fontName="Helvetica", fontSize=8.9, leading=11.3, textColor=INK)
    return {
        "title": ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=17),
        "sub": ParagraphStyle("s", parent=base, textColor=MUTED, fontSize=7.8, leading=10),
        "h": ParagraphStyle("h", parent=base, fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=ACCENT,
                            spaceBefore=5, spaceAfter=2),
        "body": ParagraphStyle("p", parent=base, spaceAfter=2.5),
        "bullet": ParagraphStyle("li", parent=base, leftIndent=9, bulletIndent=0, spaceAfter=1.5),
        "small": ParagraphStyle("sm", parent=base, fontSize=6.9, leading=8.6, textColor=MUTED),
        "cell": ParagraphStyle("c", parent=base, fontSize=7.6, leading=9.2),
        "cellb": ParagraphStyle("cb", parent=base, fontSize=7.6, leading=9.2, fontName="Helvetica-Bold"),
    }


def header(n: dict, st: dict) -> Table:
    left = [Paragraph("Agentic vision for Angus behaviour and BRD risk", st["title"]),
            Paragraph(f'<font name="Helvetica-Bold">{n["team"]}</font>' + (f": {n['members']}" if n.get("members")
                      else ""), st["body"]),
            Paragraph(f"Merck–TAMU Animal Science Hackathon 2026 · Agentic Vision challenge · model: {n['model']}",
                      st["sub"])]
    logo = Image(n["logo"], width=0.7 * inch, height=0.7 * inch) if n.get("logo") else ""
    t = Table([[left, logo]], colWidths=[6.5 * inch, 0.8 * inch])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT")]
                          + [(k, (0, 0), (-1, -1), 0) for k in ("LEFTPADDING", "RIGHTPADDING", "TOPPADDING",
                                                                  "BOTTOMPADDING")]))
    return t


def grid_table(rows, widths, st, bold_first_col=True) -> Table:
    data = [[Paragraph(str(c), st["cellb"] if (i == 0 or (j == 0 and bold_first_col)) else st["cell"])
             for j, c in enumerate(r)] for i, r in enumerate(rows)]
    t = Table(data, colWidths=[w * inch for w in widths])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.8),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8), ("LEFTPADDING", (0, 0), (-1, -1), 3),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    return t


def build(n: dict, outputs: Path, out: str) -> None:
    st = styles()
    bold = lambda t: t.replace("<b>", '<font name="Helvetica-Bold">').replace("</b>", "</font>")  # noqa: E731
    P = lambda text, k="body": Paragraph(bold(text), st[k])  # noqa: E731
    B = lambda text: Paragraph(bold(text), st["bullet"], bulletText="•")  # noqa: E731
    here = Path(out).parent
    pipeline_diagram.render(str(here / "pipeline.png"))
    ev = brd_evidence.evidence(outputs)
    ev.to_csv(here / "brd_signs.csv", index=False)
    brd_evidence.heatmap(ev, here / "brd_signs.png")
    PW, PH = pipeline_diagram.W, pipeline_diagram.H

    signs = [
        ["BRD sign (DART / Wisconsin score)", "What the literature reports", "What we measure on the video", "Seen?"],
        ["D · Depression, lethargy", "head low, ears droop, slow, stands apart; lies more, in fewer and longer "
         "bouts; less social contact [1-3, 7]", "walking + social time; head low or ears drooping while not "
         "eating (agent close look); distance to nearest herd-mate; lying %, bouts per day, bout length", "yes"],
        ["A · Appetite", "less time and fewer visits at the bunk, late to feed; feeding time down ~35 % at peak "
         "signs; predicts BRD days early [4-6]", "bunk time and visits (bunk zones on 2 cameras), herd meals "
         "missed, rumination (weak at 1 frame / 2 s)", "yes"],
        ["R · Respiration", "open-mouth or laboured breathing, nasal and eye discharge, cough [1, 2]",
         "agent close look at full-resolution crops for open-mouth breathing and nasal discharge",
         "partly (no audio for cough)"],
        ["T · Temperature", "fever, e.g. treat at ≥ 104 °F [1]; febrile cattle (39.5-41.3 °C) eat less, lie "
         "still more, groom and ruminate less [10-12]; 74 % of fever episodes show no visible sign [13]",
         "fever itself is not visible; a fever-like index averages those behaviours (bunk time and its drop, "
         "lying still, activity, breathing)", "proxy only"],
        ["Change over time", "signs build over days; change detection flags BRD ~2 days before pen riders [6]",
         "each cow vs her own day 1, against the herd's change; live check on every new clip", "yes"],
    ]
    story = [
        header(n, st), Spacer(1, 3),
        P("Pen riders find BRD by looking for <b>Depression, Appetite, Respiration and Temperature (DART)</b>, "
          "and calf scoring charts add ear droop, nasal discharge and cough [2]. By eye this catches only about "
          "<b>1 case in 4</b> (sensitivity 0.27, specificity 0.92 in a meta-analysis [1]), because sick cattle hide "
          "signs for the few minutes a person watches. Our system watches every cow, every minute, on three "
          "cameras, and measures each sign a camera can see."),
        P("1. BRD signs and what the video measures", "h"),
        grid_table(signs, [1.25, 2.35, 2.95, 0.75], st),
        P("2. Pipeline", "h"),
        Image(str(here / "pipeline.png"), width=7.3 * inch, height=7.3 * inch * PH / PW),
        P("<b>Level 1.</b> A vision-language model (Qwen3.5-9B) runs as an agent [8]: for each decision it plans, "
          "calls tools (zoom, look back or forward, other cameras, reference crops, track history) and answers; "
          "deterministic self-checks (one cow, one place; posture, movement and zone agree) reject impossible "
          "answers, which are never applied. It names the 10 cows, audits IDs every 30 min per camera, labels "
          "every cow-minute and takes a close look for visible signs. <b>Level 2</b> turns the minutes into the "
          "DART measures above, compares each cow with the herd (robust z-score) and with her own day 1, and "
          "ranks a 0-1 relative risk with reasons and video clips. <b>Level 3</b> runs the same steps on any new "
          "clip and alerts with a snapshot and clip."),
        KeepTogether([P("3. BRD evidence per cow", "h"),
                      Image(str(here / "brd_signs.png"), width=7.3 * inch, height=2.5 * inch)]),
    ]
    for line in n["brd_reading"]:
        story.append(B(line))
    story.append(P(f"<b>Live alert.</b> {n['live_alert']}"))
    story.append(P("4. Level 1 quality", "h"))
    story.append(grid_table([
        ["Measure", "Result"],
        [f"Behaviour accuracy vs {n['n_labels']} minutes we labelled", n["behaviour_acc"]],
        ["Cow ID", "ID audits: " + n["audits"] + ". ID accuracy " + n["id_acc"] + "."],
        ["Cow-minutes: observed / agent / estimated / not visible", n["coverage"]],
        ["Agent traces (outputs/level1/agent_traces)", f"success {n['trace_success']} failure {n['trace_failure']}"],
    ], [2.6, 4.7], st))
    story += [
        P("5. Limitations and what more data would add", "h"),
        B("<b>A ranking, not a diagnosis.</b> Without temperatures or health records the score cannot be "
          "validated; possibly no cow is sick. C08 and C09 should be checked in person and their temperature "
          "taken. Fever, cough and auscultation are outside what video can measure."),
        B("<b>Identity is the weakest link.</b> Black Angus look alike; a mix-up moves a cow's minutes to another. "
          "The agent's zooms show ear tags are readable at full resolution: tag reading [9] would anchor IDs."),
        B(f"<b>Behaviour labels.</b> Accuracy {n['behaviour_acc'].split(' (')[0]}: lying is reliable, standing is "
          "sometimes called lying and rumination is under-counted (jaw movement needs more frames). The whole "
          "herd lay more and ate less on day 2, so day-to-day changes are judged against the herd's change."),
        B("<b>With more data:</b> temperatures and pull dates to calibrate weights and the alert threshold; "
          "a week-long baseline per cow with change detection [6]; thermal and audio for fever and cough."),
        P(f"Models: {n['model']} on vLLM, YOLO11x + ByteTrack (AGPL-3.0), DINOv2-small (Apache-2.0); no outside "
          "cattle data; TAMU HPRC Grace (A100). Code drafted with Claude Code (Anthropic), run and checked by the "
          "team. Every minute in activity.csv is flagged observed, agent-checked, estimated or not visible.",
          "small"),
        P("[1] Timsit et al. 2016, Prev. Vet. Med. 135:67-73. [2] McGuirk 2008, Vet. Clin. North Am. Food Anim. "
          "Pract. 24:139-153. [3] Behavior assessment and applications for BRD diagnosis: beef, Anim. Health Res. "
          "Rev. 2020. [4] Toaff-Rosenstein, Gershwin, Tucker 2016, J. Anim. Sci. 94:3918-3932. [5] Wolfger et al. "
          "2015, J. Anim. Sci. 93:377-385; Sowell et al. 1999, J. Anim. Sci. 77:1105-1112. [6] Kayser et al. 2018, "
          "J. Anim. Sci. [7] Feedlot cattle with BRD vs case controls, AABP Proc.; ear-attached accelerometer for "
          "early BRD detection in calves, Animals 2022. [8] Wang et al. 2024, VideoAgent, ECCV. [9] Gao et al. "
          "2024, ear tag recognition, Sensors. [10] Borderas, de Passillé, Rushen 2008, J. Anim. Sci. 86:2920-2927. "
          "[11] Hixson et al. 2018, J. Dairy Sci. 101:10351-10360. [12] Theurer et al. 2013, J. Anim. Sci.; "
          "Eberhart et al. 2017, Holstein steers infected with M. haemolytica. [13] Timsit et al. 2011, Vet. J., "
          "reticulo-rumen temperature boluses.", "small"),
    ]

    def page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(letter[0] - 0.6 * inch, 0.4 * inch, f"{doc.page} / 2")
        canvas.restoreState()

    doc = SimpleDocTemplate(out, pagesize=letter, leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch,
                            title="Agentic vision for Angus behaviour and BRD risk")
    doc.build(story, onFirstPage=page, onLaterPages=page)


if __name__ == "__main__":
    numbers, outputs, out_pdf = sys.argv[1:4]
    build(json.load(open(numbers)), Path(outputs), out_pdf)
    print(f"wrote {out_pdf}")
