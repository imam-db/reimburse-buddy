"""Global daily LLM budget (web/reimburse_web/spend.py).

One cap per Asia/Jakarta day, on a ledger of its own: a run reserves its worst case before the worker
starts and settles the real cost from the worker's token report. A run that never reports keeps its
reservation. In production a missing budget or price refuses LLM runs (and fails startup).

No model is called here: the "worker" is a tiny python process that prints the same JSON line the real
investigate_worker.py prints.
"""

import contextlib
import json
import sys
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from conftest import install_test_users, login_as
from reimburse_web import app as web
from reimburse_web import plans, spend
from reimburse_web.agent_bridge import Investigator

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

HERO = "CLM-2026-0110"
FINAL = {"type": "final", "note": "done", "tokens": {"input": 1_000_000, "output": 100_000}}
TOKEN_LIMIT = {"type": "error", "code": "token_limit", "message": "stopped",
               "tokens": {"input": 200_000, "output": 50_000}}


def configure(monkeypatch, budget="1.00", output="1.00", miss="0.10", hit="0.01", max_tokens="100000"):
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", budget)
    monkeypatch.setenv("LLM_PRICE_INPUT_MISS_PER_MTOK", miss)
    monkeypatch.setenv("LLM_PRICE_INPUT_HIT_PER_MTOK", hit)
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", output)
    monkeypatch.setenv("REIMBURSE_AGENT_MAX_TOKENS", max_tokens)


