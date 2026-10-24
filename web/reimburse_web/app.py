"""Two-sided web simulation for reimburse-buddy (portfolio version B, not part of the hackathon demo).

Employee side: submit a claim, forward evidence to the shared reimburse@ mailbox, read messages from finance.
Finance side: batch triage, reviewer-confirmed bulk approval, exception detail with findings and evidence tools.

No LLM runs here: the reviewer drives the investigation and every amount comes from the rule engine.
A LangGraph agent will later plug in at `investigate()` and call the same tools over MCP.
"""

from __future__ import annotations

import os
import re
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.decision import draft_decision
from reimburse_buddy.models import rupiah
from reimburse_buddy.rules import check_claim
from reimburse_buddy.triage import approve_cleared_batch, triage_claims

BATCH = "2026-W43"
WEB_BATCH = "web-submissions"
MAILBOX = "reimburse@nusantara-niaga.example"
SIGNED, UNSIGNED = "signed", '<span class="tag bad">unsigned</span>'
CHOICES = ("approve_full", "approve_capped", "reject")
UPLOAD_DIR = Path(os.environ.get("REIMBURSE_WEB_UPLOADS", Path(__file__).resolve().parent.parent / "uploads"))

app = FastAPI(title="reimburse-buddy web simulation")
store = DataStore()
# Reviewer working state per claim: evidence IDs attached so far and item decisions.
REVIEW: dict[str, dict] = {}
# Employee upload drafts (drop-first flow), keyed by draft ID.
DRAFTS: dict[str, dict] = {}


def review(cid: str) -> dict:
    return REVIEW.setdefault(cid, {"evidence": [], "decisions": {}, "pending": {}, "evidence_log": []})


def pending_items(cid: str) -> dict[str, list[str]]:
    """Items whose uploaded evidence still awaits explicit reviewer acceptance: item_id -> message IDs.

    Such evidence (unreadable file, or doc type / amount only self-declared) is kept out of check_claim's
    evidence list until a reviewer accepts it, so the engine never treats it as verified.
    """
    out: dict[str, list[str]] = {}
    for mid, p in review(cid).get("pending", {}).items():
        if p["state"] == "pending":
            out.setdefault(p["item_id"], []).append(mid)
    return out


def reset_all() -> None:
    store.reset()
    REVIEW.clear()
    DRAFTS.clear()


def run_check(cid: str):
    r = review(cid)
    return check_claim(store, cid, list(r["evidence"]), dict(r["decisions"]))


def investigate(cid: str):
    """Placeholder seam: the future LangGraph agent proposes evidence here; today the reviewer does."""
    return run_check(cid)


# --- HTML helpers -----------------------------------------------------------
CSS = """body{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px;color:#1d1d1f}
nav a{margin-right:14px}table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border-bottom:1px solid #ddd;
padding:6px;text-align:left;vertical-align:top}th{background:#f4f4f6}.num{text-align:right}.tag{display:inline-block;
padding:1px 6px;border-radius:8px;background:#eef;font-size:12px;margin:1px}.warn{background:#fff3cd}.bad{background:#f8d7da}
.ok{background:#d1e7dd}.low{background:#fff3cd;border:1px solid #e0a800}.btn{display:inline-block;padding:6px 12px;
border:1px solid #888;border-radius:6px;text-decoration:none;color:inherit}.drop{display:block;border:2px dashed #999;
border-radius:10px;padding:30px;text-align:center;margin:10px 0}.drop.over{background:#eef}.cards{display:flex;flex-wrap:wrap;gap:12px}
.card{border:1px solid #ccc;border-radius:8px;padding:10px;width:330px}.card label{display:block;font-size:13px;margin-top:4px}
.err{color:#a00;font-size:13px}.box{border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}pre{white-space:pre-wrap;
background:#f7f7f9;padding:10px;border-radius:6px}.note{color:#666;font-size:13px}button{cursor:pointer}"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>{CSS}</style></head><body><nav><b>reimburse-buddy</b> &middot; <a href="/employee">Employee</a>
<a href="/admin">Finance admin</a><span class="note">Synthetic data. Simulation only.</span></nav>
<h1>{escape(title)}</h1>{body}</body></html>""")


