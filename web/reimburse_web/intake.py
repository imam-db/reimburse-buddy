"""Request intake for Challenge B office plans: the operator's request -> plan -> confirmation page.

This is the web entry point that needs no claim. A submitted request starts a run of the deterministic
plan graph in this process (mock office tools only; no LLM, no paid API), freezes the run start time once,
stores the original request for "Jalankan ulang", and redirects to the existing confirmation page. When the
graph asks a "who" question, /admin/plans/<run_id> renders the candidates from the directory; the answer
endpoint resumes the same interrupted graph.

Where the run lives depends on REIMBURSE_PLAN_BACKEND (see plan_state.py): with `file` the checkpointer is
`MemorySaver` and a restart loses every open question; with `postgres` it is `PostgresSaver` on the shared
pool, so a request waiting on a who-question or on an approval is still there after a restart. `_LIVE` is
only a cache of graphs this process already has in memory: a run that is not in it is looked up in the
checkpointer and re-registered before it is resumed.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from html import escape

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .agent_bridge import AGENT_DIR

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from langgraph.types import Command  # noqa: E402

from reimburse_agent.office import plan as P  # noqa: E402
from reimburse_agent.office import rules as R  # noqa: E402
from reimburse_agent.office.directory import Directory  # noqa: E402
from reimburse_agent.office.page import STALE_TEXT  # noqa: E402
from reimburse_agent.office.tools import MockOffice  # noqa: E402
from reimburse_agent.plan_graph import build_plan_graph, pending, run_input  # noqa: E402

from . import plan_state, plans  # noqa: E402
from .labels import label  # noqa: E402

router = APIRouter()
_LOCK = threading.RLock()
_LIVE: dict[str, dict] = {}
_GRAPH = None
_GRAPH_KEY = None

# What a resume attempt did. NOT_LIVE is the old "the decision map is only stored" case (an external driver
# such as plan_demo.py owns the graph); CANNOT_RESUME means this app started the run, so the missing thread
# is a restart that lost it and the page has to say so instead of "decisions saved".
RESUMED, NOT_LIVE, CANNOT_RESUME = "resumed", "not_live", "cannot_resume"
ANSWERED, INVALID = "answered", "invalid"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _RunOffices:
    """One MockOffice per run: JSON under <plan store>/<run_id>/mock_office.json, or Postgres rows.

    The graph itself is compiled once per process; this dispatcher routes call() by the action_id, whose
    prefix is the run_id (``<run_id>:v<version>:a<n>``)."""

    def __init__(self, directory: Directory, root, rows=None):
        self.directory = directory
        self.root = root
        self.rows = rows
        self._offices: dict[str, MockOffice] = {}

    def _office(self, run_id: str) -> MockOffice:
        with _LOCK:
            office = self._offices.get(run_id)
            if office is None:
                if self.rows is not None:
                    office = MockOffice(self.directory, None, run_id=run_id, rows=self.rows)
                else:
                    office = MockOffice(self.directory, self.root / run_id / "mock_office.json")
                self._offices[run_id] = office
            return office

    def call(self, tool: str, action_id: str, args: dict) -> dict:
        return self._office(action_id.split(":", 1)[0]).call(tool, action_id, args)


def _graph_key():
    store = plans.store()
    root = getattr(store, "root", None)
    return ("postgres" if plan_state.is_postgres() else str(root),
            os.environ.get("REIMBURSE_AUDIT_LOG"),
            os.environ.get("REIMBURSE_AUDIT_DATABASE_URL"), id(plans.STATE.get("log")))


def graph():
    """The single plan graph for this process (one thread per run).

    The checkpointer is `PostgresSaver` on the shared pool when the backend is postgres, so the open
    interrupt outlives the process; otherwise `MemorySaver`, as before."""
    global _GRAPH, _GRAPH_KEY
    key = _graph_key()
    with _LOCK:
        if _GRAPH is None or _GRAPH_KEY != key:
            store = plans.store()
            directory = plans.directory()
            root = getattr(store, "root", None)
            _GRAPH = build_plan_graph(log=plans.log(), directory=directory,
                                      office=_RunOffices(directory, root, plan_state.office_rows()),
                                      store=store, clock=_utcnow,
                                      checkpointer=plan_state.checkpointer())
            _GRAPH_KEY = key
        return _GRAPH


def reset() -> None:
    """Forget live graphs and the compiled graph (tests use fresh store/log paths per case)."""
    global _GRAPH, _GRAPH_KEY
    with _LOCK:
        _LIVE.clear()
        _GRAPH = None
        _GRAPH_KEY = None


def _config(run_id: str) -> dict:
    return {"configurable": {"thread_id": run_id}}


def _thread_exists(run_id: str) -> bool:
    """True when the checkpointer still has a checkpoint for this run (finished or waiting)."""
    return bool(graph().get_state(_config(run_id)).values)


def _live(run_id: str) -> dict | None:
    """This process's graph for the run, or the one the checkpointer still has open.

    A run that is not in `_LIVE` is not necessarily gone: after a restart the checkpointer still holds the
    interrupt, so it is re-registered here and every caller continues as if the run had never left."""
    with _LOCK:
        live = _LIVE.get(run_id)
        if live is not None:
            return live
    g, config = graph(), _config(run_id)
    if pending(g, config) is None:
        return None
    with _LOCK:
        _LIVE[run_id] = {"graph": g, "config": config}
        return _LIVE[run_id]


def pending_for(run_id: str) -> dict | None:
    live = _live(run_id)
    return pending(live["graph"], live["config"]) if live else None


def _track(run_id: str, g, config) -> None:
    with _LOCK:
        if pending(g, config) is None:
            _LIVE.pop(run_id, None)
        else:
            _LIVE[run_id] = {"graph": g, "config": config}


def _started_here(run_id: str) -> bool:
    """True when this app's intake started the run (it stored the request for "Jalankan ulang")."""
    try:
        return bool(plans.store().load_request(run_id))
    except ValueError:
        return False


