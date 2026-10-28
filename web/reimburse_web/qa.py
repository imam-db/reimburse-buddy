"""Employee Q&A on the employee claim page (EMPLOYEE_QA=1; off by default).

Identity comes from the signed login session (auth.session_of, role "employee"); there is no picker and no way
to switch identity. With the login gate off (dev and tests with no user store) the employee comes from the
REIMBURSE_DEV_EMPLOYEE env var, a development-only fallback that is never read once the gate is on. The question form carries only the question and the claim the page shows; it never
carries an employee ID, and the Q&A tools take the employee from the session. With the flag off, the box is
not rendered and POST /employee/qa answers 404.
"""

from __future__ import annotations

import os
import re
from html import escape

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import plans  # puts agent/ on sys.path and owns the shared audit log
from . import auth
from .labels import label

from reimburse_agent.qa import engine as qa_engine  # noqa: E402
from reimburse_agent.qa.tools import Session  # noqa: E402

router = APIRouter()
DEV_EMPLOYEE_ENV = "REIMBURSE_DEV_EMPLOYEE"  # DEV ONLY: acts as this employee when the login gate is off
STATE: dict = {}  # service, history per session id
QUICK = (("Apa yang kurang?", "Apa yang kurang? / What's missing?"), ("Berapa plafon hotel?", "Berapa plafon hotel? / Hotel cap?"))


def _web():
    from . import app as web  # late import: app.py includes this router
    return web


class _Data:
    def claims(self):
        return _web().store.claims

    def check(self, cid):
        from reimburse_buddy.models import to_jsonable
        from reimburse_buddy.rules import check_claim
        return to_jsonable(check_claim(_web().store, cid))


def service() -> qa_engine.QAService:
    if "svc" not in STATE:
        from reimburse_buddy.messages import render
        STATE["svc"] = qa_engine.QAService(_Data(), log=plans.log(), render=render, employees=_web().store.employees)
    return STATE["svc"]


def session_of(request: Request) -> Session | None:
    """The signed-in employee, or None. Reviewer and finance sessions have no employee Q&A identity.

    DEV ONLY: with the login gate off, REIMBURSE_DEV_EMPLOYEE names the employee (one fixed session id).
    Once the gate is on this fallback is ignored, so it can never override a real login."""
    if auth.enabled():
        sess = auth.session_of(request)
        if sess is None or sess.role != "employee":
            return None
        return Session(sess.reviewer, sess.nonce)
    emp = os.environ.get(DEV_EMPLOYEE_ENV, "").strip()
    return Session(emp, "dev") if emp else None


def _sign_in_notice() -> str:
    if auth.enabled():
        return "<p>Silakan <a href='/login'>masuk</a> sebagai karyawan. / Please <a href='/login'>sign in</a> as an employee.</p>"
    return (f"<p class='demo'><b>DEV ONLY</b>: the login gate is off; set {DEV_EMPLOYEE_ENV}=EMP-xxx to act as an employee.</p>")


CSS = """.qa{border:2px solid #3a7bd5;border-radius:10px;padding:12px 16px;margin:16px 0;background:#f6f9ff}
.qa .q{font-weight:600;margin-top:10px}.qa .a{white-space:pre-wrap;background:#fff;border:1px solid #d5dae3;border-radius:8px;
padding:8px 10px;margin:4px 0}.qa .src{font-size:12px;color:#445}.qa .refused{border-color:#e08a00;background:#fff8ec}
.qa input[type=text]{width:70%;padding:6px}.demo{background:#fff3cd;border:1px dashed #b38600;padding:8px;border-radius:6px}"""


def _labelled(text: str) -> str:
    return re.sub(r"\b[a-z]+(?:_[a-z]+)+\b", lambda m: label(m.group(0)), text or "")


def _qa_box(sess: Session, cid: str) -> str:
    hist = STATE.setdefault("history", {}).get(sess.session_id, [])
    turns = "".join(
        f"<div class='q'>{escape(q)}</div><div class='a{' refused' if r['refused'] else ''}'>{escape(_labelled(r['answer']))}</div>"
        f"<div class='src'>Sumber / Sources: {escape(', '.join(r['sources']) or '-')}</div>" for q, r in hist[-6:])
    quick = "".join(f"<button name='quick' value='{escape(v)}'>{escape(text)}</button> " for v, text in QUICK)
    return (f"<div class='qa' id='qa'><h2>Tanya-jawab / Ask about this claim</h2>"
            f"<p class='note'>Hanya kebijakan dan klaim Anda sendiri. Jawaban tidak mengubah klaim; keputusan akhir ada di finance."
            f" / Policy and your own claims only; answers never change a claim.</p>{turns}"
            # Two forms: pressing Enter in the text box must submit the text, not the first quick button.
            f"<form method='post' action='/employee/qa' class='quick'><input type='hidden' name='claim_id' value='{escape(cid)}'>{quick}</form>"
            f"<form method='post' action='/employee/qa' class='ask'><input type='hidden' name='claim_id' value='{escape(cid)}'>"
            f"<p><input type='text' name='question' aria-label='Pertanyaan' maxlength='500' placeholder='Tulis pertanyaan / Ask a question'>"
            f" <button>Tanya / Ask</button></p></form></div>")


@router.get("/employee/claims", response_class=HTMLResponse)
def my_claims_page(request: Request):
    sess = session_of(request)
    web = _web()
    if not sess:
        return web.page("Klaim saya / My claims", _sign_in_notice())
    rows = "".join(f"<li><a href='/employee/claims/{escape(c['id'])}'>{escape(c['id'])}</a> {escape(c['trip']['destination'])}"
                   f" {escape(c['trip']['start_date'])}</li>" for c in web.store.claims if c["employee_id"] == sess.employee_id)
    return web.page("Klaim saya / My claims", f"<ul>{rows}</ul>")


def claim_page_parts(request: Request | None, cid: str, owner: str) -> tuple[str, str] | HTMLResponse:
    """Hook for the existing employee claim page (upload_flow). With the flag off, returns ("", "") and changes nothing.
    With it on, the page needs a signed-in employee, and someone else's claim is a 404 exactly like a missing one."""
    if not qa_engine.enabled():
        return "", ""
    web = _web()
    sess = session_of(request) if request is not None else None
    if not sess:
        return web.page("Klaim / Claim", _sign_in_notice())
    if owner != sess.employee_id:
        raise HTTPException(404, f"Unknown claim {cid}")
    return f"<style>{CSS}</style>", _qa_box(sess, cid)


@router.post("/employee/qa")
def ask(request: Request, question: str = Form(""), quick: str = Form(""), claim_id: str = Form("")):
    question = (quick or question).strip()  # a quick button wins over the (usually empty) text box
    if not qa_engine.enabled():
        raise HTTPException(404, "Not found")
    sess = session_of(request)
    if not sess:
        raise HTTPException(401, "Silakan masuk dulu / please sign in first")
    target = f"/employee/claims/{claim_id}" if claim_id.startswith("CLM-") else "/employee/claims"
    if not question:
        return RedirectResponse(target + "#qa", status_code=303)
    r = service().ask(sess, question, context_claim_id=claim_id or None)
    STATE.setdefault("history", {}).setdefault(sess.session_id, []).append((question[:500], r))
    return RedirectResponse(target + "#qa", status_code=303)