def e(x) -> str:
    return escape(str(x))


def options(values, selected=None) -> str:
    return "".join(f'<option value="{e(v)}"{" selected" if v == selected else ""}>{e(label)}</option>' for v, label in values)


def provenance_badges(item: dict) -> str:
    prov = item.get("provenance") or {}
    if not prov:
        return ""
    def badge(k, v):
        cls = "warn" if v == "self_declared" else "ok"
        return f"<span class='tag {cls}' title='{e(k)} came from {e(v)}'>{e(k)}: {e(v)}</span>"
    return "<div>" + "".join(badge(k, v) for k, v in prov.items()) + "</div>"


def status_tag(s: str) -> str:
    cls = {"approved": "ok", "partially_approved": "warn", "rejected": "bad", "on_hold": "warn"}.get(s, "")
    return f'<span class="tag {cls}">{e(s)}</span>'


# --- index ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse("/admin", status_code=303)


# --- employee side ------------------------------------------------------------
@app.get("/employee", response_class=HTMLResponse)
def employee_page(emp: str = "EMP-001", msg: str = ""):
    staff = [x for x in store.employees if x.get("role", "staff") != "manager" and not x["id"].startswith("EMP-1")]
    me = store.get_employee(emp)
    claims = [c for c in store.claims if c["employee_id"] == emp]
    rows = "".join(
        f"<tr><td><a href='/employee/claims/{e(c['id'])}'>{e(c['id'])}</a></td><td>{e(c['purpose'])}</td><td>{e(c['trip']['destination'])} "
        f"{e(c['trip']['start_date'])} &rarr; {e(c['trip']['end_date'])}</td>"
        f"<td class=num>{e(rupiah(sum(Decimal(i['amount']) for i in c['items'])))}</td><td>{status_tag(c['status'])}</td></tr>"
        for c in claims)
    inbox = [m for m in store.outbox if m["to"] == emp]
    msgs = "".join(f"<tr><td>{e(m['id'])}</td><td>{e(m['claim_id'])}</td><td>{e(m['subject'])}</td><td>{e(m['body'])}</td></tr>"
                   for m in inbox) or ""
    fwd = [m for m in store.inbox if m.get("owner_id") == emp]
    fwd_rows = "".join(f"<tr><td>{e(m['id'])}</td><td>{e(m['date'])}</td><td>{e(m['subject'])}</td>"
                       f"<td>{e(', '.join(a['name'] for a in m.get('attachments', [])))}</td></tr>" for m in fwd)
    body = f"""
<form method="get" action="/employee">Signed in as (simulated):
<select name="emp" onchange="this.form.submit()">{options([(x['id'], x['id'] + ' ' + x['name']) for x in staff], emp)}</select></form>
{f'<p class="tag ok">{e(msg)}</p>' if msg else ''}
<h2>My claims</h2><table><tr><th>ID</th><th>Purpose</th><th>Trip</th><th class=num>Claimed</th><th>Status</th></tr>{rows}</table>
<h2>Messages from finance</h2>{'<table><tr><th>ID</th><th>Claim</th><th>Subject</th><th>Body</th></tr>' + msgs + '</table>' if msgs else '<p class=note>No messages yet.</p>'}
<div class="box"><h2>New claim</h2><p>Drop all your receipts and boarding passes at once; we draft the items for you.</p>
<p><a class="btn" href="/employee/new?emp={e(emp)}">Start a new claim</a></p></div>
<h2>Forwarded to the reimburse mailbox</h2><table><tr><th>ID</th><th>Date</th><th>Subject</th><th>Attachments</th></tr>{fwd_rows}</table>
<p class="note">Employee: {e(me['name'])} &middot; {e(me['email'])}</p>"""
    return page("Employee portal", body)


