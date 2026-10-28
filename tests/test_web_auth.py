"""Email + password login (web/reimburse_web/auth.py, users.py).

With a user store installed (tests use MemoryUserStore) every registered route except /login, /logout and
/healthz needs the signed rb_auth cookie: a browser navigation is redirected to the form, anything else gets
401. Production refuses to start without a user DSN and a stable session key. With no store and no DSN
outside production the gate is off, which is what keeps the rest of the suite green.
"""

import re

import pytest
from conftest import TEST_ACCOUNTS, TEST_PASSWORD, install_test_users, login_as
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, plans, seed_users, users

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

PUBLIC = {"/login", "/logout", "/healthz"}
REVIEWER_EMAIL = TEST_ACCOUNTS["EMP-100"][0]
GENERIC_ERROR = "Email atau kata sandi salah."


@pytest.fixture
def store():
    return install_test_users()


@pytest.fixture
def client(tmp_path, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    monkeypatch.delenv("APP_ENV", raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    auth.reset_attempts()
    yield TestClient(web.app)
    auth.reset_attempts()
    plans.STATE.clear()


def post_login(client, email=REVIEWER_EMAIL, password=TEST_PASSWORD, next_path="/admin"):
    return client.post("/login", data={"email": email, "password": password, "next": next_path},
                       follow_redirects=False)


def _walk(routes):
    """APIRoutes under `routes`, descending into included routers.

    Recent FastAPI keeps `include_router(...)` as a nested `_IncludedRouter` entry (with the router under
    `.original_router`) instead of flattening it into `app.routes`, so a flat scan would silently miss
    the intake, plan, Q&A and upload routes."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "routes"):
            yield from _walk(r.routes)
        elif hasattr(r, "original_router"):
            yield from _walk(r.original_router.routes)


def route_paths():
    """Every APIRoute with its path parameters filled in, so the middleware sees a real path."""
    return sorted({re.sub(r"\{[^}]+\}", "X", r.path) for r in _walk(web.app.routes)})


def test_the_route_list_is_not_empty():
    paths = route_paths()
    assert "/admin" in paths and "/admin/claims/X" in paths and "/employee/qa" in paths


def test_every_route_except_login_logout_healthz_needs_the_cookie(client):
    checked = 0
    for path in route_paths():
        if path in PUBLIC:
            continue
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, (path, r.status_code)
        assert r.headers["location"].startswith("/login?next="), (path, r.headers.get("location"))
        # A form post from a script never gets through either, and never reaches the handler.
        r = client.post(path, data={}, follow_redirects=False)
        assert r.status_code == 401, (path, r.status_code)
        checked += 1
    assert checked >= 20


def test_healthz_is_public_and_does_not_touch_the_log(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"
    assert plans.log().list() == []


# --- the page -----------------------------------------------------------------------------
def test_login_page_is_a_card_with_email_and_password_only(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert 'class="login-card"' in r.text and "Reimburse Buddy" in r.text
    assert 'type="email"' in r.text and 'autocomplete="username"' in r.text
    assert 'type="password"' in r.text and 'autocomplete="current-password"' in r.text
    assert 'for="email"' in r.text and 'for="password"' in r.text and "required" in r.text
    assert 'id="toggle-password"' in r.text and "width=device-width" in r.text
    assert 'name="next"' in r.text and "<select" not in r.text
    assert "Demo terbatas" not in r.text and "EMP-1" not in r.text and "<nav" not in r.text


def test_failed_login_shows_an_alert_and_keeps_the_email_but_not_the_password(client):
    r = post_login(client, password="not-the-password")
    assert r.status_code == 401
    assert 'role="alert"' in r.text and GENERIC_ERROR in r.text
    assert f'value="{REVIEWER_EMAIL}"' in r.text
    assert "not-the-password" not in r.text


# --- login outcomes -----------------------------------------------------------------------
def test_wrong_password_unknown_email_and_inactive_account_get_the_same_message(client, store):
    store.add("EMP-103", "gone@nusantara-niaga.example", "some-long-password-1", "reviewer", is_active=False)
    bodies = []
    for email, pw in ((REVIEWER_EMAIL, "not-the-password"), ("nobody@nusantara-niaga.example", TEST_PASSWORD),
                      ("gone@nusantara-niaga.example", "some-long-password-1")):
        r = post_login(client, email=email, password=pw)
        assert r.status_code == 401 and GENERIC_ERROR in r.text, email
        assert auth.COOKIE not in r.cookies
        bodies.append(re.sub(r'value="[^"]*"', "", r.text))
    assert bodies[0] == bodies[1] == bodies[2]
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_correct_login_reaches_admin_and_nav_names_the_reviewer(client, store):
    r = post_login(client, email="  " + REVIEWER_EMAIL.upper() + " ")  # strip + lowercase
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert auth.COOKIE in r.cookies
    assert store.logins == [1]  # touch_login ran for EMP-100's row
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Masuk sebagai" in page.text and "Dewi Anggraini" in page.text
    assert 'action="/logout"' in page.text


def test_the_cookie_carries_the_employee_id_and_role(client):
    r = post_login(client, email=TEST_ACCOUNTS["EMP-102"][0])
    session = auth.parse(r.cookies[auth.COOKIE])
    assert session.reviewer == "EMP-102" and session.role == "finance"


def test_cookie_is_httponly_and_samesite_lax(client):
    header = post_login(client).headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header and "secure" not in header


def test_failures_are_audited_without_the_email_or_password(client):
    post_login(client, email=REVIEWER_EMAIL, password="not-the-password")
    post_login(client, email="nobody@nusantara-niaga.example", password="another-wrong-one")
    denied = [e for e in plans.log().list("auth") if e["event_type"] == "access_denied"]
    assert len(denied) >= 1
    for event in denied:
        assert event["actor"] == "system" and event["payload"]["reason"] == "bad_credentials"
    dump = str(plans.log().list())
    for secret in (REVIEWER_EMAIL, "nobody@", "not-the-password", "another-wrong-one", TEST_PASSWORD,
                   "nusantara-niaga"):
        assert secret not in dump, secret


def test_rate_limit_blocks_the_sixth_failure(client):
    for _ in range(auth.MAX_FAILS_PER_MINUTE):
        assert post_login(client, password="wrong").status_code == 401
    r = post_login(client, password="wrong")
    assert r.status_code == 401 and "Terlalu banyak percobaan" in r.text
    # Even the right password is refused while the limit holds.
    assert "Terlalu banyak percobaan" in post_login(client).text
    reasons = [e["payload"]["reason"] for e in plans.log().list("auth")]
    assert "rate_limited" in reasons


def test_a_user_store_outage_is_a_503_page_not_a_crash(client, store, monkeypatch):
    def boom(email):
        raise RuntimeError("db down: postgresql://secret")

    monkeypatch.setattr(store, "find_by_email", boom)
    r = post_login(client)
    assert r.status_code == 503 and "Layanan masuk sedang bermasalah" in r.text
    assert "secret" not in r.text


def test_open_redirect_next_is_ignored(client):
    for bad in ("//evil.example/x", "https://evil.example", "/\\evil", "javascript:alert(1)"):
        r = post_login(client, next_path=bad)
        assert r.headers["location"] == "/", bad
    assert client.get("/login?next=//evil.example").text.count('value="//evil.example"') == 0
    r = post_login(client, next_path="/admin/plans")
    assert r.headers["location"] == "/admin/plans"


def test_logout_clears_the_cookie(client):
    post_login(client)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_expired_and_forged_cookies_are_refused(client):
    client.cookies.set(auth.COOKIE, auth.new_cookie_value("EMP-100", "reviewer", now=0))
    assert client.get("/admin", follow_redirects=False).status_code == 303

    client.cookies.set(auth.COOKIE, "1700000000|EMP-100|reviewer|deadbeef.forged")
    assert client.get("/admin", follow_redirects=False).status_code == 303

    # A validly signed cookie with an unknown role, or in the old three-part shape, is refused too.
    client.cookies.set(auth.COOKIE, auth.new_cookie_value("EMP-100", "admin"))
    assert client.get("/admin", follow_redirects=False).status_code == 303
    client.cookies.set(auth.COOKIE, auth.sign("1700000000|EMP-100|deadbeef"))
    assert client.get("/admin", follow_redirects=False).status_code == 303


# --- roles --------------------------------------------------------------------------------
def test_employee_role_is_sent_to_their_claims_and_cannot_open_reviewer_pages(client):
    r = login_as(client, "EMP-001", next_path="/admin")
    assert r.status_code == 303 and r.headers["location"] == "/employee/claims"
    for path in ("/admin", "/admin/plans", "/admin/intake", "/admin/claims/CLM-2026-0103", "/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/employee/claims", path
    for path in ("/admin/reset", "/admin/batch/approve", "/admin/plans/req-x/decide"):
        assert client.post(path, data={}, follow_redirects=False).status_code == 403, path
    assert client.get("/employee/claims", follow_redirects=False).status_code == 200


@pytest.mark.parametrize("emp", ["EMP-100", "EMP-101", "EMP-102"])
def test_reviewer_and_finance_roles_open_the_admin_pages(client, emp):
    assert login_as(client, emp).headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/plans").status_code == 200
    assert auth.reviewer_of(client) == emp  # the self-approval checks still see the employee_id


# --- gate off -----------------------------------------------------------------------------
def test_gate_is_off_without_a_store_or_dsn(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog()
    assert not auth.enabled()
    c = TestClient(web.app)
    assert c.get("/admin").status_code == 200
    assert "Gerbang login mati" in c.get("/login").text
    assert auth.reviewer_of(None) == auth.FALLBACK_REVIEWER


def test_a_configured_user_dsn_turns_the_gate_on_without_connecting(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("REIMBURSE_AUTH_DATABASE_URL", "postgresql://user@db.example:5432/reimburse")
    assert auth.enabled()


def test_the_auth_dsn_wins_over_the_audit_dsn(monkeypatch):
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://audit@db.example/x")
    assert users.user_dsn() == "postgresql://audit@db.example/x"
    monkeypatch.setenv("REIMBURSE_AUTH_DATABASE_URL", "postgresql://auth@db.example/x")
    assert users.user_dsn() == "postgresql://auth@db.example/x"


# --- password hashes ----------------------------------------------------------------------
def test_hash_format_and_verification():
    h = users.hash_password("a-long-enough-secret")
    scheme, n, r, p, salt, digest = h.split("$")
    assert (scheme, int(n), int(r), int(p)) == ("scrypt", 2 ** 14, 8, 1)
    assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert "a-long-enough-secret" not in h
    assert users.verify_password("a-long-enough-secret", h)
    assert not users.verify_password("a-long-enough-secreT", h)
    assert not users.verify_password("", h)


def test_each_hash_gets_a_fresh_salt():
    a, b = users.hash_password("same-password-12"), users.hash_password("same-password-12")
    assert a != b and a.split("$")[4] != b.split("$")[4]
    assert users.verify_password("same-password-12", a) and users.verify_password("same-password-12", b)


@pytest.mark.parametrize("bad", ["", "plain", "md5$1$2$3$aa$bb", "scrypt$x$8$1$zz$zz", "scrypt$16384$8$1$00$"])
def test_a_malformed_stored_hash_never_verifies(bad):
    assert users.verify_password("anything", bad) is False


def test_an_unknown_email_still_runs_a_hash_check(monkeypatch):
    calls = []
    real = users.verify_password

    def spy(password, stored):
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(users, "verify_password", spy)
    assert users.authenticate(users.MemoryUserStore(), "nobody@x.example", "whatever-pass") is None
    assert calls == [users.dummy_hash()]


def test_memory_store_normalizes_emails():
    store = users.MemoryUserStore()
    store.add("EMP-100", "Dewi@Example.com", "a-long-enough-secret")
    assert store.find_by_email("  DEWI@example.COM ").employee_id == "EMP-100"
    assert store.find_by_email("other@example.com") is None


def test_seed_script_takes_the_email_from_employees_json_and_no_password_argument():
    assert seed_users.employee_email("EMP-100") == REVIEWER_EMAIL
    with pytest.raises(SystemExit):
        seed_users.employee_email("EMP-999")
    with pytest.raises(SystemExit):  # a password flag does not exist
        seed_users.main(["--dsn", "x", "--employee", "EMP-100", "--role", "reviewer", "--password", "x"])


# --- production startup -------------------------------------------------------------------
def _fake_postgres(monkeypatch):
    class FakePostgresLog:
        def __init__(self, dsn):
            self.dsn = dsn

        def append(self, event):
            return True

        def list(self, run_id=None):
            return []

    monkeypatch.setattr(A, "PostgresAuditLog", FakePostgresLog)
    monkeypatch.setattr(A, "_PROD_LOG", None)


@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://user@db.example:5432/reimburse")
    # Production also defaults the plan state to Postgres (web/reimburse_web/plan_state.py). These tests are
    # about the login and audit gates, so the plan store stays on disk here; the plan-state startup check
    # has its own tests in test_web_plan_state.py.
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "file")
    _fake_postgres(monkeypatch)
    yield monkeypatch
    plans.STATE.clear()


def test_prod_without_a_user_dsn_fails_startup(prod):
    prod.delenv("REIMBURSE_AUDIT_DATABASE_URL")
    prod.delenv("REIMBURSE_AUTH_DATABASE_URL", raising=False)
    prod.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    with pytest.raises(auth.AuthConfigError) as exc:
        auth.startup_check()
    assert "REIMBURSE_AUTH_DATABASE_URL" in str(exc.value)


def test_prod_with_an_unreachable_user_database_fails_startup_without_echoing_the_dsn(prod):
    prod.setenv("REIMBURSE_AUTH_DATABASE_URL", "postgresql://user:hunter2@db.example:5432/reimburse")
    prod.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)

    class Down:
        def __init__(self, dsn):
            raise OSError("connection refused: " + dsn)

    prod.setattr(users, "PostgresUserStore", Down)
    with pytest.raises(auth.AuthConfigError) as exc:
        auth.startup_check()
    assert "hunter2" not in str(exc.value) and "user database" in str(exc.value)


def test_prod_without_session_key_fails_startup(prod, store):
    prod.delenv("REIMBURSE_DEMO_SESSION_KEY", raising=False)
    with pytest.raises(auth.AuthConfigError) as exc:
        with TestClient(web.app):
            pass
    assert "REIMBURSE_DEMO_SESSION_KEY" in str(exc.value)


def test_prod_connects_to_the_user_database_once_with_the_dsn(prod):
    dsn = "postgresql://user@db.example:5432/reimburse"
    prod.setenv("REIMBURSE_AUTH_DATABASE_URL", dsn)
    prod.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    opened = []

    class FakeUsers(users.MemoryUserStore):
        def __init__(self, dsn):
            super().__init__()
            opened.append(dsn)

    prod.setattr(users, "PostgresUserStore", FakeUsers)
    auth.startup_check()
    auth.startup_check()
    assert opened == [dsn]


def test_prod_with_everything_set_serves_the_app(prod, store):
    prod.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    # The LLM budget and its prices are also required in production (web/reimburse_web/spend.py).
    prod.setenv("LLM_DAILY_BUDGET_USD", "2.00")
    prod.setenv("LLM_PRICE_INPUT_MISS_PER_MTOK", "0.27")
    prod.setenv("LLM_PRICE_INPUT_HIT_PER_MTOK", "0.07")
    prod.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "1.10")
    with TestClient(web.app) as c:
        assert c.get("/healthz").text == "ok"
        assert c.get("/", follow_redirects=False).status_code == 303
        assert login_as(c, "EMP-100").status_code == 303
        assert c.get("/").status_code == 200
        assert "secure" in login_as(c, "EMP-100").headers["set-cookie"].lower()
