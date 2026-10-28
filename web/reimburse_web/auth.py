"""Login gate for the version B web app: email + password checked against `app_users` (users.py).

Every route except `/login`, `/logout` and `/healthz` needs the signed session cookie. The cookie is
`issued_at|employee_id|role|nonce` plus an HMAC, 12 hours, checked on every request; the role comes from
the user's row at login time. Reviewer and finance roles reach the admin pages; an employee is sent to
`/employee/claims`.

The gate is always on in production. Elsewhere it is on when a user store is installed (tests, local dev) or
a user DSN is configured, and off otherwise, so a plain local run and the rest of the test suite behave as
before. In production a missing session key, a missing DSN or an unreachable user table aborts startup
instead of serving an unprotected app.

Passwords and emails are never logged and never written to the audit log.
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

from . import users

COOKIE = "rb_auth"
TTL_SECONDS = 12 * 60 * 60
MAX_FAILS_PER_MINUTE = 5
PRODUCTION_ENVS = frozenset({"prod", "production"})
# Who a decision is attributed to when the gate is off (dev, tests): the value used before identities.
FALLBACK_REVIEWER = "reviewer"
PUBLIC_PATHS = frozenset({"/login", "/logout", "/healthz"})

_PROCESS_KEY = secrets.token_hex(32)


class AuthConfigError(RuntimeError):
    """Production is configured without a usable user database or session key."""


def is_production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in PRODUCTION_ENVS


_STORE: users.UserStore | None = None
_STORE_LOCK = threading.Lock()


def set_store(store: users.UserStore | None) -> None:
    """Install a user store (tests, local dev), or None to forget it and any lazily built Postgres store."""
    global _STORE
    with _STORE_LOCK:
        _STORE = store


def user_store() -> users.UserStore | None:
    """The installed store, else a PostgresUserStore built once from the user DSN, else None."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            dsn = users.user_dsn()
            if dsn:
                _STORE = users.PostgresUserStore(dsn)
        return _STORE


def enabled() -> bool:
    """Production: always. Elsewhere: only when a user store or a user DSN exists (no connection made here)."""
    return is_production() or _STORE is not None or bool(users.user_dsn())


def session_key() -> bytes:
    return (os.environ.get("REIMBURSE_DEMO_SESSION_KEY") or _PROCESS_KEY).encode("utf-8")


def startup_check() -> None:
    """Refuse a production deployment that would serve an open or restart-fragile app.

    Registered after plans.startup_config_check (the audit log is the first thing to be right) and
    before startup_sweep, so a misconfigured production process exits before serving a request. An
    installed store (tests) is trusted as is; otherwise the user DSN must exist and connect."""
    if not is_production():
        return
    if _STORE is None:
        if not users.user_dsn():
            raise AuthConfigError(
                "APP_ENV=prod requires REIMBURSE_AUTH_DATABASE_URL (or REIMBURSE_AUDIT_DATABASE_URL): "
                "logins are checked against the app_users table.")
        try:
            user_store()
        except Exception as exc:  # noqa: BLE001 - never echo the DSN or the driver's message
            raise AuthConfigError(
                f"APP_ENV=prod: cannot connect to the user database ({type(exc).__name__}). "
                "Check REIMBURSE_AUTH_DATABASE_URL and that migration 007_app_users.sql is applied.") from None
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
    reviewer: str  # the signed-in person's employee_id (kept under this name for the self-approval checks)
    role: str
    issued_at: int
    nonce: str


def new_cookie_value(employee_id: str, role: str = "reviewer", *, now: int | None = None) -> str:
    return sign(f"{int(now if now is not None else time.time())}|{employee_id}|{role}|{secrets.token_hex(8)}")


def parse(raw: str, *, now: float | None = None) -> Session | None:
    """A valid, unexpired session, or None. A forged, truncated or stale cookie is simply None."""
    if not raw:
        return None
    body, _, _sig = raw.rpartition(".")
    if not body or not hmac.compare_digest(sign(body), raw):
        return None
    parts = body.split("|")
    if len(parts) != 4:
        return None
    issued, employee_id, role, nonce = parts
    try:
        issued_at = int(issued)
    except ValueError:
        return None
    current = time.time() if now is None else now
    if issued_at > current + 60:  # clock skew, never a reason to accept a future cookie
        return None
    if current - issued_at > TTL_SECONDS:
        return None
    if role not in users.ROLES or not employee_id:
        return None
    return Session(employee_id, role, issued_at, nonce)


def session_of(request) -> Session | None:
    if request is None:
        return None
    try:
        raw = request.cookies.get(COOKIE, "")
    except AttributeError:  # a bare dict, used by tests
        raw = (request or {}).get(COOKIE, "")
    return parse(raw)


def reviewer_of(request) -> str:
    """Who is acting: the signed-in employee_id, else FALLBACK_REVIEWER when the gate is off."""
    session = session_of(request)
    return session.reviewer if session else FALLBACK_REVIEWER


def is_reviewer_role(role: str | None) -> bool:
    return role in users.REVIEWER_ROLES


def home_for(role: str | None) -> str:
    """Where a role lands after login."""
    return "/" if is_reviewer_role(role) else "/employee/claims"


def may_open(role: str | None, path: str) -> bool:
    """Reviewer and finance may open everything; an employee only the employee pages."""
    return is_reviewer_role(role) or path == "/employee" or path.startswith("/employee/")


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
    """Display name for an employee ID (the office directory when reachable, else the ID)."""
    if not reviewer:
        return ""
    try:
        from .plans import directory  # lazy: plans.py imports agent_bridge, not auth
        contact = directory().get(reviewer)
    except Exception:
        contact = None
    return contact.name if contact else reviewer


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
    """Forget every failure counter (tests)."""
    with _LOCK:
        _FAILS.clear()


# --- audit ---------------------------------------------------------------------------------------------
def audit_denied(reason: str, *, ip: str = "", run_id: str = "auth", **extra) -> None:
    """One `access_denied` event per IP and minute: guessing cannot flood the audit log.

    The payload carries a reason and never the password, the email or the IP in clear."""
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
