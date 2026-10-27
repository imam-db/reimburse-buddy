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
from reimburse_web.agent_bridge import Investigator

HERO = "CLM-2026-0110"
pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")


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
    client.post(f"/admin/investigations/{rid}/resume", data={"approved": "true"})
    html = wait_page(client, rid, "id=final")
    assert "Reviewer approved" in html
    step = re.search(r"<code>get_outbox</code>.*?</tr>", html, re.S).group(0)
    assert "on_hold" in step  # the write reached the server only after Approve


def test_interrupt_decline_never_writes(client, monkeypatch, tmp_path):
    use_script(monkeypatch, tmp_path, [WRITE,
        {"tool_calls": [{"name": "get_outbox", "args": {}, "id": "o1"}]}, {"content": "left unchanged"}])
    rid = start(client)
    wait_page(client, rid, "id=pending")
    client.post(f"/admin/investigations/{rid}/resume", data={"approved": "false", "reason": "not yet"})
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
