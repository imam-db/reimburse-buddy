"""Read-only snapshot of the web's claim state for an agent investigation (ADR 0011).

When "Selidiki dengan agent" starts, ``snapshot_data_dir(cid)`` writes a fresh
directory with the JSON files ``DataStore`` loads, so the worker's MCP server
reads the claim as the web app sees it — not just the seed in ``data/``.

Safety rule (ADR 0011 §1): messages the reviewer holds as ``pending`` or
``rejected`` in ``REVIEW`` are **excluded** from ``inbox.json``.  An unverified
upload does not exist for the agent.

The directory lives under the system temp dir with a fixed name prefix, so the
startup hook (``sweep_old_snapshots``) can remove leftovers from a crash without
touching anything else.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

PREFIX = "rb-agent-snapshot-"


def _seed_data_dir() -> Path:
    """The data directory the web's ``DataStore`` reads from."""
    from reimburse_buddy.data_store import DEFAULT_DATA_DIR, DataStore
    # Honour REIMBURSE_BUDDY_DATA (the eval suite / tests may set it), then the
    # constructor default, then the module-level constant.
    env_dir = os.environ.get("REIMBURSE_BUDDY_DATA")
    if env_dir:
        return Path(env_dir)
    store = DataStore()
    return Path(store.data_dir)


def _seed_files() -> list[str]:
    """Files copied unchanged from the seed directory."""
    return ["employees.json", "policy.json", "calendar.json", "memos.json", "replies.json"]


def _pending_or_rejected_message_ids(review: dict) -> set[str]:
    """Message IDs the reviewer has not accepted (state ``pending`` or ``rejected``).

    The safety rule: these messages must not appear in the snapshot's inbox.
    """
    excluded: set[str] = set()
    for _cid, r in review.items():
        for mid, p in r.get("pending", {}).items():
            if isinstance(p, dict) and p.get("state") in ("pending", "rejected"):
                excluded.add(mid)
    return excluded


def snapshot_data_dir(cid: str) -> Path:
    """Write a fresh snapshot directory for claim ``cid`` and return its path.

    The directory contains the same JSON files ``DataStore`` loads:

    - ``employees.json``, ``policy.json``, ``calendar.json``, ``memos.json``,
      ``replies.json`` — copied unchanged from the seed directory.
    - ``claims.json`` — ``store.claims`` as the web holds it now: seed claims
      with their current status, plus web submissions.
    - ``inbox.json`` — ``store.inbox`` **minus** every message that ``REVIEW``
      holds as ``pending`` or ``rejected`` for any claim.

    The directory is created under the system temp dir with a random name and
    the ``rb-agent-snapshot-`` prefix.  No secret, DSN, uploaded file bytes or
    audit data goes into the snapshot — only the JSON shapes above.

    Raises ``RuntimeError`` if the snapshot cannot be written (the run is then
    refused; the worker is not started on the seed data as a fallback).
    """
    from . import app as web

    seed_dir = _seed_data_dir()
    excluded = _pending_or_rejected_message_ids(web.REVIEW)

    try:
        tmp = tempfile.mkdtemp(prefix=PREFIX)
        snap_dir = Path(tmp)

        # Copy seed files unchanged.
        for name in _seed_files():
            src = seed_dir / name
            if src.exists():
                shutil.copy2(src, snap_dir / name)

        # claims.json: the web store's current claims list (seed + web submissions,
        # with statuses finance may have changed).
        claims = list(web.store.claims)
        (snap_dir / "claims.json").write_text(
            json.dumps(claims, ensure_ascii=False), encoding="utf-8")

        # inbox.json: the web store's current inbox, minus pending/rejected messages.
        inbox = [m for m in web.store.inbox if m.get("id") not in excluded]
        (snap_dir / "inbox.json").write_text(
            json.dumps(inbox, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        # Clean up a partial directory if one was created.
        try:
            if "snap_dir" in dir() and snap_dir.exists():
                shutil.rmtree(snap_dir, ignore_errors=True)
        except Exception:
            pass
        raise RuntimeError(
            f"Snapshot data klaim untuk penyelidikan tidak dapat dibuat: {type(exc).__name__}") from exc

    return snap_dir


def _is_snapshot(path: Path) -> bool:
    """True only if ``path`` is inside the temp dir and its name has our prefix."""
    try:
        path = Path(path).resolve()
    except (OSError, RuntimeError):
        return False
    return path.parent == Path(tempfile.gettempdir()).resolve() and path.name.startswith(PREFIX)


def remove_snapshot(path: str | Path) -> None:
    """Remove a snapshot directory, but only if its name has our prefix.

    This guard prevents accidental deletion of arbitrary directories.  A path
    that does not match is silently ignored (no error, no deletion).
    """
    p = Path(path)
    if not _is_snapshot(p):
        return
    shutil.rmtree(p, ignore_errors=True)


def sweep_old_snapshots(max_age_seconds: int = 86400) -> int:
    """Remove snapshot directories older than ``max_age_seconds`` (default: 1 day).

    Called by the startup hook in ``app.py``.  Returns the number of directories
    removed.  Only directories under the system temp dir whose name starts with
    the snapshot prefix are touched.
    """
    tmp = Path(tempfile.gettempdir())
    if not tmp.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in tmp.iterdir():
        if not entry.is_dir() or not entry.name.startswith(PREFIX):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed
