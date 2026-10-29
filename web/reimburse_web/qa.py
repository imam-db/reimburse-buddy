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
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import plans  # puts agent/ on sys.path and owns the shared audit log
from . import auth
from .labels import label

from reimburse_agent.qa import engine as qa_engine  # noqa: E402
from reimburse_agent.qa.tools import Session  # noqa: E402

router = APIRouter()
DEV_EMPLOYEE_ENV = auth.DEV_EMPLOYEE_ENV
STATE: dict = {}  # service, history per session id
QUICK_PORTAL = ("Status klaim saya", "Apa yang kurang?", "Berapa plafon hotel?", "Uang harian")
QUICK_CLAIM = ("Apa yang kurang di klaim ini?", "Status klaim ini", "Berapa plafon hotel?")


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
    """The acting employee (auth.current_employee: the login session, or REIMBURSE_DEV_EMPLOYEE with the gate off).

    Q&A stays employee-only: a reviewer or finance session has no Q&A identity."""
    emp = auth.current_employee(request)
    if not emp:
        return None
    if not auth.enabled():
        return Session(emp, "dev")
    sess = auth.session_of(request)
    return Session(emp, sess.nonce) if sess.role == "employee" else None


def _sign_in_notice() -> str:
    return auth.sign_in_notice()


JS = """<script>(function(){var l=document.getElementById('qa-log');if(l)l.scrollTop=l.scrollHeight;
var b=document.getElementById('qa');if(!b)return;
b.querySelectorAll('form').forEach(function(f){f.addEventListener('submit',function(e){
var n=b.querySelectorAll('button');var s=e.submitter;
if(s&&s.name){var h=document.createElement('input');h.type='hidden';h.name=s.name;h.value=s.value;f.appendChild(h);}
setTimeout(function(){n.forEach(function(x){x.disabled=true;});
if(s)s.textContent='Menjawab...';},0);});});})();</script>"""

EMPTY_HINT = (f"<p class='hint'>{escape(label('ui_qa_empty'))}</p>"
              + (f"<details><summary>{escape(label('ui_english'))}</summary>"
                 "<p class='hint'>No questions yet. Try: \"What's missing?\" or \"Hotel cap?\"</p></details>"))
LIMIT_EMPTY_HINT = (f"<p class='hint'>{escape(label('ui_qa_empty_cleared'))}</p>"
                    + (f"<details><summary>{escape(label('ui_english'))}</summary>"
                       "<p class='hint'>Conversation cleared. This session's question limit is reached.</p></details>"))


def _labelled(text: str) -> str:
    return re.sub(r"\b[a-z]+(?:_[a-z]+)+\b", lambda m: label(m.group(0)), text or "")


def _source_label(src: str, lang: str) -> str | None:
    """A source an employee can read: policy article by name, own claim as a link. Item IDs stay in the text."""
    if src.startswith("POL-"):
        art = next((a for a in service().rulebook.articles if a["id"] == src), None)
        if art is None:
            return None
        title = art["title_en"] if lang == "en" else art["title_id"]
        head = "Article" if lang == "en" else "Pasal"
        return f"{head} {escape(art['number'])}, {escape(title)}"
    if src.startswith("CLM-"):
        return f"<a href='/employee/claims/{escape(src)}'>{escape(src)}</a>"
    return None  # IT-n: the answer text already names the item


def _sources(r: dict) -> str:
    labels = [x for x in (_source_label(s, r["lang"]) for s in r["sources"]) if x]
    return " · ".join(labels) or "-"


def _mailto(email: str, cid: str = "") -> str:
    subject = "Pertanyaan Q&A" + (f" klaim {cid}" if cid.startswith("CLM-") else "")
    return f"mailto:{email}?subject={quote(subject)}"


def _finance_link(cid: str = "") -> str:
    """The finance contact as a mailto link; the subject names the claim when the box is on a claim page."""
    c = service().rulebook.finance_contact or {}
    name, email = c.get("name", "Tim Finance"), c.get("email", "")
    if not email:
        return escape(name)
    return f"<a href='{escape(_mailto(email, cid))}'>{escape(name)}: {escape(email)}</a>"


def _answer_html(r: dict, cid: str = "") -> str:
    """The escaped answer, with the finance e-mail as a link. Nothing is sent by the app."""
    text = escape(_labelled(r["answer"]))
    email = (service().rulebook.finance_contact or {}).get("email", "")
    if email and escape(email) in text:
        text = text.replace(escape(email), f"<a href='{escape(_mailto(email, cid))}'>{escape(email)}</a>")
    return text