ITEM_RE = re.compile(r"^\s*(\w+)\s*,\s*(\d{4}-\d{2}-\d{2})\s*,\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*(yes|no)"
                     r"(?:\s*,\s*([A-Za-z0-9 ]*?))?\s*$", re.I)
AMOUNT_RE = re.compile(r"^(\d+|\d{1,3}(?:\.\d{3})+)$")  # 1350000 or 1.350.000; no decimals, no commas
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# No .txt: a text file is typed by the sender, so it can never be evidence read from a document.
ALLOWED_UPLOAD_EXT = {".pdf", ".png", ".jpg", ".jpeg"}
# Final statuses the reviewer may pick for each engine decision. Overrides go through logged item decisions.
STATUS_FOR_DECISION = {"approve": ["approved", "on_hold"], "partial_approve": ["partially_approved", "on_hold"],
                       "reject": ["rejected", "on_hold"], "needs_info": ["on_hold"]}


def parse_amount(text: str) -> Decimal:
    text = text.strip()
    if not AMOUNT_RE.match(text):
        raise ValueError(f"amount {text!r} must be whole Rupiah, e.g. 1350000 or 1.350.000 (no decimals)")
    return Decimal(text.replace(".", ""))


@app.post("/employee/claims")
def submit_claim(emp: str = Form(...), purpose: str = Form(...), destination: str = Form(...),
                 start_date: str = Form(...), end_date: str = Form(...), items: str = Form(...)):
    store.get_employee(emp)
    parsed = []
    for n, line in enumerate([x for x in items.splitlines() if x.strip()], 1):
        m = ITEM_RE.match(line)
        if not m:
            raise HTTPException(400, f"Item line {n} must be: category, YYYY-MM-DD, description, amount, yes/no")
        cat, d, desc, amt, rec, flight_no = m.groups()
        try:
            amount = parse_amount(amt)
        except ValueError as ex:
            raise HTTPException(400, f"Item line {n}: {ex}")
        flight_no = (flight_no or "").strip()
        if cat.lower() == "flight" and not flight_no:
            raise HTTPException(400, f"Item line {n}: flight items need a flight number as the 6th field, e.g. QZ 7520")
        it = {"id": f"IT-{n}", "category": cat.lower(), "date": d, "description": desc,
              "amount": str(amount), "receipt": rec.lower() == "yes"}
        if flight_no:
            it["flight_no"] = flight_no
        if it["category"] == "hotel":
            it["nights"] = max(1, (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days)
        parsed.append(it)
    if not parsed:
        raise HTTPException(400, "Add at least one item")
    cid = f"CLM-2026-W{len([c for c in store.claims if c.get('batch') == WEB_BATCH]) + 1:03d}"
    store.claims.append({"id": cid, "employee_id": emp, "status": "submitted", "submitted_at": date.today().isoformat(),
                         "purpose": purpose, "trip": {"origin": "Jakarta", "destination": destination,
                                                      "start_date": start_date, "end_date": end_date},
                         "items": parsed, "batch": WEB_BATCH})
    return RedirectResponse(f"/employee?emp={emp}&msg=Submitted+{cid}", status_code=303)


@app.post("/employee/forward")
async def forward_evidence(emp: str = Form(...), subject: str = Form(...), claim_id: str = Form(""),
                           att_type: str = Form("receipt"), item_id: str = Form(""), flight: str = Form(""),
                           att_date: str = Form(""), body: str = Form(""), file: UploadFile | None = File(None)):
    me = store.get_employee(emp)
    mid = f"MSG-W{len([m for m in store.inbox if m['id'].startswith('MSG-W')]) + 1:03d}"
    att = None
    if file is not None and file.filename:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_UPLOAD_EXT:
            raise HTTPException(400, f"File type {ext or '(none)'} not allowed; use {', '.join(sorted(ALLOWED_UPLOAD_EXT))}")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "File is larger than 5 MB")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename).name)[:80]
        (UPLOAD_DIR / f"{mid}_{safe}").write_bytes(data)
        att = {"name": safe, "type": att_type, "stored_as": f"{mid}_{safe}"}
        if att_type == "receipt":
            att.update(claim_id=claim_id, item_id=item_id)
        if att_type == "boarding_pass":
            att.update(flight=flight, date=att_date, passenger=me["name"])
    store.inbox.append({"id": mid, "owner_id": emp, "from": me["email"], "date": date.today().isoformat(),
                        "subject": subject, "body": body, "attachments": [att] if att else [],
                        "mailbox": MAILBOX, "forwarded_by": me["email"]})
    return RedirectResponse(f"/employee?emp={emp}&msg=Forwarded+{mid}", status_code=303)


