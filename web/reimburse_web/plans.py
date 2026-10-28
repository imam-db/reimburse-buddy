"""Confirmation pages for Challenge B plans: /admin/plans and /admin/plans/<run_id>.

The office layer (agent/reimburse_agent/office) is standard-library only, so it is imported directly
(agent/ on sys.path) rather than through the agent subprocess. The web app never calls an office tool: a
submitted form only stores the reviewer's decision map; the plan graph (agent/plan_demo.py or a later
worker) resumes from it, logs approval/rejection and runs the executor.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from html import escape
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth
from .agent_bridge import AGENT_DIR
from .labels import label

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from reimburse_agent import audit  # noqa: E402
from reimburse_agent.office import page  # noqa: E402
from reimburse_agent.office import rules as R  # noqa: E402
from reimburse_agent.office.directory import Directory  # noqa: E402
from reimburse_agent.office.store import PlanStore  # noqa: E402

router = APIRouter()
STATE: dict = {}


def store() -> PlanStore:
    return STATE.setdefault("store", PlanStore())


def log() -> audit.AuditLog:
    """The audit log shared with the agent process, read fresh per request for the JSONL backend so the page
    sees events the agent wrote after the web process started. Opening a log no longer sweeps it (see
    audit.sweep_expired); the expiry sweep runs once at web startup instead, in startup_sweep().
    Without REIMBURSE_AUDIT_LOG / REIMBURSE_AUDIT_DATABASE_URL: one in-memory log for this process.
    In production only a connected Postgres log is allowed (audit.require_production_log); there is no
    fallback to JSONL or memory."""
    prod_log = audit.require_production_log()
    if prod_log is not None:
        STATE["log"] = prod_log  # never reuse a non-Postgres log cached before APP_ENV=prod
        return prod_log
    if "log" in STATE:
        return STATE["log"]
    path, dsn = os.environ.get("REIMBURSE_AUDIT_LOG"), os.environ.get("REIMBURSE_AUDIT_DATABASE_URL")
    if dsn:
        return STATE.setdefault("log", audit.PostgresAuditLog(dsn))
    if path:
        return audit.JsonlAuditLog(path)
    return STATE.setdefault("log", audit.JsonlAuditLog())


def startup_config_check() -> None:
    """Fail web startup when production is misconfigured instead of at the first request.

    Runs before startup_sweep() in FastAPI's on_startup list; an AuditConfigError here aborts startup."""
    log()


_SWEPT = False
_SWEEP_LOCK = threading.Lock()


def startup_sweep() -> int:
    """Expire orphaned staged actions once per web process (FastAPI startup hook).

    Plans still in this process's store are live and kept, so a worker opening the shared log can never
    expire a plan that is waiting on someone's page. Only runs whose plan store is gone (the server
    restarted without it) get `expired` events. Idempotent per process: a second call is a no-op.
    """
    global _SWEPT
    with _SWEEP_LOCK:
        if _SWEPT:
            return 0
        _SWEPT = True
    try:
        return audit.sweep_expired(log(), live=set(store().runs()),
                                   reason="server restarted before an answer")
    except Exception:
        logging.getLogger(__name__).exception("audit: startup expiry sweep failed")
        return 0


def directory() -> Directory:
    return STATE.setdefault("directory", Directory.load())


def _pending_plans() -> list[tuple[str, dict]]:
    """Runs still waiting for the reviewer: no saved decisions for their current version and at least one
    action with no outcome in the audit log (same status reading as the confirmation page)."""
    out: list[tuple[str, dict]] = []
    for run_id in store().runs():
        plan = store().load_plan(run_id)
        if not plan or plan.get("staging_errors"):
            continue
        if store().load_decisions(run_id, plan["version"]) is not None:
            continue
        events = log().list(run_id)
        if any(page._status(events, a["action_id"]) is None for a in plan.get("actions", [])):
            out.append((run_id, plan))
    return out


def pending_count() -> int:
    return len(_pending_plans())


def _requester_name(emp_id) -> str:
    contact = directory().get(str(emp_id))
    return contact.name if contact else str(emp_id)


def submitted_by(run_id: str, plan: dict | None = None) -> str:
    """The reviewer who filled the intake form, or "" for a plan started outside the web app.

    The plan is the first place to look; a run still waiting on a who-question has no plan yet, so
    request.json is the fallback (intake.start_run writes both)."""
    if plan and plan.get("submitted_by"):
        return str(plan["submitted_by"])
    try:
        saved = store().load_request(run_id) or {}
    except (ValueError, OSError):
        return ""
    return str(saved.get("submitted_by") or "")


