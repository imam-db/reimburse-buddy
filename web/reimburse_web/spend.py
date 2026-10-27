"""Global daily LLM budget, on its own ledger (never the audit log).

One cap for the whole web process, not one per claim: a demo left open overnight cannot spend more than
`LLM_DAILY_BUDGET_USD` in one Asia/Jakarta day. The day boundary is Asia/Jakarta, which is UTC+07:00 all
year (no daylight saving), so a fixed offset is exact and needs no tzdata on Windows.

How a run is charged:

1. Before the worker starts, `reserve()` writes a `reserve` row for the worst case of one run
   (`REIMBURSE_AGENT_MAX_TOKENS` tokens all at the output price) and refuses the run when
   `committed + worst case > budget`. Check and insert happen under one lock (a file lock for the JSONL
   ledger, `pg_advisory_xact_lock` for Postgres), so two concurrent starts cannot both take the last slot.
2. When the worker reports tokens (`final`, or `error`/`token_limit`), `settle()` writes a `settle` row
   with the real cost.
3. A run that ends without a token report (cancel, deadline, crash) keeps its whole reservation: the
   ledger fails safe rather than assuming the money was not spent.

Committed for a day = every settle row, plus the reserve row of each run that never settled. The ledger
is append-only; the app role needs INSERT + SELECT only (agent/migrations/005_llm_spend.sql).

Prices come from the environment, never from code: they change, and a stale hard-coded price would
silently under- or over-charge the cap. In production a missing budget or price refuses LLM runs (and
fails startup); outside production an unconfigured ledger is simply off, so the demo and the tests run
without a budget.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

MILLION = Decimal(1_000_000)
JAKARTA = timezone(timedelta(hours=7))  # WIB; Indonesia has no daylight saving
PRODUCTION_ENVS = frozenset({"prod", "production"})
LOCK_TIMEOUT_S = 5.0
STALE_LOCK_S = 10.0


class BudgetRefused(RuntimeError):
    """The run would push today's spend over the budget (or the ledger is unusable)."""


class SpendConfigError(RuntimeError):
    """Production is missing the budget or a price, so no LLM run may be charged."""


@dataclass(frozen=True)
class Prices:
    """USD per million tokens."""

    miss: Decimal
    hit: Decimal
    output: Decimal


def is_production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() in PRODUCTION_ENVS


def _money(name: str) -> Decimal | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value >= 0 else None


def budget() -> Decimal | None:
    return _money("LLM_DAILY_BUDGET_USD")


def prices() -> Prices | None:
    miss = _money("LLM_PRICE_INPUT_MISS_PER_MTOK")
    hit = _money("LLM_PRICE_INPUT_HIT_PER_MTOK")
    output = _money("LLM_PRICE_OUTPUT_PER_MTOK")
    if miss is None or hit is None or output is None:
        return None
    return Prices(miss, hit, output)


def configured() -> bool:
    """A budget and all three prices: without them nothing can be charged, so nothing runs in prod."""
    return budget() is not None and prices() is not None


def missing() -> list[str]:
    """The env var names a production deploy still needs (empty when configured)."""
    out = []
    if budget() is None:
        out.append("LLM_DAILY_BUDGET_USD")
    for name in ("LLM_PRICE_INPUT_MISS_PER_MTOK", "LLM_PRICE_INPUT_HIT_PER_MTOK", "LLM_PRICE_OUTPUT_PER_MTOK"):
        if _money(name) is None:
            out.append(name)
    return out


def startup_check() -> None:
    """In production, refuse to serve an app that cannot charge (or cap) a model call."""
    if not is_production():
        return
    gaps = missing()
    if gaps:
        raise SpendConfigError(
            "APP_ENV=prod requires the daily LLM budget and the token prices, otherwise no run could be "
            f"capped or charged. Missing: {', '.join(gaps)}. See the README env table.")


def today(now: float | None = None) -> str:
    """The Asia/Jakarta calendar day of `now` (epoch seconds), as YYYY-MM-DD."""
    stamp = time.time() if now is None else now
    return datetime.fromtimestamp(stamp, JAKARTA).date().isoformat()


def day_of(stamp: datetime) -> str:
    return stamp.astimezone(JAKARTA).date().isoformat()