# --- finance admin side --------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin(batch: str = BATCH, msg: str = ""):
    t = triage_claims(store, batch)
    exc = "".join(
        f"<tr><td><a href='/admin/claims/{e(x['claim_id'])}'>{e(x['claim_id'])}</a></td><td>{e(x['employee_id'])}</td>"
        f"<td>{''.join(f'<span class=tag>{e(g)}</span>' for g in x['gaps'])}</td><td>{e(', '.join(x['rejected_items']))}</td></tr>"
        for x in t["exceptions"])
    cleared = t["auto_cleared"]
    hidden = "".join(f'<input type="hidden" name="claim_ids" value="{e(c)}">' for c in cleared)
    others = "".join(f"<tr><td><a href='/admin/claims/{e(c['id'])}'>{e(c['id'])}</a></td><td>{e(c['employee_id'])}</td>"
                     f"<td>{e(c.get('batch', ''))}</td><td>{status_tag(c['status'])}</td></tr>" for c in store.claims)
    est = t["time_saved_estimate"]
    body = f"""{f'<p class="tag ok">{e(msg)}</p>' if msg else ''}
<form method="get">Batch <select name="batch" onchange="this.form.submit()">{options([(BATCH, BATCH), (WEB_BATCH, WEB_BATCH)], batch)}</select></form>
<div class="box"><h2>Triage: {t['claims']} claims, {len(cleared)} cleared, {len(t['exceptions'])} exceptions</h2>
<p>Time saved: {e(est['minutes'])} min <span class="tag warn">estimate</span> <span class="note">{e(est['assumption'])}</span></p>
<p class="note">Triage is read-only. Statuses change only after you confirm below.</p>
<h3>Cleared</h3><p>{''.join(f'<span class=tag>{e(c)}</span>' for c in cleared) or '<span class=note>None left.</span>'}</p>
{f'''<form method="post" action="/admin/batch/approve"><input type="hidden" name="batch" value="{e(batch)}">{hidden}
<label><input type="checkbox" name="reviewer_confirmed" value="true"> I reviewed these {len(cleared)} claims and approve them</label>
<button type="submit">Approve cleared batch</button></form>''' if cleared else ''}
<h3>Exceptions</h3><table><tr><th>Claim</th><th>Employee</th><th>Gaps</th><th>Rejected items</th></tr>{exc}</table></div>
<h2>All claims</h2><table><tr><th>Claim</th><th>Employee</th><th>Batch</th><th>Status</th></tr>{others}</table>
<form method="post" action="/admin/reset"><button type="submit">Reset demo data</button></form>"""
    return page("Finance admin: triage", body)


@app.post("/admin/batch/approve")
def batch_approve(batch: str = Form(...), claim_ids: list[str] = Form([]), reviewer_confirmed: str = Form("false")):
    held = [c for c in claim_ids if pending_items(c)]
    claim_ids = [c for c in claim_ids if c not in held]
    res = approve_cleared_batch(store, batch, claim_ids, reviewer_confirmed == "true")
    text = f"Approved {res['count']} claims" if res["ok"] else "Not approved: " + res["error"]
    return RedirectResponse(f"/admin?batch={batch}&msg={text.replace(' ', '+')}", status_code=303)


@app.post("/admin/reset")
def reset():
    reset_all()
    return RedirectResponse("/admin?msg=Demo+data+reset", status_code=303)


