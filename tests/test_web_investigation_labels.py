"""The investigation page must not show humanised fallback label text (e.g. "Ui status word").

A stub run stands in for the agent worker, so no uv env, model key or subprocess is needed.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web

HERO = "CLM-2026-0110"


class StubRun(SimpleNamespace):
    def snapshot(self):
        return self.events

    def stderr_tail(self, n):
        return ""

    def kill(self):
        pass


@pytest.fixture
def client(monkeypatch):
    web.reset_all()
    run = StubRun(claim_id=HERO, pending=None, question=None, started_at=0.0,
                  events=[{"type": "start", "model": "fake"}])
    monkeypatch.setattr(web.INVESTIGATOR, "runs", {"r1": run})
    yield TestClient(web.app)
    web.reset_all()


def test_investigation_page_has_no_fallback_status_label(client):
    r = client.get("/admin/investigations/r1")
    assert r.status_code == 200, r.text
    assert "Ui status word" not in r.text
    assert "Status:" in r.text
