"""End-to-end pipeline diagram (SVG -> high-resolution PNG via headless Chrome).

    python brief/pipeline_diagram.py OUT.png

Lanes for each stage, one line icon per component, numbered steps on the main flow, the agent with its
tool ring, and the live-alert loop back into the pipeline. Icons are drawn here (no third-party logos).
"""
from __future__ import annotations

import math
import subprocess
import sys
import tempfile
from pathlib import Path

NAVY, INK, MUTED = "#16324f", "#1f2933", "#52606d"
TINT, LANE, LINE = "#dcebfb", "#eef3f9", "#c9d6e3"
BLUE, CORAL, AMBER, AQUA, GREEN = "#2a78d6", "#e4572e", "#f2a541", "#1baf7a", "#107c41"
W, H = 2000, 860
FONT = "Helvetica Neue, Helvetica, Arial, sans-serif"
S = 'stroke="{c}" stroke-width="{w}" fill="{f}" stroke-linecap="round" stroke-linejoin="round"'


def st(c=NAVY, w=3, f="none"):
    return S.format(c=c, w=w, f=f)


def g(cx, cy, body, s=1.0):
    return f'<g transform="translate({cx},{cy}) scale({s})">{body}</g>'


def text(x, y, s, size=18, color=INK, weight=400, anchor="middle", italic=False):
    lines = s.split("\n")
    out = []
    for i, line in enumerate(lines):
        out.append(f'<text x="{x}" y="{y + i * size * 1.25}" font-family="{FONT}" font-size="{size}" '
                   f'fill="{color}" font-weight="{weight}" text-anchor="{anchor}"'
                   f'{" font-style=" + chr(34) + "italic" + chr(34) if italic else ""}>{line}</text>')
    return "".join(out)


# ---------------------------------------------------------------- icons (drawn in a ~80 px box at 0,0)
def camera():
    return (f'<rect x="-30" y="-16" width="60" height="36" rx="7" {st(f=TINT)}/>'
            f'<rect x="-11" y="-25" width="22" height="9" rx="2" {st(f=TINT)}/>'
            f'<circle cx="0" cy="2" r="11" {st(f="white")}/><circle cx="0" cy="2" r="4" fill="{NAVY}"/>'
            f'<circle cx="21" cy="-8" r="2.5" fill="{NAVY}"/>')


def cloud():
    return (f'<path d="M-30,16 h56 a15,15 0 0 0 -2,-30 a20,20 0 0 0 -38,-6 a15,15 0 0 0 -16,36 z" {st(f=TINT)}/>'
            f'<path d="M-6,8 v-16 M-12,-2 l6,-6 l6,6" {st(w=3)}/>')


def server():
    rows = "".join(f'<rect x="-30" y="{y}" width="60" height="18" rx="4" {st(f=TINT)}/>'
                   f'<circle cx="-19" cy="{y + 9}" r="3" fill="{AQUA}"/>'
                   f'<line x1="-6" y1="{y + 9}" x2="20" y2="{y + 9}" {st(w=2.5)}/>' for y in (-31, -9, 13))
    return rows


def film():
    holes = "".join(f'<rect x="{x}" y="-21" width="7" height="6" rx="1" fill="{NAVY}"/>'
                    f'<rect x="{x}" y="15" width="7" height="6" rx="1" fill="{NAVY}"/>' for x in (-27, -14, -1, 12, 25))
    return (f'<rect x="-34" y="-26" width="68" height="52" rx="5" {st(f=TINT)}/>{holes}'
            f'<rect x="-27" y="-11" width="22" height="22" rx="2" {st(w=2.5, f="white")}/>'
            f'<rect x="5" y="-11" width="22" height="22" rx="2" {st(w=2.5, f="white")}/>')


