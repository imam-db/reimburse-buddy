"""The confirmation page for one pending plan (/admin/plans/<run_id>), as a pure HTML renderer.

Rendering the page is what "the reviewer was shown the notice" means: each injection notice on the page is
logged as `notice_shown` with the idempotency key <run_id>:notice_shown:<notice id>, so a refresh adds
nothing. Labels are Indonesian first, then English.
"""

from __future__ import annotations

from html import escape

from ..audit import AuditLog, make_event
from . import rules as R
from .directory import Directory

CSS = """body{font-family:system-ui,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#1d2433;overflow-x:hidden}
h1{font-size:22px} .muted{color:#667} .card{border:1px solid #d5dae3;border-radius:8px;padding:14px 16px;margin:14px 0}
.notice{background:#fff4e5;border:2px solid #e08a00;border-radius:8px;padding:10px 14px;margin:10px 0}
.assume{background:#eef6ff;border-left:4px solid #3a7bd5;padding:6px 10px;margin:6px 0}
.influenced{background:#ffe3e3;outline:2px solid #d33}
table{border-collapse:collapse} td,th{padding:4px 10px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
.table-wrap{overflow-x:auto;max-width:100%} img,pre{max-width:100%}
pre{white-space:pre-wrap;background:#f7f8fa;padding:8px;border-radius:6px;margin:4px 0}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;background:#eef;font-size:12px}
.done{background:#e3f6e8} .rej{background:#fde8e8}
button{min-height:44px;display:inline-flex;align-items:center;justify-content:center;padding:8px 18px;font-size:15px}"""

WHERE = {"email": "Email", "calendar": "Kalender / Calendar", "task": "Tugas / Task"}


def e(x) -> str:
    return escape(str(x if x is not None else ""))


def log_notices_shown(plan: dict, log: AuditLog) -> int:
    n = 0
    for nt in plan.get("notices", []):
        n += log.append(make_event(plan["run_id"], "notice_shown", nt["source_id"], actor="system",
                                   payload={"text": nt["text"], "source_id": nt["source_id"],
                                            "rules": nt.get("rules", []), "where": f"/admin/plans/{plan['run_id']}",
                                            "plan_id": plan["plan_id"]},
                                   sources=[nt["source_id"]]))
    return n


def _status(events: list[dict], aid: str) -> str | None:
    for ev in reversed(events):
        if ev.get("action_id") == aid and ev["event_type"] in ("execution", "rejection", "approval", "superseded"):
            return ev["event_type"]
    return None


def _person(directory: Directory, emp: str) -> str:
    c = directory.get(emp)
    return f"{e(c.name)} <span class='muted'>({e(emp)}, {e(c.role)}, {e(c.office)} {e(c.tz)})</span>" if c else e(emp)


