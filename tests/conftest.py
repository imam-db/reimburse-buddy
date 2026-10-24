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
