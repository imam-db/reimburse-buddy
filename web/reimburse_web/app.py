"""Two-sided web simulation for reimburse-buddy (version B, the Challenge B demo).

Employee side: submit a claim, forward evidence to the shared reimburse@ mailbox, read messages from finance.
Finance side: batch triage, reviewer-confirmed bulk approval, exception detail with findings and evidence tools.

The only LLM path is "Selidiki dengan agent", which starts `agent/investigate_worker.py` as a subprocess
(see agent_bridge.py); every amount still comes from the rule engine. All routes sit behind the demo
login in auth.py (email + password against app_users; always on in production).
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.decision import draft_decision
from reimburse_buddy.models import rupiah
from reimburse_buddy.rules import check_claim
from reimburse_buddy.triage import approve_cleared_batch, triage_claims

from . import auth, plan_state, spend, users
from .agent_bridge import NO_KEY_MESSAGE, Investigator, RateLimited, key_configured
from .intake import router as intake_router
from . import manager
from .manager import router as manager_router
from .labels import label, person
from .plans import router as plans_router, startup_config_check, startup_sweep
from .qa import portal_box, router as qa_router

BATCH = "2026-W43"
# A second batch with one claim owned by EMP-101, so the self-approval refusal can be shown on the claim
# side without changing the triage numbers of the demo batch (10 claims, 7 cleared, 3 exceptions).
OWN_BATCH = "2026-W44"
WEB_BATCH = "web-submissions"
MAILBOX = "reimburse@nusantara-niaga.example"
SIGNED, UNSIGNED = "signed", '<span class="tag bad">unsigned</span>'
CHOICES = ("approve_full", "approve_capped", "reject")
UPLOAD_DIR = Path(os.environ.get("REIMBURSE_WEB_UPLOADS", Path(__file__).resolve().parent.parent / "uploads"))

# Startup order matters: refuse a production deployment without a Postgres audit log before doing any
# work, then one without a user database / session key, then one that cannot cap or charge a model call,
# then one whose plan store or checkpoint tables are unusable (REIMBURSE_PLAN_BACKEND=postgres), and
# finally run the one-per-process expiry sweep (agent workers opening the log never sweep).
app = FastAPI(title="reimburse-buddy web simulation",
              on_startup=[startup_config_check, auth.startup_check, spend.startup_check,
                          plan_state.startup_check, startup_sweep])
app.include_router(plans_router)
app.include_router(intake_router)
app.include_router(manager_router)
app.include_router(qa_router)
store = DataStore()
# Reviewer working state per claim: evidence IDs attached so far and item decisions.
REVIEW: dict[str, dict] = {}
# Employee upload drafts (drop-first flow), keyed by draft ID.
DRAFTS: dict[str, dict] = {}
# LangGraph agent runs (subprocess per run, see agent_bridge.py).
INVESTIGATOR = Investigator()


# --- demo password gate ------------------------------------------------------------------
def client_ip(request: Request) -> str:
    """Rate-limit key. Behind a proxy every request shares one address, which only makes the limit
    stricter; X-Forwarded-For is deliberately not trusted here."""
    return request.client.host if request.client else ""


@app.middleware("http")
async def require_demo_login(request: Request, call_next):
    """Close every route except /login, /logout and /healthz when the login gate is on.

    A browser navigation (GET/HEAD) is redirected to the form; anything else gets 401, so a script
    cannot post its way in. Each role may only open its own area (employee, manager, finance); a wrong-role
    navigation is sent to the role's landing page, anything else gets 403. With no user store or DSN outside production the gate
    is off and every request passes through untouched."""
    if not auth.enabled() or request.url.path in auth.PUBLIC_PATHS:
        return await call_next(request)
    session = auth.session_of(request)
    if session is None:
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(auth.login_url(request.url.path), status_code=303)
        return JSONResponse({"detail": "Silakan masuk dulu. / Please sign in first."}, status_code=401)
    if not auth.may_open(session.role, request.url.path):
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(auth.home_for(session.role), status_code=303)
        return JSONResponse({"detail": "Akses ditolak. / Access denied."}, status_code=403)
    token = auth.set_current(session.reviewer, session.role)
    try:
        return await call_next(request)
    finally:
        auth.reset_current(token)


def review(cid: str) -> dict:
    return REVIEW.setdefault(cid, {"evidence": [], "decisions": {}, "pending": {}, "evidence_log": []})


def self_approval_refusal(request: Request, cid: str, attempt: str) -> str | None:
    """The Indonesian refusal when the signed-in reviewer owns this claim, else None.

    Fail closed: the caller returns before touching any state. Only the approval direction is refused
    (accept evidence, approve an item, move the status off `on_hold`, approve a staged write); rejecting
    or holding your own claim cannot approve it, so those stay available.

    The audit entry is an `access_denied` event attributed to `human:<reviewer>`, one per claim and
    attempt, so a double submit cannot flood the log."""
    try:
        owner = store.get_claim(cid)["employee_id"]
    except KeyError:
        return None  # let the handler answer 404 as before
    reviewer = auth.reviewer_of(request)
    if not reviewer or owner != reviewer:
        return None
    auth.audit_self_approval(f"claim:{cid}", reviewer, f"self_approval:claim:{cid}:{attempt}",
                             claim_id=cid, attempt=attempt)
    return label("self_approval_blocked")


def back_to_claim(cid: str, message: str) -> RedirectResponse:
    """Redirect to the claim page with the message in `msg` (the page shows it as a tag)."""
    return RedirectResponse(f"/admin/claims/{cid}?msg={quote(message, safe='')}", status_code=303)


def submitter_of(request: Request) -> str:
    """The signed-in reviewer, recorded as the plan's submitter; "" when the demo gate is off.

    With the gate off there is no identity to record, so the intake keeps the old behaviour (the requester
    field is the only source, and tests/test_request_intake.py is unaffected)."""
    return auth.reviewer_of(request) if auth.enabled() else ""


def submitted_by_note(reviewer: str) -> str:
    """Intake form note: the request is filed under the signed-in reviewer, whatever requester is picked."""
    if not reviewer:
        return ""
    return (f'<p class="note" id="submitted-by">{e(label("submitted_by_note"))} '
            f'<b>{e(person(store, reviewer))}</b>.</p>')


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
    INVESTIGATOR.stop_all()


def run_check(cid: str):
    r = review(cid)
    return check_claim(store, cid, list(r["evidence"]), dict(r["decisions"]))


def investigate(cid: str):
    """Placeholder seam: the future LangGraph agent proposes evidence here; today the reviewer does."""
    return run_check(cid)


# --- HTML helpers -----------------------------------------------------------
CSS = """*{box-sizing:border-box}body{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px;
color:#1d1d1f;overflow-x:hidden}
nav a{margin-right:14px}table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border-bottom:1px solid #ddd;
padding:6px;text-align:left;vertical-align:top}th{background:#f4f4f6}.num{text-align:right}.tag{display:inline-block;
padding:1px 6px;border-radius:8px;background:#eef;font-size:12px;margin:1px}.warn{background:#fff3cd}.bad{background:#f8d7da}
.ok{background:#d1e7dd}.low{background:#fff3cd;border:1px solid #e0a800}
.table-wrap{overflow-x:auto;max-width:100%}img,pre{max-width:100%}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:6px 12px;
border:1px solid #888;border-radius:6px;text-decoration:none;color:inherit}.drop{display:block;border:2px dashed #999;
border-radius:10px;padding:30px;text-align:center;margin:10px 0}.drop.over{background:#eef}.cards{display:flex;flex-wrap:wrap;gap:12px}
.card{border:1px solid #ccc;border-radius:8px;padding:10px;flex:1 1 300px;max-width:100%;box-sizing:border-box}
.card label{display:block;font-size:13px;margin-top:4px}
.err{color:#a00;font-size:13px}.box{border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}pre{white-space:pre-wrap;
background:#f7f7f9;padding:10px;border-radius:6px}.note{color:#666;font-size:13px}
.badge{display:inline-block;min-width:18px;padding:0 6px;border-radius:9px;background:#c62828;color:#fff;
font-size:12px;text-align:center;line-height:18px}
button{cursor:pointer;min-height:44px;display:inline-flex;align-items:center;justify-content:center}
.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);
white-space:nowrap;border:0}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,label:focus-visible{outline:3px solid #1a5fb4;outline-offset:2px}"""


def _pending_nav() -> str:
    """Link to the pending-plan list with its count. A broken plan store must never break every page."""
    try:
        from .plans import pending_count  # lazy: plans.py imports this module
        n = pending_count()
    except Exception:
        return ""
    return (f'<a href="/admin/plans" id="nav-pending">{e(label("ui_nav_pending"))} '
            f'<span class="badge" id="pending-badge">{n}</span></a>')


# Nav rules for pages rendered outside page() (the plan confirmation screen has its own stylesheet).
NAV_CSS = ("nav{margin-bottom:12px}nav a{margin-right:14px}nav .note{margin-left:8px}"
           ".badge{display:inline-block;min-width:18px;padding:0 6px;border-radius:9px;background:#c62828;color:#fff;"
           "text-align:center;font-size:12px;margin-left:4px}")


def nav_html() -> str:
    """The nav for the signed-in role (docs/adr/0001-three-roles.md); the full dev nav when nobody is signed in."""
    cur = auth.current_session()
    if not cur:
        return (f'<nav><b>reimburse-buddy</b> &middot; <a href="/">{e(label("ui_nav_home"))}</a>\n'
                f'<a href="/employee">{e(label("ui_nav_employee"))}</a> <a href="/admin">{e(label("ui_nav_finance_admin"))}</a> '
                f'<a href="/admin/intake">{e(label("ui_nav_intake"))}</a>{_pending_nav()}{_budget_nav()}</nav>')
    emp, role = cur
    if users.is_finance(role):
        links = (f'<a href="/admin">{e(label("ui_nav_triage"))}</a> '
                 f'<a href="/admin/intake">{e(label("ui_nav_intake"))}</a>{_pending_nav()}{_budget_nav()}')
    elif users.is_manager(role):
        links = f'<a href="/manager">{e(label("ui_nav_approvals"))}</a>'
    else:
        links = (f'<a href="/employee">{e(label("ui_nav_my_claims"))}</a> '
                 f'<a href="/employee/new">{e(label("ui_nav_submit_claim"))}</a>')
    account = (f'<span class="note" id="signed-in">{e(label("ui_nav_signed_in"))} <b>{e(auth.reviewer_name(emp))}</b> '
               f'({e(users.ROLE_LABELS.get(role, role))})</span>'
               f'<form method="post" action="/logout" style="display:inline">'
               f'<button type="submit" id="logout">{e(label("ui_nav_sign_out"))}</button></form>')
    return f'<nav><b>reimburse-buddy</b> &middot; {links}{account}</nav>'


def _budget_nav() -> str:
    """Today's remaining LLM budget. A ledger problem must never break every page."""
    try:
        left = spend.remaining()
    except Exception:  # noqa: BLE001 - see docstring
        return ""
    if left is None:
        return ""
    return f'<span class="note" id="llm-budget">{e(label("budget_remaining"))}: ${left:.2f}</span> '


def reset_form(text: str) -> str:
    """The demo reset button; never rendered in production."""
    if auth.is_production():
        return ""
    return f'<form method="post" action="/admin/reset"><button type="submit">{e(text)}</button></form>'


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title>
<style>{CSS}</style></head><body>{nav_html()}
<h1>{escape(title)}</h1>{body}
<p class="note" id="footer-note">Synthetic data. Simulation only.</p></body></html>""", status_code=status_code)


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
        return f"<span class='tag {cls}' title='{e(k)}: {e(v)}'>{e(label(k))}: {e(label(v))}</span>"
    return "<div>" + "".join(badge(k, v) for k, v in prov.items()) + "</div>"


def gap_tag(g: str, cls: str = "") -> str:
    return f'<span class="tag {cls}" title="{e(g)}">{e(label(g))}</span>'


STATUS_ICON = {"approved": "✓ ", "rejected": "✕ ", "on_hold": "! ", "partially_approved": "! ",
               "needs_action": "! "}


def status_tag(s: str) -> str:
    cls = {"approved": "ok", "partially_approved": "warn", "rejected": "bad", "on_hold": "warn"}.get(s, "")
    icon = STATUS_ICON.get(s, "")
    return f'<span class="tag {cls}" title="{e(s)}">{e(icon + label(s))}</span>'


LOGIN_CSS = """*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
padding:16px;font-family:system-ui,sans-serif;color:#1d1d1f;background:#f4f5f8}
.login-card{width:100%;max-width:380px;background:#fff;border:1px solid #e1e3ea;border-radius:12px;padding:28px 24px;
box-shadow:0 4px 18px rgba(20,30,60,.08)}
.login-card h1{font-size:22px;margin:0 0 4px}.login-card .sub{color:#555;font-size:14px;margin:0 0 20px}
.login-card label{display:block;font-size:14px;font-weight:600;margin:14px 0 6px}
.login-card input{width:100%;min-height:44px;padding:8px 12px;font-size:16px;border:1px solid #b9bcc6;border-radius:8px}
.pw-row{display:flex;gap:8px}.pw-row input{flex:1;min-width:0}
.pw-row button{min-width:92px;min-height:44px;padding:0 10px;border:1px solid #b9bcc6;border-radius:8px;background:#fff;font-size:14px;cursor:pointer}
.login-card .submit{width:100%;margin-top:22px;min-height:46px;border:0;border-radius:8px;background:#1a5fb4;color:#fff;
font-size:16px;font-weight:600;cursor:pointer}.login-card .submit:hover{background:#174f95}
.login-error{background:#f8d7da;color:#842029;border:1px solid #f1aeb5;border-radius:8px;padding:10px 12px;font-size:14px;margin:0 0 6px}
a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid #1a5fb4;outline-offset:2px}"""


# --- login / logout / health ---------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "", error: str = "", email: str = ""):
    """The only public form: a centered card with email and password. With the gate off (no user store
    or DSN outside production) it says so instead."""
    target = auth.safe_next(next)
    if not auth.enabled():
        return page(label("sign_in"), f"""<div class="box" id="login-off">
<p id="auth-off">{e(label("auth_off"))}</p>
<p><a class="btn" href="{e(target)}">{e(label("continue"))}</a></p></div>""")
    return login_card(target, error, email, 401 if error else 200)


def login_card(target: str, error: str, email: str, status_code: int) -> HTMLResponse:
    err = f'<p class="login-error" id="login-error" role="alert">{e(error)}</p>' if error else ""
    show, hide = e(label("show_password")), e(label("hide_password"))
    html = f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(label("sign_in"))} - Reimburse Buddy</title>
<style>{LOGIN_CSS}</style></head><body>
<main class="login-card" id="login">
<h1>Reimburse Buddy</h1><p class="sub">{e(label("login_subtitle"))}</p>
{err}
<form method="post" action="/login">
<input type="hidden" name="next" value="{e(target)}">
<label for="email">{e(label("email"))}</label>
<input type="email" id="email" name="email" value="{e(email)}" autocomplete="username" aria-label="{e(label("email"))}" required>
<label for="password">{e(label("password"))}</label>
<div class="pw-row"><input type="password" id="password" name="password" autocomplete="current-password"
 aria-label="{e(label("password"))}" required>
<button type="button" id="toggle-password" aria-controls="password" aria-pressed="false">{show}</button></div>
<button type="submit" class="submit" id="login-submit">{e(label("sign_in"))}</button>
</form></main>
<script>
var pw=document.getElementById("password"),tg=document.getElementById("toggle-password");
tg.addEventListener("click",function(){{var on=pw.type==="password";pw.type=on?"text":"password";
tg.textContent=on?"{hide}":"{show}";tg.setAttribute("aria-pressed",on?"true":"false");}});
</script></body></html>"""
    return HTMLResponse(html, status_code=status_code)


@app.post("/login")
def login_submit(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/")):
    target = auth.safe_next(next)
    if not auth.enabled():
        return RedirectResponse(target, status_code=303)
    ip = client_ip(request)
    if auth.too_many_failures(ip):
        auth.audit_denied("rate_limited", ip=ip)
        return login_card(target, label("auth_rate_limited"), email, 401)
    try:
        store = auth.user_store()
        user = users.authenticate(store, email, password) if store else None
        if user:
            store.touch_login(user.id)
    except Exception:  # noqa: BLE001 - a user database outage is a 503 page, never a stack trace
        auth.audit_denied("user_store_unavailable", ip=ip)
        return login_card(target, label("auth_unavailable"), email, 503)
    if user is None:
        auth.record_failure(ip)
        auth.audit_denied("bad_credentials", ip=ip)  # reason only: never the email, password or IP
        return login_card(target, label("auth_bad_credentials"), email, 401)
    auth.clear_failures(ip)
    if target == "/" or not auth.may_open(user.role, target):  # "/" is the form's default: no preference
        target = auth.home_for(user.role)
    r = RedirectResponse(target, status_code=303)
    r.set_cookie(auth.COOKIE, auth.new_cookie_value(user.employee_id, user.role), max_age=auth.TTL_SECONDS,
                 path="/", httponly=True, samesite="lax", secure=auth.is_production())
    return r


@app.get("/logout")
def logout_page():
    """A bookmark or a back button should land on the form, not on a 404."""
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(auth.COOKIE, path="/")
    return r


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    """Liveness only: no audit write, no LLM, no store access, and no login (the platform probes it)."""
    return PlainTextResponse("ok")


# --- index ------------------------------------------------------------------
def demo_claim_id() -> str:
    """The claim the landing page links to: the first employee's richest Surabaya claim (CLM-2026-0110)."""
    def gap_count(c: dict) -> int:
        return len({g for item in check_claim(store, c["id"]).items for g in item.gaps})

    candidates = [c for c in store.claims
                  if c["employee_id"] == "EMP-001" and c["trip"]["destination"] == "Surabaya"]
    return max(candidates, key=gap_count)["id"] if candidates else store.claims[0]["id"]


@app.get("/", response_class=HTMLResponse)
def index():
    cid = demo_claim_id()
    owner = next((c["employee_id"] for c in store.claims if c["id"] == cid), "EMP-001")
    first_name = person(store, owner).split()[0]
    body = f"""<div class="cards">
<div class="card"><h2>Finance</h2><p>Triage klaim masuk, tinjau pengecualian, dan setujui rencana.
Semua angka dan keputusan tetap di tangan Anda.</p></div>
<div class="card"><h2>Karyawan</h2><p>Kirim klaim, unggah bukti, dan tanyakan status klaim Anda sendiri.</p></div>
</div>
<div class="box"><h2>Panduan singkat</h2>
<p><a class="btn" href="/admin">Lihat triage finance</a>
<a class="btn" href="/admin/claims/{e(cid)}">Buka klaim contoh ({e(first_name)}, Surabaya)</a>
</p>
{reset_form("Reset demo")}</div>
<p class="note">Data sintetis. Tidak ada email atau pesan sungguhan yang dikirim.</p>"""
    return page("Selamat datang di reimburse-buddy", body)


# --- employee side ------------------------------------------------------------
def require_employee(request: Request) -> str:
    """The acting employee for a POST under /employee, else 401. Never taken from the form."""
    emp = auth.current_employee(request)
    if not emp:
        raise HTTPException(401, "Silakan masuk dulu / please sign in first")
    return emp


def owned_claim(cid: str, emp: str) -> dict:
    """The claim if `emp` owns it; someone else's claim is a 404 exactly like a missing one."""
    try:
        claim = store.get_claim(cid)
    except KeyError:
        raise HTTPException(404, f"Unknown claim {cid}")
    if claim["employee_id"] != emp:
        raise HTTPException(404, f"Unknown claim {cid}")
    return claim


def flash_text(msg: str) -> str:
    """Text for a flash message the redirects carry (?msg=...). Unknown text is shown as it was sent."""
    for english, code in (("Submitted", "ui_flash_submitted"), ("File added", "ui_flash_file_added")):
        if msg == english or msg.startswith(english + " "):
            return label(code) + msg[len(english):]
    return msg


@app.get("/employee", response_class=HTMLResponse)
def employee_page(request: Request, msg: str = ""):
    emp = auth.current_employee(request)
    if not emp:
        return page(label("ui_portal_title"), auth.sign_in_notice())
    me = store.get_employee(emp)
    claims = [c for c in store.claims if c["employee_id"] == emp]
    rows = "".join(
        f"<tr><td><a href='/employee/claims/{e(c['id'])}'>{e(c['id'])}</a></td><td>{e(c['purpose'])}</td><td>{e(c['trip']['destination'])} "
        f"{e(c['trip']['start_date'])} &rarr; {e(c['trip']['end_date'])}</td>"
        f"<td class=num>{e(rupiah(sum(Decimal(i['amount']) for i in c['items'])))}</td><td>{status_tag(c['status'])}</td></tr>"
        for c in claims)
    inbox = [m for m in store.outbox if m["to"] == emp]
    msgs = "".join(f"<tr><td>{e(m['claim_id'])}</td><td>{e(m['subject'])}</td><td>{e(m['body'])}</td></tr>"
                   for m in inbox) or ""
    fwd = [m for m in store.inbox if m.get("owner_id") == emp]
    fwd_rows = "".join(f"<tr><td>{e(m['date'])}</td><td>{e(m['subject'])}</td>"
                       f"<td>{e(', '.join(a['name'] for a in m.get('attachments', [])))}</td></tr>" for m in fwd)
    body = f"""
{f'<p class="tag ok">{e(flash_text(msg))}</p>' if msg else ''}
<h2>{e(label('ui_my_claims'))}</h2><div class="table-wrap"><table><tr><th>{e(label('ui_col_id'))}</th><th>{e(label('ui_col_purpose'))}</th><th>{e(label('ui_col_trip'))}</th><th class=num>{e(label('ui_col_claimed'))}</th><th>{e(label('ui_col_status'))}</th></tr>{rows}</table></div>
{portal_box(request)}
<h2>{e(label('ui_messages_from_finance'))}</h2>{'<div class="table-wrap"><table><tr><th>' + e(label('ui_col_claim')) + '</th><th>' + e(label('ui_col_subject')) + '</th><th>' + e(label('ui_col_body')) + '</th></tr>' + msgs + '</table></div>' if msgs else '<p class=note>' + e(label('ui_no_messages')) + '</p>'}
<div class="box"><h2>{e(label('ui_new_claim'))}</h2><p>{e(label('ui_new_claim_hint'))}</p>
<p><a class="btn" href="/employee/new">{e(label('ui_start_new_claim'))}</a></p></div>
<h2>{e(label('ui_forwarded_mailbox'))}</h2><div class="table-wrap"><table><tr><th>{e(label('ui_col_date'))}</th><th>{e(label('ui_col_subject'))}</th><th>{e(label('ui_col_attachments'))}</th></tr>{fwd_rows}</table></div>
<p class="note">{e(label('ui_employee_footer'))} {e(me['name'])} &middot; {e(me['email'])}</p>"""
    return page(label("ui_portal_title"), body)


ITEM_RE = re.compile(r"^\s*(\w+)\s*,\s*(\d{4}-\d{2}-\d{2})\s*,\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*(yes|no)"
                     r"(?:\s*,\s*([A-Za-z0-9 ]*?))?\s*$", re.I)
AMOUNT_RE = re.compile(r"^(\d+|\d{1,3}(?:\.\d{3})+)$")  # 1350000 or 1.350.000; no decimals, no commas
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# No .txt: a text file is typed by the sender, so it can never be evidence read from a document.
ALLOWED_UPLOAD_EXT = {".pdf", ".png", ".jpg", ".jpeg"}
# Final statuses the reviewer may pick for each engine decision. Overrides go through logged item decisions.
STATUS_FOR_DECISION = {"approve": ["approved", "on_hold"], "partial_approve": ["partially_approved", "on_hold"],
                       "reject": ["rejected", "on_hold"], "needs_info": ["on_hold"]}
# Gap codes an override decision (approve_full / approve_capped / reject) can settle. These are exactly the
# gaps src/reimburse_buddy/rules.py resolves through the `choice in ("approve_full", "approve_capped")`
# branch, minus MULTIPLE_RECEIPTS (the engine only accepts `use:<message id>` for it, never these buttons)
# and minus the document gaps (receipt_missing / boarding_pass_missing): those need the document attached,
# an override is not a substitute.
DECISION_GAPS = frozenset({
    "hotel_over_cap", "late_checkout_unjustified", "hotel_nights_mismatch",
    "duplicate_suspected", "receipt_amount_mismatch", "receipt_shared",
})
# Gaps only a document can close; an override decision is not a substitute.
DOCUMENT_GAPS = frozenset({"receipt_missing", "boarding_pass_missing"})


def parse_amount(text: str) -> Decimal:
    text = text.strip()
    if not AMOUNT_RE.match(text):
        raise ValueError(f"amount {text!r} must be whole Rupiah, e.g. 1350000 or 1.350.000 (no decimals)")
    return Decimal(text.replace(".", ""))


@app.post("/employee/claims")
def submit_claim(request: Request, purpose: str = Form(...), destination: str = Form(...),
                 start_date: str = Form(...), end_date: str = Form(...), items: str = Form(...)):
    emp = require_employee(request)
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
    return RedirectResponse(f"/employee?msg=Submitted+{cid}", status_code=303)


@app.post("/employee/forward")
async def forward_evidence(request: Request, subject: str = Form(...), claim_id: str = Form(""),
                           att_type: str = Form("receipt"), item_id: str = Form(""), flight: str = Form(""),
                           att_date: str = Form(""), body: str = Form(""), file: UploadFile | None = File(None)):
    emp = require_employee(request)
    if claim_id:  # a receipt may only be linked to the sender's own claim
        owned_claim(claim_id, emp)
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
    return RedirectResponse("/employee?msg=File+diteruskan", status_code=303)


# --- finance admin side --------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
def admin(batch: str = BATCH, msg: str = ""):
    t = triage_claims(store, batch)
    rows = []
    for x in t["exceptions"]:
        held = run_check(x["claim_id"]).total_pending
        row = (f"<tr><td><a href='/admin/claims/{e(x['claim_id'])}'>{e(x['claim_id'])}</a></td>"
               f"<td title='{e(x['employee_id'])}'>{e(person(store, x['employee_id']))}</td>"
               f"<td class=num>{e(rupiah(held))}</td>"
               f"<td>{''.join(gap_tag(g) for g in x['gaps'])}</td>"
               f"<td>{e(', '.join(x['rejected_items']))}</td>"
               f"<td><form method=post action=\"/admin/claims/{e(x['claim_id'])}/investigate\">"
               f"<button>Selidiki dengan agent</button></form></td></tr>")
        rows.append((held, row))
    exc = "".join(row for _, row in sorted(rows, key=lambda r: r[0], reverse=True))
    cleared = t["auto_cleared"]
    hidden = "".join(f'<input type="hidden" name="claim_ids" value="{e(c)}">' for c in cleared)
    others = "".join(f"<tr><td><a href='/admin/claims/{e(c['id'])}'>{e(c['id'])}</a></td>"
                     f"<td title='{e(c['employee_id'])}'>{e(person(store, c['employee_id']))}</td>"
                     f"<td>{e(c.get('batch', ''))}</td><td>{status_tag(c['status'])}</td></tr>" for c in store.claims)
    est = t["time_saved_estimate"]
    body = f"""{f'<p class="tag ok">{e(msg)}</p>' if msg else ''}
<form method="get"><label>Batch <select name="batch" aria-label="Pilih batch" onchange="this.form.submit()">{options([(BATCH, BATCH), (OWN_BATCH, OWN_BATCH), (WEB_BATCH, WEB_BATCH)], batch)}</select></label></form>
<div class="box"><h2>Triage: {t['claims']} claims, {len(cleared)} cleared, {len(t['exceptions'])} exceptions</h2>
<p>Time saved: {e(est['minutes'])} min <span class="tag warn">estimate</span> <span class="note">{e(est['assumption'])}</span></p>
<p class="note">Triage is read-only. Statuses change only after you confirm below.</p>
<h3>Cleared</h3><p>{''.join(f'<span class=tag>{e(c)}</span>' for c in cleared) or '<span class=note>None left.</span>'}</p>
{f'''<form method="post" action="/admin/batch/approve"><input type="hidden" name="batch" value="{e(batch)}">{hidden}
<label><input type="checkbox" name="reviewer_confirmed" value="true"> I reviewed these {len(cleared)} claims and approve them</label>
<button type="submit">Approve cleared batch</button></form>''' if cleared else ''}
<h3>Exceptions</h3><div class="table-wrap"><table><tr><th>Claim</th><th>Employee</th><th class=num>Uang tertahan</th><th>Yang kurang</th><th>Rejected items</th><th></th></tr>{exc}</table></div></div>
<h2>All claims</h2><div class="table-wrap"><table><tr><th>Claim</th><th>Employee</th><th>Batch</th><th>Status</th></tr>{others}</table></div>
{reset_form("Reset demo data")}"""
    return page("Finance admin: triage", body)


@app.post("/admin/batch/approve")
def batch_approve(request: Request, batch: str = Form(...), claim_ids: list[str] = Form([]),
                  reviewer_confirmed: str = Form("false")):
    reviewer = auth.reviewer_of(request)
    mine = []
    for cid in claim_ids:
        try:
            if store.get_claim(cid)["employee_id"] == reviewer:
                mine.append(cid)
        except KeyError:
            continue  # approve_cleared_batch reports the unknown ID itself
    if mine:
        # Refuse the whole batch rather than dropping the reviewer's own claim silently: the reviewer
        # must see that their claim is still waiting for someone else.
        auth.audit_self_approval(f"batch:{batch}", reviewer, f"self_approval:batch:{batch}:{mine[0]}",
                                 claim_id=mine[0], batch=batch)
        msg = f"{label('self_approval_blocked')} Batch tidak disetujui; klaim Anda: {', '.join(mine)}."
        return RedirectResponse(f"/admin?batch={batch}&msg={quote(msg, safe='')}", status_code=303)
    held = [c for c in claim_ids if pending_items(c)]
    claim_ids = [c for c in claim_ids if c not in held]
    res = approve_cleared_batch(store, batch, claim_ids, reviewer_confirmed == "true")
    text = f"Approved {res['count']} claims" if res["ok"] else "Not approved: " + res["error"]
    return RedirectResponse(f"/admin?batch={batch}&msg={text.replace(' ', '+')}", status_code=303)


@app.post("/admin/reset")
def reset():
    if auth.is_production():
        return JSONResponse({"detail": "Reset demo tidak tersedia di produksi. / Demo reset is disabled in production."},
                            status_code=403)
    reset_all()
    return RedirectResponse("/admin?msg=Demo+data+reset", status_code=303)


def manager_cell(cid: str, item_id: str, owner: str) -> str:
    """The manager's answers for one item, and the form to ask when nothing is pending (docs/adr/0003)."""
    reqs = [r for r in manager.requests_for(cid) if r["item_id"] == item_id]
    out = "".join(
        f"<div class=note id='req-{e(r['id'])}'>{e(label('ui_req_manager'))} {e(person(store, r['manager_id']))}: "
        f"<b>{e(manager.status_text(r))}</b>{(' &middot; ' + e(r['note'])) if r.get('note') else ''}</div>"
        for r in reqs)
    if any(r["status"] == "pending" for r in reqs) or not store.get_employee(owner).get("manager_id"):
        return out
    return out + (f"<form method=post action='/admin/claims/{e(cid)}/items/{e(item_id)}/manager'>"
                  f"<input name=reason maxlength=500 aria-label='{e(label('ui_req_reason'))}' "
                  f"placeholder='{e(label('ui_req_reason'))}'> <button>{e(label('ui_req_ask_manager'))}</button></form>")


@app.get("/admin/claims/{cid}", response_class=HTMLResponse)
def claim_detail(cid: str, request: Request, q: str = "", msg: str = ""):
    try:
        claim = store.get_claim(cid)
    except KeyError:
        raise HTTPException(404, f"Unknown claim {cid}")
    emp, trip = claim["employee_id"], claim["trip"]
    # The reviewer cannot approve their own claim: the buttons are hidden here and the server refuses
    # the request anyway (self_approval_refusal).
    self_blocked = emp == auth.reviewer_of(request)
    r = review(cid)
    c = investigate(cid)
    pending = pending_items(cid)
    item_rows = ""
    for it in c.items:
        src = next(x for x in claim["items"] if x["id"] == it.item_id)
        finds = "<br>".join(f"<span class=tag title='{e(f.rule)}'>{e(label(f.rule))}</span> {e(f.message)}"
                            for f in it.findings)
        gaps = "".join(f"<span class='tag warn' title='{e(g)}'>{e(label(g))}</span>" for g in it.gaps)
        pend = pending.get(it.item_id, [])
        dec = ""
        decision_gaps = [g for g in it.gaps if g in DECISION_GAPS]
        document_only = bool(it.gaps) and set(it.gaps) <= DOCUMENT_GAPS
        if document_only and not pend:
            dec = (f"<form method=post action='/admin/claims/{e(cid)}/decide' style='display:inline'>"
                   f"<input type=hidden name=item_id value='{e(it.item_id)}'>"
                   f"<button name=choice value='reject'{' disabled' if it.human_decision == 'reject' else ''}>"
                   f"{e(label('reject'))}</button></form>"
                   f"<div class=note>Perlu dokumen: {e(', '.join(label(g) for g in it.gaps))}</div>")
            if it.human_decision:
                dec += f"<div class=note>Reviewer: {e(label(it.human_decision))}</div>"
        elif (decision_gaps or it.human_decision) and not pend:
            dec = (f"<form method=post action='/admin/claims/{e(cid)}/decide' style='display:inline'>"
                   f"<input type=hidden name=item_id value='{e(it.item_id)}'>"
                   + "".join(f"<button name=choice value='{ch}'{' disabled' if it.human_decision == ch else ''}>{e(label(ch))}</button> "
                             for ch in CHOICES) + "</form>")
            if it.human_decision:
                dec += f"<div class=note>Reviewer: {e(label(it.human_decision))}</div>"
        elif it.gaps and not pend:
            dec = f"<div class=note>Perlu ditinjau: {e(', '.join(label(g) for g in it.gaps))}</div>"
        status_cell = f"<span title='{e(it.status.value)}'>{e(label(it.status.value))}</span>"
        if pend:
            status_cell = "<span class='tag warn'>pending review</span>"
            for mid in pend:
                p = r["pending"][mid]
                why = ", ".join(label(x) for x in (p.get("reasons") or [p["reason"]]))
                dec = (f"<form method=post action='/admin/claims/{e(cid)}/evidence/{e(mid)}/review' style='display:inline'>"
                       f"<div class=note>{e(mid)}: <span title='{e(p['reason'])}'>{e(why)}</span></div>"
                       f"<button name=verdict value=accept>Accept evidence</button> "
                       f"<button name=verdict value=reject>Reject evidence</button></form>") + dec
        if self_blocked:  # UX only: the server refuses these requests anyway
            dec = f"<div class=note id='self-approval'>{e(label('self_approval_note'))}</div>"
        dec += manager_cell(cid, it.item_id, emp)
        item_rows += (f"<tr id='item-{e(it.item_id)}'><td>{e(it.item_id)}</td>"
                      f"<td>{e(label(it.category))}<div class=note>{e(src['description'])}</div></td>"
                      f"<td class=num>{e(rupiah(it.claimed))}</td><td class=num>{e(rupiah(it.approvable))}</td>"
                      f"<td>{status_cell}</td><td>{provenance_badges(src)}{finds}<div>{gaps}</div></td><td>{e(', '.join(it.evidence_used))}</td><td>{dec}</td></tr>")
    needs = [(it, [g for g in it.gaps if g in DECISION_GAPS]) for it in c.items
             if any(g in DECISION_GAPS for g in it.gaps) and not it.human_decision
             and it.item_id not in pending]
    if self_blocked:
        needs_you = (f"<div class='box' id='needs-you'>{e(label('self_approval_note'))} "
                     f"{e(label('self_approval_blocked'))}</div>")
    elif needs:
        links = ", ".join(f"<a href='#item-{e(it.item_id)}'>{e(', '.join(label(g) for g in gs))}</a>"
                          for it, gs in needs)
        needs_you = (f"<div class='box' id='needs-you'><b>Klaim ini butuh {len(needs)} keputusan Anda:</b> "
                     f"{links}</div>")
    else:
        needs_you = "<div class='box' id='needs-you'>Klaim ini tidak butuh keputusan Anda.</div>"
    log_rows = "".join(f"<li>{e(x['at'])} {e(x['evidence_id'])} ({e(x['item_id'])}, "
                       f"<span title='{e(x['reason'])}'>{e(', '.join(label(v) for v in x['reason'].split(', ')))}</span>): "
                       f"<b>{e(label(x['verdict']))}</b> by reviewer</li>" for x in r["evidence_log"])
    ignored = "".join(f"<li>{e(i.id)}: <span class='tag bad' title='{e(i.reason)}'>{e(label(i.reason))}</span></li>"
                      for i in c.ignored_evidence)
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
    note = re.sub(r"\b[a-z]+(?:_[a-z]+)+\b", lambda m: label(m.group(0)),
                  draft_decision(store, c, language="both"))
    allowed_status = STATUS_FOR_DECISION[c.decision.value]
    if pending_items(cid):
        allowed_status = ["on_hold"]
    if self_blocked:
        status_form = (f"<div class='note' id='self-approval-status'>{e(label('self_approval_note'))}</div>")
    else:
        status_form = (f"<form method=post action=\"/admin/claims/{e(cid)}/status\"><label>Final status "
                       f"<select name=status aria-label=\"Status akhir\">{options([(s, label(s)) for s in allowed_status])}"
                       f"</select></label>\n<label><input type=checkbox name=reviewer_confirmed value=true> "
                       f"I confirm this decision</label> <button>Apply status</button></form>")
    body = f"""<p><a href="/admin">&larr; Triage</a> &middot; <a href="/admin/claims/{e(cid)}/audit">Lihat jejak audit</a></p>{f'<p class="tag ok">{e(msg)}</p>' if msg else ''}
<p><span title="{e(emp)}">{e(person(store, emp))}</span> &middot; {e(claim['purpose'])} &middot; {e(trip['destination'])}
{e(trip['start_date'])} &rarr; {e(trip['end_date'])} &middot; status {status_tag(claim['status'])}</p>
<div class="box"><h2>Rule engine: {e(label(c.decision.value))}</h2>
<p>Approvable <b>{e(rupiah(c.total_approvable))}</b> of {e(rupiah(c.total_claimed))} &middot; pending {e(rupiah(c.total_pending))}</p>
<p class="note">Semua angka dihitung oleh mesin aturan, bukan oleh AI. Evidence attached: {e(', '.join(r['evidence']) or 'none')}</p>
{needs_you}
<div class="table-wrap"><table><tr><th>Item</th><th>Category</th><th class=num>Claimed</th><th class=num>Approvable</th><th>Status</th><th>Findings / gaps</th><th>Evidence</th><th>Reviewer decision</th></tr>{item_rows}</table></div>
{f'<h3>Evidence review log</h3><ul>{log_rows}</ul>' if log_rows else ''}{f'<h3>Ignored evidence</h3><ul>{ignored}</ul>' if ignored else ''}{f'<h3>Warnings</h3><ul>{warns}</ul>' if warns else ''}</div>
<div class="box"><h2>Investigate</h2>
<form method=post action="/admin/claims/{e(cid)}/investigate"><button>Selidiki dengan agent</button>
<span class="note">Runs the LangGraph agent on this claim. It pauses for your approval before any write or decision message.</span></form>
<p class="note">Or investigate by hand:</p>
<h3>Search the reimburse mailbox</h3><form method=get><label>Cari di mailbox <input name=q aria-label="Cari di mailbox" value="{e(q)}" size=40 placeholder="boarding pass QZ 7520"></label><button>Search</button></form>
{f'<div class="table-wrap"><table><tr><th>ID</th><th>Date</th><th>Subject</th><th>Attachments</th><th></th></tr>{hit_rows}</table></div>' if hits else ''}
<h3>Calendar during the trip</h3><div class="table-wrap"><table><tr><th>ID</th><th>When</th><th>Title</th><th>Location</th><th></th></tr>{cal_rows}</table></div>
<h3>Approval memos ({e(trip['destination'])})</h3><div class="table-wrap"><table><tr><th>ID</th><th>Signed</th><th>Reason</th><th></th></tr>{memo_rows}</table></div>
<h3>Ask the employee</h3><form method=post action="/admin/claims/{e(cid)}/message">
<label>Subjek <input name=subject aria-label="Subjek pesan" size=40 value="Claim {e(cid)}: missing document"></label>
<label>Isi <input name=body aria-label="Isi pesan" size=60 placeholder="Mohon kirim struk taksi tanggal 3 Okt (IT-5)." required></label>
<button>Send</button></form>{f'<ul>{sent_rows}</ul>' if sent_rows else ''}</div>
<div class="box"><h2>Decision note preview (EN + ID)</h2><pre>{e(note)}</pre>
{status_form}
<p class="note">Status options follow the engine decision ({e(label(c.decision.value))}). To override, record an item decision above.
Status changes only after you confirm. Notes are sent only by you.</p></div>"""
    return page(f"Claim {cid}", body)


@app.get("/admin/claims/{cid}/audit", response_class=HTMLResponse)
def claim_audit(cid: str):
    """Read-only audit trail for one claim. Raw system codes are shown here and only here."""
    try:
        store.get_claim(cid)
    except KeyError:
        raise HTTPException(404, f"Unknown claim {cid}")
    r = review(cid)
    status_rows = "".join(
        f"<tr><td>{e(x.get('claim_id'))}</td><td>{e(x.get('from'))}</td><td>{e(x.get('to'))}</td>"
        f"<td>{e(x.get('note', ''))}</td></tr>"
        for x in store.status_log if x.get("claim_id") == cid)
    evidence_rows = "".join(
        f"<tr><td>{e(x.get('at'))}</td><td>{e(x.get('evidence_id'))}</td><td>{e(x.get('item_id'))}</td>"
        f"<td>{e(x.get('reason'))}</td><td>{e(x.get('verdict'))}</td></tr>" for x in r["evidence_log"])
    decision_rows = "".join(f"<tr><td>{e(item_id)}</td><td>{e(choice)}</td></tr>"
                            for item_id, choice in r["decisions"].items())
    outbox_rows = "".join(
        f"<tr><td>{e(m.get('id'))}</td><td>{e(m.get('to'))}</td><td>{e(m.get('subject'))}</td>"
        f"<td>{e(m.get('body'))}</td></tr>" for m in store.outbox if m.get("claim_id") == cid)

    def table(head: list[str], rows: str, empty: str) -> str:
        if not rows:
            return f"<p class=note>{empty}</p>"
        return ("<div class='table-wrap'><table><tr>"
                + "".join(f"<th>{h}</th>" for h in head) + f"</tr>{rows}</table></div>")

    body = f"""<p><a href="/admin/claims/{e(cid)}">&larr; Kembali ke klaim / Back to claim</a></p>
<p class="note">Jejak audit mentah untuk {e(cid)}; kode sistem ditampilkan apa adanya.</p>
<div class="box"><h2>Riwayat status</h2>{table(["Claim", "Dari", "Ke", "Catatan"], status_rows, "Tidak ada perubahan status.")}</div>
<div class="box"><h2>Log bukti peninjau</h2>{table(["Waktu", "Bukti", "Item", "Alasan", "Verdict"], evidence_rows, "Tidak ada catatan bukti.")}</div>
<div class="box"><h2>Keputusan item</h2>{table(["Item", "Keputusan"], decision_rows, "Tidak ada keputusan item.")}</div>
<div class="box"><h2>Pesan keluar</h2>{table(["ID", "Ke", "Subjek", "Isi"], outbox_rows, "Tidak ada pesan keluar.")}</div>"""
    return page(f"Jejak audit {cid}", body)


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
def review_evidence(cid: str, mid: str, request: Request, verdict: str = Form(...)):
    """Explicit reviewer verdict on evidence that extraction could not verify. Logged."""
    from datetime import datetime

    store.get_claim(cid)
    if verdict == "accept":  # accepting evidence for your own claim is the approval direction
        refused = self_approval_refusal(request, cid, f"evidence:{mid}:accept")
        if refused:
            return back_to_claim(cid, refused)
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
def decide(cid: str, request: Request, item_id: str = Form(...), choice: str = Form(...)):
    if choice not in CHOICES:
        raise HTTPException(400, f"choice must be one of {CHOICES}")
    if choice != "reject":  # an approval-direction decision on your own claim
        refused = self_approval_refusal(request, cid, f"decide:{item_id}:{choice}")
        if refused:
            return back_to_claim(cid, refused)
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


@app.post("/admin/claims/{cid}/items/{iid}/manager")
def ask_manager(cid: str, iid: str, reason: str = Form("")):
    """Finance asks the claimant's own manager about one item. One open request per item; the answer is input
    for finance, never a decision (docs/adr/0003-manager-approval-requests.md)."""
    try:
        claim = store.get_claim(cid)
    except KeyError:
        raise HTTPException(404, f"Unknown claim {cid}")
    if iid not in {i["id"] for i in claim["items"]}:
        raise HTTPException(404, f"Unknown item {iid}")
    boss = store.get_employee(claim["employee_id"]).get("manager_id")
    if not boss:
        return back_to_claim(cid, label("ui_req_no_manager"))
    if any(r["item_id"] == iid and r["status"] == "pending" for r in manager.requests_for(cid)):
        return back_to_claim(cid, label("ui_req_open_exists"))
    req = store.request_approval(boss, cid, iid, reason.strip()[:500])
    return back_to_claim(cid, f"{label('ui_req_sent')} ({req['id']})")


@app.post("/admin/claims/{cid}/status")
def set_status(cid: str, request: Request, status: str = Form(...), reviewer_confirmed: str = Form("false")):
    if reviewer_confirmed != "true":
        return RedirectResponse(f"/admin/claims/{cid}?msg=Not+changed:+tick+the+confirmation+first", status_code=303)
    if status != "on_hold":  # holding your own claim is not an approval; anything else is
        refused = self_approval_refusal(request, cid, f"status:{status}")
        if refused:
            return back_to_claim(cid, refused)
    c = run_check(cid)
    if pending_items(cid) and status != "on_hold":
        return RedirectResponse(f"/admin/claims/{cid}?msg=Not+changed:+evidence+is+awaiting+review", status_code=303)
    if status not in STATUS_FOR_DECISION[c.decision.value]:
        return RedirectResponse(f"/admin/claims/{cid}?msg=Not+changed:+{label(status).replace(' ', '+')}"
                                f"+is+not+allowed+while+the+engine+says+"
                                f"{label(c.decision.value).replace(' ', '+')}", status_code=303)
    store.update_claim_status(cid, status, f"Reviewer decision; rule engine approvable {rupiah(c.total_approvable)}")
    return RedirectResponse(f"/admin/claims/{cid}?msg=Status+set+to+{label(status).replace(' ', '+')}", status_code=303)


from .upload_flow import router as upload_router  # noqa: E402  (needs the names above)

app.include_router(upload_router)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()


# --- agent investigation ---------------------------------------------------------
def _agent_ready() -> bool:
    return bool(INVESTIGATOR.fake_script) or key_configured()


def _audit_limit(cid: str, kind: str, **extra) -> None:
    """A `limit` event, so a refused run is visible in the audit trail. Best effort."""
    try:
        from . import plans  # late import: app.py includes its router
        from reimburse_agent import audit
        plans.log().append(audit.make_event(f"investigate:{cid}", "limit", f"{kind}:{cid}", actor="system",
                                            payload={"kind": kind, **extra}))
    except Exception:  # noqa: BLE001 - an audit problem must not turn a refusal into a 500
        pass


@app.post("/admin/claims/{cid}/investigate")
def investigate_start(cid: str):
    store.get_claim(cid)
    if not _agent_ready():
        return page(f"Investigate {cid}", f"<p><a href='/admin/claims/{e(cid)}'>&larr; Claim</a></p>"
                    f"<div class='box'><h2>Agent not configured</h2><p class='tag warn' id=no-key>{e(NO_KEY_MESSAGE)}</p></div>")
    if spend.is_production() and not spend.configured():
        # Defence in depth: startup already refuses this, and no uncapped run may start either way.
        _audit_limit(cid, "llm_budget_unconfigured")
        return page(f"Investigate {cid}", f"<p><a href='/admin/claims/{e(cid)}'>&larr; Claim</a></p>"
                    f"<div class='box'><p class='tag warn' id=budget-unconfigured>{e(label('budget_unconfigured'))}</p></div>")
    try:
        run = INVESTIGATOR.start(cid)
    except RateLimited as exc:
        return page(f"Investigate {cid}", f"<p><a href='/admin/claims/{e(cid)}'>&larr; Claim</a></p>"
                    f"<div class='box'><p class='tag warn' id=rate-limited>{e(exc)}</p></div>")
    except spend.LedgerUnavailable as exc:
        # The ledger could not be reached even after a reconnect: refuse the run, say so plainly. Do not
        # ask the ledger for the remaining budget here - it is the thing that is down.
        return page(f"Investigate {cid}", f"<p><a href='/admin/claims/{e(cid)}'>&larr; Claim</a></p>"
                    f"<div class='box'><p class='tag bad' id=ledger-unavailable>{e(str(exc))}</p></div>")
    except spend.BudgetRefused:
        # A second ledger call here must not turn the refusal into a 500 if the database drops meanwhile.
        try:
            left = f"${spend.remaining():.2f}"
            _audit_limit(cid, "daily_llm_budget", budget=str(spend.budget()), committed=str(spend.committed()))
        except Exception:  # noqa: BLE001 - the refusal itself already happened
            left = "?"
        return page(f"Investigate {cid}", f"<p><a href='/admin/claims/{e(cid)}'>&larr; Claim</a></p>"
                    f"<div class='box'><p class='tag warn' id=budget-exhausted>{e(label('budget_exhausted'))}</p>"
                    f"<p class='note'>{e(label('budget_remaining'))}: {e(left)}</p></div>")
    return RedirectResponse(f"/admin/investigations/{run.id}", status_code=303)


def _get_run(rid: str):
    run = INVESTIGATOR.runs.get(rid)
    if run is None:
        raise HTTPException(404, "Unknown investigation")
    return run


def decision_box(d: dict | None) -> str:
    """Rule engine's current verdict next to the proposed write, so the model's wording isn't the only input."""
    if not d:
        return ""

    def rp(v):
        try:
            return "Rp" + f"{int(float(v)):,}".replace(",", ".")
        except (TypeError, ValueError):
            return e(v)

    return (f"<div class=box id=rule-engine><b>Rule engine now (check_claim, no extra evidence):</b> "
            f"decision <code>{e(d.get('decision'))}</code>, approvable {rp(d.get('total_approvable'))} "
            f"of {rp(d.get('total_claimed'))}, pending {rp(d.get('total_pending'))}</div>")


def engine_box(engine: dict | None) -> str:
    """The exact engine basis the approval is bound to (the snapshot that goes into args_hash)."""
    if not engine:
        return ""

    def rp(v):
        try:
            return "Rp" + f"{int(float(v)):,}".replace(",", ".")
        except (TypeError, ValueError):
            return e(v)

    ev = ", ".join(str(x) for x in engine.get("evidence_ids") or []) or "tidak ada (none)"
    hd = json.dumps(engine.get("human_decisions") or {}, ensure_ascii=False, sort_keys=True)
    return (f"<div class=box id=approved-engine><b>Dasar persetujuan (what this approval is bound to):</b> "
            f"status <code>{e(engine.get('status'))}</code>, approvable {rp(engine.get('approvable_amount'))} "
            f"&middot; evidence {e(ev)} &middot; keputusan reviewer {e(hd)}</div>")


PROGRESS_STEPS = {
    "search_reimburse_mailbox": "Mencari boarding pass/struk di mailbox…",
    "get_calendar": "Memeriksa kalender…",
    "find_approval_memo": "Mencari memo persetujuan…",
    "check_claim": "Menghitung dengan mesin aturan…",
}
WRITE_OR_PLAN_PREFIXES = ("propose", "plan", "write", "send_", "request_", "update_", "reset_")


def progress_step_text(tool: str) -> str:
    if tool in PROGRESS_STEPS:
        return PROGRESS_STEPS[tool]
    if tool.startswith(WRITE_OR_PLAN_PREFIXES):
        return "Menyusun rencana…"
    return label(tool) + "…"


def run_started_at(run, start: dict) -> float:
    """Epoch seconds of the run start: the start event's timestamp when the worker sends one, else the run."""
    for key in ("started_at", "timestamp", "time"):
        value = start.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return float(getattr(run, "started_at", time.time()))


@app.get("/admin/investigations/{rid}", response_class=HTMLResponse)
def investigation_page(rid: str, request: Request):
    run = _get_run(rid)
    # A reviewer may not approve a write on their own claim; the approve control is hidden here and
    # /resume refuses it server-side.
    self_blocked = store.get_claim(run.claim_id)["employee_id"] == auth.reviewer_of(request)
    evs = run.snapshot()
    start = next((x for x in evs if x["type"] == "start"), {})
    rows = ""
    for x in evs:
        if x["type"] == "step":
            tag = " <span class='tag bad'>declined</span>" if x.get("declined") else ""
            rows += (f"<tr class=step><td>{x['n']}</td><td><code>{e(x['tool'])}</code>{tag}</td>"
                     f"<td><code>{e(x['args_summary'])}</code></td><td>{e(x['result_summary'])}</td></tr>")
        elif x["type"] == "auto_declined":
            rows += "<tr><td></td><td colspan=3 class=note>No answer in time: auto-declined</td></tr>"
        elif x["type"] == "question":
            rows += f"<tr><td></td><td colspan=3 class=note>Agent asked: {e(x['text'][:300])}</td></tr>"
        elif x["type"] == "reply_applied":
            dec = ", ".join(f"{k}: {v}" for k, v in (x.get("decisions") or {}).items()) or "none"
            rows += f"<tr><td></td><td colspan=3 class=note>Reviewer replied (decisions: {e(dec)})</td></tr>"
        elif x["type"] == "answer_applied":  # shown only once the worker acknowledged it
            who = (f" oleh {e(person(store, x['reviewer']))}" if x.get("reviewer") else "")
            rows += (f"<tr><td></td><td colspan=3 class=note>Reviewer {'approved' if x['approved'] else 'declined'} "
                     f"<code>{e(x['tool'])}</code>{who}</td></tr>")
    pend = run.pending
    pending_html = ""
    approve_form = ""
    if pend and not self_blocked:
        approve_form = (f"<form method=post action=\"/admin/investigations/{e(rid)}/resume\" style=\"display:inline\">"
                        f"<input type=hidden name=approval_id value=\"{e(pend['approval_id'])}\">"
                        f"<input type=hidden name=approved value=true><button>Approve</button></form>")
    elif pend:
        approve_form = (f"<p class='note' id='self-approval'>{e(label('self_approval_note'))}</p>")
    if pend:
        pending_html = f"""<div class="box" id=pending><h2>Pending write: awaiting your approval</h2>
<p><code>{e(pend['tool'])}</code></p>{('<p><strong>' + e(pend['prompt']) + '</strong></p>') if pend.get('prompt') else ''}<pre>{e(json.dumps(pend['args'], ensure_ascii=False, indent=2))}</pre>
{decision_box(pend.get('current_decision'))}
{engine_box(pend.get('engine'))}
<p class=note>{e(label("investigation_gate_note"))}</p>
{approve_form}
<form method=post action="/admin/investigations/{e(rid)}/resume" style="display:inline"><input type=hidden name=approval_id value="{e(pend['approval_id'])}"><input type=hidden name=approved value=false>
<label>Alasan <input name=reason aria-label="Alasan penolakan (opsional)" size=40 placeholder="Reason (optional)"></label><button>Decline</button></form></div>"""
    q = run.question
    if q:
        sel = "".join(
            f"<tr><td><code>{e(i['item_id'])}</code> {e(i.get('category') or '')}</td><td>{e(', '.join(i.get('gaps', [])))}</td>"
            f"<td><select name='dec_{e(i['item_id'])}' aria-label='Keputusan untuk {e(i['item_id'])}'><option value=''>no decision</option>"
            "<option value=approve_full>approve full</option><option value=approve_capped>approve capped</option>"
            "<option value=reject>reject</option></select></td></tr>" for i in q.get("items", []))
        pending_html += f"""<div class="box" id=question><h2>The agent is asking you</h2><pre>{e(q['text'])}</pre>
<form method=post action="/admin/investigations/{e(rid)}/reply"><input type=hidden name=question_id value="{e(q['question_id'])}">
<div class="table-wrap"><table>{sel}</table></div><p><label>Jawaban <textarea name=reply aria-label="Jawaban bebas (opsional)" rows=3 cols=60 placeholder="Reply in your own words (optional)"></textarea></label></p>
<p class=note>Decisions chosen here are the only ones the agent's tools will use; the agent cannot set them itself.</p>
<button>Send and resume</button></form></div>"""
    final = next((x for x in evs if x["type"] == "final"), None)
    err = next((x for x in evs if x["type"] == "error"), None)
    status = ("finished" if final else "stopped" if err else "awaiting approval" if pend
              else "awaiting your answer" if q else "running")
    progress_html = ""
    if not (final or err):
        steps = [progress_step_text(x["tool"]) for x in evs if x["type"] == "step"]
        steps_text = " → ".join(steps) or "Menunggu langkah pertama…"
        elapsed = max(0, int(time.time() - run_started_at(run, start)))
        progress_html = f"""<div class="box" id="progress"><h2>Progres agent</h2>
<p id="progress-steps">{e(steps_text)}</p>
<p>Waktu berjalan: <b id="elapsed">{elapsed}</b> detik</p>
<script>(function(){{var n=document.getElementById('elapsed');if(!n)return;var s=parseInt(n.textContent,10)||0;
setInterval(function(){{s+=1;n.textContent=s;}},1000);}})();</script></div>"""
    tail = ""
    if final:
        t = final.get("tokens", {})
        hit = f" ({t.get('cache_hit', 0)} cache hit)" if t.get("cache_hit") else ""
        tail = (f"<div class=box id=final><h2>Agent's final note</h2><pre>{e(final['note'])}</pre>"
                f"<p class=note>Tokens: {t.get('input', 0)} in{hit} / {t.get('output', 0)} out. Amounts come "
                f"from tool results; verify against the rule engine.</p></div>")
    if err:
        msg = NO_KEY_MESSAGE if err.get("code") == "no_key" else err.get("message", "")
        tail += f"<div class=box id=error><p class='tag bad'>{e(msg)}</p>"
        log = run.stderr_tail(20)
        if log and err.get("code") not in ("no_key", "cancelled"):
            tail += f"<details id=stderr><summary>Last log lines (redacted)</summary><pre>{e(log)}</pre></details>"
        tail += "</div>"
    if not (final or err):
        tail += (f"<form method=post action='/admin/investigations/{e(rid)}/cancel' id=cancel>"
                 "<button>Cancel run</button></form>")
    refresh = "" if (final or err or pend or q) else '<meta http-equiv="refresh" content="2">'
    body = f"""{refresh}<p><a href="/admin/claims/{e(run.claim_id)}">&larr; Claim {e(run.claim_id)}</a></p>
<div class="box"><h2>Agent investigation: {e(run.claim_id)}</h2>
<p>Status <b id=status>{e(status)}</b> &middot; model {e(start.get('model', '...'))} &middot; compact mode {'on' if start.get('compact') else '...'}</p>
<p class=note>The agent runs against its own MCP server session (fresh demo state), not this page's store.</p>
<div class="table-wrap"><table id=trace style="table-layout:fixed;width:100%;word-break:break-word"><tr><th>#</th><th>Tool</th><th>Args</th><th>Result</th></tr>{rows}</table></div></div>
{progress_html}{pending_html}{tail}"""
    return page(f"Investigate {run.claim_id}", body)


@app.post("/admin/investigations/{rid}/resume")
def investigation_resume(rid: str, request: Request, approval_id: str = Form(""), approved: str = Form("false"),
                         reason: str = Form("")):
    run = _get_run(rid)
    if approved == "true":
        refused = self_approval_refusal(request, run.claim_id, f"investigation:{rid}:resume")
        if refused:
            return back_to_claim(run.claim_id, refused)
    run.resume(approval_id, approved == "true", reason,
               reviewer=auth.reviewer_of(request))  # stale/duplicate answers are ignored
    return RedirectResponse(f"/admin/investigations/{rid}", status_code=303)


@app.post("/admin/investigations/{rid}/reply")
async def investigation_reply(rid: str, request: Request):
    run = _get_run(rid)
    form = await request.form()
    decisions = {k[4:]: str(v) for k, v in form.items() if k.startswith("dec_") and str(v)}
    if any(v.startswith("approve") for v in decisions.values()):
        refused = self_approval_refusal(request, run.claim_id, f"investigation:{rid}:reply")
        if refused:
            return back_to_claim(run.claim_id, refused)
    run.reply(str(form.get("question_id", "")), str(form.get("reply", "")), decisions,
              reviewer=auth.reviewer_of(request))  # stale answers ignored
    return RedirectResponse(f"/admin/investigations/{rid}", status_code=303)


@app.post("/admin/investigations/{rid}/cancel")
def investigation_cancel(rid: str):
    _get_run(rid).cancel()
    return RedirectResponse(f"/admin/investigations/{rid}", status_code=303)
