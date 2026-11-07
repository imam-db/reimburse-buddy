"""Login gate for the version B web app: email + password checked against `app_users` (users.py).

Every route except `/login`, `/logout` and `/healthz` needs the signed session cookie. The cookie is
`issued_at|employee_id|role|nonce` plus an HMAC, 12 hours, checked on every request; the role comes from
the user's row at login time. Each role has its own area (employee `/employee`, manager `/manager`, finance `/` and `/admin`); a
wrong-role navigation is sent to the role's landing page (docs/adr/0001-three-roles.md).

The gate is always on in production. Elsewhere it is on when a user store is installed (tests, local dev) or
a user DSN is configured, and off otherwise, so a plain local run and the rest of the test suite behave as
before. In production a missing session key, a missing DSN or an unreachable user table aborts startup
instead of serving an unprotected app.

Passwords and emails are never logged and never written to the audit log.

The demo profile picker (docs/adr/0012-demo-profiles.md) is a demo-only way in without a password:
`REIMBURSE_DEMO_PROFILES=1` turns `/login` into a picker of five `app_users` rows, only outside production
and only with the gate on. Production refuses to start when the switch is set at all.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from urllib.parse import urlsplit
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


# --- demo profile picker (ADR 0012) ---------------------------------------------------------------------
# DEMO ONLY: one-click, passwordless sign-in as one of these accounts, and only outside production.
DEMO_PROFILES = ("EMP-001", "EMP-003", "EMP-100", "EMP-101", "EMP-102")
DEMO_PROFILES_ENV = "REIMBURSE_DEMO_PROFILES"
DEMO_LOGIN_PATH = "/login/demo"
_TRUTHY = ("1", "true", "yes", "on")
MIN_SESSION_KEY_LEN = 32

# ADR 0013: the real app's login card may link to the separately deployed demo.
DEMO_URL_ENV = "REIMBURSE_DEMO_URL"
_LOCAL_HOSTS = ("localhost", "127.0.0.1")


def demo_url() -> str:
    """The validated `REIMBURSE_DEMO_URL`, or "" when unset. Raises AuthConfigError for a bad value.

    Only an absolute `https://` URL with a host is accepted (`http://` only for localhost/127.0.0.1), so the
    link on the login card can never be a `javascript:` or relative URL."""
    raw = (os.environ.get(DEMO_URL_ENV) or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        host, parts = "", None
    ok = bool(host) and parts is not None and (
        parts.scheme == "https" or (parts.scheme == "http" and host in _LOCAL_HOSTS))
    if not ok:
        raise AuthConfigError(
            f"{DEMO_URL_ENV} must be an absolute https:// URL with a host (http:// only for localhost or "
            "127.0.0.1).")
    return raw


def demo_profiles_enabled() -> bool:
    """The one answer to "is the profile picker on?": the switch is `1`, not production, and the gate is on.

    Pages and routes call this and never read the variable themselves. With the gate off there is no sign-in
    at all, as before."""
    return ((os.environ.get(DEMO_PROFILES_ENV) or "").strip() == "1"
            and not is_production() and enabled())


def demo_profile_users() -> list[users.User]:
    """The accounts a card may stand for: `DEMO_PROFILES` order, and only rows that exist and are active.

    The role is the one on the `app_users` row, never one written here or taken from a request. An empty list
    when the picker is off (or the user database cannot be read), so a page can never show a card it may not
    sign in as."""
    if not demo_profiles_enabled():
        return []
    try:
        store = user_store()
        if store is None:
            return []
        found = [store.find_by_employee_id(emp) for emp in DEMO_PROFILES]
    except Exception:  # noqa: BLE001 - a user database outage is an empty picker, never a 500 page
        return []
    return [u for u in found if u is not None and u.is_active and u.role in users.ROLES]


def session_key() -> bytes:
    return (os.environ.get("REIMBURSE_DEMO_SESSION_KEY") or _PROCESS_KEY).encode("utf-8")


def startup_check() -> None:
    """Refuse a production deployment that would serve an open or restart-fragile app.

    Registered after plans.startup_config_check (the audit log is the first thing to be right) and
    before startup_sweep, so a misconfigured production process exits before serving a request. An
    installed store (tests) is trusted as is; otherwise the user DSN must exist and connect.

    `REIMBURSE_DEMO_URL` is validated in every environment, so a bad link never reaches a page."""
    demo_url()
    if not is_production():
        if ((os.environ.get(DEMO_PROFILES_ENV) or "").strip().lower() in _TRUTHY and not enabled()):
            raise AuthConfigError(
                f"{DEMO_PROFILES_ENV} is on but the login gate is off (no user database configured): every "
                "route would be open without a sign-in. Set REIMBURSE_AUTH_DATABASE_URL (or "
                f"REIMBURSE_AUDIT_DATABASE_URL), or unset {DEMO_PROFILES_ENV}.")
        return
    if (os.environ.get(DEMO_PROFILES_ENV) or "").strip():
        raise AuthConfigError(
            f"APP_ENV=prod must not set {DEMO_PROFILES_ENV}: the demo profile picker signs anyone in without "
            "a password, so it is refused rather than silently ignored.")
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
    key = (os.environ.get("REIMBURSE_DEMO_SESSION_KEY") or "").strip()
    if not key:
        raise AuthConfigError(
            "APP_ENV=prod requires REIMBURSE_DEMO_SESSION_KEY: a random per-process key logs everyone "
            "out on restart and breaks with more than one instance.")
    if "change_me" in key.lower() or len(key) < MIN_SESSION_KEY_LEN:
        raise AuthConfigError(
            f"APP_ENV=prod: REIMBURSE_DEMO_SESSION_KEY is a placeholder or shorter than {MIN_SESSION_KEY_LEN} "
            "characters. Generate one with: openssl rand -hex 32")


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


# Logout revocation: {cookie nonce: that cookie's own expiry}. Per process and lost on restart: the cookie is
# stateless, and revoking it everywhere would need a database table (out of scope: migrations are protected).
_REVOKED: dict[str, float] = {}


def revoke(raw: str, *, now: float | None = None) -> None:
    """Refuse this cookie from now on (POST /logout) until it would have expired anyway."""
    current = time.time() if now is None else now
    sess = parse(raw, now=current)
    if sess is None:
        return
    with _LOCK:
        for nonce, expiry in list(_REVOKED.items()):
            if expiry <= current:
                del _REVOKED[nonce]
        _REVOKED[sess.nonce] = sess.issued_at + TTL_SECONDS


def _still_valid(sess: Session) -> bool:
    """Not logged out, and (with a user store configured) the user row is still active with the same role.
    One indexed lookup per gated request, deliberately uncached so a deactivation takes effect at once."""
    with _LOCK:
        if sess.nonce in _REVOKED:
            return False
    store = user_store() if enabled() else None
    if store is None:
        return True
    user = store.find_by_employee_id(sess.reviewer)
    return bool(user and user.is_active and user.role == sess.role)


def session_of(request) -> Session | None:
    if request is None:
        return None
    try:
        raw = request.cookies.get(COOKIE, "")
    except AttributeError:  # a bare dict, used by tests
        raw = (request or {}).get(COOKIE, "")
    sess = parse(raw)
    return sess if sess is not None and _still_valid(sess) else None


DEV_EMPLOYEE_ENV = "REIMBURSE_DEV_EMPLOYEE"  # DEV ONLY: acts as this employee when the login gate is off


def current_employee(request) -> str | None:
    """The one source of the acting employee on the employee pages; never read from a query or a form.

    Gate on: the signed session's employee_id (any role acts as its own employee_id). Gate off (dev, tests):
    REIMBURSE_DEV_EMPLOYEE, stripped, and only when that employee exists in the store. None when nobody acts."""
    if enabled():
        sess = session_of(request)
        return sess.reviewer if sess else None
    emp = os.environ.get(DEV_EMPLOYEE_ENV, "").strip()
    if not emp:
        return None
    from . import app as web  # lazy: app.py imports this module
    try:
        web.store.get_employee(emp)
    except KeyError:
        return None
    return emp


def sign_in_notice() -> str:
    """Shown on an employee page when nobody is acting."""
    if enabled():
        return "<p>Silakan <a href='/login'>masuk</a> sebagai karyawan. / Please <a href='/login'>sign in</a> as an employee.</p>"
    return (f"<p class='demo'><b>DEV ONLY</b>: the login gate is off; set {DEV_EMPLOYEE_ENV}=EMP-xxx to act as an employee.</p>")


def reviewer_of(request) -> str:
    """Who is acting: the signed-in employee_id, else FALLBACK_REVIEWER when the gate is off."""
    session = session_of(request)
    return session.reviewer if session else FALLBACK_REVIEWER


def home_for(role: str | None) -> str:
    """Where a role lands after login."""
    if users.is_finance(role):
        return "/admin"
    return "/manager" if users.is_manager(role) else "/employee"


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base + "/")


def may_open(role: str | None, path: str) -> bool:
    """Employee: /employee. Manager: /manager. Finance: / and /admin. Nothing else."""
    if users.is_finance(role):
        return path == "/" or _under(path, "/admin")
    if users.is_manager(role):
        return _under(path, "/manager")
    if role == "employee":
        return _under(path, "/employee")
    return False


# --- who is signed in, for the nav ---------------------------------------------------------------------
_CURRENT: ContextVar[tuple[str, str] | None] = ContextVar("rb_auth_session", default=None)


def current_session() -> tuple[str, str] | None:
    """(employee_id, role) of the signed-in person for this request, else None."""
    return _CURRENT.get()


def current_reviewer() -> str | None:
    cur = _CURRENT.get()
    return cur[0] if cur else None


def set_current(employee_id: str | None, role: str = ""):
    return _CURRENT.set((employee_id, role) if employee_id else None)


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


def account_key(email: str) -> str:
    """The per-account throttle key: the submitted email, lowercased and stripped (never mixed with an IP)."""
    return "acct:" + (email or "").strip().lower()


def _recent(key: str, now: float) -> list[float]:
    return [t for t in _FAILS.get(key, []) if now - t < 60]


def _store_recent(key: str, fails: list[float]) -> None:
    if fails:
        _FAILS[key] = fails
    else:
        _FAILS.pop(key, None)  # bound memory: no empty lists


def too_many_failures(key: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with _LOCK:
        fails = _recent(key, now)
        _store_recent(key, fails)
        return len(fails) >= MAX_FAILS_PER_MINUTE


def record_failure(key: str, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _LOCK:
        _store_recent(key, _recent(key, now) + [now])


def clear_failures(key: str) -> None:
    with _LOCK:
        _FAILS.pop(key, None)


def reset_attempts() -> None:
    """Forget every failure counter (per IP and per account) and every logout revocation (tests)."""
    with _LOCK:
        _FAILS.clear()
        _REVOKED.clear()


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
