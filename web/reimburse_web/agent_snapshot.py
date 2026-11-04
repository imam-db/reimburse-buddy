"""Read-only snapshot of the web's claim state for an agent investigation (ADR 0011).

When "Selidiki dengan agent" starts, ``snapshot_data_dir(cid)`` writes a fresh
directory with the JSON files ``DataStore`` loads, so the worker's MCP server
reads the claim as the web app sees it — not just the seed in ``data/``.

Safety rule (ADR 0011 §1): messages the reviewer holds as ``pending`` or
``rejected`` in ``REVIEW`` are **excluded** from ``inbox.json``. An unverified
upload does not exist for the agent.

The directory lives under the system temp dir with a fixed name prefix, so the
startup hook (``sweep_old_snapshots``) can remove leftovers from a crash without
touching anything else.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

# One prefix for every snapshot directory: it is how remove/sweep tell a snapshot from anything else.
PREFIX = "rb-agent-snapshot-"

# Files copied unchanged from the seed directory (ADR 0011 §1, first table row).
SEED_FILES = ("employees.json", "policy.json", "calendar.json", "memos.json", "replies.json")


class SnapshotError(RuntimeError):
    """The snapshot could not be written, so the run is refused (ADR 0011 §3)."""


def _seed_data_dir() -> Path:
    """The directory the web's ``DataStore`` reads the seed from (``store.data_dir``)."""
    from reimburse_buddy.data_store import DataStore

    return Path(DataStore().data_dir)  # honours REIMBURSE_BUDDY_DATA, else the repo's data/


def _hidden_message_ids(review: dict) -> set[str]:
    """Message IDs no run may see: everything held as ``pending`` or ``rejected``, for any claim.

    This is the safety rule, enforced in the data rather than in the prompt.
    """
    hidden: set[str] = set()
    for per_claim in review.values():
        for message_id, entry in (per_claim.get("pending") or {}).items():
            if isinstance(entry, dict) and entry.get("state") in ("pending", "rejected"):
                hidden.add(message_id)
    return hidden


def snapshot_data_dir(cid: str) -> Path:
    """Write a fresh snapshot directory for claim ``cid`` and return its path.

    The directory holds the same JSON files ``DataStore`` loads:

    - ``employees.json``, ``policy.json``, ``calendar.json``, ``memos.json`` and
      ``replies.json`` — copied unchanged from the seed directory;
    - ``claims.json`` — ``store.claims`` as the web holds it now: the seed claims
      with their current status, plus the claims employees submitted on the web;
    - ``inbox.json`` — ``store.inbox`` **minus** every message ``REVIEW`` holds as
      ``pending`` or ``rejected`` for any claim.

    ``outbox``, ``approval_requests`` and ``status_log`` are not files ``DataStore``
    loads, so they stay out; the agent's write tools start from an empty outbox.

    No secret, DSN, uploaded file bytes or audit data goes into the snapshot. The
    directory is created with ``tempfile.mkdtemp`` (random name, owner-only where
    the OS supports it) under the system temp directory.

    Raises ``SnapshotError`` when the snapshot cannot be written: the caller must
    refuse the run rather than fall back to the seed, which would silently
    investigate a different claim state.
    """
    from . import app as web

    seed_dir = _seed_data_dir()
    hidden = _hidden_message_ids(web.REVIEW)
    snap_dir: Path | None = None
    try:
        snap_dir = Path(tempfile.mkdtemp(prefix=PREFIX))

        for name in SEED_FILES:
            source = seed_dir / name
            if source.exists():
                shutil.copy2(source, snap_dir / name)

        claims = list(web.store.claims)
        (snap_dir / "claims.json").write_text(json.dumps(claims, ensure_ascii=False), encoding="utf-8")

        inbox = [m for m in web.store.inbox if m.get("id") not in hidden]
        (snap_dir / "inbox.json").write_text(json.dumps(inbox, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - every failure means "no run", never a half snapshot
        if snap_dir is not None:
            shutil.rmtree(snap_dir, ignore_errors=True)
        raise SnapshotError(
            f"Snapshot data klaim untuk {cid} tidak dapat dibuat: {type(exc).__name__}") from exc

    return snap_dir


def _is_snapshot(path: str | Path) -> bool:
    """True only for a directory directly under the temp dir whose name has our prefix."""
    try:
        resolved = Path(path).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved.parent == temp_root and resolved.name.startswith(PREFIX)


def remove_snapshot(path: str | Path) -> None:
    """Remove a snapshot directory, but only one that carries our prefix under the temp dir.

    Any other path is ignored, so a caller cannot delete an arbitrary directory by
    mistake. A directory that is already gone is not an error.
    """
    if not _is_snapshot(path):
        return
    shutil.rmtree(Path(path), ignore_errors=True)


def sweep_old_snapshots(max_age_seconds: int = 86400) -> int:
    """Remove snapshot directories older than ``max_age_seconds`` (default: one day).

    Called by the startup hook in ``app.py``. A leftover directory from a crash holds
    synthetic data only, so this is housekeeping, not a safety measure. Returns the
    number of directories removed; only prefixed directories in the temp dir are
    considered.
    """
    temp_root = Path(tempfile.gettempdir())
    if not temp_root.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in temp_root.iterdir():
        if not entry.is_dir() or not entry.name.startswith(PREFIX):
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed
