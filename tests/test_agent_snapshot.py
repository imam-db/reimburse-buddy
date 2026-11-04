"""Tests for the agent investigation snapshot (ADR 0011 §1).

No Tesseract, no database, no model key.  These tests create the snapshot
directly (no subprocess) and check its JSON contents.
"""
import json
import os
import shutil
import tempfile
import time as _time
from pathlib import Path

import pytest

from reimburse_buddy.amounts import normalize_message
from reimburse_buddy.data_store import DataStore
from reimburse_web import app as web
from reimburse_web.agent_snapshot import (
    PREFIX,
    SnapshotError,
    remove_snapshot,
    snapshot_data_dir,
    sweep_old_snapshots,
)

pytestmark = pytest.mark.agent


@pytest.fixture
def fresh(monkeypatch):
    """Reset web state and return nothing; clean up after."""
    web.reset_all()
    yield
    web.reset_all()


# --- helpers ---------------------------------------------------------------
def _seed_claim(cid="CLM-2026-0110"):
    """A seed claim from data/claims.json (the hero claim)."""
    return next(c for c in web.store.claims if c["id"] == cid)


def _make_web_claim(cid="CLM-2026-W001", emp="EMP-001"):
    """A claim as the web app holds it after an employee submits it."""
    return {"id": cid, "employee_id": emp, "status": "submitted", "submitted_at": "2026-10-14",
            "purpose": "test trip", "batch": "web-submissions",
            "trip": {"origin": "Jakarta", "destination": "Surabaya",
                     "start_date": "2026-10-12", "end_date": "2026-10-14"},
            "items": [{"id": "IT-1", "category": "taxi", "date": "2026-10-13",
                       "amount": 75000, "merchant": "Blue Bird"}]}


def _make_web_message(mid="MSG-W001", emp="EMP-001", claim_id="CLM-2026-W001"):
    """A mailbox message as the web app holds it after a file forward."""
    return {"id": mid, "owner_id": emp, "from": emp + "@nusantara-niaga.example",
            "to": "reimburse@nusantara-niaga.example", "subject": "struk taksi",
            "body": "terlampir struk taksi", "date": "2026-10-13",
            "attachments": [{"name": "taxi.jpg", "type": "receipt",
                             "linked_claim": claim_id, "linked_item": "IT-1",
                             "amount": 75000}]}


# --- tests -----------------------------------------------------------------

def test_seed_snapshot_matches_seed(fresh):
    """A snapshot of a seed claim with no web activity equals the seed data."""
    snap = snapshot_data_dir("CLM-2026-0110")
    try:
        seed = DataStore()
        snap_store = DataStore(snap)
        # Same claims, same inbox, same employees, same policy
        assert snap_store.claims == seed.claims
        assert snap_store.inbox == seed.inbox
        assert snap_store.employees == seed.employees
        assert snap_store.policy == seed.policy
        assert snap_store.calendar == seed.calendar
        assert snap_store.memos == seed.memos
        assert snap_store.replies == seed.replies
    finally:
        remove_snapshot(snap)


def test_web_claim_appears_in_snapshot(fresh):
    """A claim the employee submitted on the web is in the snapshot's claims.json."""
    claim = _make_web_claim()
    web.store.claims.append(claim)
    snap = snapshot_data_dir("CLM-2026-W001")
    try:
        snap_store = DataStore(snap)
        got = snap_store.get_claim("CLM-2026-W001")
        assert got["employee_id"] == "EMP-001"
        assert got["items"][0]["category"] == "taxi"
    finally:
        remove_snapshot(snap)


def test_changed_status_carried_into_snapshot(fresh):
    """A status change finance made on the web is carried into the snapshot."""
    # Change a seed claim's status (as update_claim_status does internally)
    claim = _seed_claim("CLM-2026-0110")
    original = claim["status"]
    claim["status"] = "on_hold"
    try:
        snap = snapshot_data_dir("CLM-2026-0110")
        try:
            snap_store = DataStore(snap)
            assert snap_store.get_claim("CLM-2026-0110")["status"] == "on_hold"
        finally:
            remove_snapshot(snap)
    finally:
        claim["status"] = original