def _lost(run_id: str) -> str:
    """No thread to resume: say which kind of nothing this is."""
    if _thread_exists(run_id):
        return NOT_LIVE  # finished: the decisions were executed when they were first posted
    return CANNOT_RESUME if _started_here(run_id) else NOT_LIVE  # a driver owns the graph


def start_run(*, request: str, requester: str, documents: list[dict] | None = None,
              now: datetime | None = None, submitted_by: str = "") -> str:
    """Start one run with a frozen UTC start time; returns the new run id (``req-<short uuid>``).

    `submitted_by` is the signed-in reviewer who filled the form. The reviewer may name anyone as the
    requester, so without it the same person could submit a request "for" a colleague and then approve it.
    Stored in request.json and on the plan (web side only: plan_graph.py is untouched). Empty when the
    demo password gate is off, so local runs and the old tests keep their behaviour."""
    run_id = f"req-{uuid.uuid4().hex[:8]}"
    frozen = now or _utcnow()
    now_iso = R.iso(frozen)
    docs = list(documents or [])
    saved = {"request": request, "requester": requester, "documents": docs, "created_at": now_iso}
    if submitted_by:
        saved["submitted_by"] = submitted_by
    plans.store().save_request(run_id, saved)
    g = graph()
    config = _config(run_id)
    g.invoke(run_input(run_id, request, requester, docs, now_iso), config)
    _track(run_id, g, config)
    _stamp_submitted_by(run_id)
    return run_id