@app.get("/admin/claims/{cid}", response_class=HTMLResponse)
def claim_detail(cid: str, q: str = "", msg: str = ""):
    try:
        claim = store.get_claim(cid)
    except KeyError:
        raise HTTPException(404, f"Unknown claim {cid}")
    emp, trip = claim["employee_id"], claim["trip"]
    r = review(cid)
    c = investigate(cid)
    item_rows = ""
    for it in c.items:
        src = next(x for x in claim["items"] if x["id"] == it.item_id)
        finds = "<br>".join(f"<span class=tag>{e(f.rule)}</span> {e(f.message)}" for f in it.findings)
        gaps = "".join(f"<span class='tag warn'>{e(g)}</span>" for g in it.gaps)
        dec = ""
        if (it.gaps or it.human_decision) and it.item_id not in pending_items(cid):
            dec = (f"<form method=post action='/admin/claims/{e(cid)}/decide' style='display:inline'>"
                   f"<input type=hidden name=item_id value='{e(it.item_id)}'>"
                   + "".join(f"<button name=choice value='{ch}'{' disabled' if it.human_decision == ch else ''}>{ch}</button> "
                             for ch in CHOICES) + "</form>")
            if it.human_decision:
                dec += f"<div class=note>Reviewer: {e(it.human_decision)}</div>"
        pend = pending_items(cid).get(it.item_id, [])
        status_cell = e(it.status.value)
        if pend:
            status_cell = "<span class='tag warn'>pending review</span>"
            for mid in pend:
                p = r["pending"][mid]
                dec = (f"<form method=post action='/admin/claims/{e(cid)}/evidence/{e(mid)}/review' style='display:inline'>"
                       f"<div class=note>{e(mid)}: {e(p['reason'])}</div><button name=verdict value=accept>Accept evidence</button> "
                       f"<button name=verdict value=reject>Reject evidence</button></form>") + dec
        item_rows += (f"<tr><td>{e(it.item_id)}</td><td>{e(it.category)}<div class=note>{e(src['description'])}</div></td>"
                      f"<td class=num>{e(rupiah(it.claimed))}</td><td class=num>{e(rupiah(it.approvable))}</td>"
                      f"<td>{status_cell}</td><td>{provenance_badges(src)}{finds}<div>{gaps}</div></td><td>{e(', '.join(it.evidence_used))}</td><td>{dec}</td></tr>")
    log_rows = "".join(f"<li>{e(x['at'])} {e(x['evidence_id'])} ({e(x['item_id'])}, {e(x['reason'])}): "
                       f"<b>{e(x['verdict'])}</b> by reviewer</li>" for x in r["evidence_log"])
    ignored = "".join(f"<li>{e(i.id)}: <span class='tag bad'>{e(i.reason)}</span></li>" for i in c.ignored_evidence)
    warns = "".join(f"<li>{e(w.message)}</li>" for w in c.warnings)

    def attach_btn(eid: str) -> str:
        if eid in r["evidence"]:
            return "<span class='tag ok'>attached</span>"
        return (f"<form method=post action='/admin/claims/{e(cid)}/evidence' style='display:inline'>"
                f"<input type=hidden name=evidence_id value='{e(eid)}'><input type=hidden name=q value='{e(q)}'><button>Attach</button></form>")

    hits = store.search_reimburse_mailbox(emp, q) if q else []
    hit_rows = "".join(f"<tr><td>{e(h['id'])}</td><td>{e(h['date'])}</td><td>{e(h['subject'])}<div class=note>{e(h['snippet'])}</div></td>"
                       f"<td>{e(', '.join(h['attachments']))}</td><td>{attach_btn(h['id'])}</td></tr>" for h in hits)
    cal = store.get_calendar(emp, trip["start_date"], trip["end_date"])
    cal_rows = "".join(f"<tr><td>{e(x['id'])}</td><td>{e(x['date'])} {e(x['start'])}-{e(x['end'])}</td><td>{e(x['title'])}</td>"
                       f"<td>{e(x['location'])}</td><td>{attach_btn(x['id'])}</td></tr>" for x in cal)
    memos = store.find_approval_memo(emp, trip["destination"])
    memo_rows = "".join(f"<tr><td>{e(m['id'])}</td><td>{SIGNED if m.get('signed') else UNSIGNED}</td>"
                        f"<td>{e(m.get('reason', ''))}</td><td>{attach_btn(m['id'])}</td></tr>" for m in memos)
    sent = [m for m in store.outbox if m.get("claim_id") == cid]
    sent_rows = "".join(f"<li>{e(m['id'])} to {e(m['to'])}: {e(m['subject'])}</li>" for m in sent)
    note = draft_decision(store, c, language="both")
    allowed_status = STATUS_FOR_DECISION[c.decision.value]
    if pending_items(cid):
        allowed_status = ["on_hold"]
    body = f"""<p><a href="/admin">&larr; Triage</a></p>{f'<p class="tag ok">{e(msg)}</p>' if msg else ''}
<p>{e(emp)} {e(store.get_employee(emp)['name'])} &middot; {e(claim['purpose'])} &middot; {e(trip['destination'])}
{e(trip['start_date'])} &rarr; {e(trip['end_date'])} &middot; status {status_tag(claim['status'])}</p>
<div class="box"><h2>Rule engine: {e(c.decision.value)}</h2>
<p>Approvable <b>{e(rupiah(c.total_approvable))}</b> of {e(rupiah(c.total_claimed))} &middot; pending {e(rupiah(c.total_pending))}</p>
<p class="note">Every amount on this page comes from check_claim. Evidence attached: {e(', '.join(r['evidence']) or 'none')}</p>
<table><tr><th>Item</th><th>Category</th><th class=num>Claimed</th><th class=num>Approvable</th><th>Status</th><th>Findings / gaps</th><th>Evidence</th><th>Reviewer decision</th></tr>{item_rows}</table>
{f'<h3>Evidence review log</h3><ul>{log_rows}</ul>' if log_rows else ''}{f'<h3>Ignored evidence</h3><ul>{ignored}</ul>' if ignored else ''}{f'<h3>Warnings</h3><ul>{warns}</ul>' if warns else ''}</div>
<div class="box"><h2>Investigate</h2><p class="note">Today the reviewer investigates; the LangGraph agent will propose these steps later.</p>
<h3>Search the reimburse mailbox</h3><form method=get><input name=q value="{e(q)}" size=40 placeholder="boarding pass QZ 7520"><button>Search</button></form>
{f'<table><tr><th>ID</th><th>Date</th><th>Subject</th><th>Attachments</th><th></th></tr>{hit_rows}</table>' if hits else ''}
<h3>Calendar during the trip</h3><table><tr><th>ID</th><th>When</th><th>Title</th><th>Location</th><th></th></tr>{cal_rows}</table>
<h3>Approval memos ({e(trip['destination'])})</h3><table><tr><th>ID</th><th>Signed</th><th>Reason</th><th></th></tr>{memo_rows}</table>
<h3>Ask the employee</h3><form method=post action="/admin/claims/{e(cid)}/message">
<input name=subject size=40 value="Claim {e(cid)}: missing document"> <input name=body size=60 placeholder="Mohon kirim struk taksi tanggal 3 Okt (IT-5)." required>
<button>Send</button></form>{f'<ul>{sent_rows}</ul>' if sent_rows else ''}</div>
<div class="box"><h2>Decision note preview (EN + ID)</h2><pre>{e(note)}</pre>
<form method=post action="/admin/claims/{e(cid)}/status">Final status <select name=status>{options([(s, s) for s in allowed_status])}</select>
<label><input type=checkbox name=reviewer_confirmed value=true> I confirm this decision</label> <button>Apply status</button></form>
<p class="note">Status options follow the engine decision ({e(c.decision.value)}). To override, record an item decision above.
Status changes only after you confirm. Notes are sent only by you.</p></div>"""
    return page(f"Claim {cid}", body)


