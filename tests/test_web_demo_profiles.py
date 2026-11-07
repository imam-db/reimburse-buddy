"""The demo profile picker (docs/adr/0012-demo-profiles.md): stage 1, the switch and the user store.

`REIMBURSE_DEMO_PROFILES=1` is on only outside production and only with the login gate on; production refuses
to start when it is set at all. The accounts a card may stand for are `auth.DEMO_PROFILES` in order, and only
the rows that exist and are active in `app_users` (here a MemoryUserStore: no database is touched).
"""

import logging
import re

import pytest
from conftest import TEST_ACCOUNTS, TEST_PASSWORD, install_test_users
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, plans, users

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

DEMO_ON = "1"


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


# --- the switch ---------------------------------------------------------------------------
def test_the_switch_is_on_with_the_flag_outside_production_and_the_gate_on(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    assert auth.demo_profiles_enabled()


def test_the_switch_is_off_without_the_flag(client, monkeypatch):
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    assert not auth.demo_profiles_enabled()
    # Any value other than 1 leaves it off: a typo can never open a passwordless way in.
    for value in ("", "  ", "0", "true", "yes", "2"):
        monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", value)
        assert not auth.demo_profiles_enabled(), value


def test_the_switch_is_off_in_production_even_with_the_flag(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    monkeypatch.setenv("APP_ENV", "prod")
    assert not auth.demo_profiles_enabled()
    monkeypatch.setenv("APP_ENV", "production")
    assert not auth.demo_profiles_enabled()


def test_the_switch_is_off_when_the_login_gate_is_off(tmp_path, monkeypatch):
    """With the gate off there is no sign-in at all, so there is nothing for the picker to do."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    auth.set_store(None)
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog()
    assert not auth.enabled()
    assert not auth.demo_profiles_enabled()
    assert auth.demo_profile_users() == []
    assert "Gerbang login mati" in TestClient(web.app).get("/login").text


# --- production refuses to start -----------------------------------------------------------
@pytest.fixture
def prod(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "k" * 32)
    monkeypatch.setenv("REIMBURSE_AUDIT_DATABASE_URL", "postgresql://user@db.example:5432/reimburse")
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "file")
    yield monkeypatch
    plans.STATE.clear()


def test_production_with_the_switch_refuses_to_start_and_names_the_variable(prod):
    for value in ("1", "true", "0", "anything"):
        prod.setenv("REIMBURSE_DEMO_PROFILES", value)
        with pytest.raises(auth.AuthConfigError) as exc:
            auth.startup_check()
        assert "REIMBURSE_DEMO_PROFILES" in str(exc.value), value


def test_production_without_the_switch_starts(prod, store):
    prod.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    auth.startup_check()  # no exception


def test_the_startup_refusal_survives_the_other_checks(prod):
    """The switch is refused even when the rest of the production config is incomplete: no ordering escape."""
    prod.delenv("REIMBURSE_DEMO_SESSION_KEY", raising=False)
    prod.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    with pytest.raises(auth.AuthConfigError) as exc:
        auth.startup_check()
    assert "REIMBURSE_DEMO_PROFILES" in str(exc.value)


# --- which profiles -----------------------------------------------------------------------
def test_the_profiles_are_the_five_demo_accounts_in_order():
    assert auth.DEMO_PROFILES == ("EMP-001", "EMP-003", "EMP-100", "EMP-101", "EMP-102")


def test_the_card_list_follows_the_demo_order_and_the_row_role(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    rows = auth.demo_profile_users()
    assert [u.employee_id for u in rows] == list(auth.DEMO_PROFILES)
    assert [u.role for u in rows] == ["employee", "employee", "reviewer", "reviewer", "finance"]


def test_the_role_comes_from_the_row_not_from_the_list(client, monkeypatch):
    """Re-seeding EMP-101 as finance moves that card's role; nothing in the picker decides it."""
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    install_test_users(roles={"EMP-101": "finance"})
    roles = {u.employee_id: u.role for u in auth.demo_profile_users()}
    assert roles["EMP-101"] == "finance"


def test_an_inactive_or_missing_account_gets_no_card(client, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    store.add("EMP-003", "sari.kusumawati@nusantara-niaga.example", "a-long-enough-secret", "employee",
              is_active=False)  # the later row wins the employee_id index, and it is inactive
    listed = [u.employee_id for u in auth.demo_profile_users()]
    assert "EMP-003" not in listed
    assert listed == ["EMP-001", "EMP-100", "EMP-101", "EMP-102"]

    fresh = users.MemoryUserStore()
    for emp in ("EMP-001", "EMP-100"):  # EMP-003, EMP-101 and EMP-102 were never created
        fresh.add(emp, TEST_ACCOUNTS[emp][0], role=TEST_ACCOUNTS[emp][1], password_hash="scrypt$1$1$1$00$00")
    auth.set_store(fresh)
    assert [u.employee_id for u in auth.demo_profile_users()] == ["EMP-001", "EMP-100"]


def test_the_card_list_is_empty_when_the_switch_is_off(client, monkeypatch):
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    assert auth.demo_profile_users() == []


def test_a_broken_user_store_is_an_empty_list_not_a_crash(client, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)

    def boom(employee_id):
        raise RuntimeError("db down: postgresql://secret")

    monkeypatch.setattr(store, "find_by_employee_id", boom)
    assert auth.demo_profile_users() == []


# --- find_by_employee_id ------------------------------------------------------------------
def test_memory_store_finds_a_row_by_employee_id():
    store = users.MemoryUserStore()
    store.add("EMP-100", "Dewi@Example.com", "a-long-enough-secret", "reviewer")
    found = store.find_by_employee_id("EMP-100")
    assert found is not None and found.employee_id == "EMP-100" and found.role == "reviewer"
    assert store.find_by_employee_id(" EMP-100 ") is not None  # the form value is trimmed
    assert store.find_by_employee_id("EMP-999") is None
    assert store.find_by_employee_id("") is None


def test_memory_store_keeps_the_latest_row_for_one_employee_id():
    store = users.MemoryUserStore()
    store.add("EMP-100", "old@example.com", "a-long-enough-secret", "reviewer")
    store.add("EMP-100", "new@example.com", "a-long-enough-secret", "finance")
    assert store.find_by_employee_id("EMP-100").role == "finance"


# --- the picker page ----------------------------------------------------------------------
CARDS = [  # employee_id, name, department, role chip (users.ROLE_LABELS)
    ("EMP-001", "Rina Wulandari", "Sales", "Karyawan"),
    ("EMP-003", "Sari Kusumawati", "Sales", "Karyawan"),
    ("EMP-100", "Dewi Anggraini", "Sales", "Manager"),
    ("EMP-101", "Hendra Saputra", "Finance", "Manager"),
    ("EMP-102", "Rani Kusuma", "Finance", "Finance"),
]


def post_demo(client, employee_id, next_path="/"):
    return client.post(auth.DEMO_LOGIN_PATH, data={"employee_id": employee_id, "next": next_path},
                       follow_redirects=False)


def test_the_login_page_becomes_the_picker_with_the_switch_on(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    r = client.get("/login")
    assert r.status_code == 200
    assert "<h1>Pilih profil</h1>" in r.text
    assert "Mode demo: masuk tanpa kata sandi. Data sintetis." in r.text
    assert 'id="demo-profiles"' in r.text
    assert "Apa yang bisa dibuka tiap peran" in r.text
    assert r.text.count('class="demo-card"') == 5


def test_the_role_legend_is_plain_sentences_without_url_paths(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    html = client.get("/login").text
    legend = html.split('id="demo-legend"', 1)[1].split("</section>", 1)[0]
    assert "Karyawan: klaim saya, ajukan klaim baru, dan tanya-jawab klaim." in legend
    assert "Manager: menjawab permintaan persetujuan dari tim sendiri." in legend
    assert "Finance: triage, meninjau bukti, memutuskan klaim, dan menyetujui rencana." in legend
    assert not re.search(r"/(employee|manager|admin)\b", legend)


def test_each_card_shows_the_name_department_and_role_chip(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    html = client.get("/login").text
    for employee_id, name, department, chip in CARDS:
        assert f'id="demo-{employee_id}"' in html, employee_id
        assert f'<span class="demo-name">{name}</span>' in html, name
        assert f'<span class="demo-dept">{department}</span>' in html, department
        assert f'<span class="demo-role">{chip}</span>' in html, chip
    # Initials in a circle, no photos of real-looking people (ADR 0012 §2).
    assert '<span class="demo-avatar" aria-hidden="true">RW</span>' in html
    assert "<img" not in html and "avatar.jpg" not in html
    # One sentence per role, from labels.py.
    assert "Kirim klaim dan unggah struk" in html
    assert "Triage, tinjau bukti, dan putuskan klaim" in html


def test_the_cards_are_in_the_demo_order(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    html = client.get("/login").text
    positions = [html.index(f'id="demo-{emp}"') for emp, *_ in CARDS]
    assert positions == sorted(positions)


def test_every_card_is_a_form_that_works_without_javascript(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    html = client.get("/login").text
    assert html.count(f'<form method="post" action="{auth.DEMO_LOGIN_PATH}">') == 5
    assert html.count('name="employee_id"') == 5 and html.count('name="next"') == 6  # 5 cards + the email form
    assert "<script" not in html  # a card is a plain submit, so the page needs no JavaScript


def test_the_email_form_stays_inside_a_closed_details(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    html = client.get("/login").text
    assert '<details class="demo-email" id="demo-email">' in html  # closed, no `open`
    assert "<summary>Masuk dengan email</summary>" in html
    details = html.index('<details class="demo-email"')
    assert details < html.index('<form method="post" action="/login">')
    assert 'type="password"' in html and 'autocomplete="current-password"' in html
    # The email form still works: the picker does not remove the password way in.
    assert client.post("/login", data={"email": TEST_ACCOUNTS["EMP-102"][0], "password": "wrong-one",
                                       "next": "/admin"}, follow_redirects=False).status_code == 401


def test_the_picker_opens_the_email_form_again_after_a_failed_sign_in(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    html = client.get("/login", params={"error": "Email atau kata sandi salah.", "email": "a@b.example"}).text
    assert '<details class="demo-email" id="demo-email" open>' in html
    assert 'role="alert"' in html and "Email atau kata sandi salah." in html


def test_the_picker_shows_no_card_for_an_inactive_account(client, monkeypatch, store):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    store.add("EMP-101", TEST_ACCOUNTS["EMP-101"][0], "a-long-enough-secret", "reviewer", is_active=False)
    html = client.get("/login").text
    assert html.count('class="demo-card"') == 4
    assert 'id="demo-EMP-101"' not in html


def test_the_picker_says_so_when_no_demo_account_is_seeded(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    auth.set_store(users.MemoryUserStore())
    html = client.get("/login").text
    assert 'id="demo-profiles-empty"' in html
    assert "Jalankan seed_users lebih dulu." in html
    assert 'class="demo-card"' not in html


def test_without_the_switch_the_login_page_is_exactly_the_old_card(client, monkeypatch):
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    r = client.get("/login")
    assert r.status_code == 200
    assert 'class="login-card"' in r.text and "Reimburse Buddy" in r.text
    assert 'type="email"' in r.text and 'type="password"' in r.text
    assert 'id="toggle-password"' in r.text and 'name="next"' in r.text
    assert "Pilih profil" not in r.text and "demo-card" not in r.text
    assert "EMP-1" not in r.text and "<nav" not in r.text


# --- signing in through a card ------------------------------------------------------------
LANDING = {"EMP-001": "/employee", "EMP-003": "/employee", "EMP-100": "/manager",
           "EMP-101": "/manager", "EMP-102": "/admin"}


@pytest.mark.parametrize("employee_id", sorted(LANDING))
def test_a_card_signs_in_and_lands_on_the_role_page(client, monkeypatch, employee_id):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    r = post_demo(client, employee_id)
    assert r.status_code == 303 and r.headers["location"] == LANDING[employee_id]
    assert auth.COOKIE in r.cookies
    session = auth.parse(r.cookies[auth.COOKIE])
    assert session.reviewer == employee_id
    assert session.role == TEST_ACCOUNTS[employee_id][1]
    assert client.get(LANDING[employee_id]).status_code == 200


def test_rina_dewi_and_rani_land_on_their_pages(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    assert post_demo(client, "EMP-001").headers["location"] == "/employee"
    assert post_demo(client, "EMP-100").headers["location"] == "/manager"
    assert post_demo(client, "EMP-102").headers["location"] == "/admin"


def test_the_cookie_is_the_same_one_the_password_form_sets(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    demo = post_demo(client, "EMP-102").headers["set-cookie"].lower()
    password = client.post("/login", data={"email": TEST_ACCOUNTS["EMP-102"][0], "password": TEST_PASSWORD,
                                           "next": "/admin"}, follow_redirects=False).headers["set-cookie"].lower()
    for flag in ("httponly", "samesite=lax", "path=/", f"max-age={auth.TTL_SECONDS}"):
        assert flag in demo and flag in password, flag
    assert ("secure" in demo) == ("secure" in password) == False  # noqa: E712 - outside production


def test_next_is_honoured_only_when_the_role_may_open_it(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    assert post_demo(client, "EMP-102", "/admin/plans").headers["location"] == "/admin/plans"
    assert post_demo(client, "EMP-001", "/employee/new").headers["location"] == "/employee/new"
    # A next the role may not open falls back to its landing page, and an off-site next is refused.
    assert post_demo(client, "EMP-001", "/admin").headers["location"] == "/employee"
    assert post_demo(client, "EMP-102", "/employee").headers["location"] == "/admin"
    assert post_demo(client, "EMP-100", "https://evil.example/x").headers["location"] == "/manager"
    assert post_demo(client, "EMP-102", "//evil.example/x").headers["location"] == "/admin"


def test_the_role_comes_from_the_row_so_a_finance_manager_lands_on_admin(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    install_test_users(roles={"EMP-101": "finance"})  # the row, not the list, decides
    assert post_demo(client, "EMP-101").headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    assert auth.reviewer_of(client) == "EMP-101"


def test_a_posted_role_is_ignored(client, monkeypatch):
    """Only `employee_id` and `next` are read; a `role` in the form cannot raise anyone's access."""
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    r = client.post(auth.DEMO_LOGIN_PATH, data={"employee_id": "EMP-001", "role": "finance", "next": "/admin"},
                    follow_redirects=False)
    assert r.headers["location"] == "/employee"
    assert auth.parse(r.cookies[auth.COOKIE]).role == "employee"
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/employee"


def test_an_id_outside_the_list_an_inactive_account_and_an_unknown_id_all_answer_404(client, monkeypatch,
                                                                                     store):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    store.add("EMP-103", "gone@nusantara-niaga.example", "a-long-enough-secret", "employee", is_active=False)
    answers = set()
    for employee_id in ("EMP-103", "EMP-999", "EMP-002", "", "   ", "EMP-001;DROP TABLE app_users"):
        r = post_demo(client, employee_id)
        assert r.status_code == 404, employee_id
        assert auth.COOKIE not in r.cookies
        answers.add(r.text)
    assert len(answers) == 1  # the same answer every time: the route never says which accounts exist


def test_the_route_answers_404_when_the_switch_is_off(tmp_path, monkeypatch):
    """With the gate off nothing is protected, so the route itself answers: 404, not a sign-in."""
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    auth.set_store(None)
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog()
    c = TestClient(web.app)
    for employee_id in ("EMP-001", "EMP-102", "EMP-999"):
        r = c.post(auth.DEMO_LOGIN_PATH, data={"employee_id": employee_id, "next": "/admin"},
                   follow_redirects=False)
        assert r.status_code == 404, employee_id
        assert auth.COOKIE not in r.cookies


def test_with_the_gate_on_and_the_switch_off_the_login_gate_refuses_the_route(client, monkeypatch):
    """The switch off leaves /login/demo as protected as every other route: no session, no sign-in."""
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    r = post_demo(client, "EMP-001")
    assert r.status_code == 401 and auth.COOKIE not in r.cookies
    assert client.get(auth.DEMO_LOGIN_PATH, follow_redirects=False).status_code == 303


def test_one_log_line_per_sign_in_with_the_id_and_role_and_no_ip(client, monkeypatch, caplog):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    caplog.set_level(logging.INFO, logger="reimburse_web.app")
    post_demo(client, "EMP-001")
    lines = [r.getMessage() for r in caplog.records if "demo profile sign-in" in r.getMessage()]
    assert lines == ["demo profile sign-in: EMP-001 (employee)"]
    caplog.clear()
    post_demo(client, "EMP-102")
    lines = [r.getMessage() for r in caplog.records if "demo profile sign-in" in r.getMessage()]
    assert lines == ["demo profile sign-in: EMP-102 (finance)"]
    assert "testclient" not in lines[0] and "127.0.0.1" not in lines[0]


def test_a_refused_sign_in_writes_no_log_line(client, monkeypatch, caplog):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    caplog.set_level(logging.INFO, logger="reimburse_web.app")
    post_demo(client, "EMP-999")
    assert [r for r in caplog.records if "demo profile sign-in" in r.getMessage()] == []


def test_a_demo_sign_in_writes_nothing_to_the_audit_log(client, monkeypatch):
    """The audit event types are fixed by migrations 001-004; a sign-in is not one of them."""
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    post_demo(client, "EMP-102")
    assert plans.log().list() == []


def test_the_demo_session_obeys_the_same_role_rules(client, monkeypatch):
    """The session is the same cookie, so `may_open` / `home_for` and the guards that read them are unchanged."""
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    post_demo(client, "EMP-102")
    assert auth.reviewer_of(client) == "EMP-102"
    assert not auth.may_open(auth.parse(client.cookies[auth.COOKIE]).role, "/employee")


# --- the header ---------------------------------------------------------------------------
def test_a_signed_in_user_sees_ganti_profil_with_the_switch_on(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    post_demo(client, "EMP-102")
    nav = client.get("/admin").text.split("</nav>")[0]
    assert 'id="logout"' in nav and "Ganti profil" in nav
    assert "Keluar" not in nav
    # The same POST /logout, and it lands on the picker.
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Pilih profil" in client.get("/login").text


def test_the_header_still_says_keluar_without_the_switch(client, monkeypatch):
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    client.post("/login", data={"email": TEST_ACCOUNTS["EMP-102"][0], "password": TEST_PASSWORD,
                                "next": "/admin"}, follow_redirects=False)
    nav = client.get("/admin").text.split("</nav>")[0]
    assert 'id="logout"' in nav and "Keluar" in nav
    assert "Ganti profil" not in nav


# --- stylesheet ---------------------------------------------------------------------------
def test_the_picker_grid_is_fluid_and_one_column_on_a_phone(client, monkeypatch):
    """A 375 px screen must not scroll sideways: no fixed large width on the grid, one column there."""
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", DEMO_ON)
    css = client.get("/static/app.css").text
    grid = css[css.index(".demo-grid {"):css.index(".demo-card {")]
    assert "display: grid" in grid
    assert "repeat(auto-fit, minmax(170px, 1fr))" in grid  # fluid tracks, no fixed `width`
    assert not re.search(r"width:/s*/d{3,}px", grid)
    assert not re.search(r"min-width:/s*/d{3,}px", grid)
    phone = css[css.rindex("@media (max-width: 480px)"):]  # the picker's own narrow-screen block
    assert ".demo-grid" in phone and "grid-template-columns: 1fr" in phone
    assert "min-height: 44px" in css[css.index(".demo-card-button {"):css.index(".demo-avatar {")]
    assert "summary:focus-visible" in css  # the keyboard outline covers the details toggle too


def test_the_switch_without_a_user_store_refuses_to_start_outside_production(monkeypatch):
    auth.set_store(None)
    for var in ("APP_ENV", "REIMBURSE_AUTH_DATABASE_URL", "REIMBURSE_AUDIT_DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", "1")
    with pytest.raises(auth.AuthConfigError) as exc:
        auth.startup_check()
    assert "REIMBURSE_DEMO_PROFILES" in str(exc.value)
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES")
    auth.startup_check()  # the switch unset keeps the old dev behaviour