@pytest.fixture
def ledger_file(tmp_path, monkeypatch):
    path = tmp_path / "llm_spend.jsonl"
    monkeypatch.setenv("REIMBURSE_LLM_SPEND_LEDGER", str(path))
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    for name in ("APP_ENV", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    spend.reset()
    yield path
    spend.reset()


@pytest.fixture
def client(ledger_file, tmp_path, monkeypatch):
    configure(monkeypatch)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    yield TestClient(web.app)
    web.reset_all()
    plans.STATE.clear()


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- pricing -------------------------------------------------------------------------------
def test_worst_case_is_every_token_at_the_output_price(monkeypatch):
    configure(monkeypatch, budget="10", output="2.00", max_tokens="250000")
    assert spend.worst_case_usd() == Decimal("0.5")  # 250k tokens * $2.00/Mtok
    assert spend.worst_case_usd(1_000_000) == Decimal("2.0")


def test_cost_of_splits_cache_hits_and_misses(monkeypatch):
    configure(monkeypatch, miss="0.10", hit="0.01", output="1.00")
    # 1M input of which 600k hit, plus 100k output
    tokens = {"input": 1_000_000, "cache_hit": 600_000, "output": 100_000}
    assert spend.cost_of(tokens) == Decimal("0.04") + Decimal("0.006") + Decimal("0.1")
    # A provider that reports only input is charged the miss price.
    assert spend.cost_of({"input": 1_000_000, "output": 0}) == Decimal("0.1")


# --- ledger --------------------------------------------------------------------------------
def test_reserve_then_settle_replaces_the_reservation(monkeypatch, ledger_file):
    configure(monkeypatch, budget="1.00", output="1.00", max_tokens="100000")  # worst case 0.10
    spend.reserve("run-1")
    assert spend.committed() == Decimal("0.10")  # the reservation counts while the run is open

    spend.settle("run-1", {"input": 100_000, "output": 0})  # real cost 0.01
    assert spend.committed() == Decimal("0.01")
    assert spend.remaining() == Decimal("0.99")
    assert [r["kind"] for r in rows(ledger_file)] == ["reserve", "settle"]


def test_a_run_without_a_token_report_keeps_its_reservation(monkeypatch, ledger_file):
    configure(monkeypatch, budget="1.00", max_tokens="100000")  # worst case 0.10
    spend.reserve("run-cancelled")
    assert spend.committed() == Decimal("0.10")  # fails safe: assumed spent
    assert [r["kind"] for r in rows(ledger_file)] == ["reserve"]


def test_reservation_is_refused_at_the_cap(monkeypatch, ledger_file):
    configure(monkeypatch, budget="0.15", output="1.00", max_tokens="100000")  # worst case 0.10
    spend.reserve("run-1")
    with pytest.raises(spend.BudgetRefused):
        spend.reserve("run-2")  # 0.10 + 0.10 > 0.15
    assert len([r for r in rows(ledger_file) if r["kind"] == "reserve"]) == 1


def test_two_ledger_instances_cannot_both_take_the_last_slot(monkeypatch, ledger_file):
    """Two processes (two ledger objects, one lock file) must not both reserve the last slot."""
    configure(monkeypatch, budget="0.15", output="1.00", max_tokens="100000")
    first = spend.JsonlSpendLedger(ledger_file, Decimal("0.15"))
    second = spend.JsonlSpendLedger(ledger_file, Decimal("0.15"))
    first.reserve("run-a", Decimal("0.10"), spend.today())
    with pytest.raises(spend.BudgetRefused):
        second.reserve("run-b", Decimal("0.10"), spend.today())


def test_the_day_boundary_is_asia_jakarta(monkeypatch, ledger_file):
    configure(monkeypatch, budget="0.15", output="1.00", max_tokens="100000")
    # 2026-10-26 16:59 UTC is 23:59 WIB; one minute later it is the next Jakarta day.
    late = spend.today(1_793_033_940)
    assert late == "2026-10-26"
    assert spend.today(1_793_034_000) == "2026-10-27"

    spend.reserve("run-1", day=late)
    assert spend.committed(late) == Decimal("0.10")
    assert spend.committed("2026-10-27") == Decimal(0)  # a new day starts from zero
    spend.reserve("run-2", day="2026-10-27")  # so the same slot is free again


def test_the_ledger_is_not_the_audit_log(client, ledger_file, tmp_path):
    spend.reserve("run-1")
    assert ledger_file.exists()
    assert not (tmp_path / "audit.jsonl").exists()


# --- web wiring ------------------------------------------------------------------------------
def _stub_worker(monkeypatch, inv, event: dict):
    script = f"import json;print(json.dumps({event!r}))"
    monkeypatch.setattr(inv, "command", lambda claim_id: [sys.executable, "-c", script])
    return inv


def test_a_run_reserves_then_settles_from_the_worker_report(client, monkeypatch, ledger_file):
    inv = _stub_worker(monkeypatch, Investigator(max_per_min=10, fake_script="x"), FINAL)
    monkeypatch.setattr(web, "INVESTIGATOR", inv)
    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    assert r.status_code == 303
    run = inv.runs[r.headers["location"].rsplit("/", 1)[1]]
    run.wait(30)

    kinds = [x["kind"] for x in rows(ledger_file)]
    assert kinds == ["reserve", "settle"]
    # 1M input at the miss price + 100k output at the output price
    assert Decimal(rows(ledger_file)[1]["usd"]) == Decimal("0.2")
    assert rows(ledger_file)[1]["tokens"]["output"] == 100_000


def test_a_token_limit_error_still_settles(client, monkeypatch, ledger_file):
    inv = _stub_worker(monkeypatch, Investigator(max_per_min=10, fake_script="x"), TOKEN_LIMIT)
    monkeypatch.setattr(web, "INVESTIGATOR", inv)
    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    run = inv.runs[r.headers["location"].rsplit("/", 1)[1]]
    run.wait(30)
    assert [x["kind"] for x in rows(ledger_file)] == ["reserve", "settle"]


def test_a_crashed_worker_keeps_the_reservation(client, monkeypatch, ledger_file):
    inv = Investigator(max_per_min=10, fake_script="x")
    monkeypatch.setattr(inv, "command", lambda claim_id: [sys.executable, "-c", "raise SystemExit(3)"])
    monkeypatch.setattr(web, "INVESTIGATOR", inv)
    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    run = inv.runs[r.headers["location"].rsplit("/", 1)[1]]
    run.wait(30)
    assert [x["kind"] for x in rows(ledger_file)] == ["reserve"]  # no token report: assume spent


def test_a_run_crossing_midnight_settles_on_the_day_it_reserved(client, monkeypatch, ledger_file):
    """The reservation must be replaced by the settle row, whatever the clock says at the end: otherwise
    the old day keeps a reservation for ever and the new day is charged twice."""
    monkeypatch.setattr(spend, "today", lambda now=None: "2026-10-26")
    inv = Investigator(max_per_min=10, fake_script="x")
    slow = f"import json,time;time.sleep(0.6);print(json.dumps({FINAL!r}))"
    monkeypatch.setattr(inv, "command", lambda claim_id: [sys.executable, "-c", slow])
    monkeypatch.setattr(web, "INVESTIGATOR", inv)
    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    run = inv.runs[r.headers["location"].rsplit("/", 1)[1]]

    monkeypatch.setattr(spend, "today", lambda now=None: "2026-10-27")  # midnight WIB passes mid-run
    run.wait(30)

    assert [(x["kind"], x["day"]) for x in rows(ledger_file)] == [("reserve", "2026-10-26"),
                                                                 ("settle", "2026-10-26")]
    assert spend.committed("2026-10-26") == Decimal("0.2")  # settled, not the 0.10 reservation
    assert spend.committed("2026-10-27") == Decimal(0)


def test_done_is_seen_only_after_the_settle_row_is_written(client, monkeypatch, ledger_file):
    """Regression: `done` used to turn true before the settle was written, so a waiter (or the page) could
    read a ledger that still held only the reservation. The settle must land before the run reports done."""
    configure(monkeypatch)
    monkeypatch.setattr(spend, "today", lambda now=None: "2026-10-26")
    real_settle = spend.settle

    def slow_settle(*args, **kwargs):
        time.sleep(0.5)
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(spend, "settle", slow_settle)
    inv = Investigator(max_per_min=10, fake_script="x")
    fast = f"import json;print(json.dumps({FINAL!r}))"
    monkeypatch.setattr(inv, "command", lambda claim_id: [sys.executable, "-c", fast])
    monkeypatch.setattr(web, "INVESTIGATOR", inv)
    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    run = inv.runs[r.headers["location"].rsplit("/", 1)[1]]

    run.wait(30)

    assert run.done
    assert [x["kind"] for x in rows(ledger_file)] == ["reserve", "settle"]


def test_the_investigate_button_refuses_at_the_cap_and_logs_it(client, monkeypatch, ledger_file):
    monkeypatch.setenv("LLM_DAILY_BUDGET_USD", "0.15")  # one worst case (0.10) fits, two do not
    spend.reserve("someone-else")
    monkeypatch.setattr(web, "INVESTIGATOR", Investigator(max_per_min=10, fake_script="x"))

    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    assert r.status_code == 200
    assert "Batas biaya harian demo tercapai" in r.text
    assert "id=budget-exhausted" in r.text
    assert not web.INVESTIGATOR.runs  # nothing was spawned

    limits = [e for e in plans.log().list(f"investigate:{HERO}") if e["event_type"] == "limit"]
    assert limits and limits[0]["payload"]["kind"] == "daily_llm_budget"


def test_the_nav_shows_the_remaining_budget(client):
    assert "Sisa anggaran LLM hari ini: $1.00" in client.get("/admin").text
    spend.reserve("run-1")  # worst case 0.10
    assert "Sisa anggaran LLM hari ini: $0.90" in client.get("/admin").text


def test_without_a_budget_the_demo_runs_uncapped(monkeypatch, tmp_path, ledger_file):
    for name in ("LLM_DAILY_BUDGET_USD", "LLM_PRICE_INPUT_MISS_PER_MTOK",
                 "LLM_PRICE_INPUT_HIT_PER_MTOK", "LLM_PRICE_OUTPUT_PER_MTOK"):
        monkeypatch.delenv(name, raising=False)
    spend.reset()
    assert spend.configured() is False and spend.remaining() is None
    assert spend.reserve("run-1") == Decimal(0)  # nothing charged, nothing written
    assert not ledger_file.exists()


# --- production --------------------------------------------------------------------------------
def _fake_postgres(monkeypatch):
    class FakePostgresLog:
        def __init__(self, dsn):
            pass

        def append(self, event):
            return True

        def list(self, run_id=None):
            return []

    monkeypatch.setattr(A, "PostgresAuditLog", FakePostgresLog)
    monkeypatch.setattr(A, "_PROD_LOG", None)


@pytest.fixture
def prod(ledger_file, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://user@db.example:5432/reimburse")
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    # Production also defaults the plan state to Postgres (web/reimburse_web/plan_state.py). These tests are
    # about the daily cap, so the plan store stays on disk.
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "file")
    _fake_postgres(monkeypatch)

    class FakePostgresSpend(spend.JsonlSpendLedger):
        """Same contract as the Postgres ledger, backed by the JSONL file: no database in the tests."""

        def __init__(self, dsn, budget_usd):
            super().__init__(ledger_file, budget_usd)

    monkeypatch.setattr(spend, "PostgresSpendLedger", FakePostgresSpend)
    spend.reset()
    plans.STATE.clear()
    yield monkeypatch
    plans.STATE.clear()
    spend.reset()


def test_prod_without_a_budget_fails_startup(prod):
    for name in ("LLM_DAILY_BUDGET_USD", "LLM_PRICE_INPUT_MISS_PER_MTOK",
                 "LLM_PRICE_INPUT_HIT_PER_MTOK", "LLM_PRICE_OUTPUT_PER_MTOK"):
        prod.delenv(name, raising=False)
    with pytest.raises(spend.SpendConfigError) as exc:
        with TestClient(web.app):
            pass
    assert "LLM_DAILY_BUDGET_USD" in str(exc.value)


def test_prod_without_a_price_fails_startup(prod):
    configure(prod)
    prod.delenv("LLM_PRICE_OUTPUT_PER_MTOK", raising=False)
    with pytest.raises(spend.SpendConfigError) as exc:
        with TestClient(web.app):
            pass
    assert "LLM_PRICE_OUTPUT_PER_MTOK" in str(exc.value)


def test_prod_with_budget_and_prices_serves_the_app(prod):
    configure(prod)
    with TestClient(web.app, base_url="https://testserver") as c:
        assert login_as(c, "EMP-102", next_path="/").status_code == 303
        assert c.get("/").status_code == 200
        assert "Sisa anggaran LLM hari ini: $1.00" in c.get("/").text


# --- a dropped Postgres connection --------------------------------------------------------------
class _FakeOperationalError(Exception):
    pass


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.bump()
        head = sql.strip().upper()
        if head.startswith("SELECT COALESCE"):
            self._row = (self.conn.spent,)
        elif head.startswith("INSERT"):
            self.conn.inserted.append(params)

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, fake, fail_left):
        self.fake, self.fail_left = fake, fail_left
        self.spent = Decimal(0)
        self.inserted: list = []

    def bump(self):
        if self.fail_left:
            self.fail_left -= 1
            raise self.fake.OperationalError("server closed the connection unexpectedly")

    def cursor(self):
        return _FakeCursor(self)

    def transaction(self):
        return contextlib.nullcontext()

    def close(self):
        pass