@app.post("/admin/claims/{cid}/evidence")
def add_evidence(cid: str, evidence_id: str = Form(...), q: str = Form("")):
    store.get_claim(cid)
    p = review(cid)["pending"].get(evidence_id)
    if p and p["state"] != "accepted":
        return RedirectResponse(f"/admin/claims/{cid}?msg=Not+attached:+{evidence_id}+needs+Accept+evidence+first",
                                status_code=303)
    ev = review(cid)["evidence"]
    if evidence_id not in ev:
        ev.append(evidence_id)
    return RedirectResponse(f"/admin/claims/{cid}?q={q}", status_code=303)


@app.post("/admin/claims/{cid}/evidence/{mid}/review")
def review_evidence(cid: str, mid: str, verdict: str = Form(...)):
    """Explicit reviewer verdict on evidence that extraction could not verify. Logged."""
    from datetime import datetime

    store.get_claim(cid)
    r = review(cid)
    p = r["pending"].get(mid)
    if not p:
        raise HTTPException(404, f"{mid} is not awaiting review on {cid}")
    if verdict not in ("accept", "reject"):
        raise HTTPException(400, "verdict must be accept or reject")
    p["state"] = "accepted" if verdict == "accept" else "rejected"
    for m in store.inbox:
        if m["id"] == mid:
            for a in m["attachments"]:
                a["evidence_status"] = "accepted_by_reviewer" if verdict == "accept" else "rejected_by_reviewer"
    if verdict == "accept" and mid not in r["evidence"]:
        r["evidence"].append(mid)
    r["evidence_log"].append({"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "evidence_id": mid, "item_id": p["item_id"],
                              "reason": p["reason"], "verdict": p["state"]})
    return RedirectResponse(f"/admin/claims/{cid}?msg=Evidence+{mid}+{p['state']}", status_code=303)


