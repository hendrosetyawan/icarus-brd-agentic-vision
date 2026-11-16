# Team ICARUS · Agentic Vision for Angus Behavior and BRD Prediction

**Members:** Hendro Setyawan, Ruri Kaimudin, Ikhwan Fadlu
Merck–TAMU Animal Science Hackathon 2026, Problem 2.

Start with `technical_brief.pdf` (2 pages) and the [presentation video](https://youtu.be/htUo69yyhLI) (4:59).

## Where each required item is

| Challenge asks for | File |
|---|---|
| Code that reproduces all results, README with exact commands, model and versions | `code/` (start at `code/README.md`) |
| activity.csv: one row per behaviour bout, every cow, every minute, both days | `outputs/level1/activity.csv` (per minute: `activity_minutes.csv`) |
| ID-check frames every 30 min on each camera | `outputs/level1/id_check/<camera>/` (cow gallery: `gallery_sheet.jpg`) |
| Per-cow time budgets | `outputs/level1/time_budgets.csv` and `.png` |
| Three agent reasoning traces: success, failure, night | `outputs/level1/agent_traces/{success,failure,night}/trace.md` |
| brd_risk.csv: cow ID, risk 0–1, rank, top reasons | `outputs/level2/brd_risk.csv` |
| BRD signs per cow (DART: depression, appetite, respiration, fever proxy) | `outputs/level2/brd_signs.csv` and `brd_signs.png` |
| Visualisation tool | `code/app.py` (Streamlit; see README step 7) |
| One-page risk card for the top 3 cows with 2–3 video clips | `outputs/level2/risk_cards/<cow>/card.html` and `clip*.mp4` |
| Live alert with video evidence on a new clip | `outputs/level3/alerts/<clip>/<cow>.{json,jpg,mp4}`, `live_risk.csv`, `alerts.jsonl` |
| Technical brief, max 2 pages | `technical_brief.pdf` |
| Presentation video | https://youtu.be/htUo69yyhLI |

## Results at a glance

- 28,740 cow-minutes labelled for 10 cows over 2 days; behaviour accuracy 0.51 against 144 hand-labelled minutes.
- BRD risk: **C08 and C09 – inspect today.** Both show lethargy and poor appetite together (lying 74–79 % in very long bouts, missing 79–84 % of herd meals), worse on day 2. C09 scores highest on the fever-like behaviour index: take her temperature first.
- Live alert: on the day-2 17:30 clip, C08 skipped the evening meal while the herd ate.
- The risk score is a relative ranking, not a diagnosis; temperature cannot be seen on video.

No raw video or extracted frames are included. Event data stay within the event.
