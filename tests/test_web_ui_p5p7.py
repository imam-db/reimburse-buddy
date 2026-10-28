"""P5 landing page, P6 accessibility/progress, P7 triage focus on held money.

Concrete rendered-output assertions only: the landing links, the status icons, the label coverage of
every form control, the human-readable agent progress and the triage ordering by the rule engine's
`total_pending`.
"""

import json
import re
import shutil
import time
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web.agent_bridge import Investigator
from reimburse_agent.office import page as office_page

HERO = "CLM-2026-0110"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")  # gate off: the acting employee comes from the environment
    monkeypatch.delenv("EMPLOYEE_QA", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


# --- P5 landing page -----------------------------------------------------------
def test_landing_page_explains_roles_and_links_the_demo_claim(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert "Selamat datang di reimburse-buddy" in html
    assert '<a href="/">Beranda</a>' in html
    assert '<a class="btn" href="/admin">Lihat triage finance</a>' in html
    assert f'<a class="btn" href="/admin/claims/{HERO}">Buka klaim contoh (Rina, Surabaya)</a>' in html
    assert f'<a class="btn" href="/employee/claims/{HERO}">Coba tanya sebagai karyawan</a>' in html
    assert '<form method="post" action="/admin/reset">' in html
    assert "Reset demo" in html
    assert "Data sintetis. Tidak ada email atau pesan sungguhan yang dikirim." in html
    assert "Finance" in html and "Karyawan" in html


def test_landing_links_the_richest_emp001_surabaya_claim(client):
    assert web.demo_claim_id() == HERO
    assert f"/admin/claims/{HERO}" in client.get("/").text


# --- P6 status icons -----------------------------------------------------------
def test_status_tag_is_not_colour_only():
    assert ">✓ Disetujui<" in web.status_tag("approved")
    assert ">✕ Ditolak<" in web.status_tag("rejected")
    assert ">! Ditahan<" in web.status_tag("on_hold")
    assert ">! Disetujui sebagian<" in web.status_tag("partially_approved")
    assert ">! Perlu tindakan<" in web.status_tag("needs_action")


def test_employee_evidence_status_uses_icons(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", web.store.get_claim("CLM-2026-0102")["employee_id"])
    complete = client.get("/employee/claims/CLM-2026-0102").text
    assert "✓ Beres" in complete

    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", web.store.get_claim(HERO)["employee_id"])
    missing = client.get(f"/employee/claims/{HERO}").text
    assert "! Perlu tindakan:" in missing
    assert "Boarding pass belum ada" in missing


def test_employee_evidence_status_rejected(client):
    # IT-3 is a decision gap (hotel over cap), so the reviewer can reject it outright.
    client.post(f"/admin/claims/{HERO}/decide", data={"item_id": "IT-3", "choice": "reject"})
    html = client.get(f"/employee/claims/{HERO}").text
    assert "✕ Ditolak" in html


def test_focus_visible_style_is_visible_on_both_surfaces():
    rule = ":focus-visible{outline:3px solid #1a5fb4;outline-offset:2px}"
    assert rule in web.CSS
    assert rule in office_page.CSS
    for css in (web.CSS, office_page.CSS):
        for element in ("a", "button", "input", "select", "textarea", "label"):
            assert f"{element}:focus-visible" in css


# --- P6 drop zone --------------------------------------------------------------
def test_drop_zone_has_visible_pick_button_and_keyboard_reachable_input(client):
    html = client.get("/employee/new").text
    assert '<button type="button" id="pick-files">Pilih file</button>' in html
    tag = re.search(r"<input[^>]*id=\"files\"[^>]*>", html).group(0)
    assert "display:none" not in tag.replace(" ", "")
    assert "visually-hidden" in tag
    assert 'name="files"' in tag and "multiple" in tag
    assert "document.getElementById('pick-files')" in html and "addEventListener('click'" in html


# --- P6 every form control has a label -----------------------------------------
class _LabelAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.for_ids: set[str] = set()
        self.controls: list[tuple[str, dict, bool]] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "label":
            self.depth += 1
            if attrs.get("for"):
                self.for_ids.add(attrs["for"])
        elif tag in ("input", "select", "textarea"):
            self.controls.append((tag, attrs, self.depth > 0))

    def handle_endtag(self, tag):
        if tag == "label" and self.depth:
            self.depth -= 1


def _unlabelled(markup: str) -> list[str]:
    audit = _LabelAudit()
    audit.feed(markup)
    bad = []
    for tag, attrs, inside in audit.controls:
        if tag == "input" and attrs.get("type", "text").lower() in ("hidden", "submit"):
            continue
        if inside or attrs.get("aria-label", "").strip():
            continue
        if attrs.get("id") and attrs["id"] in audit.for_ids:
            continue
        bad.append(f"<{tag} {attrs}>")
    return bad


class _FakeRun:
    """Enough of agent_bridge.Run to render the investigation page without spawning the agent."""

    def __init__(self):
        self.id = "fake-run"
        self.claim_id = HERO
        self.started_at = time.time() - 4

    def snapshot(self):
        return [
            {"type": "start", "model": "fake", "compact": True},
            {"type": "step", "n": 1, "tool": "check_claim", "args_summary": "{}", "result_summary": "ok"},
            {"type": "interrupt", "approval_id": "a1", "tool": "update_claim_status", "args": {},
             "prompt": ""},
            {"type": "question", "question_id": "q1", "text": "Which item?",
             "items": [{"item_id": "IT-3", "category": "hotel", "gaps": ["hotel_over_cap"]}]},
        ]

    @property
    def pending(self):
        return self.snapshot()[2]

    @property
    def question(self):
        return self.snapshot()[3]

    def kill(self):
        pass


def _employee_draft(client) -> str:
    r = client.post("/employee/drafts", data={"purpose": "Client visit",
                                              "destination": "Surabaya", "start_date": "2026-10-02",
                                              "end_date": "2026-10-04"}, follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].rsplit("/", 1)[1]


def test_every_form_control_on_the_pages_has_a_label(client):
    did = _employee_draft(client)
    web.INVESTIGATOR.runs["fake-run"] = _FakeRun()
    try:
        pages = {
            "/admin": client.get("/admin").text,
            f"/admin/claims/{HERO}": client.get(f"/admin/claims/{HERO}").text,
            "/admin/investigations/fake-run": client.get("/admin/investigations/fake-run").text,
            "/employee": client.get("/employee").text,
            "/employee/claims": client.get("/employee/claims").text,
            f"/employee/claims/{HERO}": client.get(f"/employee/claims/{HERO}").text,
            "/employee/new": client.get("/employee/new").text,
            f"/employee/drafts/{did}": client.get(f"/employee/drafts/{did}").text,
        }
    finally:
        web.INVESTIGATOR.runs.pop("fake-run", None)
    for url, markup in pages.items():
        assert _unlabelled(markup) == [], url


def test_label_audit_can_actually_fail():
    assert _unlabelled("<form><input name=x></form>") != []
    assert _unlabelled("<label>X <input name=x></label>") == []
    assert _unlabelled("<input name=x aria-label='X'>") == []
    assert _unlabelled("<label for=a>X</label><input id=a name=x>") == []
    assert _unlabelled("<input type=hidden name=x>") == []


# --- P6 progress box -----------------------------------------------------------
def use_script(monkeypatch, tmp_path, script):
    f = tmp_path / "script.json"
    f.write_text(json.dumps(script))
    monkeypatch.setattr(web, "INVESTIGATOR", Investigator(max_per_min=10, fake_script=str(f)))


def wait_page(client, rid, pattern, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        html = client.get(f"/admin/investigations/{rid}").text
        if re.search(pattern, html):
            return html
        time.sleep(0.2)
    raise AssertionError(f"{pattern!r} never appeared:\n{html}")


@pytest.mark.agent
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_progress_box_maps_steps_and_counts_seconds(client, monkeypatch, tmp_path):
    write = {"tool_calls": [{"name": "update_claim_status",
                             "args": {"claim_id": HERO, "status": "on_hold"}, "id": "w1"}]}
    use_script(monkeypatch, tmp_path, [
        {"tool_calls": [{"name": "check_claim", "args": {"claim_id": HERO}, "id": "r1"}]},
        {"tool_calls": [{"name": "search_reimburse_mailbox",
                         "args": {"employee_id": "EMP-001", "query": "taxi"}, "id": "r2"}]},
        write,
    ])
    r = client.post(f"/admin/claims/{HERO}/investigate", follow_redirects=False)
    assert r.status_code == 303
    rid = r.headers["location"].rsplit("/", 1)[1]
    html = wait_page(client, rid, "id=pending")
    assert 'id="progress"' in html
    assert "Menghitung dengan mesin aturan…" in html
    assert "Mencari boarding pass/struk di mailbox…" in html
    assert "Menghitung dengan mesin aturan… → Mencari boarding pass/struk di mailbox…" in html
    assert re.search(r"Waktu berjalan: <b id=\"elapsed\">\d+</b> detik", html)
    assert "setInterval" in html
    client.post(f"/admin/investigations/{rid}/cancel")


# --- P7 triage ----------------------------------------------------------------
ROW = re.compile(r"<tr><td><a href='/admin/claims/(CLM-[^']+)'.*?</tr>", re.S)


def _exception_rows(html: str) -> list[tuple[str, str]]:
    section = html.split("<h3>Exceptions</h3>", 1)[1].split("</table>", 1)[0]
    return [(m.group(1), m.group(0)) for m in ROW.finditer(section)]


def test_triage_exceptions_sorted_by_held_money_and_show_engine_amounts(client):
    html = client.get("/admin").text
    rows = _exception_rows(html)
    assert "Uang tertahan" in html and "Yang kurang" in html
    ids = [cid for cid, _ in rows]
    assert ids == [HERO, "CLM-2026-0101", "CLM-2026-0104"]  # 2.05m, 1.45m, 0.57m held

    held = {cid: web.run_check(cid).total_pending for cid in ids}
    assert ids == sorted(ids, key=lambda cid: held[cid], reverse=True)

    from reimburse_buddy.models import rupiah

    for cid, row in rows:
        shown = re.search(r"<td class=num>(Rp[^<]+)</td>", row).group(1)
        assert shown == rupiah(held[cid]), cid
        assert f'action="/admin/claims/{cid}/investigate"' in row
        assert "Selidiki dengan agent" in row


def test_triage_keeps_legacy_counts_and_reset(client):
    html = client.get("/admin").text
    assert "10 claims, 7 cleared, 3 exceptions" in html
    assert 'action="/admin/reset"' in html