@app.post("/admin/claims/{cid}/decide")
def decide(cid: str, item_id: str = Form(...), choice: str = Form(...)):
    if choice not in CHOICES:
        raise HTTPException(400, f"choice must be one of {CHOICES}")
    if item_id in pending_items(cid):
        raise HTTPException(409, f"{item_id} has evidence awaiting review: accept or reject it first")
    review(cid)["decisions"][item_id] = choice
    try:
        run_check(cid)
    except ValueError as ex:
        review(cid)["decisions"].pop(item_id, None)
        raise HTTPException(400, str(ex))
    return RedirectResponse(f"/admin/claims/{cid}", status_code=303)


@app.post("/admin/claims/{cid}/message")
def message(cid: str, subject: str = Form(...), body: str = Form(...)):
    claim = store.get_claim(cid)
    res = store.send_message(claim["employee_id"], body, subject, cid)
    return RedirectResponse(f"/admin/claims/{cid}?msg=Sent+{res['message_id']}+replies+{res['replies_received']}", status_code=303)


@app.post("/admin/claims/{cid}/status")
def set_status(cid: str, status: str = Form(...), reviewer_confirmed: str = Form("false")):
    if reviewer_confirmed != "true":
        return RedirectResponse(f"/admin/claims/{cid}?msg=Not+changed:+tick+the+confirmation+first", status_code=303)
    c = run_check(cid)
    if pending_items(cid) and status != "on_hold":
        return RedirectResponse(f"/admin/claims/{cid}?msg=Not+changed:+evidence+is+awaiting+review", status_code=303)
    if status not in STATUS_FOR_DECISION[c.decision.value]:
        return RedirectResponse(f"/admin/claims/{cid}?msg=Not+changed:+{status}+is+not+allowed+while+the+engine+says+"
                                f"{c.decision.value}", status_code=303)
    store.update_claim_status(cid, status, f"Reviewer decision; rule engine approvable {rupiah(c.total_approvable)}")
    return RedirectResponse(f"/admin/claims/{cid}?msg=Status+set+to+{status}", status_code=303)


from .upload_flow import router as upload_router  # noqa: E402  (needs the names above)

app.include_router(upload_router)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
