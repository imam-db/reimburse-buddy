"""Generate docs/architecture.svg, the version B architecture diagram used in README.md.

Same palette and style as docs/agent-how-it-works.svg. Edit the boxes below and run:

    python docs/architecture_diagram.py

Then render the PNG fallback (3200 px wide) with headless Chrome:

    chrome --headless=new --hide-scrollbars --force-device-scale-factor=2 --window-size=1600,880 \
        --screenshot=docs/architecture.png docs/architecture.svg
"""

from __future__ import annotations

from html import escape
from pathlib import Path

W, H = 1600, 880
EMPLOYEE = ("#b2f2bb", "#2f9e44")
AGENT = ("#a5d8ff", "#1971c2")
CODE = ("#d0bfff", "#6741d9")
HUMAN = ("#ffd8a8", "#e8590c")
DATA = ("#e9ecef", "#495057")
GUARD = ("#fff5f5", "#c92a2a")
LINE_H = 16.25

out: list[str] = []


def box(x, y, w, h, colors, lines, align="middle"):
    fill, stroke = colors
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    top = y + h / 2 - (len(lines) - 1) * LINE_H / 2 + 4.55
    tx = x + w / 2 if align == "middle" else x + 14
    for i, line in enumerate(lines):
        weight = ' font-weight="bold"' if i == 0 else ""
        out.append(f'<text x="{tx}" y="{top + i * LINE_H:.2f}" font-size="13" text-anchor="{"middle" if align == "middle" else "start"}"'
                   f'{weight}>{escape(line)}</text>')


def arrow(points, label=None, at=None, both=False, dashed=False):
    pts = " ".join(f"{x},{y}" for x, y in points)
    extra = (' marker-start="url(#ahs)"' if both else "") + (' stroke-dasharray="8 6"' if dashed else "")
    out.append(f'<polyline points="{pts}" fill="none" stroke="#1e1e1e" stroke-width="2" marker-end="url(#ah)"{extra}/>')
    if label:
        lx, ly = at
        out.append(f'<text x="{lx}" y="{ly}" font-size="12" text-anchor="middle" fill="#343a40">{escape(label)}</text>')


# Title and legend
box(30, 20, 1540, 56, DATA, ["Reimburse Buddy version B: architecture"])
for i, (colors, name) in enumerate([(EMPLOYEE, "Employee"), (AGENT, "AI agent / UI flow"), (CODE, "Deterministic code"),
                                     (HUMAN, "Finance (human)"), (DATA, "Data / mock systems"), (GUARD, "Guardrail")]):
    fill, stroke = colors
    x = 30 + i * 258
    out.append(f'<rect x="{x}" y="92" width="240" height="28" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    out.append(f'<text x="{x + 120}" y="110.5" font-size="13" text-anchor="middle">{escape(name)}</text>')

# Columns: actors | c1 | c2 | gate | executor | mock tools
AX, AW = 30, 150
C1, C1W = 215, 225
C2, C2W = 475, 250
GX, GW = 760, 230
EX, EW = 1025, 230
MX, MW = 1290, 280

# Claims lane
box(AX, 150, AW, 90, EMPLOYEE, ["Employee", "(browser)"])
box(C1, 150, C1W, 90, EMPLOYEE, ["Employee portal", "claims, uploads, OCR draft;", "files go to the reimburse@", "mailbox"])
box(C2, 150, C2W, 90, AGENT, ["Claim agent", "LangGraph + DeepSeek, own process;", "searches evidence, drafts the", "decision note"])
box(C1, 280, C1W, 80, AGENT, ["Employee Q&A", "flag EMPLOYEE_QA; own claims +", "rulebook only; cites a source"])
box(C2, 280, C2W, 80, CODE, ["Rule engine (check_claim)", "Decimal; every amount and gap", "+ read-only MCP tools"])

# Requests lane
box(AX, 440, AW, 90, HUMAN, ["Finance reviewer", "(browser)"])
box(C1, 440, C1W, 90, HUMAN, ["Request intake", "/admin/intake: request in", "own words + pasted document"])
box(C2, 440, C2W, 90, AGENT, ["Plan graph (no LLM)", "propose -> resolve -> schedule;", "asks \"which Budi?\" before", "anything is staged"])
box(C2, 570, C2W, 70, CODE, ["Directory + operating rules", "recipients are EMP- IDs; times", "come from the rules tool"])

# Gate, executor, tools
box(GX, 150, GW, 490, HUMAN, ["Approval gate", "", "Claim agent: one interrupt", "per write, evidence requests", "included", "", "Plans: confirmation page", "/admin/plans/<run_id>,", "Reject is pre-selected", "", "Approval is bound to the", "args_hash of what the", "reviewer saw"])
box(EX, 150, EW, 490, CODE, ["Executor (separate node)", "", "Runs an action only if the", "audit log holds a matching", "approval (same action_id", "and args_hash)", "", "Claim writes re-run", "check_claim first", "", "Anything else is refused:", "fail closed"])
box(MX, 150, MW, 490, DATA, ["Mock office tools", "", "email, calendar event, task,", "evidence request,", "claim status, outbox", "", "Synthetic data only;", "nothing real is sent"])

# Arrows
arrow([(AX + AW, 195), (C1, 195)])
arrow([(105, 240), (105, 320), (C1, 320)], "asks", (150, 313))
arrow([(C1 + C1W, 195), (C2, 195)])
arrow([(C1 + C1W, 320), (C2, 320)])
arrow([(600, 240), (600, 280)], both=True)
arrow([(C2 + C2W, 195), (GX, 195)])
arrow([(AX + AW, 485), (C1, 485)])
arrow([(C1 + C1W, 485), (C2, 485)])
arrow([(600, 530), (600, 570)], both=True)
arrow([(C2 + C2W, 485), (GX, 485)])
arrow([(105, 530), (105, 670), (875, 670), (875, 640)], "approves / rejects, answers questions", (480, 663))
arrow([(GX + GW, 395), (EX, 395)])
arrow([(EX + EW, 395), (MX, 395)])

# Guardrails
box(30, 700, 1540, 34, GUARD, ["Safety guardrails (always on)"])
gw = (1540 - 3 * 20) / 4
for i, lines in enumerate([
    ["Audit log, append-only", "Postgres (Neon) in prod, unique", "event_key; app role may only", "INSERT and SELECT"],
    ["Prompt-injection detector", "HIGH/LOW rules, withheld spans,", "echo check, argument-source", "allowlist, needs_review flag"],
    ["The rule engine decides money", "not the LLM; documents are data,", "never instructions; no mass send"],
    ["Caps per claim run", "250k tokens, 600 s, 10 gated calls,", "3 searches per gap; 3 runs/min,", "1 live run at a time"],
]):
    box(30 + i * (gw + 20), 750, gw, 100, GUARD, lines)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       'font-family="Helvetica,Arial,sans-serif"><rect width="100%" height="100%" fill="white"/><defs>'
       '<marker id="ah" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">'
       '<path d="M0,0 L10,4 L0,8z" fill="#1e1e1e"/></marker>'
       '<marker id="ahs" markerWidth="10" markerHeight="8" refX="1" refY="4" orient="auto">'
       '<path d="M10,0 L0,4 L10,8z" fill="#1e1e1e"/></marker></defs>\n' + "\n".join(out) + "\n</svg>\n")
Path(__file__).with_name("architecture.svg").write_text(svg, encoding="utf-8")
print("wrote docs/architecture.svg")
