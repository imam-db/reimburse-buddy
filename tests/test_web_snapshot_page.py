"""The investigation page line and the startup sweep for snapshots (ADR 0011 §2/§3).

Fast: no subprocess, no model key, no database.
"""
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web.agent_snapshot import PREFIX
from reimburse_web.labels import label

ADR_LINE = "Agent membaca data klaim saat penyelidikan dimulai."


class _FinishedRun:
    """Just enough of agent_bridge.Run for the investigation page to render."""

    claim_id = "CLM-2026-0110"
    started_at = time.time()
    pending = None
    question = None

    def snapshot(self):
        return [{"type": "start", "model": "fake-scripted", "compact": True},
                {"type": "final", "note": "selesai", "tokens": {"input": 1, "output": 1}}]

    def stderr_tail(self, n: int = 20):
        return ""


@pytest.fixture
def client():
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def test_the_page_says_the_agent_reads_the_state_at_start(client):
    web.INVESTIGATOR.runs["rid-page"] = _FinishedRun()
    try:
        html = client.get("/admin/investigations/rid-page").text
    finally:
        web.INVESTIGATOR.runs.clear()
    assert ADR_LINE in html


def test_the_label_matches_the_adr_sentence():
    assert label("ui_agent_note") == ADR_LINE


def test_the_snapshot_sweep_runs_last_at_startup():
    hooks = [getattr(h, "__name__", str(h)) for h in web.app.router.on_startup]
    # The new hook is appended after the existing ones; none of them moved.
    assert hooks[-1] == "startup_snapshot_sweep"
    assert hooks[-2] == "startup_sweep"
    assert hooks[0] == "startup_config_check"
    assert hooks.count("startup_snapshot_sweep") == 1


def test_the_startup_hook_removes_only_old_snapshot_dirs():
    temp = Path(tempfile.gettempdir())
    old = temp / (PREFIX + "startup-old")
    fresh = temp / (PREFIX + "startup-fresh")
    other = temp / "not-a-snapshot-startup"
    for d in (old, fresh, other):
        d.mkdir(exist_ok=True)
    stamp = time.time() - 100000
    os.utime(old, (stamp, stamp))
    os.utime(other, (stamp, stamp))
    try:
        web.startup_snapshot_sweep()
        assert not old.exists()      # older than a day: removed
        assert fresh.exists()        # recent: kept
        assert other.exists()        # different prefix: never touched
    finally:
        for d in (old, fresh, other):
            shutil.rmtree(d, ignore_errors=True)


def test_a_broken_sweep_does_not_stop_startup(monkeypatch):
    def boom(*a, **kw):
        raise OSError("temp dir unreadable")

    monkeypatch.setattr(web, "sweep_old_snapshots", boom)
    web.startup_snapshot_sweep()  # must not raise