class _FakePsycopg:
    """Just enough of the module: connect() hands out connections that may fail their first statement."""

    def __init__(self, fail_first=0, always=False):
        self.OperationalError = _FakeOperationalError
        self.fail_first, self.always, self.conns = fail_first, always, []

    def connect(self, dsn, **kwargs):
        fail = self.fail_first if (not self.conns or self.always) else 0
        conn = _FakeConn(self, fail)
        self.conns.append(conn)
        return conn


def test_postgres_ledger_reconnects_once_after_a_dropped_connection(monkeypatch):
    fake = _FakePsycopg(fail_first=1)  # a suspended Neon endpoint: the first statement fails
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    led = spend.PostgresSpendLedger("postgresql://user@db.example:5432/reimburse", Decimal("1.00"))

    led.reserve("run-1", Decimal("0.10"), "2026-10-26")

    assert len(fake.conns) == 2  # the dead one, then a fresh one
    assert len(fake.conns[1].inserted) == 1 and fake.conns[1].inserted[0][1] == "run-1"


def test_postgres_ledger_refuses_the_run_when_it_stays_unreachable(monkeypatch):
    fake = _FakePsycopg(fail_first=1, always=True)
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    led = spend.PostgresSpendLedger("postgresql://user@db.example:5432/reimburse", Decimal("1.00"))

    with pytest.raises(spend.LedgerUnavailable) as exc:
        led.reserve("run-1", Decimal("0.10"), "2026-10-26")
    assert "Ledger biaya LLM tidak bisa dihubungi" in str(exc.value)
    assert len(fake.conns) == 2  # exactly one retry, not a loop


def test_an_unreachable_ledger_refuses_the_run_instead_of_a_500(prod, monkeypatch):
    configure(prod)

    class DeadLedger:
        def __init__(self, dsn, budget_usd):
            self.budget = budget_usd

        def reserve(self, *a, **kw):
            raise spend.LedgerUnavailable("Ledger biaya LLM tidak bisa dihubungi, jadi tidak ada run "
                                          "yang dijalankan. Coba lagi sebentar lagi.")

        def settle(self, *a, **kw):
            return None

        def committed(self, day):
            raise spend.LedgerUnavailable("down")

    monkeypatch.setattr(spend, "PostgresSpendLedger", DeadLedger)
    spend.reset()
    inv = Investigator(max_per_min=10, fake_script="x")
    monkeypatch.setattr(web, "INVESTIGATOR", inv)

    # https, because in production the session cookie is Secure and TestClient would not send it otherwise
    with TestClient(web.app, base_url="https://testserver") as c:
        login_as(c, "EMP-102")  # production needs the gate
        r = c.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    assert r.status_code == 200  # not a 500
    assert "id=ledger-unavailable" in r.text and "tidak ada run yang dijalankan" in r.text
    assert inv.runs == {}  # nothing was spawned