def cow(fill=NAVY):
    return (f'<rect x="-26" y="-9" width="42" height="21" rx="9" fill="{fill}"/>'
            f'<path d="M12,-12 L30,-9 L30,3 L16,6 Z" fill="{fill}"/>'
            f'<path d="M14,-12 L10,-19 L19,-13 Z" fill="{fill}"/>'
            + "".join(f'<rect x="{x}" y="8" width="6" height="15" rx="2" fill="{fill}"/>' for x in (-22, -12, 3, 10))
            + f'<path d="M-25,-5 Q-33,0 -31,10" {st(c=fill, w=3)}/>')


def detect():
    return (cow() + f'<rect x="-38" y="-27" width="74" height="55" rx="3" stroke="{CORAL}" stroke-width="3" '
            f'fill="none" stroke-dasharray="7 5"/><rect x="-38" y="-41" width="44" height="15" rx="3" fill="{CORAL}"/>'
            f'<text x="-16" y="-29.5" font-family="{FONT}" font-size="11" font-weight="700" fill="white" '
            f'text-anchor="middle">cow .94</text>')


def tracks():
    out = ""
    for (d, c) in ((f"M-32,22 C-12,12 4,-4 32,-14", NAVY), (f"M-32,2 C-14,0 8,10 32,4", BLUE),
                   (f"M-32,-20 C-14,-14 6,-28 32,-30", CORAL)):
        out += f'<path d="{d}" stroke="{c}" stroke-width="3" fill="none" stroke-dasharray="2 6" stroke-linecap="round"/>'
    for (x, y, c) in ((32, -14, NAVY), (32, 4, BLUE), (32, -30, CORAL)):
        out += f'<circle cx="{x}" cy="{y}" r="6" fill="{c}" stroke="white" stroke-width="2"/>'
    return out


def agent():
    pins = "".join(f'<line x1="{x}" y1="-44" x2="{x}" y2="-36" {st(w=4)}/><line x1="{x}" y1="36" x2="{x}" y2="44" {st(w=4)}/>'
                   f'<line x1="-44" y1="{x}" x2="-36" y2="{x}" {st(w=4)}/><line x1="36" y1="{x}" x2="44" y2="{x}" {st(w=4)}/>'
                   for x in (-20, 0, 20))
    return (pins + f'<rect x="-36" y="-36" width="72" height="72" rx="12" fill="{NAVY}"/>'
            f'<text x="0" y="9" font-family="{FONT}" font-size="26" font-weight="700" fill="white" '
            f'text-anchor="middle">VLM</text>'
            f'<path d="M24,-30 l3,8 l8,3 l-8,3 l-3,8 l-3,-8 l-8,-3 l8,-3 z" fill="{AMBER}"/>')


def zoom():
    return (f'<circle cx="-6" cy="-6" r="17" {st(f=TINT)}/><line x1="7" y1="7" x2="24" y2="24" {st(w=6)}/>'
            f'<line x1="-14" y1="-6" x2="2" y2="-6" {st(w=2.5)}/><line x1="-6" y1="-14" x2="-6" y2="2" {st(w=2.5)}/>')


def rewind():
    return (f'<circle cx="0" cy="0" r="17" {st(f=TINT)}/><path d="M0,-9 V0 L8,5" {st(w=3)}/>'
            f'<path d="M-25,-4 A25,25 0 1 1 -16,19" {st(w=3)}/><path d="M-31,-10 L-25,-2 L-18,-9" {st(w=3)}/>')


def cams():
    return (g(-12, -10, camera(), 0.55) + g(14, 14, camera(), 0.55)
            + f'<path d="M-30,18 Q-26,30 -10,30" {st(w=2.5)}/><path d="M28,-16 Q24,-30 8,-30" {st(w=2.5)}/>')


def card():
    return (f'<rect x="-30" y="-21" width="60" height="42" rx="6" {st(f=TINT)}/>'
            f'<circle cx="-13" cy="-3" r="8" {st(w=2.5, f="white")}/><path d="M-24,14 Q-13,4 -2,14" {st(w=2.5)}/>'
            f'<line x1="5" y1="-8" x2="22" y2="-8" {st(w=2.5)}/><line x1="5" y1="1" x2="22" y2="1" {st(w=2.5)}/>'
            f'<line x1="5" y1="10" x2="16" y2="10" {st(w=2.5)}/>')


