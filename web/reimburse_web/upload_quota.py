"""How much one employee may store through uploads: open drafts, bytes per rolling day, draft lifetime.

Every limit has a default and an env override that is read at call time (a positive integer; anything
else falls back to the default). The byte quota is counted from two places and the larger number wins:
the `uploaded_files` rows of the claim-state database (owner and created_at, so it survives a restart on
the postgres backend) and an in-memory ledger of this process (the only source on the memory backend; it
resets on restart, like the login limiter in auth.py). No schema change: the tables are the ones in
agent/migrations/008_claim_state.sql.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

from fastapi import HTTPException

from . import claim_state

DEFAULT_MAX_OPEN_DRAFTS = 10
DEFAULT_MAX_UPLOAD_BYTES_PER_DAY = 100 * 1024 * 1024
DEFAULT_DRAFT_TTL_DAYS = 30
WINDOW_SECONDS = 24 * 3600


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def max_open_drafts() -> int:
    return _env_int("REIMBURSE_MAX_OPEN_DRAFTS", DEFAULT_MAX_OPEN_DRAFTS)


def max_upload_bytes_per_day() -> int:
    return _env_int("REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", DEFAULT_MAX_UPLOAD_BYTES_PER_DAY)


def draft_ttl_days() -> int:
    return _env_int("REIMBURSE_DRAFT_TTL_DAYS", DEFAULT_DRAFT_TTL_DAYS)


# --- open drafts -----------------------------------------------------------------------------
def check_open_drafts(emp: str, drafts: dict[str, dict]) -> None:
    """Refuse (429) when `emp` already has the maximum of unsubmitted drafts (`drafts` is core.DRAFTS)."""
    limit = max_open_drafts()
    if sum(1 for d in drafts.values() if d.get("emp") == emp) >= limit:
        raise HTTPException(429, f"Terlalu banyak draf terbuka (maks {limit}). Kirim atau hapus draf lama dulu.")


# --- bytes per rolling day -------------------------------------------------------------------
_LEDGER: dict[str, list[tuple[float, int]]] = defaultdict(list)
_LEDGER_LOCK = threading.Lock()


def reset_ledger() -> None:
    with _LEDGER_LOCK:
        _LEDGER.clear()


def _ledger_bytes(emp: str, now: float) -> int:
    with _LEDGER_LOCK:
        kept = [(t, n) for t, n in _LEDGER.get(emp, []) if now - t < WINDOW_SECONDS]
        if kept:
            _LEDGER[emp] = kept
        else:
            _LEDGER.pop(emp, None)  # bound memory: no empty lists
        return sum(n for _, n in kept)


def bytes_used_today(emp: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    try:
        stored = claim_state.uploaded_bytes_since(emp, WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 - an unreadable count must not block uploads; the ledger still applies
        stored = 0
    return max(_ledger_bytes(emp, now), stored)


def _human(n: int) -> str:
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def check_daily_bytes(emp: str, incoming: int, now: float | None = None) -> None:
    """Refuse (429) when `incoming` bytes would take `emp` past the rolling 24 h quota. Call it with the
    whole request's bytes BEFORE anything is written."""
    limit = max_upload_bytes_per_day()
    if bytes_used_today(emp, now) + incoming > limit:
        raise HTTPException(429, f"Batas unggah harian terlampaui (maks {_human(limit)} per 24 jam). "
                                 "Coba lagi nanti.")


def record_upload(emp: str, nbytes: int, now: float | None = None) -> None:
    """Count bytes of an accepted upload (call after the request succeeded)."""
    if nbytes <= 0:
        return
    with _LEDGER_LOCK:
        _LEDGER[emp].append((time.time() if now is None else now, int(nbytes)))