def approval_block(run_id: str, plan: dict, reviewer: str) -> str | None:
    """Why this reviewer may not approve this plan, or None when they may.

    Two cases, both fail closed: the reviewer is the plan's requester, or the reviewer is the person who
    submitted the request (the intake lets them name anyone as the requester, so without the second check
    a reviewer could file a request "for" a colleague and then approve it)."""
    if not reviewer or reviewer == auth.FALLBACK_REVIEWER:
        return None  # the gate is off: no identity, no rule (unchanged dev behaviour)
    if plan.get("requester") == reviewer:
        return "requester"
    if submitted_by(run_id, plan) == reviewer:
        return "submitted_by"
    return None


@router.get("/admin/plans", response_class=HTMLResponse)
def plans_index():
    from . import app as web  # late import: app.py includes this router

    pending = _pending_plans()
    if not pending:
        body = "<p>Tidak ada rencana yang menunggu persetujuan.</p>"
    else:
        rows = "".join(
            f"<tr><td><a href='/admin/plans/{escape(run_id)}'>{escape(run_id)}</a></td>"
            f"<td>{escape(_requester_name(plan.get('requester')))}</td>"
            f"<td>{escape(plan.get('request', ''))}</td>"
            f"<td class=num>{len(plan.get('actions', []))}</td></tr>"
            for run_id, plan in pending)
        body = ("<div class='table-wrap'><table><tr><th>Run</th><th>Peminta / Requester</th>"
                "<th>Permintaan / Request</th><th class=num>Aksi / Actions</th></tr>"
                f"{rows}</table></div>")
    return web.page("Rencana menunggu / Pending plans", body)


@router.get("/admin/plans/{run_id}", response_class=HTMLResponse)
def plan_page(run_id: str, request: Request, msg: str = ""):
    from . import intake  # late import: intake.py imports this module

    question = intake.pending_for(run_id)
    if question and question.get("kind") == "clarify":
        return HTMLResponse(intake.render_question(run_id, question, msg=msg))
    try:
        plan = store().load_plan(run_id)
    except ValueError:
        plan = None
    if not plan:
        raise HTTPException(404, "no such plan")
    from . import app as web  # late import: app.py includes this router

    # A requester cannot approve their own plan, and neither can the reviewer who submitted the request
    # on their behalf: the approve control is disabled here and /decide refuses it server-side (fail
    # closed, nothing is stored).
    reviewer = auth.reviewer_of(request)
    block = approval_block(run_id, plan, reviewer)
    if plan.get("submitted_by") is None:
        plan["submitted_by"] = submitted_by(run_id)  # for the "Diajukan oleh" line only
    markup = page.render(plan, log(), directory(), R.default_rules(),
                         action_url=f"/admin/plans/{run_id}/decide", msg=msg, allow_approve=block is None,
                         blocked_note=(label("self_approval_submitted_note") if block == "submitted_by"
                                       else None))
    markup = markup.replace("</head>", f"<style>{web.NAV_CSS}</style></head>", 1)
    return HTMLResponse(markup.replace("<body>", f"<body>{web.nav_html()}", 1))


@router.post("/admin/plans/{run_id}/decide")
async def decide(run_id: str, request: Request):
    plan = store().load_plan(run_id)
    if not plan:
        raise HTTPException(404, "no such plan")
    form = await request.form()
    if form.get("plan_id") != plan["plan_id"]:
        return RedirectResponse(f"/admin/plans/{run_id}?msg=Stale+form:+the+plan+changed", status_code=303)
    ids = {a["action_id"] for a in plan["actions"]}
    decisions = {k.split(":", 1)[1]: v for k, v in form.items()
                 if k.startswith("decision:") and k.split(":", 1)[1] in ids and v in ("approve", "reject")}
    notes = {k.split(":", 1)[1]: str(v).strip()[:500] for k, v in form.items()
             if k.startswith("note:") and k.split(":", 1)[1] in ids and str(v).strip()}
    reviewer = auth.reviewer_of(request)
    block = approval_block(run_id, plan, reviewer)
    if block and any(v == "approve" for v in decisions.values()):
        # Rejections and notes stay allowed: they cannot approve anything. Nothing is stored.
        auth.audit_self_approval(run_id, reviewer, f"self_approval:plan:{run_id}:v{plan['version']}",
                                 plan_id=plan["plan_id"], requester=plan.get("requester"),
                                 submitted_by=submitted_by(run_id, plan), blocked=block)
        return RedirectResponse(f"/admin/plans/{run_id}?msg={quote(label('self_approval_blocked'), safe='')}",
                                status_code=303)
    decision = {"plan_id": plan["plan_id"], "decisions": decisions, "notes": notes, "actor": f"human:{reviewer}"}
    store().save_decisions(run_id, plan["version"], decision)
    from . import intake  # late import: intake.py imports this module

    # When this process started the run (request intake), resume it here: approval -> executor -> mock
    # tools. Otherwise the decision map is only stored, for an external driver such as plan_demo.py.
    intake.resume_if_live(run_id, dict(decision))
    return RedirectResponse(f"/admin/plans/{run_id}?msg=Keputusan+disimpan+/+decisions+saved", status_code=303)
