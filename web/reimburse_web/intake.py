"""Request intake for Challenge B office plans: the operator's request -> plan -> confirmation page.

This is the web entry point that needs no claim. A submitted request starts a run of the deterministic
plan graph in this process (in-memory checkpointer, mock office tools only; no LLM, no paid API), freezes
the run start time once, stores the original request for "Jalankan ulang", and redirects to the existing
confirmation page. When the graph asks a "who" question, /admin/plans/<run_id> renders the candidates
from the directory; the answer endpoint resumes the same interrupted graph.
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from html import escape

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from .agent_bridge import AGENT_DIR

if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from langgraph.types import Command  # noqa: E402

from reimburse_agent.office import plan as P  # noqa: E402
from reimburse_agent.office import rules as R  # noqa: E402
from reimburse_agent.office.directory import Directory  # noqa: E402
from reimburse_agent.office.tools import MockOffice  # noqa: E402
from reimburse_agent.plan_graph import build_plan_graph, pending, run_input  # noqa: E402

from . import plans  # noqa: E402

router = APIRouter()
_LOCK = threading.RLock()
_LIVE: dict[str, dict] = {}
_GRAPH = None
_GRAPH_KEY = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _RunOffices:
    """One MockOffice per run, stored under <plan store>/<run_id>/mock_office.json (as plan_demo.py).

    The graph itself is compiled once per process; this dispatcher routes call() by the action_id, whose
    prefix is the run_id (``<run_id>:v<version>:a<n>``)."""

    def __init__(self, directory: Directory, root):
        self.directory = directory
        self.root = root
        self._offices: dict[str, MockOffice] = {}

    def _office(self, run_id: str) -> MockOffice:
        with _LOCK:
            office = self._offices.get(run_id)
            if office is None:
                office = MockOffice(self.directory, self.root / run_id / "mock_office.json")
                self._offices[run_id] = office
            return office

    def call(self, tool: str, action_id: str, args: dict) -> dict:
        return self._office(action_id.split(":", 1)[0]).call(tool, action_id, args)


def _graph_key():
    store = plans.store()
    return (str(store.root), os.environ.get("REIMBURSE_AUDIT_LOG"),
            os.environ.get("REIMBURSE_AUDIT_DATABASE_URL"), id(plans.STATE.get("log")))


def graph():
    """The single plan graph for this process (in-memory checkpointer, one thread per run)."""
    global _GRAPH, _GRAPH_KEY
    key = _graph_key()
    with _LOCK:
        if _GRAPH is None or _GRAPH_KEY != key:
            store = plans.store()
            directory = plans.directory()
            _GRAPH = build_plan_graph(log=plans.log(), directory=directory,
                                      office=_RunOffices(directory, store.root), store=store, clock=_utcnow)
            _GRAPH_KEY = key
        return _GRAPH


def reset() -> None:
    """Forget live graphs and the compiled graph (tests use fresh store/log paths per case)."""
    global _GRAPH, _GRAPH_KEY
    with _LOCK:
        _LIVE.clear()
        _GRAPH = None
        _GRAPH_KEY = None


def pending_for(run_id: str) -> dict | None:
    with _LOCK:
        live = _LIVE.get(run_id)
    if not live:
        return None
    return pending(live["graph"], live["config"])


def _track(run_id: str, g, config) -> None:
    with _LOCK:
        if pending(g, config) is None:
            _LIVE.pop(run_id, None)
        else:
            _LIVE[run_id] = {"graph": g, "config": config}


def start_run(*, request: str, requester: str, documents: list[dict] | None = None,
              now: datetime | None = None) -> str:
    """Start one run with a frozen UTC start time; returns the new run id (``req-<short uuid>``)."""
    run_id = f"req-{uuid.uuid4().hex[:8]}"
    frozen = now or _utcnow()
    now_iso = R.iso(frozen)
    docs = list(documents or [])
    plans.store().save_request(run_id, {"request": request, "requester": requester,
                                        "documents": docs, "created_at": now_iso})
    g = graph()
    config = {"configurable": {"thread_id": run_id}}
    g.invoke(run_input(run_id, request, requester, docs, now_iso), config)
    _track(run_id, g, config)
    return run_id


def resume_if_live(run_id: str, decision: dict) -> bool:
    """Resume a graph this process still holds (approval -> executor -> mock tools). False otherwise."""
    with _LOCK:
        live = _LIVE.get(run_id)
        if not live:
            return False
        live["graph"].invoke(Command(resume=decision), live["config"])
        _track(run_id, live["graph"], live["config"])
    return True


def _answer(run_id: str, payload: dict, answer: str) -> bool:
    question = payload["question"]
    emp = P.valid_answer(question, answer, plans.directory())
    if not emp:
        return False
    with _LOCK:
        live = _LIVE.get(run_id)
        if not live:
            return False
        live["graph"].invoke(Command(resume={"answer": emp}), live["config"])
        _track(run_id, live["graph"], live["config"])
    return True


def e(x) -> str:
    return escape(str(x if x is not None else ""))


def _page(title: str, body: str) -> str:
    from . import app as web  # late import: app.py includes this router

    return web.page(title, body).body.decode("utf-8")


def render_question(run_id: str, payload: dict, msg: str = "", error: str = "") -> str:
    """The open who-question page. No plan exists yet, so there is no approve control here."""
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
        control = ('<label>ID karyawan / Employee ID '
                   '<input name="answer" aria-label="ID karyawan / Employee ID" required></label>')
    body = f"""<p><a href="/admin/plans">&larr; Rencana / Plans</a></p>
{f'<p class="tag ok">{e(msg)}</p>' if msg else ''}
{f'<p class="tag bad" id="answer-refused">{e(error)}</p>' if error else ''}
<div class="box"><h2>Pertanyaan dari agen / Question from the agent</h2>
<p><b>{e(q.get('id_text', ''))}</b></p>
<p class="note"><i>{e(q.get('en', ''))}</i></p>
<p class="note">Belum ada tindakan yang disusun atau disetujui sampai pertanyaan ini dijawab.
/ No action is staged or approvable until this question is answered.</p>
<form method="post" action="/admin/plans/{e(run_id)}/answer">
{control}
<p><button type="submit">Kirim jawaban / Send answer</button></p></form></div>"""
    return _page("Pertanyaan rencana / Plan question", body)


@router.get("/admin/intake", response_class=HTMLResponse)
def intake_page():
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
    body = f"""<p class="note">Tulis permintaan seperti yang Anda ucapkan. Isi dokumen (opsional) diperlakukan
