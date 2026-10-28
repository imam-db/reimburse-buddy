"""Login accounts: password hashing and the user stores (Postgres in production, memory for tests/dev).

Hash format: `scrypt$n$r$p$<salt_hex>$<hash_hex>` (hashlib.scrypt, 16-byte random salt, n=2**14, r=8, p=1),
verified with hmac.compare_digest. The app can read users and touch `last_login_at`; creating accounts is the
owner's job (seed_users.py). Neither a password nor an email is ever logged here.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Protocol

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SALT_BYTES = 16
KEY_BYTES = 32
ROLES = ("employee", "reviewer", "finance")
ROLE_LABELS = {"employee": "Karyawan", "reviewer": "Manager", "finance": "Finance"}  # DB value -> shown as


def is_manager(role: str | None) -> bool:
    """The DB value stays `reviewer`; it means Manager (docs/adr/0001-three-roles.md)."""
    return role == "reviewer"


def is_finance(role: str | None) -> bool:
    return role == "finance"


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int = KEY_BYTES) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen,
                          maxmem=128 * n * r * 2 + 1024 * 1024)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """True only for a well-formed hash that matches. A malformed hash is simply False."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(hash_hex)
        actual = _derive(password, bytes.fromhex(salt_hex), int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY: str | None = None


def dummy_hash() -> str:
    """A hash nobody knows the password of, so an unknown email costs the same time as a wrong password."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(16))
    return _DUMMY


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


@dataclass(frozen=True)
class User:
    id: int
    employee_id: str
    email: str
    password_hash: str
    role: str
    is_active: bool = True


class UserStore(Protocol):
    def find_by_email(self, email: str) -> User | None: ...

    def touch_login(self, user_id: int) -> None: ...


class MemoryUserStore:
    """For tests and local dev without a database."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self.logins: list[int] = []
        self._next_id = 1

    def add(self, employee_id: str, email: str, password: str | None = None, role: str = "reviewer",
            is_active: bool = True, *, password_hash: str | None = None) -> User:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        stored = password_hash or hash_password(password or "")
        user = User(self._next_id, employee_id, normalize_email(email), stored, role, is_active)
        self._next_id += 1
        self._users[user.email] = user
        return user

    def find_by_email(self, email: str) -> User | None:
        return self._users.get(normalize_email(email))

    def touch_login(self, user_id: int) -> None:
        self.logins.append(user_id)


class PostgresUserStore:
    """Expects agent/migrations/007_app_users.sql. Connect as reimburse_app (SELECT + UPDATE last_login_at).

    Same connection style as PostgresAuditLog: autocommit, no server-side prepared statements (safe behind a
    pooler). One reconnect is attempted when Neon has closed an idle connection."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn = None
        self._connect()

    def _connect(self) -> None:
        import psycopg  # optional dependency, imported only when used
        self._conn = psycopg.connect(self._dsn, autocommit=True, prepare_threshold=None)

    def _run(self, sql: str, params: tuple, fetch: bool):
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._connect()
                    with self._conn.cursor() as cur:
                        cur.execute(sql, params)
                        return cur.fetchone() if fetch else None
                except Exception:  # noqa: BLE001 - one reconnect, then let the caller see the failure
                    if attempt == 2:
                        raise
                    self._conn = None

    def find_by_email(self, email: str) -> User | None:
        row = self._run("SELECT id, employee_id, email, password_hash, role, is_active FROM app_users"
                        " WHERE email = %s", (normalize_email(email),), True)
        return User(int(row[0]), row[1], row[2], row[3], row[4], bool(row[5])) if row else None

    def touch_login(self, user_id: int) -> None:
        self._run("UPDATE app_users SET last_login_at = now() WHERE id = %s", (user_id,), False)


def user_dsn() -> str:
    """REIMBURSE_AUTH_DATABASE_URL, else the audit log's DSN. Never logged."""
    return (os.environ.get("REIMBURSE_AUTH_DATABASE_URL") or os.environ.get("REIMBURSE_AUDIT_DATABASE_URL")
            or "").strip()


def authenticate(store: UserStore, email: str, password: str) -> User | None:
    """The user when email, password and is_active all pass, else None (one outcome for every failure).

    The hash check runs even for an unknown email, against a dummy hash, so timing does not reveal which
    emails are registered."""
    user = store.find_by_email(normalize_email(email))
    ok = verify_password(password or "", user.password_hash if user else dummy_hash())
    if user is None or not ok or not user.is_active or user.role not in ROLES:
        return None
    return user
