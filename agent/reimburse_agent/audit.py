"""Append-only audit log for version B.

Interface: ``append(event) -> bool`` (False when the event_key already exists: ON CONFLICT DO NOTHING)
and ``list(run_id=None) -> list[dict]`` (insertion order). Two backends share one contract
(tests/test_audit_contract.py): JSONL (default, local) and Postgres (agent/migrations/001_audit_log.sql;
app role has INSERT + SELECT only).

Structural rule this log relies on: a node that calls interrupt() only *collects* decisions and logs
staged_action / approval / rejection (idempotent by event_key, so a re-run on resume writes nothing new).
Side effects and `execution` events happen only in executor code (``execute_approved``), which runs only
when the log holds an approval for the same action_id and args_hash and no execution for it yet.
"""

from __future__ import annotations

import hashlib
import functools
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

EVENT_TYPES = frozenset({
    "request", "plan", "tool_call", "staged_action", "approval", "rejection", "execution", "clarification",
    "limit", "refusal", "access_denied", "injection_notice", "expired", "error",
})
OUTCOME_TYPES = frozenset({"approval", "rejection", "expired"})
ACTION_TYPES = frozenset({"staged_action", "approval", "rejection", "execution", "expired"})
FIELDS = ("id", "event_key", "run_id", "action_id", "ts", "actor", "event_type", "args_hash", "payload", "sources")
ACTOR_RE = re.compile(r"^(agent|system|human:[^\s:][^:]{0,79})$")
MAX_TEXT = 500
# args the human does not author or see as content: graph-trusted inputs or the idempotency key itself
HASH_EXCLUDE = frozenset({"evidence_ids", "human_decisions", "action_id"})


class AuditError(RuntimeError):
    pass


# --- hygiene -------------------------------------------------------------------------------------------
NIK_RE = re.compile(r"(?<!\d)\d{16}(?!\d)")
PHONE_RE = re.compile(r"(?<![\w])(?:\+62|62|0)8\d(?:[\s.-]?\d){6,11}(?!\d)")
SECRET_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|xox[abpr]-[\w-]{10,})\b")
SECRET_KEYS = re.compile(r"(api[_-]?key|secret|token|password|passwd|authorization|cookie|database_url)", re.I)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mask_text(text: str) -> str:
    text = SECRET_RE.sub("[secret]", text)
    text = NIK_RE.sub(lambda m: m.group(0)[:4] + "*" * 8 + m.group(0)[-4:], text)
    return PHONE_RE.sub(lambda m: m.group(0)[:4] + "****" + re.sub(r"\D", "", m.group(0))[-3:], text)


def clean(value: Any, limit: int = MAX_TEXT) -> Any:
    """Mask NIK, phone numbers and secrets; drop secret-named keys; truncate long strings (with a hash)."""
    if isinstance(value, dict):
        return {k: ("[secret]" if SECRET_KEYS.search(str(k)) else clean(v, limit)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, limit) for v in value]
    if isinstance(value, str):
        s = mask_text(value)
        return s if len(s) <= limit else f"{s[:limit]}… [truncated {len(value)} chars, sha256 {sha256(value)[:16]}]"
    return value


def output_summary(text: Any, limit: int = MAX_TEXT) -> dict:
    """A tool output as stored: masked + truncated text, plus the hash of the full original."""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return {"output": clean(s, limit), "output_sha256": sha256(s), "output_chars": len(s)}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def args_hash(tool: str, args: dict | None) -> str:
    """Hash of exactly what the human approves: tool + its content args (see HASH_EXCLUDE)."""
    view = {k: v for k, v in (args or {}).items() if k not in HASH_EXCLUDE and v is not None}
    return sha256(canonical({"tool": tool, "args": view}))


SOURCE_RE = re.compile(r"\b(?:CLM|IT|MSG|EVT|MEMO|EMP|OUT|APR)-[\w-]+\b")


def source_ids(*values: Any) -> list[str]:
    seen: list[str] = []
    for v in values:
        for m in SOURCE_RE.findall(v if isinstance(v, str) else canonical(v)):
            if m not in seen:
                seen.append(m)
    return seen[:50]


# --- events --------------------------------------------------------------------------------------------
def event_key(run_id: str, event_type: str, ref: str) -> str:
    return f"{run_id}:{event_type}:{ref}"


def make_event(run_id: str, event_type: str, ref: str, *, actor: str = "agent", action_id: str | None = None,
               args_hash: str | None = None, payload: dict | None = None, sources: Iterable[str] = ()) -> dict:
    return {"id": str(uuid.uuid4()), "event_key": event_key(run_id, event_type, ref), "run_id": run_id,
            "action_id": action_id, "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "actor": actor, "event_type": event_type, "args_hash": args_hash,
            "payload": clean(payload or {}), "sources": list(sources)}


