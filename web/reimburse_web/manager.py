"""Manager area: approval requests addressed to the signed-in manager (docs/adr/0001-three-roles.md).

Read-only for now; answering a request is a later step, so there is no form and no POST route here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import auth
from .labels import label, person

router = APIRouter()


def _web():
    from . import app as web  # late import: app.py includes this router
    return web


@router.get("/manager", response_class=HTMLResponse)
def manager_home(request: Request):
    web = _web()
    e = web.e
    me = auth.current_employee(request)
    if not me:
        return web.page(label("ui_nav_approvals"), auth.sign_in_notice())
    mine = [r for r in web.store.approval_requests if r["manager_id"] == me]
    rows = ""
    for r in mine:
        claim = web.store.get_claim(r["claim_id"])
        rows += (f"<tr><td>{e(r['claim_id'])}</td>"
                 f"<td>{e(person(web.store, claim['employee_id']))}</td>"
                 f"<td>{e(r['item_id'])}</td><td>{e(r.get('reason', ''))}</td>"
                 f"<td>{e(r['status'])}</td></tr>")
    body = (f"<div class=\"table-wrap\"><table><tr><th>{e(label('ui_col_claim'))}</th>"
            f"<th>{e(label('ui_col_employee'))}</th><th>{e(label('ui_col_item'))}</th>"
            f"<th>{e(label('ui_col_reason'))}</th><th>{e(label('ui_col_status'))}</th></tr>{rows}</table></div>" if rows
            else f"<p>{e(label('ui_no_approval_requests'))}</p>")
    return web.page(label("ui_nav_approvals"), body)
