"""Demo password gate (web/reimburse_web/auth.py).

With DEMO_PASSWORD set, every registered route except /login, /logout and /healthz needs the signed
rb_auth cookie: a browser navigation is redirected to the form, anything else gets 401. Production
refuses to start without a password, a long-enough password and a stable session key. With the
variable unset outside production the gate is off, which is what keeps the rest of the suite green.
"""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, plans

from reimburse_agent import audit as A
from reimburse_agent.office.store import PlanStore

PW = "correct-horse-battery-staple"
PUBLIC = {"/login", "/logout", "/healthz"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", PW)
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    for name in ("APP_ENV", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = PlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    auth.reset_attempts()
    yield TestClient(web.app)
    auth.reset_attempts()
    plans.STATE.clear()


def login(client, password=PW, reviewer="EMP-100", next_path="/admin"):
    return client.post("/login", data={"password": password, "reviewer": reviewer, "next": next_path},
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


def test_login_form_renders_in_indonesian(client):
    r = client.get("/login")
    assert r.status_code == 200
    for text in ("Kata sandi demo", "Masuk sebagai reviewer", "Data sintetis. Demo terbatas.", "EMP-100"):
        assert text in r.text, text
    assert 'type="password"' in r.text


def test_wrong_password_is_refused_and_audited(client):
    r = login(client, password="not-the-password")
    assert r.status_code == 401
    assert "Kata sandi demo salah" in r.text
    assert auth.COOKIE not in r.cookies
    assert client.get("/admin", follow_redirects=False).status_code == 303

    denied = [e for e in plans.log().list("auth") if e["event_type"] == "access_denied"]
    assert len(denied) == 1
    assert denied[0]["actor"] == "system"
    assert denied[0]["payload"]["reason"] == "bad_password"
    # The password itself never reaches the log.
    assert PW not in str(denied[0]) and "not-the-password" not in str(denied[0])


def test_correct_password_reaches_admin_and_nav_names_the_reviewer(client):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert auth.COOKIE in r.cookies
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Masuk sebagai" in page.text and "Dewi Anggraini" in page.text
    assert 'action="/logout"' in page.text


def test_cookie_is_httponly_and_samesite_lax(client):
    r = login(client)
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header and "secure" not in header


def test_rate_limit_blocks_the_sixth_failure(client):
    for _ in range(auth.MAX_FAILS_PER_MINUTE):
        assert login(client, password="wrong").status_code == 401
    r = login(client, password="wrong")
    assert r.status_code == 401 and "Terlalu banyak percobaan" in r.text
    # Even the right password is refused while the limit holds.
    assert "Terlalu banyak percobaan" in login(client).text
    reasons = [e["payload"]["reason"] for e in plans.log().list("auth")]
    assert "rate_limited" in reasons


def test_open_redirect_next_is_ignored(client):
    for bad in ("//evil.example/x", "https://evil.example", "/\\evil", "javascript:alert(1)"):
        r = login(client, next_path=bad)
        assert r.headers["location"] == "/", bad
    assert client.get("/login?next=//evil.example").text.count('value="//evil.example"') == 0
    r = login(client, next_path="/admin/plans")
    assert r.headers["location"] == "/admin/plans"


def test_logout_clears_the_cookie(client):
    login(client)
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_expired_and_forged_cookies_are_refused(client):
    client.cookies.set(auth.COOKIE, auth.new_cookie_value("EMP-100", now=0))
    assert client.get("/admin", follow_redirects=False).status_code == 303

    client.cookies.set(auth.COOKIE, "1700000000|EMP-100|deadbeef.forged")
    assert client.get("/admin", follow_redirects=False).status_code == 303

    # A valid signature for an ID that is not a reviewer is refused too.
    client.cookies.set(auth.COOKIE, auth.new_cookie_value("EMP-001"))
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_gate_is_off_without_the_password(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = PlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog()
    c = TestClient(web.app)
    assert c.get("/admin").status_code == 200
    assert "Gerbang login mati" in c.get("/login").text
    assert auth.reviewer_of(None) == auth.FALLBACK_REVIEWER


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
    _fake_postgres(monkeypatch)
    yield monkeypatch
    plans.STATE.clear()


def test_prod_without_demo_password_fails_startup(prod):
    prod.delenv("DEMO_PASSWORD", raising=False)
    prod.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    with pytest.raises(auth.AuthConfigError) as exc:
        with TestClient(web.app):
            pass
    assert "DEMO_PASSWORD" in str(exc.value)


def test_prod_with_short_password_fails_startup(prod):
    prod.setenv("DEMO_PASSWORD", "short")
    prod.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    with pytest.raises(auth.AuthConfigError) as exc:
        with TestClient(web.app):
            pass
    assert "12" in str(exc.value)


def test_prod_without_session_key_fails_startup(prod):
    prod.setenv("DEMO_PASSWORD", PW)
    prod.delenv("REIMBURSE_DEMO_SESSION_KEY", raising=False)
    with pytest.raises(auth.AuthConfigError) as exc:
        with TestClient(web.app):
            pass
    assert "REIMBURSE_DEMO_SESSION_KEY" in str(exc.value)


def test_prod_with_everything_set_serves_the_app(prod):
    prod.setenv("DEMO_PASSWORD", PW)
    prod.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    with TestClient(web.app) as c:
        assert c.get("/healthz").text == "ok"
        assert c.get("/", follow_redirects=False).status_code == 303
        assert login(c).status_code == 303
        assert c.get("/").status_code == 200
        assert "secure" in login(c).headers["set-cookie"].lower()