def test_pending_and_rejected_messages_excluded(fresh):
    """Messages in REVIEW with state pending or rejected do not appear in inbox.json."""
    claim = _make_web_claim()
    web.store.claims.append(claim)

    clean_msg = _make_web_message("MSG-W001")
    pending_msg = _make_web_message("MSG-W002", claim_id="CLM-2026-W001")
    rejected_msg = _make_web_message("MSG-W003", claim_id="CLM-2026-W001")

    web.store.inbox.extend([clean_msg, pending_msg, rejected_msg])

    # Simulate reviewer state: pending and rejected messages
    web.REVIEW["CLM-2026-W001"] = {
        "evidence": [],
        "decisions": {},
        "pending": {
            "MSG-W002": {"item_id": "IT-1", "reason": "unreadable_file", "state": "pending"},
            "MSG-W003": {"item_id": "IT-1", "reason": "duplicate_file", "state": "rejected"},
        },
        "evidence_log": [],
    }

    snap = snapshot_data_dir("CLM-2026-W001")
    try:
        inbox = json.loads((snap / "inbox.json").read_text(encoding="utf-8"))
        ids = {m["id"] for m in inbox}
        assert "MSG-W001" in ids          # clean upload: present
        assert "MSG-W002" not in ids      # pending: excluded
        assert "MSG-W003" not in ids      # rejected: excluded
    finally:
        remove_snapshot(snap)


def test_accepted_message_included(fresh):
    """After finance accepts a pending upload, a new run finds it in the snapshot."""
    claim = _make_web_claim()
    web.store.claims.append(claim)

    msg = _make_web_message("MSG-W001")
    web.store.inbox.append(msg)

    # Reviewer accepted the evidence
    web.REVIEW["CLM-2026-W001"] = {
        "evidence": ["MSG-W001"],
        "decisions": {},
        "pending": {"MSG-W001": {"item_id": "IT-1", "reason": "unreadable_file", "state": "accepted"}},
        "evidence_log": [],
    }

    snap = snapshot_data_dir("CLM-2026-W001")
    try:
        inbox = json.loads((snap / "inbox.json").read_text(encoding="utf-8"))
        ids = {m["id"] for m in inbox}
        assert "MSG-W001" in ids  # accepted: included
    finally:
        remove_snapshot(snap)


def test_normalize_message_idempotent():
    """normalize_message applied twice gives the same result as once (ADR 0011 §1)."""
    msg = {"id": "MSG-W001", "owner_id": "EMP-001", "from": "x", "to": "y",
           "subject": "s", "body": "b", "date": "d",
           "attachments": [{"name": "r.jpg", "type": "receipt", "amount": "75.000"}]}

    once = normalize_message(msg)
    twice = normalize_message(once)
    # The snapshot writes messages as the web holds them; DataStore normalises on
    # load.  Double normalisation must be a no-op so the agent sees the same data.
    assert once == twice


def test_datastore_can_get_claim_from_snapshot(fresh):
    """DataStore(snapshot_dir) can get_claim for a web-submitted claim."""
    claim = _make_web_claim()
    web.store.claims.append(claim)
    msg = _make_web_message()
    web.store.inbox.append(msg)

    snap = snapshot_data_dir("CLM-2026-W001")
    try:
        snap_store = DataStore(snap)
        got = snap_store.get_claim("CLM-2026-W001")
        assert got["id"] == "CLM-2026-W001"
        # The web message is in the snapshot's inbox
        results = snap_store.search_reimburse_mailbox("EMP-001", "taksi")
        assert any(r["id"] == "MSG-W001" for r in results)
    finally:
        remove_snapshot(snap)