def history():
    return (f'<path d="M-28,-24 V22 H30" {st(w=3)}/>'
            f'<polyline points="-22,12 -10,0 0,6 12,-12 24,-6" {st(c=BLUE, w=3)}/>'
            + "".join(f'<circle cx="{x}" cy="{y}" r="3.5" fill="{BLUE}"/>' for x, y in ((-10, 0), (12, -12))))


def shield():
    return (f'<path d="M0,-28 L24,-19 V2 C24,17 11,25 0,30 C-11,25 -24,17 -24,2 V-19 Z" '
            f'stroke="{GREEN}" stroke-width="3" fill="#e2f3e8" stroke-linejoin="round"/>'
            f'<path d="M-10,1 L-2,10 L12,-8" stroke="{GREEN}" stroke-width="4.5" fill="none" stroke-linecap="round" '
            f'stroke-linejoin="round"/>')


def tags():
    out = ""
    for dy, label, c in ((-14, "C08", CORAL), (12, "C09", BLUE)):
        out += (f'<path d="M-30,{dy - 11} h40 l12,11 l-12,11 h-40 z" stroke="{NAVY}" stroke-width="2.5" '
                f'fill="white" stroke-linejoin="round"/><circle cx="12" cy="{dy}" r="3" fill="{c}"/>'
                f'<text x="-12" y="{dy + 5}" font-family="{FONT}" font-size="13" font-weight="700" fill="{INK}" '
                f'text-anchor="middle">{label}</text>')
    return out


def timeline():
    pal = [BLUE, BLUE, AQUA, AQUA, AMBER, BLUE, BLUE, BLUE, CORAL, AMBER]
    out = ""
    for r in range(3):
        for i in range(10):
            c = pal[(i + 3 * r) % 10]
            out += f'<rect x="{-34 + i * 7}" y="{-18 + r * 13}" width="6" height="10" rx="1.5" fill="{c}"/>'
    return out + f'<line x1="-36" y1="24" x2="36" y2="24" {st(w=2)}/>'


def trough():
    return (f'<path d="M-32,-4 L32,-4 L24,16 L-24,16 Z" {st(f="#f6e7c8")}/>'
            f'<path d="M-24,-4 Q-12,-16 0,-6 Q12,-15 24,-4" stroke="{AMBER}" stroke-width="3" fill="#f9dfa7"/>'
            f'<line x1="-20" y1="16" x2="-20" y2="24" {st()}/><line x1="20" y1="16" x2="20" y2="24" {st()}/>')


def lying():
    return (f'<ellipse cx="-4" cy="8" rx="26" ry="11" fill="{NAVY}"/><circle cx="22" cy="0" r="8" fill="{NAVY}"/>'
            f'<path d="M24,-6 L22,-14 L30,-8 Z" fill="{NAVY}"/>'
            f'<path d="M-30,-14 Q0,-30 30,-18" stroke="{BLUE}" stroke-width="3" fill="none" '
            f'stroke-dasharray="3 6" stroke-linecap="round"/><circle cx="30" cy="-18" r="4" fill="{BLUE}"/>')


def gauge():
    arcs = ""
    for a0, a1, c in ((180, 240, AQUA), (240, 300, AMBER), (300, 360, CORAL)):
        x0, y0 = 32 * math.cos(math.radians(a0)), 32 * math.sin(math.radians(a0))
        x1, y1 = 32 * math.cos(math.radians(a1)), 32 * math.sin(math.radians(a1))
        arcs += f'<path d="M{x0:.1f},{y0 + 12:.1f} A32,32 0 0 1 {x1:.1f},{y1 + 12:.1f}" stroke="{c}" stroke-width="9" fill="none"/>'
    return (arcs + f'<line x1="0" y1="12" x2="20" y2="-10" {st(w=4)}/><circle cx="0" cy="12" r="6" fill="{NAVY}"/>')


