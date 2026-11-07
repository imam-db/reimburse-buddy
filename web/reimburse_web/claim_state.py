"""Where the web app keeps what a person did to a claim (ADR 0010).

The JSON seed in `data/` stays read-only in git. Everything the web app *writes* — a submitted claim, a
forwarded message, the reviewer's working state, a draft, the outbox, a manager request, a status change
and the uploaded bytes — also goes to Neon, so a restart or a redeploy keeps it. On startup `hydrate`
replays the stored rows on top of the seed, and the pages keep reading memory, which is why they did not
change.

Two rules the callers rely on:

* **Database first, then memory.** Every write goes to the database before the in-memory state changes, so
  a write the database did not keep never looks kept. A caller that gets an exception answers 503 and
  leaves memory alone.
* **One transaction per user action.** Submitting a claim writes the claim, its forwarded messages, its
  review state and the draft's `submitted_at` together, or none of them.

The backend follows `REIMBURSE_PLAN_BACKEND` (ADR 0010 §1): `postgres` means these rows live in Neon,
`file` — the default outside production — means this module is a no-op and behaviour is exactly as before.
No new environment variable, no second pool: the DSN and the pool come from `plan_state`.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import plan_state

# The tables of agent/migrations/008_claim_state.sql, in the order the migration creates them.
CLAIM_TABLES = ("demo_epoch", "web_claims", "web_messages", "claim_reviews", "employee_drafts",
                "outbox_messages", "approval_requests", "claim_status_changes", "uploaded_files")

# A derived id can collide with a row the database already holds; the writer takes the next number.
_ID_RETRIES = 50


def _bump(ident: str) -> str:
    """`CLM-2026-W001` -> `CLM-2026-W002`; only the trailing digits move and the padding is kept."""
    found = re.search(r"(\d+)$", str(ident))
    if not found:
        raise ValueError(f"no number to advance in id {ident!r}")
    digits = found.group(1)
    return f"{str(ident)[:found.start()]}{int(digits) + 1:0{len(digits)}d}"


def _decode(value) -> dict:
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


@runtime_checkable
class ClaimRows(Protocol):
    """One function per fact, plus `hydrate`, `transaction` and `bump_epoch`."""

    def add_claim(self, claim: dict) -> str: ...
    def add_message(self, message: dict) -> str: ...
    def update_message(self, message: dict) -> None: ...
    def save_review(self, claim_id: str, review: dict) -> None: ...
    def save_draft(self, draft: dict) -> None: ...
    def mark_draft_submitted(self, draft_id: str) -> None: ...
    def add_outbox(self, message: dict) -> str: ...
    def save_approval_request(self, request: dict) -> None: ...
    def add_status_change(self, entry: dict) -> None: ...
    def save_file(self, stored_as: str, data: bytes, owner_id: str, content_type: str = "",
                  sha256: str = "") -> None: ...
    def read_file(self, stored_as: str) -> bytes | None: ...
    def uploaded_bytes_since(self, owner_id: str, seconds: float) -> int: ...
    def expire_draft(self, draft_id: str, stored: list[str], owner_id: str) -> None: ...
    def transaction(self): ...
    def bump_epoch(self) -> int: ...
    def hydrate(self, store, review: dict, drafts: dict) -> None: ...
    def max_request_number(self) -> int: ...


class MemoryClaimRows:
    """The default outside production: nothing is stored, so the web app behaves exactly as before."""

    def add_claim(self, claim: dict) -> str:
        return str(claim["id"])

    def add_message(self, message: dict) -> str:
        return str(message["id"])

    def update_message(self, message: dict) -> None:
        return None

    def save_review(self, claim_id: str, review: dict) -> None:
        return None

    def save_draft(self, draft: dict) -> None:
        return None

    def mark_draft_submitted(self, draft_id: str) -> None:
        return None

    def add_outbox(self, message: dict) -> str:
        return str(message["id"])

    def save_approval_request(self, request: dict) -> None:
        return None

    def add_status_change(self, entry: dict) -> None:
        return None

    def save_file(self, stored_as: str, data: bytes, owner_id: str, content_type: str = "",
                  sha256: str = "") -> None:
        return None

    def read_file(self, stored_as: str) -> bytes | None:
        return None

    def uploaded_bytes_since(self, owner_id: str, seconds: float) -> int:
        return 0

    def expire_draft(self, draft_id: str, stored: list[str], owner_id: str) -> None:
        return None

    @contextmanager
    def transaction(self):
        yield

    def bump_epoch(self) -> int:
        return 1

    def hydrate(self, store, review: dict, drafts: dict) -> None:
        return None

    def max_request_number(self) -> int:
        return 0


class PostgresClaimRows:
    """`agent/migrations/008_claim_state.sql`, one row per fact, on the shared plan-state pool.

    Connections are autocommit (`plan_state.make_pool`), so a single write is its own transaction. A
    `transaction()` block pins one connection for the whole block, and a nested block becomes a savepoint,
    which is what lets a submit write the claim, its messages, the review and the draft together.
    """

    def __init__(self, pool):
        from psycopg.errors import UniqueViolation  # only needed when the postgres backend is on

        self.pool = pool
        self._unique_violation = UniqueViolation
        self._local = threading.local()
        self._epoch: int | None = None

    # --- connection handling -------------------------------------------------------------------
    @contextmanager
    def _cursor(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with conn.cursor() as cur:
                yield cur
            return
        with self.pool.connection() as own, own.cursor() as cur:
            yield cur

    @contextmanager
    def transaction(self):
        """One connection, one transaction for the whole block (a nested block is a savepoint)."""
        current = getattr(self._local, "conn", None)
        if current is not None:
            with current.transaction():
                yield
            return
        with self.pool.connection() as conn, conn.transaction():
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    # --- epoch ---------------------------------------------------------------------------------
    def epoch(self) -> int:
        if self._epoch is None:
            with self._cursor() as cur:
                cur.execute("SELECT epoch FROM demo_epoch WHERE id = 1")
                row = cur.fetchone()
            self._epoch = int(row[0]) if row else 1
        return self._epoch

    def bump_epoch(self) -> int:
        """Reset demo: every table reads as empty for the new epoch, and no row is deleted."""
        with self._cursor() as cur:
            cur.execute("UPDATE demo_epoch SET epoch = epoch + 1 WHERE id = 1 RETURNING epoch")
            row = cur.fetchone()
        self._epoch = int(row[0]) if row else self.epoch() + 1
        return self._epoch

    # --- claims and messages -------------------------------------------------------------------
    def _insert_row(self, sql: str, params, ident: str, payload: dict) -> str:
        """Insert under `ident`; when the key is taken, take the next number and try again.

        The stored JSON carries the id it is stored under, so a replay never brings back the old number."""
        current = str(ident)
        for _ in range(_ID_RETRIES):
            stored = dict(payload, id=current)
            with self._cursor() as cur:
                cur.execute(sql, (*params, current, json.dumps(stored, ensure_ascii=False)))
                if cur.fetchone() is not None:
                    return current
            current = _bump(current)
        raise RuntimeError(f"could not allocate an id after {_ID_RETRIES} attempts (last: {current})")

    def add_claim(self, claim: dict) -> str:
        claim_id = self._insert_row(
            "INSERT INTO web_claims (epoch, employee_id, claim_id, claim) VALUES (%s, %s, %s, %s::json) "
            "ON CONFLICT (epoch, claim_id) DO NOTHING RETURNING claim_id",
            (self.epoch(), claim.get("employee_id", "")), claim["id"], claim)
        claim["id"] = claim_id
        return claim_id

    def add_message(self, message: dict) -> str:
        attachments = message.get("attachments") or []
        claim_id = next((a.get("linked_claim") or "" for a in attachments if a.get("linked_claim")), "")
        message_id = self._insert_row(
            "INSERT INTO web_messages (epoch, owner_id, claim_id, message_id, message) "
            "VALUES (%s, %s, %s, %s, %s::json) ON CONFLICT (epoch, message_id) DO NOTHING "
            "RETURNING message_id",
            (self.epoch(), message.get("owner_id", ""), claim_id), message["id"], message)
        message["id"] = message_id
        return message_id

    def update_message(self, message: dict) -> None:
        with self._cursor() as cur:
            cur.execute("UPDATE web_messages SET message = %s::json, updated_at = now() "
                        "WHERE epoch = %s AND message_id = %s",
                        (json.dumps(message, ensure_ascii=False), self.epoch(), message["id"]))

    # --- reviewer state, drafts, outbox, requests ----------------------------------------------
    def save_review(self, claim_id: str, review: dict) -> None:
        with self._cursor() as cur:
            cur.execute("INSERT INTO claim_reviews (epoch, claim_id, review) VALUES (%s, %s, %s::json) "
                        "ON CONFLICT (epoch, claim_id) DO UPDATE "
                        "SET review = EXCLUDED.review, updated_at = now()",
                        (self.epoch(), claim_id, json.dumps(review, ensure_ascii=False)))

    def save_draft(self, draft: dict) -> None:
        with self._cursor() as cur:
            cur.execute("INSERT INTO employee_drafts (epoch, draft_id, employee_id, draft) "
                        "VALUES (%s, %s, %s, %s::json) ON CONFLICT (epoch, draft_id) DO UPDATE "
                        "SET draft = EXCLUDED.draft, updated_at = now()",
                        (self.epoch(), draft["id"], draft.get("emp", ""),
                         json.dumps(draft, ensure_ascii=False)))

    def mark_draft_submitted(self, draft_id: str) -> None:
        """A submitted draft keeps its row: the epoch is the only thing that ever hides a draft."""
        with self._cursor() as cur:
            cur.execute("UPDATE employee_drafts SET submitted_at = now(), updated_at = now() "
                        "WHERE epoch = %s AND draft_id = %s AND submitted_at IS NULL",
                        (self.epoch(), draft_id))

    def add_outbox(self, message: dict) -> str:
        message_id = self._insert_row(
            "INSERT INTO outbox_messages (epoch, message_id, message) VALUES (%s, %s, %s::json) "
            "ON CONFLICT (epoch, message_id) DO NOTHING RETURNING message_id",
            (self.epoch(),), message["id"], message)
        message["id"] = message_id
        return message_id

    def save_approval_request(self, request: dict) -> None:
        """Create the request, or update it when a manager answers (ADR 0003 keeps that order)."""
        with self._cursor() as cur:
            cur.execute("INSERT INTO approval_requests (epoch, request_id, claim_id, request) "
                        "VALUES (%s, %s, %s, %s::json) ON CONFLICT (epoch, request_id) DO UPDATE "
                        "SET request = EXCLUDED.request, updated_at = now()",
                        (self.epoch(), request["id"], request.get("claim_id", ""),
                         json.dumps(request, ensure_ascii=False)))

    def add_status_change(self, entry: dict) -> None:
        with self._cursor() as cur:
            cur.execute("INSERT INTO claim_status_changes (epoch, claim_id, from_status, to_status, note) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (self.epoch(), entry["claim_id"], str(entry.get("from", "")),
                         str(entry.get("to", "")), str(entry.get("note", ""))))

    # --- uploaded bytes ------------------------------------------------------------------------
    def save_file(self, stored_as: str, data: bytes, owner_id: str, content_type: str = "",
                  sha256: str = "") -> None:
        with self._cursor() as cur:
            cur.execute("INSERT INTO uploaded_files (epoch, stored_as, owner_id, content_type, size_bytes, "
                        "sha256, content) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (epoch, stored_as) DO UPDATE SET content = EXCLUDED.content, "
                        "size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256",
                        (self.epoch(), stored_as, owner_id, content_type or "application/octet-stream",
                         len(data), sha256, data))

    def read_file(self, stored_as: str) -> bytes | None:
        with self._cursor() as cur:
            cur.execute("SELECT content FROM uploaded_files WHERE epoch = %s AND stored_as = %s",
                        (self.epoch(), stored_as))
            row = cur.fetchone()
        return bytes(row[0]) if row and row[0] is not None else None

    def uploaded_bytes_since(self, owner_id: str, seconds: float) -> int:
        """Bytes this owner stored in the last `seconds` (uploaded_files.created_at), current epoch."""
        with self._cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM uploaded_files WHERE epoch = %s "
                        "AND owner_id = %s AND created_at > now() - make_interval(secs => %s)",
                        (self.epoch(), owner_id, float(seconds)))
            row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def expire_draft(self, draft_id: str, stored: list[str], owner_id: str) -> None:
        """Retire an unsubmitted draft without DELETE (the app role has none, see 008): setting
        submitted_at takes the row out of the startup replay, and the file rows keep their key but lose
        their bytes (UPDATE only). Only rows of this owner and only an unsubmitted draft are touched."""
        with self._cursor() as cur:
            cur.execute("UPDATE employee_drafts SET submitted_at = now(), updated_at = now() "
                        "WHERE epoch = %s AND draft_id = %s AND employee_id = %s AND submitted_at IS NULL",
                        (self.epoch(), draft_id, owner_id))
            for name in stored:
                cur.execute("UPDATE uploaded_files SET content = %s, size_bytes = 0 "
                            "WHERE epoch = %s AND stored_as = %s AND owner_id = %s",
                            (b"", self.epoch(), str(name), owner_id))

    # --- startup replay ------------------------------------------------------------------------
    def load(self) -> dict:
        """The stored rows of the current epoch, in the order they were written."""
        epoch = self.epoch()
        with self._cursor() as cur:
            cur.execute("SELECT claim FROM web_claims WHERE epoch = %s ORDER BY created_at, claim_id",
                        (epoch,))
            claims = [_decode(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT message FROM web_messages WHERE epoch = %s ORDER BY created_at, message_id",
                        (epoch,))
            messages = [_decode(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT claim_id, from_status, to_status, note FROM claim_status_changes "
                        "WHERE epoch = %s ORDER BY id", (epoch,))
            changes = [{"claim_id": row[0], "from": row[1], "to": row[2], "note": row[3]}
                       for row in cur.fetchall()]
            cur.execute("SELECT claim_id, review FROM claim_reviews WHERE epoch = %s", (epoch,))
            reviews = [(row[0], _decode(row[1])) for row in cur.fetchall()]
            cur.execute("SELECT draft, updated_at FROM employee_drafts WHERE epoch = %s AND submitted_at IS NULL "
                        "ORDER BY updated_at, draft_id", (epoch,))
            drafts = []
            for row in cur.fetchall():
                draft = _decode(row[0])
                if row[1] is not None:  # the row's clock is the draft's age (it expires on this)
                    draft["updated_at"] = row[1].isoformat()
                drafts.append(draft)
            cur.execute("SELECT message FROM outbox_messages WHERE epoch = %s ORDER BY created_at, message_id",
                        (epoch,))
            outbox = [_decode(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT request FROM approval_requests WHERE epoch = %s "
                        "ORDER BY created_at, request_id", (epoch,))
            requests = [_decode(row[0]) for row in cur.fetchall()]
        return {"claims": claims, "messages": messages, "changes": changes, "reviews": reviews,
                "drafts": drafts, "outbox": outbox, "requests": requests}

    def hydrate(self, store, review: dict, drafts: dict) -> None:
        replay(store, review, drafts, self.load())

    def max_request_number(self) -> int:
        """Highest APR number of ANY epoch (a SELECT only): request ids must never repeat in the audit log."""
        with self._cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(NULLIF(regexp_replace(request_id, '[^0-9]', '', 'g'), '')::int), 0) "
                        "FROM approval_requests")
            row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0


# --- the process store ---------------------------------------------------------------------
_ROWS: ClaimRows | None = None
_LOCK = threading.RLock()


def rows() -> ClaimRows:
    """The process store: memory outside production, Neon with `REIMBURSE_PLAN_BACKEND=postgres`."""
    global _ROWS
    with _LOCK:
        if _ROWS is None:
            pool = plan_state.pool()  # None for the file backend; raises when the DSN is missing
            _ROWS = MemoryClaimRows() if pool is None else PostgresClaimRows(pool)
        return _ROWS


def set_rows(replacement: ClaimRows | None) -> None:
    """Install a store (tests), or None to fall back to the configured backend."""
    global _ROWS
    with _LOCK:
        _ROWS = replacement


def reset() -> None:
    """Forget the process store (tests, Reset demo)."""
    set_rows(None)


# --- one function per fact, for the call sites ----------------------------------------------
def add_claim(claim: dict) -> str:
    return rows().add_claim(claim)


def add_message(message: dict) -> str:
    return rows().add_message(message)


def update_message(message: dict) -> None:
    rows().update_message(message)


def save_review(claim_id: str, review: dict) -> None:
    rows().save_review(claim_id, review)


def save_draft(draft: dict) -> None:
    rows().save_draft(draft)


def mark_draft_submitted(draft_id: str) -> None:
    rows().mark_draft_submitted(draft_id)


def add_outbox(message: dict) -> str:
    return rows().add_outbox(message)


def save_approval_request(request: dict) -> None:
    rows().save_approval_request(request)


def add_status_change(entry: dict) -> None:
    rows().add_status_change(entry)


def save_file(stored_as: str, data: bytes, owner_id: str, content_type: str = "",
              sha256: str = "") -> None:
    rows().save_file(stored_as, data, owner_id, content_type, sha256)


def read_file(stored_as: str) -> bytes | None:
    return rows().read_file(stored_as)


def uploaded_bytes_since(owner_id: str, seconds: float) -> int:
    return rows().uploaded_bytes_since(owner_id, seconds)


def expire_draft(draft_id: str, stored: list[str], owner_id: str) -> None:
    rows().expire_draft(draft_id, stored, owner_id)


@contextmanager
def transaction():
    """Everything inside is one transaction, or one savepoint when a transaction is already open."""
    with rows().transaction():
        yield


def hydrate(store, review: dict, drafts: dict) -> None:
    rows().hydrate(store, review, drafts)
    store.raise_approval_floor(rows().max_request_number())


def replay(store, review: dict, drafts: dict, data: dict) -> None:
    """Put stored rows on top of the seed (ADR 0010 §4). Reads only; nothing is written back.

    `data` holds the lists in the order they were written: claims before the status changes that move
    them, drafts that were never submitted, and the messages whose presence marks a scripted reply as
    already delivered.
    """
    for claim in data.get("claims", []):
        store.claims.append(claim)
    for message in data.get("messages", []):
        store.inbox.append(message)
    for change in data.get("changes", []):
        try:
            store.get_claim(change["claim_id"])["status"] = change["to"]
        except KeyError:  # a change for a claim of an older epoch: nothing to move
            continue
        store.status_log.append({"claim_id": change["claim_id"], "from": change["from"],
                                 "to": change["to"], "note": change["note"]})
    for claim_id, row in data.get("reviews", []):
        review[claim_id] = row
    for draft in data.get("drafts", []):
        drafts[draft["id"]] = draft
    for message in data.get("outbox", []):
        store.outbox.append(message)
    for request in data.get("requests", []):
        store.approval_requests.append(request)

    # A scripted reply counts as delivered when its reply message is among the stored messages.
    present = {m["id"] for m in store.inbox}
    for scripted in store.replies:
        if scripted.get("reply", {}).get("id") in present:
            scripted["_delivered"] = True


# --- file paths ----------------------------------------------------------------------------
def restore_file(upload_dir, stored_as: str) -> Path | None:
    """The disk path of an uploaded file, restored from the database when the disk copy is gone.

    Extraction, OCR and duplicate detection read from disk, so the bytes stay in `UPLOAD_DIR` as well. On
    a host with an ephemeral disk the database copy is what brings the file back. The path is only ever
    inside `upload_dir`: the caller keeps its own ownership and content-type checks.
    """
    root = Path(upload_dir).resolve()
    path = (root / str(stored_as)).resolve()
    if root not in path.parents:  # a stored name that climbs out of the upload directory
        return None
    if path.is_file():
        return path
    data = read_file(str(stored_as))
    if data is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --- startup and Reset demo ----------------------------------------------------------------
def _missing(pool, table: str) -> bool:
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} LIMIT 0")
    except Exception:  # noqa: BLE001 - a missing table and a missing grant both mean "unusable"
        return True
    return False


def startup_check() -> None:
    """Fail startup when the postgres backend cannot keep claim state, naming the migration to run.

    Runs right after `plan_state.startup_check` and before the expiry sweep (see app.py). There is no
    silent fallback to memory: that would lose exactly the state this backend exists to keep.
    """
    if not plan_state.is_postgres():
        return
    from reimburse_agent.office.store import StoreConfigError  # agent/ is on sys.path via plan_state

    pool = plan_state.pool()  # raises StoreConfigError when the DSN is missing
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - the class is enough, the DSN must not be printed
        raise StoreConfigError(
            "The claim-state database is unreachable "
            f"({type(exc).__name__}), so no claim could be stored or replayed. Check "
            "REIMBURSE_STATE_DATABASE_URL.") from exc

    missing = [table for table in CLAIM_TABLES if _missing(pool, table)]
    if missing:
        raise StoreConfigError(
            "The claim-state tables are missing: " + ", ".join(missing) + ". Apply "
            "agent/migrations/008_claim_state.sql as the owner role.")


def startup_hydrate() -> None:
    """Replay the stored rows on top of the seed once, at startup (a no-op outside production)."""
    from . import app as web  # late import: app.py imports this module

    hydrate(web.store, web.REVIEW, web.DRAFTS)
    from . import upload_flow  # late import, as above: expire drafts past their TTL now that they are loaded

    upload_flow.startup_prune()


def reset_demo() -> None:
    """Reset demo: a new epoch for postgres, then reload the seed and replay it (nothing is deleted)."""
    rows().bump_epoch()
    from . import app as web  # late import: app.py imports this module

    floor = web.store.highest_approval()  # ids of earlier epochs stay taken: the audit log is never cleared
    web.reset_all()
    web.store.raise_approval_floor(floor)
    hydrate(web.store, web.REVIEW, web.DRAFTS)
