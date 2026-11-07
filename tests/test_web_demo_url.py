"""ADR 0013: `REIMBURSE_DEMO_URL` adds a "Coba demo" link to the login card, and is validated at startup."""

import pytest
from conftest import install_test_users
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth

URL = "https://demo.reimbursebuddy.my.id"


@pytest.fixture
def client(monkeypatch):
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("REIMBURSE_DEMO_PROFILES", raising=False)
    monkeypatch.delenv("REIMBURSE_DEMO_URL", raising=False)
    return TestClient(web.app)


@pytest.mark.parametrize("value", [
    URL, URL + "/", "https://demo.example.com:8443/path", "http://localhost:8001", "http://127.0.0.1:8001/",
])
def test_valid_values_are_accepted(monkeypatch, value):
    monkeypatch.setenv("REIMBURSE_DEMO_URL", value)
    assert auth.demo_url() == value
    auth.startup_check()


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "http://demo.example.com", "https://", "https:///x", "/demo", "demo.example.com",
    "ftp://demo.example.com", "data:text/html,x", "https://host:notaport", "//demo.example.com",
])
def test_invalid_values_stop_startup(monkeypatch, value):
    monkeypatch.setenv("REIMBURSE_DEMO_URL", value)
    with pytest.raises(auth.AuthConfigError):
        auth.startup_check()


def test_empty_or_unset_means_no_link(monkeypatch):
    monkeypatch.delenv("REIMBURSE_DEMO_URL", raising=False)
    assert auth.demo_url() == ""
    monkeypatch.setenv("REIMBURSE_DEMO_URL", "  ")
    assert auth.demo_url() == ""
    auth.startup_check()


def test_login_card_shows_the_button_when_set(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_URL", URL)
    html = client.get("/login").text
    assert f'href="{URL}"' in html
    assert "Coba demo →" in html
    assert "Data sintetis, tanpa kata sandi." in html


def test_login_card_is_unchanged_when_unset(client, monkeypatch):
    html_unset = client.get("/login").text
    assert "Coba demo" not in html_unset and "try-demo" not in html_unset
    monkeypatch.setenv("REIMBURSE_DEMO_URL", URL)
    html_set = client.get("/login").text
    assert html_set.replace(html_set[html_set.index('<div class="try-demo"'):html_set.index("</main>")], "") == html_unset


def test_the_link_is_html_escaped(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_URL", 'https://demo.example.com/?a=1&b="x"')
    html = client.get("/login").text
    assert 'href="https://demo.example.com/?a=1&amp;b=&quot;x&quot;"' in html


def test_the_picker_does_not_show_the_link(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEMO_URL", URL)
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", "1")
    assert "Coba demo" not in client.get("/login").text


# --- demo banner (ADR 0013 section 3) ---------------------------------------------------------
BANNER = "Mode demo · data sintetis · bisa di-reset kapan saja"


def test_banner_is_on_every_page_with_the_picker_on(client, monkeypatch):
    from conftest import login_as
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", "1")
    assert BANNER in client.get("/login").text
    login_as(client, "EMP-102")
    for path in ("/admin", "/me"):
        r = client.get(path, follow_redirects=True)
        assert r.status_code == 200 and BANNER in r.text, path


def test_banner_is_absent_with_the_picker_off(client, monkeypatch):
    from conftest import login_as
    assert BANNER not in client.get("/login").text
    login_as(client, "EMP-102")
    assert BANNER not in client.get("/admin").text
    monkeypatch.setenv("REIMBURSE_DEMO_PROFILES", "0")
    assert BANNER not in client.get("/admin").text