def cost_of(tokens: dict | None, price: Prices | None = None) -> Decimal:
    """Real cost of one run from the provider's token counts (input hit/miss split, as graph.message_usage).

    A provider that reports only `input` is charged every input token at the miss price."""
    p = price or prices()
    if p is None:
        return Decimal(0)
    tokens = tokens or {}
    inp = int(tokens.get("input") or 0)
    hit = min(int(tokens.get("cache_hit") or 0), inp)
    miss = max(0, inp - hit)
    out = int(tokens.get("output") or 0)
    return (Decimal(miss) * p.miss + Decimal(hit) * p.hit + Decimal(out) * p.output) / MILLION


def worst_case_usd(max_tokens: int | None = None, price: Prices | None = None) -> Decimal:
    """The most one run can cost: every budgeted token billed at the output price.

    `REIMBURSE_AGENT_MAX_TOKENS` counts cache-miss input plus output, so charging all of it as output
    (the most expensive rate) is an upper bound and never an under-estimate."""
    p = price or prices()
    if p is None:
        return Decimal(0)
    n = int(max_tokens if max_tokens is not None else os.environ.get("REIMBURSE_AGENT_MAX_TOKENS", "250000"))
    return Decimal(max(0, n)) * p.output / MILLION


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


# --- committed spend ---------------------------------------------------------------------------
def committed_of(rows: list[dict], day: str) -> Decimal:
    """Settled spend plus the reservation of every run that never settled."""
    settled: dict[str, Decimal] = {}
    reserved: dict[str, Decimal] = {}
    for row in rows:
        if row.get("day") != day:
            continue
        amount = Decimal(str(row.get("usd") or "0"))
        if row.get("kind") == "settle":
            settled[row["run_id"]] = settled.get(row["run_id"], Decimal(0)) + amount
        elif row.get("kind") == "reserve":
            reserved[row["run_id"]] = amount
    total = sum(settled.values(), Decimal(0))
    for run_id, amount in reserved.items():
        if run_id not in settled:
            total += amount
    return total


# --- ledgers -----------------------------------------------------------------------------------
class Ledger(Protocol):
    def reserve(self, run_id: str, usd: Decimal, day: str) -> None: ...
    def settle(self, run_id: str, usd: Decimal, day: str, tokens: dict) -> None: ...
    def committed(self, day: str) -> Decimal: ...


class JsonlSpendLedger:
    """Append-only JSONL with an exclusive lock file, for dev and tests (one machine, several processes).

    The lock file makes the read-check-append sequence atomic across processes; the thread lock does the
    same inside one process."""

    def __init__(self, path: str | Path, budget_usd: Decimal):
        self.path = Path(path)
        self.budget = budget_usd
        self._lock = threading.RLock()

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line (killed process) must not block every later run
        return rows

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        deadline = time.time() + LOCK_TIMEOUT_S
        fd = None
        with self._lock:
            while fd is None:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    try:
                        if time.time() - lock_path.stat().st_mtime > STALE_LOCK_S:
                            lock_path.unlink()  # a crashed process left it behind
                            continue
                    except FileNotFoundError:
                        continue
                    if time.time() > deadline:
                        raise BudgetRefused("Ledger sibuk, coba lagi. / The spend ledger is busy.")
                    time.sleep(0.02)
            try:
                yield
            finally:
                os.close(fd)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _append(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _row(self, run_id: str, kind: str, usd: Decimal, day: str, tokens: dict | None = None) -> dict:
        return {"ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"), "day": day,
                "run_id": run_id, "kind": kind, "usd": str(_quantize(usd)), "tokens": tokens or {}}

    def committed(self, day: str) -> Decimal:
        with self._locked():
            return committed_of(self._rows(), day)

    def reserve(self, run_id: str, usd: Decimal, day: str) -> None:
        with self._locked():
            spent = committed_of(self._rows(), day)
            if spent + usd > self.budget:
                raise BudgetRefused(f"spent {spent} + {usd} > budget {self.budget}")
            self._append(self._row(run_id, "reserve", usd, day))

    def settle(self, run_id: str, usd: Decimal, day: str, tokens: dict) -> None:
        with self._locked():
            self._append(self._row(run_id, "settle", usd, day, tokens))


