"""Investigate button: web -> agent subprocess (own uv env) with a scripted fake model. No key, no network.

Skipped when `uv` is missing; the agent env is created on first use from agent/uv.lock.
"""
import json
import re
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web.agent_bridge import Investigator, redact

HERO = "CLM-2026-0110"
pytestmark = [pytest.mark.agent, pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")]


@pytest.fixture
def client(monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "REIMBURSE_AGENT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()
    monkeypatch.setattr(web, "INVESTIGATOR", Investigator())


def use_script(monkeypatch, tmp_path, script):
    f = tmp_path / "script.json"
    f.write_text(json.dumps(script))
    monkeypatch.setattr(web, "INVESTIGATOR", Investigator(max_per_min=10, fake_script=str(f)))


def wait_page(client, rid, pattern, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        html = client.get(f"/admin/investigations/{rid}").text
        if re.search(pattern, html):
            return html
        time.sleep(0.2)
    raise AssertionError(f"{pattern!r} never appeared:\n{html}")


def pending_id(rid):
    return web.INVESTIGATOR.runs[rid].pending["approval_id"]


def start(client):
    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


WRITE = {"tool_calls": [{"name": "update_claim_status", "args": {"claim_id": HERO, "status": "on_hold"}, "id": "w1"}]}


def test_claim_page_has_investigate_button(client):
    assert f'action="/admin/claims/{HERO}/investigate"' in client.get(f"/admin/claims/{HERO}").text


def test_no_key_message_and_nothing_runs(client):
    r = client.post(f"/admin/claims/{HERO}/investigate")
    assert "id=no-key" in r.text and "No model key configured" in r.text
    assert web.INVESTIGATOR.runs == {}


def test_trace_rendering_and_final_note(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [
        {"tool_calls": [{"name": "check_claim", "args": {"claim_id": HERO}, "id": "r1"}]},
        {"content": "Approvable Rp4.840.000; taxi receipt still missing."}])
    rid = start(client)
    html = wait_page(client, rid, "id=final")
    assert "<code>check_claim</code>" in html and "CLM-2026-0110" in html
    assert "4840000" in html  # result summary from the real MCP tool
    assert "Approvable Rp4.840.000" in html and "id=pending" not in html
    assert "fake-scripted" in html


def test_interrupt_approve_runs_write(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [WRITE,
        {"tool_calls": [{"name": "get_outbox", "args": {}, "id": "o1"}]}, {"content": "status set"}])
    rid = start(client)
    html = wait_page(client, rid, "id=pending")
    assert "update_claim_status" in html and "awaiting approval" in html
    assert "<code>update_claim_status</code></td>" not in html  # not executed yet
    client.post(f"/admin/investigations/{rid}/resume", data={"approved": "true", "approval_id": pending_id(rid)})
    html = wait_page(client, rid, "id=final")
    assert "Reviewer approved" in html
    step = re.search(r"<code>get_outbox</code>.*?</tr>", html, re.S).group(0)
    assert "on_hold" in step  # the write reached the server only after Approve


def test_interrupt_decline_never_writes(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [WRITE,
        {"tool_calls": [{"name": "get_outbox", "args": {}, "id": "o1"}]}, {"content": "left unchanged"}])
    rid = start(client)
    wait_page(client, rid, "id=pending")
    client.post(f"/admin/investigations/{rid}/resume", data={"approved": "false", "reason": "not yet", "approval_id": pending_id(rid)})
    html = wait_page(client, rid, "id=final")
    assert "Reviewer declined" in html and "reviewer_declined" in html and "class='tag bad'>declined" in html
    step = re.search(r"<code>get_outbox</code>.*?</tr>", html, re.S).group(0)
    assert "on_hold" not in step


def test_rate_guard(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [{"content": "nothing to do"}])
    web.INVESTIGATOR.max_per_min = 1
    rid = start(client)
    wait_page(client, rid, "id=final")
    r = client.post(f"/admin/claims/{HERO}/investigate")
    assert "id=rate-limited" in r.text


def test_pending_form_carries_approval_id_rule_engine_and_honest_text(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [WRITE, {"content": "x"}])
    rid = start(client)
    html = wait_page(client, rid, "id=pending")
    assert f'name=approval_id value="{pending_id(rid)}"' in html
    assert "id=rule-engine" in html and "Rp4.840.000" in html
    assert "id=approved-engine" in html and "Dasar persetujuan" in html  # the basis the approval binds to
    assert "Nothing is written until you click Approve" not in html
    client.post(f"/admin/investigations/{rid}/cancel")


def test_mismatched_and_duplicate_answers_ignored(client, monkeypatch, tmp_path):
    second = {"tool_calls": [{"name": "update_claim_status", "args": {"claim_id": HERO, "status": "approved"}, "id": "w2"}]}
    use_script(monkeypatch, tmp_path, [WRITE, second,
        {"tool_calls": [{"name": "get_outbox", "args": {}, "id": "o1"}]}, {"content": "done"}])
    rid = start(client)
    run = web.INVESTIGATOR.runs[rid]
    wait_page(client, rid, "id=pending")
    first = pending_id(rid)
    assert not run.resume("w1", True)  # model id, not the approval id: not sent
    assert not run.resume("", True)
    assert run.resume(first, False, "no")
    assert not run.resume(first, True)  # double click / second tab: ignored
    run.wait(60, until=lambda r: r.pending and r.pending["approval_id"] != first)
    assert run.pending["tool_call_id"] == "w2"  # second write is pending, not approved by the duplicate
    client.post(f"/admin/investigations/{rid}/resume", data={"approved": "false", "approval_id": pending_id(rid)})
    html = wait_page(client, rid, "id=final")
    assert html.count("Reviewer declined") == 2 and "Reviewer approved" not in html
    step = re.search(r"<code>get_outbox</code>.*?</tr>", html, re.S).group(0)
    assert "on_hold" not in step and '"to":"approved"' not in step


def test_pending_survives_answer_rejected_and_decision_needs_ack():
    from reimburse_web.agent_bridge import Run
    intr = {"type": "interrupt", "approval_id": "a1", "tool": "t"}
    assert Run._pending([intr, {"type": "answer_rejected"}, {"type": "step"}]) == intr
    for closer in ("answer_applied", "auto_declined", "final", "error"):
        assert Run._pending([intr, {"type": closer}]) is None


def test_cancel_kills_run(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [WRITE, {"content": "x"}])
    rid = start(client)
    wait_page(client, rid, "id=pending")
    assert "id=cancel" in client.get(f"/admin/investigations/{rid}").text
    client.post(f"/admin/investigations/{rid}/cancel")
    run = web.INVESTIGATOR.runs[rid]
    assert run.proc.poll() is not None and run.done
    assert "Cancelled by the reviewer" in client.get(f"/admin/investigations/{rid}").text
    # a new run can start right away
    assert client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False).status_code == 303


def test_redact(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-abcdef1234567890")
    out = redact("auth failed for sk-abcdef1234567890; Authorization: Bearer xyz123")
    assert "sk-abcdef1234567890" not in out and "xyz123" not in out


def test_question_form_resumes_the_run_with_decisions(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [
        {"tool_calls": [{"name": "check_claim", "args": {"claim_id": HERO}, "id": "r1"}]},
        {"content": "Should the hotel IT-3 be capped?"},
        {"content": "Noted, done."}])
    rid = start(client)
    html = wait_page(client, rid, "id=question")
    assert "awaiting your answer" in html and "name='dec_IT-3'" in html
    qid = web.INVESTIGATOR.runs[rid].question["question_id"]
    client.post(f"/admin/investigations/{rid}/reply", data={"question_id": "stale", "dec_IT-3": "reject"})
    assert web.INVESTIGATOR.runs[rid].question is not None  # stale id ignored
    client.post(f"/admin/investigations/{rid}/reply", data={"question_id": qid, "dec_IT-3": "approve_capped",
                                                            "reply": "cap it"})
    html = wait_page(client, rid, "id=final")
    assert "Reviewer replied (decisions: IT-3: approve_capped)" in html and "Noted, done." in html