def ranked():
    out = ""
    for i, (lab, wdt, c) in enumerate((("C08", 52, CORAL), ("C09", 52, CORAL), ("C10", 42, AMBER), ("C05", 37, AMBER),
                                        ("C06", 22, "#b9b8b1"))):
        y = -26 + i * 12
        out += (f'<text x="-26" y="{y + 8}" font-family="{FONT}" font-size="10" fill="{INK}" text-anchor="end">'
                f'{lab}</text><rect x="-22" y="{y}" width="{wdt}" height="8" rx="3" fill="{c}"/>')
    return out


def bell():
    return (f'<path d="M-20,12 C-16,8 -16,2 -16,-6 C-16,-16 -8,-24 0,-24 C8,-24 16,-16 16,-6 C16,2 16,8 20,12 Z" '
            f'{st(f=TINT)}/><path d="M-6,16 Q0,24 6,16" {st()}/>'
            f'<circle cx="19" cy="-19" r="11" fill="{CORAL}"/><text x="19" y="-14" font-family="{FONT}" '
            f'font-size="15" font-weight="700" fill="white" text-anchor="middle">!</text>')


def newclip():
    return (film() + f'<circle cx="28" cy="-24" r="12" fill="{AQUA}"/>'
            f'<path d="M28,-30 V-18 M22,-24 H34" stroke="white" stroke-width="3" stroke-linecap="round"/>')


def person():
    return (f'<circle cx="0" cy="-14" r="11" {st(f=TINT)}/>'
            f'<path d="M-22,26 C-22,4 22,4 22,26 Z" {st(f=TINT)}/>'
            f'<path d="M-30,-30 h20 v12 h-20 z" stroke="{NAVY}" stroke-width="2" fill="white"/>'
            f'<line x1="-26" y1="-26" x2="-14" y2="-26" stroke="{CORAL}" stroke-width="2"/>')


def gpu():
    return (f'<rect x="-30" y="-16" width="60" height="32" rx="5" {st(f=TINT)}/>'
            f'<rect x="-20" y="-8" width="16" height="16" rx="2" fill="{NAVY}"/>'
            f'<circle cx="14" cy="0" r="9" {st(w=2.5, f="white")}/>'
            + "".join(f'<line x1="{x}" y1="16" x2="{x}" y2="22" {st(w=2.5)}/>' for x in (-20, -10, 0, 10, 20)))


# ---------------------------------------------------------------- composition
def arrow(x1, y1, x2, y2, c=NAVY, w=3, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{c}" stroke-width="{w}"{d} '
            f'marker-end="url(#ah{"" if c == NAVY else c[1:]})"/>')


def num(x, y, n):
    return (f'<circle cx="{x}" cy="{y}" r="17" fill="{GREEN}"/>'
            f'<text x="{x}" y="{y + 6.5}" font-family="{FONT}" font-size="18" font-weight="700" fill="white" '
            f'text-anchor="middle">{n}</text>')