def validate(e: dict) -> dict:
    if not isinstance(e, dict):
        raise AuditError("event must be a dict")
    extra = set(e) - set(FIELDS)
    if extra:
        raise AuditError(f"unknown fields: {sorted(extra)}")
    for f in ("id", "event_key", "run_id", "ts", "actor", "event_type"):
        if not isinstance(e.get(f), str) or not e[f]:
            raise AuditError(f"{f} is required")
    if e["event_type"] not in EVENT_TYPES:
        raise AuditError(f"unknown event_type {e['event_type']!r}")
    if not ACTOR_RE.match(e["actor"]):
        raise AuditError(f"bad actor {e['actor']!r} (agent | system | human:<name>)")
    try:
        ts = datetime.fromisoformat(e["ts"])
    except ValueError as exc:
        raise AuditError("ts must be ISO 8601") from exc
    if ts.utcoffset() is None or ts.utcoffset().total_seconds() != 0:
        raise AuditError("ts must be UTC")
    if e["event_type"] in ACTION_TYPES and not e.get("action_id"):
        raise AuditError(f"{e['event_type']} needs an action_id")
    if e["event_type"] in {"staged_action", "approval", "execution"} and not e.get("args_hash"):
        raise AuditError(f"{e['event_type']} needs an args_hash")
    if not isinstance(e.get("payload", {}), dict) or not isinstance(e.get("sources", []), list):
        raise AuditError("payload must be an object and sources a list")
    out = {f: e.get(f) for f in FIELDS}
    out["payload"] = out["payload"] or {}
    out["sources"] = out["sources"] or []
    json.dumps(out)  # must be JSON-serialisable (raises TypeError otherwise)
    return out


class AuditLog(Protocol):
    def append(self, event: dict) -> bool: ...
    def list(self, run_id: str | None = None) -> list[dict]: ...


