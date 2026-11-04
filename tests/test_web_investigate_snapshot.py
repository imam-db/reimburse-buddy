"""The investigation runs against a snapshot of the web's claim state (ADR 0011 §2/§3).

Fake script only: no model key, no network. Skipped when `uv` is missing.
"""
import json
import re
import shutil
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reimburse_web import agent_snapshot, spend
from reimburse_web import app as web
from reimburse_web.agent_bridge import Investigator
from reimburse_web.agent_snapshot import PREFIX, SnapshotError

WEB_CLAIM = "CLM-2026-W001"
EMP = "EMP-001"

pytestmark = [pytest.mark.agent, pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")]


@pytest.fixture
def client(monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "REIMBURSE_AGENT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()
    monkeypatch.setattr(web, "INVESTIGATOR", Investigator())


# --- fixtures: a claim the employee submitted on the web ---------------------
def submit_web_claim():
    """Put a web-submitted claim in the store, the way upload_flow leaves it."""
    web.store.claims.append({
        "id": WEB_CLAIM, "employee_id": EMP, "status": "submitted", "submitted_at": "2026-10-14",
        "purpose": "Kunjungan klien", "batch": web.WEB_BATCH,
        "trip": {"origin": "Jakarta", "destination": "Surabaya",
                 "start_date": "2026-10-12", "end_date": "2026-10-14"},
        "items": [{"id": "IT-1", "category": "taxi", "date": "2026-10-13",
                   "amount": 75000, "merchant": "Blue Bird"}]})


def forward_message(mid, subject, body="terlampir struk"):
    """Put a forwarded file in the mailbox, the way upload_flow leaves it."""
    web.store.inbox.append({
        "id": mid, "owner_id": EMP, "from": EMP + "@nusantara-niaga.example",
        "to": web.MAILBOX, "subject": subject, "body": body, "date": "2026-10-13",
        "attachments": [{"name": mid + ".jpg", "type": "receipt", "linked_claim": WEB_CLAIM,
                         "linked_item": "IT-1", "amount": 75000}]})


def mark(mid, state):
    """The reviewer's verdict on one upload (as review_evidence records it)."""
    r = web.REVIEW.setdefault(WEB_CLAIM, {"evidence": [], "decisions": {}, "pending": {}, "evidence_log": []})
    r["pending"][mid] = {"item_id": "IT-1", "reason": "unreadable_file", "state": state}


def use_script(monkeypatch, tmp_path, script):
    f = tmp_path / "script.json"
    f.write_text(json.dumps(script))
    monkeypatch.setattr(web, "INVESTIGATOR", Investigator(max_per_min=10, fake_script=str(f)))


def wait_page(client, rid, pattern, timeout=180):
    end = time.time() + timeout
    html = ""
    while time.time() < end:
        html = client.get(f"/admin/investigations/{rid}").text
        if re.search(pattern, html):
            return html
        time.sleep(0.2)
    raise AssertionError(f"{pattern!r} never appeared:\n{html}")


def wait_until(pred, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def start(client):
    r = client.post(f"/admin/claims/{WEB_CLAIM}/investigate", follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


def step_row(html, tool):
    """The trace row of one tool call."""
    found = re.search(rf"<code>{tool}</code>.*?</tr>", html, re.S)
    assert found, f"no {tool} step in:\n{html}"
    return found.group(0)


READ_SCRIPT = [
    {"tool_calls": [{"name": "get_claim", "args": {"claim_id": WEB_CLAIM}, "id": "g1"}]},
    {"tool_calls": [{"name": "search_reimburse_mailbox",
                     "args": {"employee_id": EMP, "query": "bersih"}, "id": "s1"}]},
    {"tool_calls": [{"name": "search_reimburse_mailbox",
                     "args": {"employee_id": EMP, "query": "pending"}, "id": "s2"}]},
    {"content": "selesai"},
]


# --- the acceptance scenario -------------------------------------------------
def test_web_claim_is_investigated_and_pending_upload_is_hidden(client, monkeypatch, tmp_path):
    """get_claim returns the web claim; the clean upload is found and the pending one is not."""
    submit_web_claim()
    forward_message("MSG-W001", "Struk taksi bersih")
    forward_message("MSG-W002", "Struk taksi pending", body="terlampir struk kedua")
    mark("MSG-W002", "pending")
    use_script(monkeypatch, tmp_path, READ_SCRIPT)

    html = wait_page(client, start(client), "id=final")

    # get_claim returned the claim the employee submitted on the web, not "Unknown claim"
    assert WEB_CLAIM in step_row(html, "get_claim")
    assert "Unknown claim" not in html
    assert "EMP-001" in step_row(html, "get_claim")
    # the clean upload is in the snapshot's mailbox
    assert "MSG-W001" in step_row(html, "search_reimburse_mailbox")
    # the pending upload does not exist for the agent
    assert "MSG-W002" not in html


def test_accepted_upload_is_found_by_a_new_run(client, monkeypatch, tmp_path):
    submit_web_claim()
    forward_message("MSG-W002", "Struk taksi pending", body="terlampir struk kedua")
    mark("MSG-W002", "pending")
    use_script(monkeypatch, tmp_path, READ_SCRIPT)

    first = wait_page(client, start(client), "id=final")
    assert "MSG-W002" not in first

    mark("MSG-W002", "accepted")  # finance accepts the upload
    second = wait_page(client, start(client), "id=final")
    assert "MSG-W002" in second


def test_rejected_upload_is_not_found_by_a_new_run(client, monkeypatch, tmp_path):
    submit_web_claim()
    forward_message("MSG-W002", "Struk taksi pending", body="terlampir struk kedua")
    mark("MSG-W002", "pending")
    use_script(monkeypatch, tmp_path, READ_SCRIPT)

    wait_page(client, start(client), "id=final")

    mark("MSG-W002", "rejected")  # finance rejects the upload
    html = wait_page(client, start(client), "id=final")
    assert "MSG-W002" not in html


def test_changed_status_reaches_the_agent(client, monkeypatch, tmp_path):
    """A status finance set on the web is what the agent's get_claim returns."""
    submit_web_claim()
    web.store.get_claim(WEB_CLAIM)["status"] = "under_finance_review"
    use_script(monkeypatch, tmp_path, [
        {"tool_calls": [{"name": "get_claim", "args": {"claim_id": WEB_CLAIM}, "id": "g1"}]},
        {"content": "selesai"}])
    html = wait_page(client, start(client), "id=final")
    assert "under_finance_review" in step_row(html, "get_claim")


# --- lifecycle ---------------------------------------------------------------
def test_snapshot_dir_is_removed_after_the_run(client, monkeypatch, tmp_path):
    submit_web_claim()
    created = []
    real = agent_snapshot.snapshot_data_dir

    def recording(cid):
        p = real(cid)
        created.append(p)
        return p

    monkeypatch.setattr(agent_snapshot, "snapshot_data_dir", recording)
    use_script(monkeypatch, tmp_path, READ_SCRIPT)

    wait_page(client, start(client), "id=final")
    assert created and created[0].name.startswith(PREFIX)
    assert wait_until(lambda: not created[0].exists()), "snapshot directory still exists after the run"


def test_snapshot_dir_is_removed_after_cancel(client, monkeypatch, tmp_path):
    submit_web_claim()
    created = []
    real = agent_snapshot.snapshot_data_dir

    def recording(cid):
        p = real(cid)
        created.append(p)
        return p

    monkeypatch.setattr(agent_snapshot, "snapshot_data_dir", recording)
    # A write call interrupts, so the run stays open until we cancel it.
    use_script(monkeypatch, tmp_path, [
        {"tool_calls": [{"name": "update_claim_status",
                         "args": {"claim_id": WEB_CLAIM, "status": "on_hold"}, "id": "w1"}]},
        {"content": "x"}])
    rid = start(client)
    wait_page(client, rid, "id=pending")
    assert created[0].exists()

    client.post(f"/admin/investigations/{rid}/cancel")
    assert wait_until(lambda: not created[0].exists()), "snapshot directory still exists after cancel"


def test_stop_all_removes_snapshots(client, monkeypatch, tmp_path):
    """Reset demo (stop_all) removes a live run's snapshot too."""
    submit_web_claim()
    created = []
    real = agent_snapshot.snapshot_data_dir

    def recording(cid):
        p = real(cid)
        created.append(p)
        return p

    monkeypatch.setattr(agent_snapshot, "snapshot_data_dir", recording)
    use_script(monkeypatch, tmp_path, [
        {"tool_calls": [{"name": "update_claim_status",
                         "args": {"claim_id": WEB_CLAIM, "status": "on_hold"}, "id": "w1"}]},
        {"content": "x"}])
    start(client)
    assert wait_until(lambda: bool(created))
    web.INVESTIGATOR.stop_all()
    assert not created[0].exists()


# --- refusal when the snapshot cannot be written -----------------------------
def test_snapshot_failure_starts_nothing_and_reserves_nothing(client, monkeypatch, tmp_path):
    submit_web_claim()
    ledger_path = tmp_path / "spend.jsonl"
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "10")
    monkeypatch.setenv("LLM_PRICE_INPUT_MISS_PER_MTOK", "0.28")
    monkeypatch.setenv("LLM_PRICE_INPUT_HIT_PER_MTOK", "0.028")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "0.42")
    monkeypatch.setenv("REIMBURSE_LLM_SPEND_LEDGER", str(ledger_path))
    spend.reset()
    try:
        def boom(cid):
            raise SnapshotError("simulated: snapshot cannot be written")

        monkeypatch.setattr(agent_snapshot, "snapshot_data_dir", boom)
        use_script(monkeypatch, tmp_path, READ_SCRIPT)

        r = client.post(f"/admin/claims/{WEB_CLAIM}/investigate", follow_redirects=False)
        assert r.status_code == 200
        assert "id=snapshot-failed" in r.text
        assert "tidak dapat disiapkan" in r.text
        # no worker process was started
        assert web.INVESTIGATOR.runs == {}
        assert list(web.INVESTIGATOR.started) == []
        # no budget was reserved
        assert not ledger_path.exists()
        assert spend.committed() == Decimal(0)
    finally:
        spend.reset()


def test_snapshot_failure_does_not_fall_back_to_seed(client, monkeypatch, tmp_path):
    """A refused run must not investigate the seed claim instead."""
    submit_web_claim()
    monkeypatch.setattr(agent_snapshot, "snapshot_data_dir",
                        lambda cid: (_ for _ in ()).throw(SnapshotError("nope")))
    use_script(monkeypatch, tmp_path, READ_SCRIPT)
    r = client.post(f"/admin/claims/{WEB_CLAIM}/investigate", follow_redirects=False)
    assert "id=snapshot-failed" in r.text
    assert web.INVESTIGATOR.runs == {}


# --- the seed demo is unchanged ---------------------------------------------
def test_seed_claim_still_investigates_the_same(client, monkeypatch, tmp_path):
    """A seed claim with no web activity gives the same trace as before (hero claim)."""
    use_script(monkeypatch, tmp_path, [
        {"tool_calls": [{"name": "check_claim", "args": {"claim_id": "CLM-2026-0110"}, "id": "r1"}]},
        {"content": "Approvable Rp4.840.000; taxi receipt still missing."}])
    r = client.post("/admin/claims/CLM-2026-0110/investigate", follow_redirects=False)
    assert r.status_code == 303
    rid = r.headers["location"].rsplit("/", 1)[1]
    html = wait_page(client, rid, "id=final")
    assert "4840000" in html and "id=pending" not in html
