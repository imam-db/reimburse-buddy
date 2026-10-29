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
from contextvars import ContextVar
from datetime import date
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.decision import draft_decision
from reimburse_buddy.models import rupiah
from reimburse_buddy.rules import check_claim
from reimburse_buddy.triage import approve_cleared_batch, triage_claims

from . import auth, plan_state, spend, ui, users
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
SIGNED, UNSIGNED = label("ui_signed"), label("ui_unsigned")
CHOICES = ("approve_full", "approve_capped", "reject")
UPLOAD_DIR = Path(os.environ.get("REIMBURSE_WEB_UPLOADS", Path(__file__).resolve().parent.parent / "uploads"))
# One stylesheet for every screen, served by StaticFiles (the plan confirmation page links it too).
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_URL = "/static/app.css"
# The path of the request being rendered, so the nav can mark the active item (aria-current).
CURRENT_PATH: ContextVar[str] = ContextVar("rb_current_path", default="")

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
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
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
    """Close every route except /login, /logout, /healthz and /static/ when the login gate is on.

    A browser navigation (GET/HEAD) is redirected to the form; anything else gets 401, so a script
    cannot post its way in. Each role may only open its own area (employee, manager, finance); a wrong-role
    navigation is sent to the role's landing page, anything else gets 403. With no user store or DSN outside production the gate
    is off and every request passes through untouched."""
    path_token = CURRENT_PATH.set(request.url.path)
    try:
        if not auth.enabled() or request.url.path in auth.PUBLIC_PATHS or request.url.path.startswith("/static/"):
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
    finally:
        CURRENT_PATH.reset(path_token)


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
ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
        "%3Crect width='32' height='32' rx='8' fill='%231a5fb4'/%3E"
        "%3Ctext x='16' y='22' font-family='system-ui' font-size='18' font-weight='700' "
        "fill='white' text-anchor='middle'%3ER%3C/text%3E%3C/svg%3E")


def _pending_nav(path: str = "") -> str:
    """Link to the pending-plan list with its count. A broken plan store must never break every page."""
    try:
        from .plans import pending_count  # lazy: plans.py imports this module
        n = pending_count()
    except Exception:
        return ""
    current = ' aria-current="page"' if path == "/admin/plans" else ""
    return (f'<a href="/admin/plans" id="nav-pending"{current}>{e(label("ui_nav_pending"))} '
            f'<span class="badge" id="pending-badge">{n}</span></a>')


def _nav_link(href: str, text: str, path: str = "") -> str:
    """One nav item; the current page is marked for assistive tech."""
    current = ' aria-current="page"' if path == href else ""
    return f'<a href="{href}"{current}>{e(text)}</a>'


def nav_html() -> str:
    """The nav for the signed-in role (docs/adr/0001-three-roles.md); the full dev nav when nobody is signed in.

    Below 720 px the links live behind a CSS-only checkbox disclosure; the account block stays visible.
    The nav markup itself stays a bare `<nav>` element (tests read it by that tag)."""
    path = CURRENT_PATH.get()
    cur = auth.current_session()
    account = ""
    home = "/"
    if not cur:
        links = (_nav_link("/", label("ui_nav_home"), path)
                 + _nav_link("/employee", label("ui_nav_employee"), path)
                 + _nav_link("/admin", label("ui_nav_finance_admin"), path)
                 + _nav_link("/admin/intake", label("ui_nav_intake"), path)
                 + _pending_nav(path) + _budget_nav())
    else:
        emp, role = cur
        if users.is_finance(role):
            home = "/admin"
            links = (_nav_link("/admin", label("ui_nav_triage"), path)
                     + _nav_link("/admin/intake", label("ui_nav_intake"), path)
                     + _pending_nav(path) + _budget_nav())
        elif users.is_manager(role):
            home = "/manager"
            links = _nav_link("/manager", label("ui_nav_approvals"), path)
        else:
            home = "/employee"
            links = (_nav_link("/employee", label("ui_nav_my_claims"), path)
                     + _nav_link("/employee/new", label("ui_nav_submit_claim"), path))
        account = (f'<div class="nav-account">'
                   f'<span class="note" id="signed-in">{e(label("ui_nav_signed_in"))} '
                   f'<b>{e(auth.reviewer_name(emp))}</b> ({e(users.ROLE_LABELS.get(role, role))})</span>'
                   f'<form method="post" action="/logout">'
                   f'<button type="submit" id="logout">{e(label("ui_nav_sign_out"))}</button></form></div>')
    return (f'<nav><a class="brand" href="{e(home)}">reimburse-buddy</a>'
            f'<input type="checkbox" id="nav-toggle" class="nav-toggle">'
            f'<label for="nav-toggle" class="nav-toggle-label">'
            f'{e(label("ui_nav_menu"))} <span aria-hidden="true">▾</span></label>'
            f'<div class="nav-links">{links}</div>{account}</nav>')


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
    return (f'<form method="post" action="/admin/reset"><button type="submit" class="btn quiet-danger" '
            f"onclick=\"return confirm('Reset data demo?')\">{e(text)}</button></form>")