def render(plan: dict, log: AuditLog, directory: Directory, rules: R.Rules, *, action_url: str | None = None,
           msg: str = "") -> str:
    log_notices_shown(plan, log)
    events = log.list(plan["run_id"])
    run = e(plan["run_id"])
    out = [f"<!doctype html><html><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width, initial-scale=1'>"
           f"<title>Konfirmasi rencana {run}</title>"
           f"<style>{CSS}</style></head><body>",
           f"<h1>Konfirmasi rencana / Plan confirmation</h1>",
           f"<p class=muted>Run {run} &middot; {e(plan['plan_id'])} &middot; peminta / requester: "
           f"{_person(directory, plan['requester'])}</p>",
           "<p><b>Tidak ada yang dikirim atau dijadwalkan sebelum Anda menyetujui.</b> "
           "Nothing is sent or scheduled until you approve. Mock tools only.</p>"]
    if msg:
        out.append(f"<p class=tag>{e(msg)}</p>")
    out.append(f"<div class=card><b>Permintaan / Request</b><pre>{e(plan['request'])}</pre></div>")
    for nt in plan.get("notices", []):
        out.append(f"<div class=notice><b>&#9888; Peringatan injeksi / Injection notice</b><br>"
                   f"{e(nt['id'])}<br><i>{e(nt['en'])}</i></div>")
    if plan.get("open_questions"):
        for q in plan["open_questions"]:
            out.append(f"<div class=notice><b>Pertanyaan / Question</b><br>{e(q['id_text'])}<br><i>{e(q['en'])}</i></div>")
    if plan.get("superseded"):
        out.append(f"<p class=muted>Menggantikan / supersedes: {e(', '.join(plan['superseded']))}</p>")
    by_action: dict[str, list[dict]] = {}
    for a in plan.get("assumptions", []):
        by_action.setdefault(a.get("action_id", ""), []).append(a)
    form = action_url is not None
    if form:
        out.append(f"<form method=post action='{e(action_url)}'><input type=hidden name=plan_id value='{e(plan['plan_id'])}'>")
    for i, a in enumerate(plan["actions"], 1):
        aid = a["action_id"]
        st = _status(events, aid)
        cls = " done" if st == "execution" else " rej" if st in ("rejection", "superseded") else ""
        infl = a.get("influenced_fields", {})

        def field(name, text, pre=False):
            c = " class=influenced" if name in infl else ""
            flag = (" <span class='tag influenced'>dipengaruhi konten yang ditandai / influenced by flagged "
                    "content</span>") if name in infl else ""
            return f"<pre{c}>{e(text)}</pre>{flag}" if pre else f"<span{c}>{e(text)}</span>{flag}"
        rows = [f"<tr><th>Siapa / Who</th><td>{'<br>'.join(_person(directory, w) for w in a['who'])}</td></tr>",
                f"<tr><th>Apa / What</th><td><span class=tag>{e(a['tool'])}</span> "
                + (field("subject", a["what"].get("subject", "")) if a["what"].get("subject")
                   else field("title", a["what"].get("title", "")))
                + field("body", a["what"].get("body", ""), pre=True) + "</td></tr>"]
        if a.get("when"):
            w = a["when"]
            loc = "".join(f"<tr><td>{_person(directory, p)}</td><td><b>{e(t)}</b></td></tr>"
                          for p, t in w["local"].items())
            alts = ", ".join(R.fmt_local(rules, R.parse_iso(x), directory.get(plan["requester"]).tz)
                             for x in w.get("alternatives", []))
            rows.append(f"<tr><th>Kapan / When</th><td>{e(w['duration_min'])} menit / minutes, "
                        f"<span class=muted>{e(w['start'])}</span><table>{loc}</table>"
                        + (f"<span class=muted>Alternatif / alternatives: {e(alts)}</span>" if alts else "")
                        + "</td></tr>")
        else:
            rows.append("<tr><th>Kapan / When</th><td>Segera setelah disetujui / right after approval</td></tr>")
        rows.append(f"<tr><th>Di mana / Where</th><td>{e(WHERE[a['where']])}</td></tr>")
        rows.append(f"<tr><th>Mengapa / Why</th><td>{e(', '.join(a['why']) or '-')}</td></tr>")
        assume = "".join(f"<div class=assume><b>Asumsi / Assumption:</b> {e(x['id'])}<br><i>{e(x['en'])}</i></div>"
                         for x in by_action.get(aid, []))
        decide = ""
        if form and st is None:
            decide = (f"<p><label><input type=radio name='decision:{e(aid)}' value=approve> Setujui / Approve</label> "
                      f"<label><input type=radio name='decision:{e(aid)}' value=reject checked> Tolak / Reject</label></p>"
                      f"<p><label>Catatan (opsional, agen akan mengusulkan rencana baru) / Note (optional, the agent "
                      f"proposes a new plan):<br><textarea name='note:{e(aid)}' rows=2 cols=70></textarea></label></p>")
        elif st:
            decide = f"<p class=tag>Status: {e(st)}</p>"
        out.append(f"<div class='card{cls}'><b>Aksi {i} / Action {i}</b> <span class=muted>{e(aid)} &middot; "
                   f"args_hash {e(a['args_hash'][:12])}</span><table>{''.join(rows)}</table>{assume}{decide}</div>")
    if form:
        out.append("<p><button type=submit>Kirim keputusan / Submit decisions</button></p></form>")
    out.append("</body></html>")
    return "".join(out)