def _stamp_submitted_by(run_id: str) -> None:
    """Copy request.json's submitted_by onto the plan.

    Called after every resume, because the plan does not exist yet when the run first stops to ask who
    the request is for: it is staged on the answer, so the stamp has to happen then. No plan at all is
    fine: plans.submitted_by() falls back to request.json."""
    try:
        saved = plans.store().load_request(run_id) or {}
    except ValueError:
        return
    reviewer = str(saved.get("submitted_by") or "")
    if not reviewer:
        return
    try:
        plan = plans.store().load_plan(run_id)
    except ValueError:
        return
    if plan and plan.get("submitted_by") != reviewer:
        plan["submitted_by"] = reviewer
        plans.store().save_plan(plan)


def resume_if_live(run_id: str, decision: dict) -> str:
    """Resume the run with the reviewer's decisions (approval -> executor -> mock tools).

    Returns RESUMED when the graph ran, NOT_LIVE when this process never had the thread (an external driver
    such as plan_demo.py reads the stored decisions itself) and CANNOT_RESUME when the intake started the
    run but the checkpointer has no thread for it: a restart that lost the plan, which the page must say
    instead of claiming the decisions were saved."""
    live = _live(run_id)
    if live is None:
        return _lost(run_id)
    with _LOCK:
        live["graph"].invoke(Command(resume=decision), live["config"])
        _track(run_id, live["graph"], live["config"])
    return RESUMED


def _answer(run_id: str, payload: dict, answer: str) -> str:
    question = payload["question"]
    emp = P.valid_answer(question, answer, plans.directory())
    if not emp:
        return INVALID
    live = _live(run_id)
    if live is None:
        return CANNOT_RESUME
    with _LOCK:
        live["graph"].invoke(Command(resume={"answer": emp}), live["config"])
        _track(run_id, live["graph"], live["config"])
    _stamp_submitted_by(run_id)  # the answer is what stages the plan
    return ANSWERED


def e(x) -> str:
    return escape(str(x if x is not None else ""))


def _page(title: str, body: str) -> str:
    from . import app as web  # late import: app.py includes this router

    return web.page(title, body).body.decode("utf-8")


def render_question(run_id: str, payload: dict | None, msg: str = "", error: str = "",
                    stale: bool = False) -> str:
    """The open who-question page. No plan exists yet, so there is no approve control here.

    `stale` is the restart case: the question was open when the server stopped and the checkpointer has no
    thread for it, so there is nothing to answer. It says so and offers "Jalankan ulang" instead of a form
    that cannot work."""
    if stale:
        body = f"""<p><a href="/admin/plans">&larr; Rencana</a></p>
<div class="box" id="plan-stale"><h2>{e(label('ui_question_stale'))}</h2>
<p>{e(STALE_TEXT)}</p>
<form method="post" action="/admin/intake/rerun/{e(run_id)}">
<button class="btn" type="submit" id="rerun">Jalankan ulang</button></form></div>"""
        return _page(label("ui_question_title"), body)
    q = payload["question"]
    options = q.get("options") or []
    if options:
        rows = []
        for emp in options:
            c = plans.directory().get(emp)
            if c:
                text = f"{c.name} — {c.role} — {c.office} — {c.tz} — {c.id}"
            else:
                text = str(emp)
            rows.append(f"<label style='display:block;margin:6px 0'>"
                        f"<input type='radio' name='answer' value='{e(emp)}' required> {e(text)}</label>")
        control = "".join(rows)
    else:
        control = (f'<label>{e(label("ui_employee_id"))} '
                   f'<input name="answer" aria-label="{e(label("ui_employee_id"))}" required></label>')
    body = f"""<p><a href="/admin/plans">&larr; Rencana</a></p>
{f'<p class="tag ok">{e(msg)}</p>' if msg else ''}
{f'<p class="tag bad" id="answer-refused">{e(error)}</p>' if error else ''}
<div class="box"><h2>{e(label('ui_question_from_agent'))}</h2>
<p><b>{e(q.get('id_text', ''))}</b></p>
{f'<details><summary>{e(label("ui_english"))}</summary><p class="note"><i>{e(q.get("en", ""))}</i></p></details>' if q.get('en') else ''}
<p class="note">{e(label('ui_question_no_action'))}</p>
<form method="post" action="/admin/plans/{e(run_id)}/answer">
{control}
<p><button type="submit">{e(label('ui_question_send'))}</button></p></form></div>"""
    return _page(label("ui_question_title"), body)