def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """The one page shell: language, head metadata, skip link, role-aware header, main and footer."""
    return HTMLResponse(f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Reimburse Buddy</title>
<meta name="description" content="Simulasi reimburse: triage finance, klaim karyawan, dan persetujuan rencana. Data sintetis.">
<meta name="theme-color" content="#1a5fb4">
<link rel="icon" href="{ICON}">
<link rel="stylesheet" href="{STATIC_URL}"></head><body>
{ui.skip_link()}<header class="site-header">{nav_html()}</header>
<main id="main" tabindex="-1"><h1>{escape(title)}</h1>{body}</main>
<footer class="site-footer"><p class="note" id="footer-note">Synthetic data. Simulation only.</p></footer>
<script>window.addEventListener('load',function(){{var a=document.querySelector('.alert');if(a&&!location.hash)a.focus();}});</script>
</body></html>""", status_code=status_code)


def e(x) -> str:
    return escape(str(x))


MONTHS_ID = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def fmt_date(value) -> str:
    """'2026-10-02' -> '2 Okt 2026'; anything else is shown unchanged."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return str(value or "")
    return f"{d.day} {MONTHS_ID[d.month - 1]} {d.year}"


def fmt_moment(value) -> str:
    """'2026-10-02 14:30' -> '2 Okt 2026 14:30'; the ISO value stays in the title attribute."""
    text = str(value or "")
    if len(text) >= 16 and text[10] in " T":
        return f"{fmt_date(text)} {text[11:16]}"
    return fmt_date(text)


def date_html(value) -> str:
    iso = e(str(value if value is not None else ""))
    return f'<time datetime="{iso}" title="{iso}">{e(fmt_date(value))}</time>'


def moment_html(value) -> str:
    iso = e(str(value if value is not None else ""))
    return f'<time datetime="{iso}" title="{iso}">{e(fmt_moment(value))}</time>'


# --- failed navigations get a page, scripts still get JSON -----------------------------
ERROR_TEXT = {400: "ui_err_400", 403: "ui_err_403", 404: "ui_err_404", 409: "ui_err_409",
              413: "ui_err_413", 422: "ui_err_422", 503: "ui_err_503"}


def _wants_html(request: Request) -> bool:
    """A browser navigation: GET/HEAD that accepts HTML. Everything else keeps the JSON body."""
    return request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", "")


def _error_page(request: Request, status_code: int, detail: str = "") -> HTMLResponse:
    cur = auth.current_session()
    home = auth.home_for(cur[1]) if cur else "/"
    body = (ui.alert(label(ERROR_TEXT.get(status_code, "ui_err_400")), tone="danger", role="alert", id="error")
            + f'<p><a class="btn primary" href="{e(home)}">{e(label("ui_back_home"))}</a></p>')
    response = page(label("ui_error_title"), body, status_code=status_code)
    if detail:
        response.headers["X-Error-Detail"] = detail[:200]
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if not _wants_html(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    response = _error_page(request, exc.status_code)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    if not _wants_html(request):
        return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)
    return _error_page(request, 422, "validation")


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
    err = f'<p class="login-error" id="login-error" role="alert" tabindex="-1">{e(error)}</p>' if error else ""
    show, hide = e(label("show_password")), e(label("hide_password"))
    html = f"""<!doctype html><html lang="id"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(label("sign_in"))} · Reimburse Buddy</title>
<meta name="theme-color" content="#1a5fb4"><link rel="icon" href="{ICON}">
<link rel="stylesheet" href="{STATIC_URL}"></head><body class="login-shell">
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
var er=document.getElementById("login-error");if(er)er.focus();
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


def flash_text(msg: str, item: str = "") -> str:
    """Text for a flash message the redirects carry (?msg=...). Unknown text is shown as it was sent.

    A bare "Submitted" is the new-claim flow (files were forwarded); "Submitted CLM-..." is the older
    text form, which forwards nothing. `item` is the readable name of the item a file was added to."""
    if msg == "Submitted":
        return label("ui_flash_submitted_files")
    if msg == "File added" and item:
        return label("ui_flash_file_added_to").format(item=item)
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
    from .upload_flow import action_count  # lazy: upload_flow imports this module
    claims = [c for c in store.claims if c["employee_id"] == emp]
    needs = {c["id"]: action_count(c["id"]) for c in claims}
    # claims that need the employee first, then newest submitted, ties by ID descending
    claims.sort(key=lambda c: (str(c.get("submitted_at") or ""), c["id"]), reverse=True)
    claims.sort(key=lambda c: needs[c["id"]] == 0)

    def status_cell(c: dict) -> str:
        n = needs[c["id"]]
        return status_tag(c["status"]) + (ui.badge(label("ui_n_need_action").format(n=n), "warn") if n else "")

    rows = [[f"<a href='/employee/claims/{e(c['id'])}'>{e(c['id'])}</a>", e(c["purpose"]),
             f"{e(c['trip']['destination'])} {date_html(c['trip']['start_date'])} "
             f"&rarr; {date_html(c['trip']['end_date'])}",
             e(rupiah(sum(Decimal(i["amount"]) for i in c["items"]))), status_cell(c)]
            for c in claims]
    cards = "".join(
        f"<li class='claim-card'><div class='line1'><a href='/employee/claims/{e(c['id'])}'>{e(c['purpose'])}</a> "
        f"{status_cell(c)}</div>"
        f"<div class='line2'>{e(c['trip']['destination'])} {date_html(c['trip']['start_date'])} "
        f"&rarr; {date_html(c['trip']['end_date'])}</div>"
        f"<div class='line3 note'>{e(rupiah(sum(Decimal(i['amount']) for i in c['items'])))} &middot; {e(c['id'])}</div></li>"
        for c in claims)
    claims_html = ui.table([label('ui_col_id'), label('ui_col_purpose'), label('ui_col_trip'), label('ui_col_claimed'),
                            label('ui_col_status')], rows, num=(3,), caption=label('ui_my_claims'),
                           empty=label('ui_no_claims'))
    if rows:  # a table on wide screens, one compact block per claim below 720 px
        claims_html = f"<div class='claims-wide'>{claims_html}</div><ul class='claims-compact'>{cards}</ul>"
    inbox = [m for m in store.outbox if m["to"] == emp]
    msgs = [[e(m["claim_id"]), e(m["subject"]), e(m["body"])] for m in inbox]
    messages_html = ""  # only when finance wrote to this employee, right after "Klaim saya"
    if msgs:
        messages_html = (f"<h2>{e(label('ui_messages_from_finance'))}</h2>"
                         + ui.table([label('ui_col_claim'), label('ui_col_subject'), label('ui_col_body')], msgs,
                                    caption=label('ui_messages_from_finance')))
    fwd = [m for m in store.inbox if m.get("owner_id") == emp]
    fwd_rows = [[date_html(m["date"]), e(m["subject"]),
                 e(", ".join(a["name"] for a in m.get("attachments", [])))] for m in fwd]
    history_table = ui.table([label('ui_col_date'), label('ui_col_subject'), label('ui_col_attachments')], fwd_rows,
                             caption=label('ui_forwarded_mailbox'), empty=label('ui_no_forwarded'))
    body = f"""
{ui.alert(flash_text(msg), tone="success") if msg else ''}
{ui.page_header("", label('ui_new_claim_hint'), '<a class="btn primary" href="/employee/new">' + e(label('ui_start_new_claim')) + '</a>')}
<h2>{e(label('ui_my_claims'))}</h2>
{claims_html}
{messages_html}
{portal_box(request)}
{ui.details(label('ui_forwarded_history').format(n=len(fwd_rows)), history_table)}
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
    entries = [(run_check(x["claim_id"]).total_pending, x) for x in t["exceptions"]]
    entries.sort(key=lambda entry: entry[0], reverse=True)  # held money, descending (P7)
    held_total = sum((held for held, _ in entries), Decimal("0"))
    cards = []
    for held, x in entries:
        gaps = "".join(gap_tag(g) for g in x["gaps"])
        rejected = (f'<p class="note">{e(label("ui_rejected_items"))}: {e(", ".join(x["rejected_items"]))}</p>'
                    if x["rejected_items"] else "")
        cards.append(
            f'<article class="item-card" id="exception-{e(x["claim_id"])}" data-held="{int(held)}">'
            f'<div class="item-head"><a href="/admin/claims/{e(x["claim_id"])}">{e(x["claim_id"])}</a>'
            f'<span class="note" title="{e(x["employee_id"])}">{e(person(store, x["employee_id"]))}</span></div>'
            f'<div class="amount">{e(rupiah(held))}</div>'
            f'<div class="chips">{gaps}</div>{rejected}'
            f'<form method=post action="/admin/claims/{e(x["claim_id"])}/investigate">'
            f'<button class="primary">{e(label("ui_action_investigate"))}</button></form></article>')
    cleared = t["auto_cleared"]
    hidden = "".join(f'<input type="hidden" name="claim_ids" value="{e(c)}">' for c in cleared)
    others = [[f"<a href='/admin/claims/{e(c['id'])}'>{e(c['id'])}</a>",
               f'<span title="{e(c["employee_id"])}">{e(person(store, c["employee_id"]))}</span>',
               e(c.get("batch", "")), status_tag(c["status"])] for c in store.claims]
    est = t["time_saved_estimate"]
    summary = label("ui_triage_summary").format(claims=t["claims"], cleared=len(cleared),
                                                exceptions=len(entries))
    body = f"""{ui.alert(msg, tone="success") if msg else ''}
<form method="get" class="filter"><label>{e(label('ui_batch'))} <select name="batch" aria-label="Pilih batch" onchange="this.form.submit()">{options([(BATCH, BATCH), (OWN_BATCH, OWN_BATCH), (WEB_BATCH, WEB_BATCH)], batch)}</select></label></form>
<h2 class="visually-hidden">{e(summary)}</h2>
{ui.stats([ui.stat(label('ui_claims'), t['claims']), ui.stat(label('ui_cleared'), len(cleared)),
           ui.stat(label('ui_exceptions'), len(entries), tone='warn' if entries else ''),
           ui.stat(label('ui_held_money'), rupiah(held_total), tone='warn' if held_total else '')],
          id='triage-stats')}
<div class="box"><p>{e(label('ui_time_saved'))}: {e(label('ui_minutes').format(n=est['minutes']))} <span class="tag warn">{e(label('ui_estimate'))}</span> <span class="note">{e(est['assumption'])}</span></p>
<p class="note">{e(label('ui_triage_readonly'))}</p>
<h3>{e(label('ui_cleared'))}</h3><p>{''.join(f'<span class=tag>{e(c)}</span>' for c in cleared) or '<span class=note>' + e(label('ui_cleared_none')) + '</span>'}</p>
{f'''<form method="post" action="/admin/batch/approve"><input type="hidden" name="batch" value="{e(batch)}">{hidden}
<label><input type="checkbox" name="reviewer_confirmed" value="true"> {e(label('ui_reviewed_approve').format(n=len(cleared)))}</label>
<button type="submit">{e(label('ui_approve_cleared'))}</button></form>''' if cleared else ''}
<h3>{e(label('ui_exceptions'))}</h3>
<div class="item-list" id="exceptions">{''.join(cards) or ui.empty_state(label('ui_exceptions_none'))}</div></div>
<h2>{e(label('ui_all_claims'))}</h2>
{ui.table([label('ui_claims'), label('ui_employee'), label('ui_batch'), label('ui_status')], others,
          caption=label('ui_all_claims'))}
{reset_form(label("ui_demo_reset"))}"""
    return page(label("ui_triage_title"), body)


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
    text = (label("ui_approved_n").format(n=res["count"]) if res["ok"]
            else label("ui_batch_refused_note"))
    return RedirectResponse(f"/admin?batch={batch}&msg={quote(text, safe='')}", status_code=303)


@app.post("/admin/reset")
def reset():
    if auth.is_production():
        return JSONResponse({"detail": "Reset demo tidak tersedia di produksi."}, status_code=403)
    reset_all()
    return RedirectResponse(f"/admin?msg={quote(label('ui_msg_demo_reset'), safe='')}", status_code=303)


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
    item_cards: list[str] = []
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
                   f"<button name=choice value='reject'{' disabled' if it.human_decision == 'reject' else ''} class='btn quiet-danger'>"
                   f"{e(label('ui_action_reject'))}</button></form>"
                   f"<div class=note>{e(label('ui_needs_document').format(documents=', '.join(label(g) for g in it.gaps)))}</div>")
            if it.human_decision:
                dec += f"<div class=note>{e(label('ui_reviewer_said').format(decision=label(it.human_decision)))}</div>"
        elif (decision_gaps or it.human_decision) and not pend:
            dec = (f"<form method=post action='/admin/claims/{e(cid)}/decide' style='display:inline'>"
                   f"<input type=hidden name=item_id value='{e(it.item_id)}'>"
                   + "".join(
                       f"<button name=choice value='{ch}'"
                       + (" disabled" if it.human_decision == ch else "")
                       + (" class='btn quiet-danger'" if ch == "reject" else "")
                       + f">{e(label(ch))}</button> "
                       for ch in CHOICES) + "</form>")
            if it.human_decision:
                dec += f"<div class=note>{e(label('ui_reviewer_said').format(decision=label(it.human_decision)))}</div>"
        elif it.gaps and not pend:
            dec = f"<div class=note>{e(label('ui_needs_review').format(gaps=', '.join(label(g) for g in it.gaps)))}</div>"
        status_cell = f"<span title='{e(it.status.value)}'>{e(label(it.status.value))}</span>"
        if pend:
            status_cell = f"<span class='tag warn'>{e(label('ui_pending_review'))}</span>"
            for mid in pend:
                p = r["pending"][mid]
                why = ", ".join(label(x) for x in (p.get("reasons") or [p["reason"]]))
                dec = (f"<form method=post action='/admin/claims/{e(cid)}/evidence/{e(mid)}/review' style='display:inline'>"
                       f"<div class=note>{e(mid)}: <span title='{e(p['reason'])}'>{e(why)}</span></div>"
                       f"<button name=verdict value=accept>{e(label('ui_accept_evidence'))}</button> "
                       f"<button name=verdict value=reject>{e(label('ui_reject_evidence'))}</button></form>") + dec
        if self_blocked:  # UX only: the server refuses these requests anyway
            dec = f"<div class=note id='self-approval'>{e(label('self_approval_note'))}</div>"
        if decision_gaps or document_only:  # one action row, only when the item needs a decision
            dec += manager_cell(cid, it.item_id, emp)
        evidence = (ui.chips(list(it.evidence_used), tone="ok") if it.evidence_used
                    else f"<span class=note>{e(label('ui_none_attached'))}</span>")
        item_cards.append(
            f"<article class='item-card' id='item-{e(it.item_id)}' tabindex='-1'>"
            f"<div class='item-head'><div><span class='item-title'>{e(label(it.category))}</span>"
            f"<div class='note'>{e(src['description'])}</div></div>"
            f"<div class='amount'>{e(rupiah(it.claimed))}</div></div>"
            f"<p class='note'>{e(label('ui_approvable'))}: {e(rupiah(it.approvable))} &middot; {status_cell}</p>"
            f"{provenance_badges(src)}{finds}"
            f"<div class='chips'>{gaps}</div>"
            f"<p class='note'>{e(label('ui_evidence'))}: {evidence}</p>"
            + (f"<div class='btn-row'>{dec}</div>" if dec else "")
            + "</article>")
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
                       f"<b>{e(label(x['verdict']))}</b></li>" for x in r["evidence_log"])
    ignored = "".join(f"<li>{e(i.id)}: <span class='tag bad' title='{e(i.reason)}'>{e(label(i.reason))}</span></li>"
                      for i in c.ignored_evidence)
    warns = "".join(f"<li>{e(w.message)}</li>" for w in c.warnings)

    def attach_btn(eid: str) -> str:
        if eid in r["evidence"]:
            return f"<span class='tag ok'>{e(label('ui_attached'))}</span>"
        return (f"<form method=post action='/admin/claims/{e(cid)}/evidence' style='display:inline'>"
                f"<input type=hidden name=evidence_id value='{e(eid)}'><input type=hidden name=q value='{e(q)}'><button>{e(label('ui_attach'))}</button></form>")

    hits = store.search_reimburse_mailbox(emp, q) if q else []
    hit_rows = [[e(h["id"]), e(h["date"]), f"{e(h['subject'])}<div class=note>{e(h['snippet'])}</div>",
                 e(", ".join(h["attachments"])), attach_btn(h["id"])] for h in hits]
    cal = store.get_calendar(emp, trip["start_date"], trip["end_date"])
    cal_rows = [[e(x["id"]), f"{e(x['date'])} {e(x['start'])}-{e(x['end'])}", e(x["title"]),
                 e(x["location"]), attach_btn(x["id"])] for x in cal]
    memos = store.find_approval_memo(emp, trip["destination"])
    memo_rows = [[e(m["id"]), SIGNED if m.get("signed") else UNSIGNED, e(m.get("reason", "")),
                  attach_btn(m["id"])] for m in memos]
    sent = [m for m in store.outbox if m.get("claim_id") == cid]
    sent_rows = "".join(f"<li>{e(m['id'])} {e(label('ui_to')).lower()} {e(m['to'])}: {e(m['subject'])}</li>" for m in sent)
    note = re.sub(r"\b[a-z]+(?:_[a-z]+)+\b", lambda m: label(m.group(0)),
                  draft_decision(store, c, language="id"))
    note_en = re.sub(r"\b[a-z]+(?:_[a-z]+)+\b", lambda m: label(m.group(0)),
                     draft_decision(store, c, language="en"))
    allowed_status = STATUS_FOR_DECISION[c.decision.value]
    if pending_items(cid):
        allowed_status = ["on_hold"]
    if self_blocked:
        status_form = (f"<div class='note' id='self-approval-status'>{e(label('self_approval_note'))}</div>")
    else:
        status_form = (f"<form method=post action=\"/admin/claims/{e(cid)}/status\"><label>{e(label('ui_final_status'))} "
                       f"<select name=status aria-label=\"{e(label('ui_final_status'))}\">{options([(s, label(s)) for s in allowed_status])}"
                       f"</select></label>\n<label><input type=checkbox name=reviewer_confirmed value=true> "
                       f"{e(label('ui_confirm_decision'))}</label> <button>{e(label('ui_action_apply_status'))}</button></form>")
    engine_amounts = label('ui_engine_amounts').format(
        approvable=rupiah(c.total_approvable), claimed=rupiah(c.total_claimed), pending=rupiah(c.total_pending))
    manual = f"""{f'<h3>' + e(label('ui_evidence_review_log')) + f'</h3><ul>{log_rows}</ul>' if log_rows else ''}
{f'<h3>' + e(label('ui_ignored_evidence')) + f'</h3><ul>{ignored}</ul>' if ignored else ''}
{f'<h3>' + e(label('ui_warnings')) + f'</h3><ul>{warns}</ul>' if warns else ''}
<h3>{e(label('ui_search_mailbox'))}</h3><form method=get><label>{e(label('ui_search_mailbox'))} <input name=q aria-label="{e(label('ui_search_mailbox'))}" value="{e(q)}" size=40 placeholder="{e(label('ui_mailbox_placeholder'))}"></label><button>{e(label('ui_search'))}</button></form>
{ui.table(['ID', label('ui_col_date'), label('ui_col_subject'), label('ui_col_attachments'), ''], hit_rows, caption=label('ui_search_mailbox')) if hits else ''}
<h3>{e(label('ui_calendar_trip'))}</h3>{ui.table(['ID', label('ui_col_date'), label('ui_col_subject'), label('ui_field_destination'), ''], cal_rows, caption=label('ui_calendar_trip'), empty=label('ui_calendar_empty'))}
<h3>{e(label('ui_memos').format(destination=trip['destination']))}</h3>{ui.table(['ID', label('ui_signature'), label('ui_col_reason'), ''], memo_rows, caption=label('ui_memos').format(destination=trip['destination']), empty=label('ui_memos_empty'))}
<h3>{e(label('ui_ask_employee'))}</h3><form method=post action="/admin/claims/{e(cid)}/message">
<label>{e(label('ui_subject'))} <input name=subject aria-label="{e(label('ui_subject'))}" size=40 value="{e(label('ui_default_subject').format(cid=cid))}"></label>
<label>{e(label('ui_body'))} <input name=body aria-label="{e(label('ui_body'))}" size=60 placeholder="Mohon kirim struk taksi tanggal 3 Okt (IT-5)." required></label>
<button>{e(label('ui_send'))}</button></form>{f'<ul>{sent_rows}</ul>' if sent_rows else ''}"""
    body = f"""<p><a href="/admin">&larr; {e(label('ui_back_to_triage'))}</a></p>{ui.alert(msg, tone="success") if msg else ''}
<section class="claim-summary" id="claim-summary" aria-label="{e(label('ui_claim_summary'))}">
<div class="summary-body">
<h2>{e(label('ui_rule_engine').format(decision=label(c.decision.value)))}</h2>
<p class="note"><span class="tag">{e(cid)}</span> <span title="{e(emp)}">{e(person(store, emp))}</span> &middot; {e(claim['purpose'])} &middot; {e(trip['destination'])}
{date_html(trip['start_date'])} &rarr; {date_html(trip['end_date'])} &middot; {e(label('ui_status'))} {status_tag(claim['status'])}</p>
<p>{engine_amounts}</p>
<p class="note">{e(label('ui_engine_note'))} {e(label('ui_evidence_attached').format(evidence=', '.join(r['evidence']) or label('ui_none_attached')))}</p>
</div>
<div class="summary-actions">
<form method=post action="/admin/claims/{e(cid)}/investigate"><button class="primary">{e(label('ui_action_investigate'))}</button></form>
<a class="btn" href="#claim-items">{e(label('ui_view_items'))}</a>
<a class="btn" href="/admin/claims/{e(cid)}/audit">{e(label('ui_audit_link'))}</a>
</div>
{needs_you}
</section>
<div class="item-list" id="claim-items">{''.join(item_cards)}</div>
{ui.details(label('ui_investigate_manual'), manual, id='manual')}
<div class="box" id="decision-note-box"><h2>{e(label('ui_decision_note'))}</h2>
<p class="btn-row"><button type="button" class="btn" id="copy-note">{e(label('ui_copy_note'))}</button></p>
<pre id="decision-note">{e(note)}</pre>
{ui.details(label('ui_english'), f"<pre>{e(note_en)}</pre>")}
{status_form}
<p class="note">{e(label('ui_status_follows').format(decision=label(c.decision.value)))} {e(label('ui_status_confirmed_note'))}</p></div>
<script>(function(){{var b=document.getElementById('copy-note'),n=document.getElementById('decision-note');
if(!b||!n)return;b.addEventListener('click',function(){{navigator.clipboard.writeText(n.textContent).then(
function(){{b.textContent='{e(label("ui_copied"))}';}},function(){{b.textContent='{e(label("ui_copy_failed"))}';}});}});}})();</script>"""
    return page(label("ui_claim_title").format(cid=cid), body)


