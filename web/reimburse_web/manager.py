"""Manager area: approval requests addressed to the signed-in manager (docs/adr/0001-three-roles.md).

Read-only for now; answering a request is a later step, so there is no form and no POST route here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from . import auth
from .labels import person

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
        return web.page("Persetujuan", auth.sign_in_notice())
    mine = [r for r in web.store.approval_requests if r["manager_id"] == me]
    rows = ""
    for r in mine:
        claim = web.store.get_claim(r["claim_id"])
        rows += (f"<tr><td>{e(r['claim_id'])}</td>"
                 f"<td>{e(person(web.store, claim['employee_id']))}</td>"
                 f"<td>{e(r['item_id'])}</td><td>{e(r.get('reason', ''))}</td>"
                 f"<td>{e(r['status'])}</td></tr>")
    body = ("<div class=\"table-wrap\"><table><tr><th>Klaim</th><th>Karyawan</th><th>Item</th><th>Alasan</th>"
            f"<th>Status</th></tr>{rows}</table></div>" if rows
            else "<p>Belum ada permintaan persetujuan.</p>")
    return web.page("Persetujuan", body)