def _qa_box(sess: Session, cid: str) -> str:
    hist = STATE.setdefault("history", {}).get(sess.session_id, [])
    turns = "".join(
        f"<div class='q'>{escape(q)}</div><div class='a{' refused' if r['refused'] else ''}'>{_answer_html(r, turn_cid)}</div>"
        f"<div class='src'>{escape(label('ui_qa_sources'))} {_sources(r)}</div>" for q, r, turn_cid in hist[-6:])
    left, total = service().remaining_questions(sess), service().config.max_questions
    empty = LIMIT_EMPTY_HINT if not left else EMPTY_HINT
    log = f"<div class='qa-log' id='qa-log' role='log' aria-live='polite'>{turns or empty}</div>"
    quota = f"<span class='quota'>{escape(label('ui_qa_quota').format(left=left, total=total))}</span>"
    limit = (f"<p class='quota limit'>{escape(label('ui_qa_limit_prefix'))} {_finance_link(cid)}.</p>"
             if not left else "")
    off = "" if left else " disabled"
    hidden = f"<input type='hidden' name='claim_id' value='{escape(cid)}'>"
    # State-changing, so a POST form; only shown when there is history to clear.
    new = (f"<form method='post' action='/employee/qa/new' class='new'>{hidden}"
           f"<button type='submit' class='ghost'><span aria-hidden='true'>\u21ba</span> {escape(label('ui_qa_new'))}</button></form>"
           if turns else "")
    quick = "".join(f"<button name='quick' value='{escape(q)}'{off}>{escape(q)}</button> "
                    for q in (QUICK_CLAIM if cid else QUICK_PORTAL))
    title = label("ui_qa_title_claim") if cid else label("ui_qa_title_portal")
    return (f"<div class='qa' id='qa'><div class='qa-head'><h2>{escape(title)}</h2>"
            f"<div class='qa-meta'>{quota}{new}</div></div>"
            f"<p class='note'>{escape(label('ui_qa_scope'))}</p>{limit}{log}"
            # Two forms: pressing Enter in the text box must submit the text, not the first quick button.
            f"<form method='post' action='/employee/qa' class='quick'>{hidden}"
            f"<span class='chips-label'>{escape(label('ui_qa_examples'))}</span>{quick}</form>"
            f"<form method='post' action='/employee/qa' class='ask'>{hidden}"
            f"<input type='text' name='question' aria-label='{escape(label('ui_qa_placeholder'))}' maxlength='500' placeholder='{escape(label('ui_qa_placeholder'))}'{off}>"
            f"<button{off}>{escape(label('ui_qa_ask'))}</button></form>{JS}</div>")


def portal_box(request: Request) -> str:
    """The Q&A box for the /employee portal (policy questions need no claim). "" with the flag off or no employee session."""
    if not qa_engine.enabled():
        return ""
    sess = session_of(request)
    return _qa_box(sess, '') if sess else ""


@router.get("/employee/claims")
def my_claims_page(msg: str = ""):
    """The employee portal is /employee; this old address redirects there (keeping ?msg=)."""
    return RedirectResponse("/employee" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


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
    return "", _qa_box(sess, cid)


@router.post("/employee/qa/new")
def new_conversation(request: Request, claim_id: str = Form("")):
    """Clear the turns shown for this session. The question counter and the audit log stay as they are."""
    if not qa_engine.enabled():
        raise HTTPException(404, "Not found")
    sess = session_of(request)
    if not sess:
        raise HTTPException(401, "Silakan masuk dulu / please sign in first")
    STATE.get("history", {}).pop(sess.session_id, None)
    target = f"/employee/claims/{claim_id}" if claim_id.startswith("CLM-") else "/employee"
    return RedirectResponse(target + "#qa", status_code=303)


@router.post("/employee/qa")
def ask(request: Request, question: str = Form(""), quick: str = Form(""), claim_id: str = Form("")):
    question = (quick or question).strip()  # a quick button wins over the (usually empty) text box
    if not qa_engine.enabled():
        raise HTTPException(404, "Not found")
    sess = session_of(request)
    if not sess:
        raise HTTPException(401, "Silakan masuk dulu / please sign in first")
    target = f"/employee/claims/{claim_id}" if claim_id.startswith("CLM-") else "/employee"
    if not question:
        return RedirectResponse(target + "#qa", status_code=303)
    r = service().ask(sess, question, context_claim_id=claim_id or None)
    STATE.setdefault("history", {}).setdefault(sess.session_id, []).append((question[:500], r, claim_id))
    return RedirectResponse(target + "#qa", status_code=303)