class PostgresSpendLedger:
    """Same contract on Postgres (agent/migrations/005_llm_spend.sql), app role INSERT + SELECT only.

    Check and reserve run in one transaction under `pg_advisory_xact_lock`, so concurrent web instances
    share one cap. Connects with prepare_threshold=None like audit.py (safe behind a pooler)."""

    ADVISORY_KEY = 0x1E4B_2026  # any constant works; it only has to be shared by every instance

    def __init__(self, dsn: str, budget_usd: Decimal):
        import psycopg  # optional dependency, imported only when used

        self.budget = budget_usd
        self._conn = psycopg.connect(dsn, autocommit=True, prepare_threshold=None)

    _COMMITTED = """
        SELECT COALESCE(SUM(usd), 0) FROM llm_spend
         WHERE day = %(day)s
           AND (kind = 'settle'
                OR (kind = 'reserve'
                    AND run_id NOT IN (SELECT run_id FROM llm_spend WHERE day = %(day)s AND kind = 'settle')))
    """

    def committed(self, day: str) -> Decimal:
        with self._conn.cursor() as cur:
            cur.execute(self._COMMITTED, {"day": day})
            return Decimal(str(cur.fetchone()[0]))

    def reserve(self, run_id: str, usd: Decimal, day: str) -> None:
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (self.ADVISORY_KEY,))
                cur.execute(self._COMMITTED, {"day": day})
                spent = Decimal(str(cur.fetchone()[0]))
                if spent + usd > self.budget:
                    raise BudgetRefused(f"spent {spent} + {usd} > budget {self.budget}")
                cur.execute(
                    "INSERT INTO llm_spend (day, run_id, kind, usd, tokens) VALUES (%s, %s, 'reserve', %s, '{}'::jsonb)",
                    (day, run_id, str(_quantize(usd))))

    def settle(self, run_id: str, usd: Decimal, day: str, tokens: dict) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO llm_spend (day, run_id, kind, usd, tokens) VALUES (%s, %s, 'settle', %s, %s::jsonb)",
                (day, run_id, str(_quantize(usd)), json.dumps(tokens or {})))


_STATE: dict = {}
_STATE_LOCK = threading.Lock()


def ledger_path() -> Path:
    explicit = os.environ.get("REIMBURSE_LLM_SPEND_LEDGER")
    if explicit:
        return Path(explicit)
    root = os.environ.get("REIMBURSE_PLAN_STORE")
    base = Path(root) if root else Path(__file__).resolve().parents[2] / "agent" / ".plans"
    return base / "llm_spend.jsonl"


def ledger() -> Ledger | None:
    """The process ledger, or None when the cap is not configured (dev, tests)."""
    cap = budget()
    if cap is None or prices() is None:
        return None
    dsn = os.environ.get("REIMBURSE_AUDIT_DATABASE_URL")
    key = (str(ledger_path()), dsn or "", str(cap))
    with _STATE_LOCK:
        if _STATE.get("key") != key:
            _STATE["ledger"] = (PostgresSpendLedger(dsn, cap) if dsn and is_production()
                                else JsonlSpendLedger(ledger_path(), cap))
            _STATE["key"] = key
        return _STATE["ledger"]


def reset() -> None:
    """Forget the cached ledger and its connection (tests, Reset demo)."""
    with _STATE_LOCK:
        _STATE.clear()


def committed(day: str | None = None) -> Decimal:
    led = ledger()
    if led is None:
        return Decimal(0)
    return led.committed(day or today())


def remaining(day: str | None = None) -> Decimal | None:
    cap = budget()
    if cap is None:
        return None
    return max(Decimal(0), cap - committed(day))


def reserve(run_id: str, usd: Decimal | None = None, day: str | None = None) -> Decimal:
    """Reserve the worst case of one run. Raises BudgetRefused when it would cross the cap.

    Returns the amount reserved (0 when the cap is not configured)."""
    led = ledger()
    if led is None:
        return Decimal(0)
    amount = worst_case_usd() if usd is None else usd
    led.reserve(run_id, amount, day or today())
    return amount


def settle(run_id: str, tokens: dict | None, day: str | None = None) -> Decimal:
    """Record the real cost of a finished run. Returns the settled amount (0 when not configured)."""
    led = ledger()
    if led is None:
        return Decimal(0)
    amount = cost_of(tokens)
    led.settle(run_id, amount, day or today(), tokens or {})
    return amount
