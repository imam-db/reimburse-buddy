"""Manager area: approval requests addressed to the signed-in manager (docs/adr/0003-manager-approval-requests.md).

A manager answers a request for their own team only, once, and never one on their own claim. The answer is
written to the audit log before the request changes; if the log refuses it, nothing changes. The answer is
input for finance: it does not touch the rule engine or the claim, and finance still decides.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth
from . import ui
from .labels import label, person

router = APIRouter()
DECISIONS = ("approved", "rejected")
NOTE_MAX = 500
TOOL = "answer_approval_request"  # args_hash namespace for the audit event
_LOCK = threading.Lock()  # check, log and update one answer at a time (a double submit must not answer twice)


def _web():
    from . import app as web  # late import: app.py includes this router
    return web


def run_id(rid: str) -> str:
    """Audit run for one request; never a plan or investigation run, so the action executor never reads it."""
    return f"approval_request:{rid}"


def find_request(rid: str) -> dict | None:
    return next((r for r in _web().store.approval_requests if r["id"] == rid), None)


def requests_for(claim_id: str) -> list[dict]:
    return [r for r in _web().store.approval_requests if r["claim_id"] == claim_id]


def status_text(req: dict) -> str:
    return label(f"ui_req_{req['status']}")


def _item(claim: dict, item_id: str) -> dict:
    return next((i for i in claim["items"] if i["id"] == item_id), {})


def _back(msg: str) -> RedirectResponse:
    return RedirectResponse(f"/manager?msg={quote(msg, safe='')}", status_code=303)


@router.get("/manager", response_class=HTMLResponse)
def manager_home(request: Request, msg: str = ""):
    web = _web()
    e = web.e
    me = auth.current_employee(request)
    if not me:
        return web.page(label("ui_nav_approvals"), auth.sign_in_notice())
    mine = [r for r in web.store.approval_requests if r["manager_id"] == me]
    rows: list[list[str]] = []
    for r in mine:
        claim = web.store.get_claim(r["claim_id"])
        it = _item(claim, r["item_id"])
        if r["status"] == "pending":
            act = (f"<form method=post action='/manager/requests/{e(r['id'])}'>"
                   f"<input name=note maxlength={NOTE_MAX} aria-label='{e(label('ui_req_note'))}' "
                   f"placeholder='{e(label('ui_req_note'))}'> "
                   f"<button name=decision value=approved>{e(label('ui_req_approve'))}</button> "
                   f"<button name=decision value=rejected>{e(label('ui_req_reject'))}</button></form>")
        else:
            act = (f"<span class=note>{e(r.get('answered_at', ''))}</span>"
                   + (f"<div class=note>{e(r['note'])}</div>" if r.get("note") else ""))
        rows.append([e(r["claim_id"]), e(person(web.store, claim["employee_id"])),
                     f"{e(label(it.get('category', '')))}<div class=note>{e(it.get('description', r['item_id']))}</div>",
                     e(web.rupiah(Decimal(it["amount"]))) if it.get("amount") else "",
                     e(r.get("reason", "")), e(status_text(r)), act])
    flash = ui.alert(msg, tone="success") if msg else ""
    table = ui.table([label("ui_col_claim"), label("ui_col_employee"), label("ui_col_item"),
                      label("ui_col_claimed"), label("ui_col_reason"), label("ui_col_status"), ""],
                     rows, num=(3,), caption=label("ui_nav_approvals"),
                     empty=label("ui_no_approval_requests"))
    body = f"{flash}<p class=note>{e(label('ui_req_explain'))}</p>{table}"
    return web.page(label("ui_nav_approvals"), body)


@router.post("/manager/requests/{rid}")
def answer(request: Request, rid: str, decision: str = Form(""), note: str = Form("")):
    from . import plans  # lazy: plans.py owns the shared audit log
    from reimburse_agent import audit

    me = auth.current_employee(request)
    if not me:
        raise HTTPException(401, "Silakan masuk dulu / please sign in first")
    if decision not in DECISIONS:
        raise HTTPException(400, f"decision must be one of {', '.join(DECISIONS)}")
    note = note.strip()[:NOTE_MAX]
    with _LOCK:
        req = find_request(rid)
        if req is None or req["manager_id"] != me:  # someone else's request is a 404 exactly like a missing one
            raise HTTPException(404, f"Unknown request {rid}")
        owner = _web().store.get_claim(req["claim_id"])["employee_id"]
        if owner == me:
            auth.audit_self_approval(run_id(rid), me, f"self_approval:request:{rid}:{decision}",
                                     claim_id=req["claim_id"], request_id=rid)
            return _back(label("self_approval_blocked"))
        if req["status"] != "pending":
            return _back(label("ui_req_already_answered"))
        event = audit.make_event(
            run_id(rid), "approval" if decision == "approved" else "rejection", rid, actor=f"human:{me}",
            action_id=rid, args_hash=audit.args_hash(TOOL, {"request_id": rid, "decision": decision}),
            payload={"tool": TOOL, "claim_id": req["claim_id"], "item_id": req["item_id"],
                     "decision": decision, "note": note})
        try:
            stored = plans.log().append(event)
        except Exception:  # noqa: BLE001 - fail closed: an unlogged answer is not an answer
            raise HTTPException(503, label("ui_req_not_saved"))
        if not stored:  # the log already holds an answer for this request
            return _back(label("ui_req_already_answered"))
        req.update(status=decision, answered_by=me, note=note,
                   answered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return _back(f"{rid}: {status_text(req)}")