@app.get("/admin/claims/{cid}/audit", response_class=HTMLResponse)
def claim_audit(cid: str):
    """Read-only audit trail for one claim. Raw system codes are shown here and only here."""
    try:
        store.get_claim(cid)
    except KeyError:
        raise HTTPException(404, f"Unknown claim {cid}")
    r = review(cid)
    status_rows = [[e(x.get("claim_id")), e(x.get("from")), e(x.get("to")), e(x.get("note", ""))]
                   for x in store.status_log if x.get("claim_id") == cid]
    evidence_rows = [[moment_html(x.get("at")), e(x.get("evidence_id")), e(x.get("item_id")),
                      e(x.get("reason")), e(x.get("verdict"))] for x in r["evidence_log"]]
    decision_rows = [[e(item_id), e(choice)] for item_id, choice in r["decisions"].items()]
    outbox_rows = [[e(m.get("id")), e(m.get("to")), e(m.get("subject")), e(m.get("body"))]
                   for m in store.outbox if m.get("claim_id") == cid]
    body = f"""<p><a href="/admin/claims/{e(cid)}">&larr; {e(label('ui_back_to_claim'))}</a></p>
<p class="note">{e(label('ui_audit_note').format(cid=cid))}</p>
<div class="box"><h2>{e(label('ui_status_history'))}</h2>{ui.table([label('ui_claims'), 'Dari', label('ui_to'), label('ui_note')], status_rows, caption=label('ui_status_history'), empty=label('ui_no_status_changes'))}</div>
<div class="box"><h2>{e(label('ui_evidence_log'))}</h2>{ui.table(['Waktu', label('ui_evidence'), label('ui_item'), label('ui_col_reason'), label('ui_verdict')], evidence_rows, caption=label('ui_evidence_log'), empty=label('ui_no_evidence_records'))}</div>
<div class="box"><h2>{e(label('ui_item_decisions'))}</h2>{ui.table([label('ui_item'), label('ui_reviewer_decision')], decision_rows, caption=label('ui_item_decisions'), empty=label('ui_no_item_decisions'))}</div>
<div class="box"><h2>{e(label('ui_outbound'))}</h2>{ui.table(['ID', label('ui_to'), label('ui_subject'), label('ui_body')], outbox_rows, caption=label('ui_outbound'), empty=label('ui_no_outbound'))}</div>"""
    return page(label("ui_audit_title").format(cid=cid), body)


