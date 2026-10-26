"""Confirmation pages for Challenge B plans: /admin/plans and /admin/plans/<run_id>.

The office layer (agent/reimburse_agent/office) is standard-library only, so it is imported directly
(agent/ on sys.path) rather than through the agent subprocess. The web app never calls an office tool: a
submitted form only stores the reviewer's decision map; the plan graph (agent/plan_demo.py or a later
worker) resumes from it, logs approval/rejection and runs the executor.
"""

from __future__ import annotations

import os
import sys
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .agent_bridge import AGENT_DIR

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
    """The audit log shared with the agent process. Not audit.default_log(): that one sweeps pending staged
    actions to `expired` when it opens, which is right for a restarted agent but would expire every plan
    still waiting on this page. A JSONL log is re-read per request so the page sees the agent's events.
    Without REIMBURSE_AUDIT_LOG / REIMBURSE_AUDIT_DATABASE_URL: one in-memory log for this process."""
    if "log" in STATE:
        return STATE["log"]
    path, dsn = os.environ.get("REIMBURSE_AUDIT_LOG"), os.environ.get("REIMBURSE_AUDIT_DATABASE_URL")
    if dsn:
        return STATE.setdefault("log", audit.PostgresAuditLog(dsn))
    if path:
        return audit.JsonlAuditLog(path)
    return STATE.setdefault("log", audit.JsonlAuditLog())


def directory() -> Directory:
    return STATE.setdefault("directory", Directory.load())


def _pending_plans() -> list[tuple[str, dict]]:
    """Runs still waiting for the reviewer: no saved decisions for their current version and at least one
    action with no outcome in the audit log (same status reading as the confirmation page)."""
    out: list[tuple[str, dict]] = []
    for run_id in store().runs():
        plan = store().load_plan(run_id)
        if not plan:
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
def plan_page(run_id: str, msg: str = ""):
    try:
        plan = store().load_plan(run_id)
    except ValueError:
        plan = None
    if not plan:
        raise HTTPException(404, "no such plan")
    return HTMLResponse(page.render(plan, log(), directory(), R.default_rules(),
                                    action_url=f"/admin/plans/{run_id}/decide", msg=msg))


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
    store().save_decisions(run_id, plan["version"], {"plan_id": plan["plan_id"], "decisions": decisions,
                                                     "notes": notes, "actor": "human:reviewer"})
    return RedirectResponse(f"/admin/plans/{run_id}?msg=Keputusan+disimpan+/+decisions+saved", status_code=303)