def build() -> str:
    o = []
    o.append(f'<rect width="{W}" height="{H}" fill="white"/>')
    o.append(text(30, 38, "BRD agentic vision: end-to-end pipeline", 30, NAVY, 700, "start"))
    o.append(text(30, 64, "Pen 6, 10 Angus cows, 3 cameras, 2 days of video  →  who is who, what she does every "
                  "minute, who may be getting sick, and a live alert", 17, MUTED, 400, "start"))
    lanes = [("Ingest", None, 20, 330), ("Detect + track", None, 365, 340), ("Agentic vision", "Level 1", 720, 560),
             ("Predict BRD risk", "Level 2", 1295, 330), ("Early warning", "Level 3", 1640, 340)]
    tagc = {"Level 1": BLUE, "Level 2": CORAL, "Level 3": AQUA}
    for name, tag, x, w in lanes:
        o.append(f'<rect x="{x}" y="100" width="{w}" height="585" rx="14" fill="{LANE}"/>')
        o.append(text(x + w / 2, 140, name, 25, NAVY, 700))
        if tag:
            o.append(f'<rect x="{x + w / 2 - 38}" y="152" width="76" height="24" rx="12" fill="{tagc[tag]}"/>')
            o.append(text(x + w / 2, 169, tag, 14, "white", 700))
    Y = 300  # main row
    # lane A
    o.append(g(120, Y - 18, camera(), 0.95) + g(185, Y - 40, camera(), 0.95) + g(250, Y - 18, camera(), 0.95))
    o.append(text(185, Y + 40, "3 pen cameras", 18, INK, 700) + text(185, Y + 62, "back · front · top", 16, MUTED))
    o.append(g(185, 505, cloud(), 1.1) + text(185, 560, "1,752 clips · 154 GB", 16, INK, 700)
             + text(185, 581, "HEVC, 12 MP, 30 fps, 48 h", 15, MUTED) + text(185, 602, "copied to Grace (rclone)", 15, MUTED))
    o.append(arrow(185, Y + 80, 185, 455, MUTED, 2))
    # lane B
    o.append(g(535, Y - 20, detect(), 1.35))
    o.append(text(535, Y + 40, "YOLO11x finds every cow", 18, INK, 700) + text(535, Y + 62, "1.5 M detections (FP16, A100)", 16, MUTED))
    o.append(g(450, 505, film(), 1.0) + text(450, 560, "keyframes", 16, INK, 700) + text(450, 581, "1 every 2 s", 15, MUTED))
    o.append(g(620, 505, tracks(), 1.0) + text(620, 560, "ByteTrack", 16, INK, 700) + text(620, 581, "tracks across clips", 15, MUTED))
    o.append(arrow(535, Y + 80, 535, 460, MUTED, 2))
    # lane C: agent + tool ring
    ax, ay = 1000, Y + 5
    rx, ry = 150, 92
    o.append(f'<ellipse cx="{ax}" cy="{ay}" rx="{rx}" ry="{ry}" stroke="{LINE}" stroke-width="2.5" fill="none" '
             f'stroke-dasharray="4 7"/>')
    tools = [("zoom", zoom(), -150), ("other\ncameras", cams(), -30), ("reference\ncrops", card(), 150),
             ("track\nhistory", history(), 30), ("look back /\nforward", rewind(), 90)]
    for label, icon, ang in tools:
        tx, ty = ax + rx * math.cos(math.radians(ang)), ay + ry * math.sin(math.radians(ang))
        o.append(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="33" fill="white" stroke="{LINE}" stroke-width="2"/>')
        o.append(g(round(tx, 1), round(ty, 1), icon, 0.7))
        right = math.cos(math.radians(ang)) > -0.3
        lx = tx + (46 if right else -46)
        o.append(text(round(lx, 1), round(ty - 2, 1), label, 14, MUTED, 400, "start" if right else "end"))
    o.append(g(ax, ay, agent(), 1.15))
    o.append(text(ax, ay + 150, "Qwen3.5-9B agent: plan → call a tool → observe → answer", 16, INK, 700))
    for x, icon, t1, t2 in ((835, tags(), "who is who", "10 cows · audit every 30 min"),
                            (1000, timeline(), "what she does", "28,740 cow-minutes"),
                            (1165, shield(), "self-check", "fails go back, never applied")):
        o.append(g(x, 530, icon, 1.05) + text(x, 585, t1, 16, INK, 700) + text(x, 606, t2, 14, MUTED))
        o.append(arrow(ax + (x - ax) * 0.35, ay + 162, x, 490, MUTED, 2))
    # lane D
    o.append(g(1460, Y - 15, gauge(), 1.45))
    o.append(text(1460, Y + 40, "relative risk 0–1", 18, INK, 700) + text(1460, Y + 62, "vs herd and her own day 1", 16, MUTED))
    o.append(g(1380, 505, trough(), 0.95) + text(1380, 560, "bunk time,", 15, INK, 700) + text(1380, 579, "missed meals", 15, INK, 700))
    o.append(g(1545, 505, lying(), 0.95) + text(1545, 560, "fewer, longer", 15, INK, 700) + text(1545, 579, "lying bouts", 15, INK, 700))
    o.append(text(1460, 618, "top risk: C08 · C09", 16, CORAL, 700))
    o.append(arrow(1460, Y + 80, 1460, 460, MUTED, 2))
    # lane E
    o.append(g(1810, Y - 18, bell(), 1.5))
    o.append(text(1810, Y + 40, "alert + snapshot + clip", 18, INK, 700) + text(1810, Y + 62, "C08 skipped the evening meal", 16, MUTED))
    o.append(g(1725, 505, newclip(), 0.95) + text(1725, 560, "any new clip", 15, INK, 700) + text(1725, 579, "(folder watch)", 14, MUTED))
    o.append(g(1895, 505, person(), 1.0) + text(1895, 560, "pen rider / vet", 15, INK, 700) + text(1895, 579, "webhook, text", 14, MUTED))
    o.append(arrow(1810, Y + 80, 1810, 460, MUTED, 2))
    # main flow with numbered steps
    for (x1, x2, n, lab) in ((290, 455, 1, "video"), (615, 800, 2, "tracks + frames"), (1200, 1385, 3, "behaviour"),
                             (1535, 1745, 4, "risk scores")):
        o.append(arrow(x1, Y - 15, x2, Y - 15, NAVY, 3.5))
        mx = (x1 + x2) / 2
        o.append(num(mx, Y - 48, n) + text(mx, Y + 8, lab, 14, MUTED))
    # live loop: new clip back through the same pipeline
    o.append(f'<path d="M1725,592 V638 H1968 V86 H1000 V98" stroke="{AQUA}" stroke-width="3" fill="none" '
             f'stroke-dasharray="9 7" stroke-linejoin="round" marker-end="url(#ah{AQUA[1:]})"/>')
    o.append(num(1250, 86, 5) + text(1276, 78, "any new clip runs through the same detect, ID and behaviour steps",
                                     14, MUTED, 400, "start"))
    # outputs strip
    outs = [(185, "raw/ · clips.csv"), (535, "dets · frames"), (1000, "activity.csv · ID-check frames · agent traces"),
            (1460, "brd_risk.csv · risk cards"), (1810, "alerts/ · live_risk.csv")]
    for x, s_ in outs:
        o.append(text(x, 668, s_, 14, MUTED, 400, "middle", italic=True))
    # compute band
    o.append(f'<rect x="20" y="705" width="1960" height="130" rx="14" fill="#f7f7f4" stroke="{LINE}" stroke-width="2"/>')
    o.append(g(95, 770, server(), 1.15) + g(210, 770, gpu(), 1.05) + g(300, 770, gpu(), 1.05))
    o.append(text(370, 752, "Runs on TAMU HPRC Grace (Slurm)", 20, NAVY, 700, "start"))
    o.append(text(370, 781, "Tracking: 6 camera-days in parallel on one A100 (~2 h)   ·   Agent: Qwen3.5-9B on vLLM, "
                  "up to 4 × A100 40 GB, requests spread round-robin (~1 h)", 16, MUTED, 400, "start"))
    o.append(text(370, 808, "Every agent episode is saved as a trace; every minute is flagged observed, agent-checked, "
                  "estimated or not visible", 16, MUTED, 400, "start"))
    defs = "".join(f'<marker id="ah{"" if c == NAVY else c[1:]}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" '
                   f'markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
                   for c in (NAVY, MUTED, AQUA))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
            f'<defs>{defs}</defs>{"".join(o)}</svg>')


def render(out_png: str, scale: int = 3) -> None:
    svg = build()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "p.html").write_text(f'<html><body style="margin:0">{svg}</body></html>')
    Path(out_png).with_suffix(".svg").write_text(svg)
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--force-device-scale-factor={scale}", f"--window-size={W},{H}",
                    f"--screenshot={Path(out_png).resolve()}", f"file://{tmp / 'p.html'}"],
                   check=True, capture_output=True)


if __name__ == "__main__":
    render(sys.argv[1])
    print("wrote", sys.argv[1])
