import json
import shutil
from pathlib import Path

import pytest

from reimburse_buddy.data_store import DEFAULT_DATA_DIR, DataStore


@pytest.fixture
def make_store(tmp_path):
    """Copy the synthetic data, apply edits, return a fresh DataStore."""
    def _make(claims=(), inbox=(), calendar=(), memos=()):
        d = tmp_path / "data"
        shutil.copytree(DEFAULT_DATA_DIR, d)
        for name, extra in (("claims", claims), ("inbox", inbox), ("calendar", calendar), ("memos", memos)):
            rows = json.loads((d / f"{name}.json").read_text())
            rows.extend(extra)
            (d / f"{name}.json").write_text(json.dumps(rows))
        return DataStore(d)
    return _make


def claim(cid, items, emp="EMP-001", dest="Surabaya", start="2026-10-12", end="2026-10-14"):
    return {"id": cid, "employee_id": emp, "status": "submitted", "submitted_at": end, "purpose": "test",
            "trip": {"origin": "Jakarta", "destination": dest, "start_date": start, "end_date": end},
            "items": items}


# Agent end-to-end tests spawn the agent's own uv env (slow, needs uv + agent/uv.lock).
# Opt in with `pytest -m agent` or RB_AGENT_TESTS=1; the default run stays fast and offline.
def pytest_configure(config):
    config.addinivalue_line("markers", "agent: web <-> agent subprocess tests (opt-in: -m agent)")


def pytest_collection_modifyitems(config, items):
    import os

    if os.environ.get("RB_AGENT_TESTS") == "1" or "agent" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="opt-in: run with -m agent or RB_AGENT_TESTS=1")
    for item in items:
        if "agent" in item.keywords:
            item.add_marker(skip)