class JsonlAuditLog:
    """Postgres-like JSONL: UNIQUE(event_key) via a key set loaded at startup, insertion order, validation,
    fsync per append. path=None keeps everything in memory (default per graph in tests)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._rows: list[dict] = []
        self._keys: set[str] = set()
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if row["event_key"] not in self._keys:
                        self._keys.add(row["event_key"])
                        self._rows.append(row)

    def append(self, event: dict) -> bool:
        row = validate(event)
        with self._lock:
            if row["event_key"] in self._keys:
                return False
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            self._keys.add(row["event_key"])
            self._rows.append(row)
            return True

    def list(self, run_id: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows if run_id is None or r["run_id"] == run_id]


class PostgresAuditLog:
    """Same contract on Postgres (psycopg 3). Expects agent/migrations/001_audit_log.sql applied by the
    owner role; connect with the app role (INSERT + SELECT only). Not exercised without DATABASE_URL."""

    def __init__(self, dsn: str):
        import psycopg  # optional dependency, imported only when used

        self._conn = psycopg.connect(dsn, autocommit=True)

    def append(self, event: dict) -> bool:
        r = validate(event)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_events (id, event_key, run_id, action_id, ts, actor, event_type, args_hash,"
                " payload, sources) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)"
                " ON CONFLICT (event_key) DO NOTHING",
                (r["id"], r["event_key"], r["run_id"], r["action_id"], r["ts"], r["actor"], r["event_type"],
                 r["args_hash"], json.dumps(r["payload"]), json.dumps(r["sources"])))
            return cur.rowcount == 1

    def list(self, run_id: str | None = None) -> list[dict]:
        q = ("SELECT id::text, event_key, run_id, action_id, ts, actor, event_type, args_hash, payload, sources"
             " FROM audit_events {} ORDER BY seq")
        with self._conn.cursor() as cur:
            if run_id is None:
                cur.execute(q.format(""))
            else:
                cur.execute(q.format("WHERE run_id = %s"), (run_id,))
            rows = cur.fetchall()
        out = []
        for row in rows:
            d = dict(zip(FIELDS, row))
            d["ts"] = d["ts"].astimezone(timezone.utc).isoformat(timespec="microseconds")
            out.append(d)
        return out


# --- queries / sweep -----------------------------------------------------------------------------------
def unmatched_executions(events: Iterable[dict]) -> list[dict]:
    """Executions without an approval with the same run_id, action_id and args_hash. Should be []."""
    events = list(events)
    ok = {(e["run_id"], e["action_id"], e["args_hash"]) for e in events if e["event_type"] == "approval"}
    return [e for e in events if e["event_type"] == "execution"
            and (e["run_id"], e["action_id"], e["args_hash"]) not in ok]


UNMATCHED_SQL = """SELECT x.* FROM audit_events x WHERE x.event_type = 'execution' AND NOT EXISTS (
  SELECT 1 FROM audit_events a WHERE a.event_type = 'approval' AND a.run_id = x.run_id
  AND a.action_id = x.action_id AND a.args_hash = x.args_hash)"""


def sweep_expired(log: AuditLog, reason: str = "process restarted before an answer") -> int:
    """Startup: every staged_action with no approval/rejection/expired gets an `expired` event (actor system)."""
    events = log.list()
    done = {(e["run_id"], e["action_id"]) for e in events if e["event_type"] in OUTCOME_TYPES}
    n = 0
    for e in events:
        if e["event_type"] == "staged_action" and (e["run_id"], e["action_id"]) not in done:
            n += log.append(make_event(e["run_id"], "expired", e["action_id"], actor="system",
                                       action_id=e["action_id"], args_hash=e["args_hash"],
                                       payload={"reason": reason, "tool": e["payload"].get("tool")}))
            done.add((e["run_id"], e["action_id"]))
    return n


_DEFAULT: AuditLog | None = None
_DEFAULT_LOCK = threading.Lock()


def default_log() -> AuditLog:
    """Process-wide log from REIMBURSE_AUDIT_LOG (JSONL path) or DATABASE_URL; opened once, swept at open.
    Without either, a fresh in-memory log per call (tests, scripted runs)."""
    global _DEFAULT
    path, dsn = os.environ.get("REIMBURSE_AUDIT_LOG"), os.environ.get("REIMBURSE_AUDIT_DATABASE_URL")
    if not path and not dsn:
        return JsonlAuditLog()
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = PostgresAuditLog(dsn) if dsn else JsonlAuditLog(path)
            sweep_expired(_DEFAULT)
        return _DEFAULT


def _file_hash(path: Path) -> str | None:
    try:
        return sha256(path.read_text(encoding="utf-8"))
    except OSError:
        return None


@functools.lru_cache(maxsize=1)
def _git_sha() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


@functools.lru_cache(maxsize=1)
def _fixture_hash() -> str | None:
    root = Path(os.environ.get("REIMBURSE_BUDDY_DATA") or Path(__file__).resolve().parents[2] / "data")
    if not root.is_dir():
        return None
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.json")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


@functools.lru_cache(maxsize=1)
def _policy_version() -> str | None:
    try:
        from reimburse_buddy import rules  # type: ignore

        v = getattr(rules, "POLICY_VERSION", None)
        if v:
            return str(v)
    except Exception:
        pass
    root = Path(os.environ.get("REIMBURSE_BUDDY_DATA") or Path(__file__).resolve().parents[2] / "data")
    return _file_hash(root / "policy.json")


def run_versions(prompt_path: Path | None = None, model: Any = None) -> dict:
    """Run-level versions for the `request` event (recorded once per run)."""
    name = getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__
    return {"git_sha": _git_sha(), "prompt_sha256": _file_hash(prompt_path) if prompt_path else None,
            "model": str(name) if model is not None else None,
            "provider": type(model).__module__.split(".")[0] if model is not None else None,
            "policy_version": _policy_version(), "fixture_sha256": _fixture_hash()}


# --- executor --------------------------------------------------------------------------------------------
async def execute_approved(log: AuditLog, run_id: str, action_id: str, tool: str, args: dict, run) -> dict:
    """The only path to a side effect. Runs `run(args)` iff the log holds an approval for (run_id, action_id)
    with the same args_hash as `args` and no execution yet. Logs `execution` (or `error`) afterwards.
    Returns {"status": "executed"|"refused"|"duplicate"|"failed", ...}."""
    h = args_hash(tool, args)
    events = [e for e in log.list(run_id) if e["action_id"] == action_id]
    if any(e["event_type"] == "execution" for e in events):
        return {"status": "duplicate", "reason": "action_id already executed"}
    approved = [e for e in events if e["event_type"] == "approval"]
    if not approved:
        log.append(make_event(run_id, "refusal", f"{action_id}:no_approval", actor="system", action_id=action_id,
                              args_hash=h, payload={"tool": tool, "reason": "no approval logged"}))
        return {"status": "refused", "reason": "no approval logged for this action_id"}
    if approved[-1]["args_hash"] != h:
        log.append(make_event(run_id, "refusal", f"{action_id}:args_changed", actor="system", action_id=action_id,
                              args_hash=h, payload={"tool": tool, "reason": "args differ from the approved args",
                                                    "approved_args_hash": approved[-1]["args_hash"]}))
        return {"status": "refused", "reason": "args differ from what the reviewer approved"}
    try:
        result = await run(args)
    except Exception as exc:
        log.append(make_event(run_id, "error", f"{action_id}:execution", actor="system", action_id=action_id,
                              args_hash=h, payload={"tool": tool, "error": str(exc)}))
        return {"status": "failed", "error": str(exc)}
    log.append(make_event(run_id, "execution", action_id, actor="system", action_id=action_id, args_hash=h,
                          payload={"tool": tool, "args": args, **output_summary(result)},
                          sources=source_ids(args, result)))
    return {"status": "executed", "result": result}