@router.get("/admin/intake", response_class=HTMLResponse)
def intake_page(request: Request):
    from . import app as web  # late import: app.py includes this router
    from .plans import directory

    ids = sorted(directory().ids())
    default = "EMP-101" if "EMP-101" in ids else (ids[0] if ids else "")
    choices = []
    for emp in ids:
        c = directory().get(emp)
        name = c.name if c else emp
        choices.append(f'<option value="{e(emp)}"{" selected" if emp == default else ""}>'
                       f'{e(name)} ({e(emp)})</option>')
    who = web.submitter_of(request)
    body = f"""<p class="note">{e(label('ui_intake_note'))}</p>
{web.submitted_by_note(who)}
<div class="box"><form method="post" action="/admin/intake">
<label>{e(label('ui_intake_request'))}<br><textarea name="request" rows="4" required
placeholder="Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up Jumat siang."></textarea></label>
<label>{e(label('ui_intake_document'))}<br><textarea name="document" rows="5"></textarea></label>
<label>{e(label('ui_intake_requester'))} <select name="requester">{''.join(choices)}</select></label>
<p><button type="submit">{e(label('ui_intake_create'))}</button></p></form></div>
<p class="note">{e(label('ui_intake_no_llm'))}</p>"""
    return web.page(label("ui_intake_title"), body)


@router.post("/admin/intake")
def intake_submit(request: Request, request_text: str = Form("", alias="request"),
                  document: str = Form(""), requester: str = Form("EMP-101")):
    from . import app as web  # late import: app.py includes this router
    from .plans import directory

    text = (request_text or "").strip()
    if not text:
        raise HTTPException(400, "Permintaan tidak boleh kosong")
    if requester not in directory().ids():
        raise HTTPException(400, f"Peminta tidak ada di direktori: {requester}")
    docs = [{"id": "DOC-1", "text": document}] if (document or "").strip() else []
    run_id = start_run(request=text, requester=requester, documents=docs,
                       submitted_by=web.submitter_of(request))
    return RedirectResponse(f"/admin/plans/{run_id}", status_code=303)


@router.post("/admin/plans/{run_id}/answer")
def plan_answer(run_id: str, answer: str = Form("")):
    payload = pending_for(run_id)
    if not payload or payload.get("kind") != "clarify":
        # The question was open when the server stopped and the checkpointer no longer has the thread: say
        # so and offer a new run, instead of a bare 404 the reviewer cannot act on.
        if _started_here(run_id) and not _thread_exists(run_id):
            return HTMLResponse(render_question(run_id, None, stale=True), status_code=409)
        raise HTTPException(404, "no open question for this run")
    outcome = _answer(run_id, payload, answer)
    if outcome == CANNOT_RESUME:
        return HTMLResponse(render_question(run_id, payload, stale=True), status_code=409)
    if outcome == INVALID:
        error = label("ui_question_invalid").format(answer=answer or "(kosong)")
        return HTMLResponse(render_question(run_id, payload, error=error), status_code=400)
    return RedirectResponse(f"/admin/plans/{run_id}", status_code=303)


@router.post("/admin/intake/rerun/{run_id}")
def intake_rerun(run_id: str, request: Request):
    from . import app as web  # late import: app.py includes this router

    try:
        saved = plans.store().load_request(run_id)
    except ValueError:
        saved = None
    if not saved:
        return RedirectResponse("/admin/plans", status_code=303)
    new_id = start_run(request=saved["request"], requester=saved["requester"],
                       documents=saved.get("documents") or [], submitted_by=web.submitter_of(request))
    return RedirectResponse(f"/admin/plans/{new_id}", status_code=303)
