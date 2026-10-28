"""Demo password gate for the version B web app.

There are no real accounts. One shared password (`DEMO_PASSWORD`) closes every route except `/login`,
`/logout` and `/healthz`, and the login form also picks which synthetic reviewer the session acts as
(`REVIEWERS`, default `EMP-100`). The session is a signed cookie: `issued_at|reviewer|nonce` plus an
HMAC, 12 hours, checked on every request.

Outside production an unset `DEMO_PASSWORD` turns the gate off, so a local run and the existing test
suite behave exactly as before. In production a missing or short password, or an unset session key,
aborts startup instead of serving an unprotected demo: a random per-process key logs everyone out on
restart and cannot work behind more than one instance.

The password is never logged, never written to the audit log, and never echoed back to the page.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass

COOKIE = "rb_auth"
TTL_SECONDS = 12 * 60 * 60
MIN_PASSWORD_CHARS = 12
MAX_FAILS_PER_MINUTE = 5
PRODUCTION_ENVS = frozenset({"prod", "production"})
# Synthetic reviewers the login form offers. Intake defaults the requester to EMP-101, so the demo
# reviewer is EMP-100: the same person may not request and approve (see self_approval.py). EMP-102
# (Finance) is the third reviewer, so the demo can show "refused as EMP-100, approved as EMP-102"
# without changing who the request is for.
REVIEWERS = ("EMP-100", "EMP-101", "EMP-102")
DEFAULT_REVIEWER = "EMP-100"
# Who a decision is attributed to when the gate is off (dev, tests): the value used before identities.
FALLBACK_REVIEWER = "reviewer"
PUBLIC_PATHS = frozenset({"/login", "/logout", "/healthz"})

_PROCESS_KEY = secrets.token_hex(32)


class AuthConfigError(RuntimeError):
    """Production is configured without a usable demo password or session key."""


def is_production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in PRODUCTION_ENVS


def password() -> str:
    """The shared demo password, read per call so a test (or a restart) can change it."""
    return os.environ.get("DEMO_PASSWORD", "") or ""


def enabled() -> bool:
    """False outside production when DEMO_PASSWORD is unset: every route stays open, as before."""
    return bool(password())


def session_key() -> bytes:
    return (os.environ.get("REIMBURSE_DEMO_SESSION_KEY") or _PROCESS_KEY).encode("utf-8")


def startup_check() -> None:
    """Refuse a production deployment that would serve an open or restart-fragile demo.

    Registered after plans.startup_config_check (the audit log is the first thing to be right) and
    before startup_sweep, so a misconfigured production process exits before serving a request."""
    if not is_production():
        return
    pw = password()
    if not pw:
        raise AuthConfigError(
            "APP_ENV=prod requires DEMO_PASSWORD: without it every route except /login, /logout and "
            "/healthz would be open. Set DEMO_PASSWORD (12+ characters) in the environment.")
    if len(pw) < MIN_PASSWORD_CHARS:
        raise AuthConfigError(
            f"APP_ENV=prod: DEMO_PASSWORD must be at least {MIN_PASSWORD_CHARS} characters "
            f"(got {len(pw)}).")
    if not (os.environ.get("REIMBURSE_DEMO_SESSION_KEY") or "").strip():
        raise AuthConfigError(
            "APP_ENV=prod requires REIMBURSE_DEMO_SESSION_KEY: a random per-process key logs everyone "
            "out on restart and breaks with more than one instance.")


# --- session cookie ------------------------------------------------------------------------------------
def sign(value: str) -> str:
    """`value` plus a truncated HMAC. Shared with the employee demo session in qa.py."""
    return value + "." + hmac.new(session_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class Session:
    reviewer: str
    issued_at: int
    nonce: str


def new_cookie_value(reviewer: str, *, now: int | None = None) -> str:
    return sign(f"{int(now if now is not None else time.time())}|{reviewer}|{secrets.token_hex(8)}")


def parse(raw: str, *, now: float | None = None) -> Session | None:
    """A valid, unexpired session, or None. A forged, truncated or stale cookie is simply None."""
    if not raw:
        return None
    body, _, _sig = raw.rpartition(".")
    if not body or not hmac.compare_digest(sign(body), raw):
        return None
    parts = body.split("|")
    if len(parts) != 3:
        return None
    issued, reviewer, nonce = parts
    try:
        issued_at = int(issued)
    except ValueError:
        return None
    current = time.time() if now is None else now
    if issued_at > current + 60:  # clock skew, never a reason to accept a future cookie
        return None
    if current - issued_at > TTL_SECONDS:
        return None
    if reviewer not in REVIEWERS:
        return None
    return Session(reviewer, issued_at, nonce)


def session_of(request) -> Session | None:
    if request is None:
        return None
    try:
        raw = request.cookies.get(COOKIE, "")
    except AttributeError:  # a bare dict, used by tests
        raw = (request or {}).get(COOKIE, "")
    return parse(raw)


def reviewer_of(request) -> str:
    """Who is acting: the signed-in reviewer, else FALLBACK_REVIEWER when the gate is off."""
    session = session_of(request)
    return session.reviewer if session else FALLBACK_REVIEWER


# --- who is signed in, for the nav ---------------------------------------------------------------------
_CURRENT: ContextVar[str | None] = ContextVar("rb_auth_reviewer", default=None)


def current_reviewer() -> str | None:
    return _CURRENT.get()


def set_current(reviewer: str | None):
    return _CURRENT.set(reviewer)


def reset_current(token) -> None:
    try:
        _CURRENT.reset(token)
    except (ValueError, LookupError):  # different context; nothing to restore
        pass


def reviewer_name(reviewer: str | None) -> str:
    """Display name for a reviewer ID (the office directory when reachable, else the ID)."""
    if not reviewer:
        return ""
    try:
        from .plans import directory  # lazy: plans.py imports agent_bridge, not auth
        contact = directory().get(reviewer)
    except Exception:
        contact = None
    return contact.name if contact else reviewer


def reviewer_options() -> list[tuple[str, str]]:
    """(id, label) pairs for the login select: name and role when the directory is reachable."""
    try:
        from .plans import directory
        contacts = directory()
    except Exception:
        contacts = None
    out: list[tuple[str, str]] = []
    for rid in REVIEWERS:
        contact = contacts.get(rid) if contacts is not None else None
        out.append((rid, f"{contact.name} — {contact.role}" if contact else rid))
    return out


# --- login attempts ------------------------------------------------------------------------------------
_FAILS: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def _recent(ip: str, now: float) -> list[float]:
    return [t for t in _FAILS.get(ip, []) if now - t < 60]


def too_many_failures(ip: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _LOCK:
        fails = _recent(ip, now)
        _FAILS[ip] = fails
        return len(fails) >= MAX_FAILS_PER_MINUTE


def record_failure(ip: str, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _LOCK:
        _FAILS[ip] = _recent(ip, now) + [now]


def clear_failures(ip: str) -> None:
    with _LOCK:
        _FAILS.pop(ip, None)


def reset_attempts() -> None:
    """Forget every failure counter (tests, and Reset demo)."""
    with _LOCK:
        _FAILS.clear()


# --- audit ---------------------------------------------------------------------------------------------
def audit_denied(reason: str, *, ip: str = "", run_id: str = "auth", **extra) -> None:
    """One `access_denied` event per IP and minute: guessing cannot flood the audit log.

    The payload carries a reason and never the password or the IP in clear."""
    bucket = int(time.time() // 60)
    ip_hash = hashlib.sha256(ip.encode("utf-8")).hexdigest()[:12] if ip else "none"
    _append(run_id, "access_denied", f"{reason}:{ip_hash}:{bucket}", "system",
            {"reason": reason, **extra})


def audit_self_approval(run_id: str, reviewer: str, ref: str, **extra) -> None:
    """A refused self-approval, attributed to the person who tried it."""
    _append(run_id, "access_denied", ref, f"human:{reviewer}", {"reason": "self_approval", **extra})


def _append(run_id: str, event_type: str, ref: str, actor: str, payload: dict) -> None:
    """Best effort: an audit failure must never turn a refusal into a 500."""
    try:
        from .plans import log  # lazy: plans.py owns the shared log
        from reimburse_agent import audit
        log().append(audit.make_event(run_id, event_type, ref, actor=actor, payload=payload))
    except Exception:  # noqa: BLE001 - see docstring
        pass


def login_url(next_path: str = "") -> str:
    """`/login?next=...`, accepting only a local absolute path (never `//host` or a scheme)."""
    from urllib.parse import quote
    if next_path.startswith("/") and not next_path.startswith("//") and "\\" not in next_path:
        return f"/login?next={quote(next_path, safe='/')}"
    return "/login"


def safe_next(value: str | None, default: str = "/") -> str:
    """Where to go after login: a local path only, so `next` can never redirect off-site."""
    if not value:
        return default
    value = value.strip()
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    if value.startswith("/login") or value.startswith("/logout"):
        return default
    return value