@app.post("/admin/claims/{cid}/evidence")
def add_evidence(cid: str, evidence_id: str = Form(...), q: str = Form("")):
    store.get_claim(cid)
    p = review(cid)["pending"].get(evidence_id)
    if p and p["state"] != "accepted":
        msg = label("ui_msg_evidence_not_attached").format(ref=evidence_id)
        return RedirectResponse(f"/admin/claims/{cid}?msg={quote(msg, safe='')}", status_code=303)
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
    return RedirectResponse(f"/admin/claims/{cid}?msg={quote(label('ui_msg_evidence_state').format(ref=mid, state=label(p['state'])), safe='')}", status_code=303)


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
    return RedirectResponse(f"/admin/claims/{cid}#item-{item_id}", status_code=303)


@app.post("/admin/claims/{cid}/message")
def message(cid: str, subject: str = Form(...), body: str = Form(...)):
    claim = store.get_claim(cid)
    res = store.send_message(claim["employee_id"], body, subject, cid)
    msg = label("ui_msg_sent").format(ref=res["message_id"], replies=res["replies_received"])
    return RedirectResponse(f"/admin/claims/{cid}?msg={quote(msg, safe='')}", status_code=303)


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
        msg = label("ui_msg_not_changed").format(reason="centang konfirmasi dulu")
        return RedirectResponse(f"/admin/claims/{cid}?msg={quote(msg, safe='')}", status_code=303)
    if status != "on_hold":  # holding your own claim is not an approval; anything else is
        refused = self_approval_refusal(request, cid, f"status:{status}")
        if refused:
            return back_to_claim(cid, refused)
    c = run_check(cid)
    if pending_items(cid) and status != "on_hold":
        msg = label("ui_msg_not_changed").format(reason="bukti masih menunggu tinjauan")
        return RedirectResponse(f"/admin/claims/{cid}?msg={quote(msg, safe='')}", status_code=303)
    if status not in STATUS_FOR_DECISION[c.decision.value]:
        reason = (f"{label(status)} tidak diizinkan selama mesin aturan menyatakan "
                  f"{label(c.decision.value)}")
        msg = label("ui_msg_not_changed").format(reason=reason)
        return RedirectResponse(f"/admin/claims/{cid}?msg={quote(msg, safe='')}", status_code=303)
    store.update_claim_status(cid, status, f"Keputusan peninjau; mesin aturan menyetujui {rupiah(c.total_approvable)}")
    msg = label("ui_msg_status_set").format(status=label(status))
    return RedirectResponse(f"/admin/claims/{cid}?msg={quote(msg, safe='')}", status_code=303)


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
    back = f"<p><a href='/admin/claims/{e(cid)}'>&larr; {e(label('ui_back_to_claim'))}</a></p>"
    if not _agent_ready():
        return page(label("ui_investigate_title").format(cid=cid),
                    f"{back}<div class='box'><h2>{e(label('ui_agent_not_configured'))}</h2>"
                    f"<p class='tag warn' id=no-key>{e(NO_KEY_MESSAGE)}</p></div>")
    if spend.is_production() and not spend.configured():
        # Defence in depth: startup already refuses this, and no uncapped run may start either way.
        _audit_limit(cid, "llm_budget_unconfigured")
        return page(label("ui_investigate_title").format(cid=cid),
                    f"{back}<div class='box'><p class='tag warn' id=budget-unconfigured>{e(label('budget_unconfigured'))}</p></div>")
    try:
        run = INVESTIGATOR.start(cid)
    except RateLimited as exc:
        return page(label("ui_investigate_title").format(cid=cid),
                    f"{back}<div class='box'><p class='tag warn' id=rate-limited>{e(exc)}</p></div>")
    except spend.LedgerUnavailable as exc:
        # The ledger could not be reached even after a reconnect: refuse the run, say so plainly. Do not
        # ask the ledger for the remaining budget here - it is the thing that is down.
        return page(label("ui_investigate_title").format(cid=cid),
                    f"{back}<div class='box'><p class='tag bad' id=ledger-unavailable>{e(str(exc))}</p></div>")
    except spend.BudgetRefused:
        # A second ledger call here must not turn the refusal into a 500 if the database drops meanwhile.
        try:
            left = f"${spend.remaining():.2f}"
            _audit_limit(cid, "daily_llm_budget", budget=str(spend.budget()), committed=str(spend.committed()))
        except Exception:  # noqa: BLE001 - the refusal itself already happened
            left = "?"
        return page(label("ui_investigate_title").format(cid=cid),
                    f"{back}<div class='box'><p class='tag warn' id=budget-exhausted>{e(label('budget_exhausted'))}</p>"
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

    return (f"<div class=box id=rule-engine><b>Mesin aturan saat ini (check_claim, tanpa bukti tambahan):</b> "
            f"keputusan <code>{e(d.get('decision'))}</code>, disetujui {rp(d.get('total_approvable'))} "
            f"dari {rp(d.get('total_claimed'))}, tertunda {rp(d.get('total_pending'))}</div>")


def engine_box(engine: dict | None) -> str:
    """The exact engine basis the approval is bound to (the snapshot that goes into args_hash)."""
    if not engine:
        return ""

    def rp(v):
        try:
            return "Rp" + f"{int(float(v)):,}".replace(",", ".")
        except (TypeError, ValueError):
            return e(v)

    ev = ", ".join(str(x) for x in engine.get("evidence_ids") or []) or label("ui_none_attached")
    hd = json.dumps(engine.get("human_decisions") or {}, ensure_ascii=False, sort_keys=True)
    return (f"<div class=box id=approved-engine><b>Dasar persetujuan:</b> "
            f"status <code>{e(engine.get('status'))}</code>, disetujui {rp(engine.get('approvable_amount'))} "
            f"&middot; bukti {e(ev)} &middot; keputusan peninjau {e(hd)}</div>")


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
            tag = f" <span class='tag bad'>{e(label('ui_action_reject'))}</span>" if x.get("declined") else ""
            rows += (f"<tr class=step><td>{x['n']}</td><td><code>{e(x['tool'])}</code>{tag}</td>"
                     f"<td><code>{e(x['args_summary'])}</code></td><td>{e(x['result_summary'])}</td></tr>")
        elif x["type"] == "auto_declined":
            rows += f"<tr><td></td><td colspan=3 class=note>{e(label('ui_auto_declined'))}</td></tr>"
        elif x["type"] == "question":
            rows += f"<tr><td></td><td colspan=3 class=note>{e(label('ui_agent_asked').format(text=x['text'][:300]))}</td></tr>"
        elif x["type"] == "reply_applied":
            dec = ", ".join(f"{k}: {v}" for k, v in (x.get("decisions") or {}).items()) or label("ui_none_attached")
            rows += f"<tr><td></td><td colspan=3 class=note>{e(label('ui_reviewer_replied'))} ({e(dec)})</td></tr>"
        elif x["type"] == "answer_applied":  # shown only once the worker acknowledged it
            who = (f" oleh {e(person(store, x['reviewer']))}" if x.get("reviewer") else "")
            verdict = label("ui_reviewer_approved") if x["approved"] else label("ui_reviewer_declined")
            rows += (f"<tr><td></td><td colspan=3 class=note>{e(verdict)} "
                     f"<code>{e(x['tool'])}</code>{who}</td></tr>")
    pend = run.pending
    pending_html = ""
    approve_form = ""
    if pend and not self_blocked:
        approve_form = (f"<form method=post action=\"/admin/investigations/{e(rid)}/resume\" style=\"display:inline\">"
                        f"<input type=hidden name=approval_id value=\"{e(pend['approval_id'])}\">"
                        f"<input type=hidden name=approved value=true><button>{e(label('ui_approve'))}</button></form>")
    elif pend:
        approve_form = (f"<p class='note' id='self-approval'>{e(label('self_approval_note'))}</p>")
    if pend:
        pending_html = f"""<div class="box" id=pending><h2>{e(label('ui_pending_write'))}</h2>
<p><code>{e(pend['tool'])}</code></p>{('<p><strong>' + e(pend['prompt']) + '</strong></p>') if pend.get('prompt') else ''}<pre>{e(json.dumps(pend['args'], ensure_ascii=False, indent=2))}</pre>
{decision_box(pend.get('current_decision'))}
{engine_box(pend.get('engine'))}
<p class=note>{e(label("investigation_gate_note"))}</p>
{approve_form}
<form method=post action="/admin/investigations/{e(rid)}/resume" style="display:inline"><input type=hidden name=approval_id value="{e(pend['approval_id'])}"><input type=hidden name=approved value=false>
<label>{e(label('ui_decline'))} <input name=reason aria-label="{e(label('ui_reason_optional'))}" size=40 placeholder="{e(label('ui_reason_optional'))}"></label><button>{e(label('ui_decline'))}</button></form></div>"""
    q = run.question
    if q:
        sel = "".join(
            f"<tr><td><code>{e(i['item_id'])}</code> {e(i.get('category') or '')}</td><td>{e(', '.join(i.get('gaps', [])))}</td>"
            f"<td><select name='dec_{e(i['item_id'])}' aria-label='{e(label('ui_decision_for').format(item=i['item_id']))}'><option value=''>{e(label('ui_no_decision_option'))}</option>"
            f"<option value=approve_full>{e(label('approve_full'))}</option><option value=approve_capped>{e(label('approve_capped'))}</option>"
            f"<option value=reject>{e(label('reject'))}</option></select></td></tr>" for i in q.get("items", []))
        pending_html += f"""<div class="box" id=question><h2>{e(label('ui_agent_asking'))}</h2><pre>{e(q['text'])}</pre>
<form method=post action="/admin/investigations/{e(rid)}/reply"><input type=hidden name=question_id value="{e(q['question_id'])}">
<div class="table-wrap"><table><caption class=visually-hidden>{e(label('ui_item_decisions'))}</caption><tr><th scope=col>{e(label('ui_item'))}</th><th scope=col>{e(label('ui_missing'))}</th><th scope=col>{e(label('ui_reviewer_decision'))}</th></tr>{sel}</table></div><p><label>{e(label('ui_reply'))} <textarea name=reply aria-label="{e(label('ui_reply'))}" rows=3 cols=60 placeholder="{e(label('ui_reply_placeholder'))}"></textarea></label></p>
<p class=note>{e(label('ui_decisions_note'))}</p>
<button>{e(label('ui_send_resume'))}</button></form></div>"""
    final = next((x for x in evs if x["type"] == "final"), None)
    err = next((x for x in evs if x["type"] == "error"), None)
    status = (label("ui_status_finished") if final else label("ui_status_stopped") if err
              else label("ui_status_awaiting_approval") if pend
              else label("ui_status_awaiting_answer") if q else label("ui_status_running"))
    progress_html = ""
    if not (final or err):
        steps = [progress_step_text(x["tool"]) for x in evs if x["type"] == "step"]
        elapsed = max(0, int(time.time() - run_started_at(run, start)))
        items = []
        for i, text in enumerate(steps):
            attr = (' aria-current="step"' if i == len(steps) - 1
                    else ' data-done="true"' if i < len(steps) - 1 else "")
            items.append(f"<li{attr}>{e(text)}</li>")
        if not items:
            items = [f'<li aria-current="step">{e(label("ui_waiting_first_step"))}</li>']
        progress_html = f"""<div class="box" id="progress"><h2>{e(label('ui_progress'))}</h2>
<ol class="stepper" id="progress-steps" aria-live="polite" aria-atomic="false">{''.join(items)}</ol>
<p>{e(label('ui_elapsed'))}: <b id="elapsed">{elapsed}</b> {e(label('ui_seconds'))}</p>
<script>(function(){{var n=document.getElementById('elapsed');if(!n)return;var s=parseInt(n.textContent,10)||0;
setInterval(function(){{s+=1;n.textContent=s;}},1000);}})();</script></div>"""
    tail = ""
    if final:
        t = final.get("tokens", {})
        hit = f" ({t.get('cache_hit', 0)} cache hit)" if t.get("cache_hit") else ""
        tail = (f"<div class=box id=final><h2>{e(label('ui_agent_final_note'))}</h2><pre>{e(final['note'])}</pre>"
                f"<p class=note>{e(label('ui_tokens').format(input=t.get('input', 0), output=t.get('output', 0)))}{hit} "
                f"{e(label('ui_amounts_note'))}</p></div>")
    if err:
        msg = NO_KEY_MESSAGE if err.get("code") == "no_key" else err.get("message", "")
        tail += f"<div class=box id=error><p class='tag bad'>{e(msg)}</p>"
        log = run.stderr_tail(20)
        if log and err.get("code") not in ("no_key", "cancelled"):
            tail += ui.details(label("ui_last_log"), f"<pre>{e(log)}</pre>", id="stderr")
        tail += "</div>"
    if not (final or err):
        tail += (f"<form method=post action='/admin/investigations/{e(rid)}/cancel' id=cancel>"
                 f"<button>{e(label('ui_cancel_run'))}</button></form>")
    refresh = "" if (final or err or pend or q) else (
        '<script>(function(){var t;function go(){clearTimeout(t);if(document.hidden)return;'
        't=setTimeout(function(){location.reload();},2000);}'
        'document.addEventListener("visibilitychange",go);go();})();</script>')
    body = f"""{refresh}<p><a href="/admin/claims/{e(run.claim_id)}">&larr; {e(label('ui_claim_title').format(cid=run.claim_id))}</a></p>
<div class="box"><h2>{e(label('ui_agent_investigation').format(cid=run.claim_id))}</h2>
<p>{e(label('ui_status_word'))} <b id=status>{e(status)}</b> &middot; {e(label('ui_model'))} {e(start.get('model', '...'))} &middot; {e(label('ui_compact'))} {e(label('ui_on') if start.get('compact') else '...')}</p>
<p class=note>{e(label('ui_agent_note'))}</p>
<div class="table-wrap"><table id=trace style="table-layout:fixed;width:100%;word-break:break-word"><caption class=visually-hidden>{e(label('ui_trace'))}</caption><tr><th scope=col>#</th><th scope=col>{e(label('ui_tool'))}</th><th scope=col>{e(label('ui_args'))}</th><th scope=col>{e(label('ui_result'))}</th></tr>{rows}</table></div></div>
{progress_html}{pending_html}{tail}"""
    return page(label("ui_investigate_title").format(cid=run.claim_id), body)


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