sebagai data, bukan perintah. / Write the request in your own words; optional document content is treated
as data, never as instructions.</p>
<div class="box"><form method="post" action="/admin/intake">
<label>Permintaan / Request<br><textarea name="request" rows="4" cols="80" required
placeholder="Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up Jumat siang."></textarea></label>
<label>Isi dokumen / file content (opsional)<br><textarea name="document" rows="5" cols="80"></textarea></label>
<label>Peminta / Requester <select name="requester">{''.join(choices)}</select></label>
<p><button type="submit">Buat rencana / Create plan</button></p></form></div>
<p class="note">Tidak ada LLM atau API berbayar yang dipanggil. Alat kantor hanya tiruan.</p>"""
    return web.page("Permintaan baru / New request", body)


@router.post("/admin/intake")
def intake_submit(request: str = Form(...), document: str = Form(""), requester: str = Form("EMP-101")):
    from .plans import directory

    text = (request or "").strip()
    if not text:
        raise HTTPException(400, "Permintaan tidak boleh kosong / request must not be empty")
    if requester not in directory().ids():
        raise HTTPException(400, f"Peminta tidak ada di direktori / unknown requester: {requester}")
    docs = [{"id": "DOC-1", "text": document}] if (document or "").strip() else []
    run_id = start_run(request=text, requester=requester, documents=docs)
    return RedirectResponse(f"/admin/plans/{run_id}", status_code=303)


@router.post("/admin/plans/{run_id}/answer")
def plan_answer(run_id: str, answer: str = Form("")):
    payload = pending_for(run_id)
    if not payload or payload.get("kind") != "clarify":
        raise HTTPException(404, "no open question for this run")
    if not _answer(run_id, payload, answer):
        error = (f"Jawaban tidak valid: {answer or '(kosong)'}. Pilih salah satu EMP-ID dari daftar. "
                 f"/ Invalid answer: choose one of the listed employee IDs.")
        return HTMLResponse(render_question(run_id, payload, error=error), status_code=400)
    return RedirectResponse(f"/admin/plans/{run_id}", status_code=303)


@router.post("/admin/intake/rerun/{run_id}")
def intake_rerun(run_id: str):
    try:
        saved = plans.store().load_request(run_id)
    except ValueError:
        saved = None
    if not saved:
        return RedirectResponse("/admin/plans", status_code=303)
    new_id = start_run(request=saved["request"], requester=saved["requester"],
                       documents=saved.get("documents") or [])
    return RedirectResponse(f"/admin/plans/{new_id}", status_code=303)