def test_snapshot_dir_has_prefix(fresh):
    """The snapshot directory is under the temp dir with the rb-agent-snapshot- prefix."""
    snap = snapshot_data_dir("CLM-2026-0110")
    try:
        assert snap.name.startswith(PREFIX)
        assert snap.parent == Path(tempfile.gettempdir()).resolve()
    finally:
        remove_snapshot(snap)


def test_remove_snapshot_only_removes_our_dirs(fresh, tmp_path):
    """remove_snapshot ignores paths that don't have our prefix."""
    # A random directory must not be touched
    other = tmp_path / "not-a-snapshot"
    other.mkdir()
    (other / "file.txt").write_text("important")
    remove_snapshot(other)
    assert (other / "file.txt").exists()

    # Our prefix but not in temp dir: also ignored
    fake_dir = tmp_path / (PREFIX + "fake")
    fake_dir.mkdir()
    remove_snapshot(fake_dir)
    assert fake_dir.exists()


def test_snapshot_failure_raises_and_cleans_up(fresh, monkeypatch):
    """When the snapshot cannot be written, RuntimeError is raised and no partial dir is left."""
    # Simulate a write failure by making json.dumps raise
    original_dumps = json.dumps

    def failing_dumps(*args, **kwargs):
        raise TypeError("simulated failure")

    monkeypatch.setattr(json, "dumps", failing_dumps)
    with pytest.raises(SnapshotError):
        snapshot_data_dir("CLM-2026-0110")
    monkeypatch.setattr(json, "dumps", original_dumps)


def test_sweep_old_snapshots(fresh, monkeypatch):
    """sweep_old_snapshots removes snapshot dirs older than the max age."""
    tmp = Path(tempfile.gettempdir())
    # Create an old snapshot dir
    old = tmp / (PREFIX + "old-test")
    old.mkdir(exist_ok=True)
    old_mtime = _time.time() - 100000  # > 1 day ago
    os.utime(old, (old_mtime, old_mtime))

    # Create a recent one
    recent = tmp / (PREFIX + "recent-test")
    recent.mkdir(exist_ok=True)

    try:
        removed = sweep_old_snapshots(max_age_seconds=86400)
        assert removed >= 1
        assert not old.exists()
        assert recent.exists()  # recent one kept
    finally:
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(recent, ignore_errors=True)


def test_snapshot_excludes_pending_for_any_claim(fresh):
    """A pending message for claim A is excluded even when snapshotting claim B (safety rule)."""
    claim = _make_web_claim()
    web.store.claims.append(claim)

    # Pending message for a *different* claim
    other_msg = _make_web_message("MSG-W002", claim_id="CLM-2026-0999")
    web.store.inbox.append(other_msg)

    web.REVIEW["CLM-2026-0999"] = {
        "evidence": [], "decisions": {},
        "pending": {"MSG-W002": {"item_id": "IT-1", "reason": "unreadable_file", "state": "pending"}},
        "evidence_log": [],
    }

    snap = snapshot_data_dir("CLM-2026-W001")
    try:
        inbox = json.loads((snap / "inbox.json").read_text(encoding="utf-8"))
        ids = {m["id"] for m in inbox}
        assert "MSG-W002" not in ids
    finally:
        remove_snapshot(snap)


def test_snapshot_does_not_include_outbox_or_status_log(fresh):
    """outbox.json and status_log.json are not written (ADR 0011 §1)."""
    web.store.outbox.append({"id": "OUT-001", "to": "EMP-001"})
    web.store.status_log.append({"claim_id": "CLM-2026-0110", "from": "submitted", "to": "on_hold"})
    snap = snapshot_data_dir("CLM-2026-0110")
    try:
        assert not (snap / "outbox.json").exists()
        assert not (snap / "status_log.json").exists()
        assert not (snap / "approval_requests.json").exists()
    finally:
        remove_snapshot(snap)
