"""Upload quotas: open drafts per employee, bytes per rolling day, and expiry of stale drafts."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import install_test_users, login_as
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, plans, upload_flow, upload_quota
from reimburse_web.extract import Draft

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore

PDF = b"%PDF-1.4\n%%EOF\n"
TRIP = dict(purpose="Visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")


class Stub:
    name = "stub"

    def extract(self, path):
        return Draft()


@pytest.fixture
def client(tmp_path, monkeypatch):
    install_test_users()
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("REIMBURSE_DEV_EMPLOYEE", raising=False)
    for name in ("REIMBURSE_MAX_OPEN_DRAFTS", "REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", "REIMBURSE_DRAFT_TTL_DAYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(upload_flow, "get_extractor", lambda: Stub())
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    auth.reset_attempts()
    yield TestClient(web.app)
    auth.reset_attempts()
    plans.STATE.clear()
    web.reset_all()


def _post(client, n_files=1, body=PDF):
    files = [("files", (f"a{i}.pdf", body, "application/pdf")) for i in range(n_files)]
    return client.post("/employee/drafts", data=TRIP, files=files, follow_redirects=False)


def _on_disk():
    return sorted(p.name for p in web.UPLOAD_DIR.glob("*")) if web.UPLOAD_DIR.exists() else []


def test_defaults_and_invalid_env_fall_back(monkeypatch):
    monkeypatch.delenv("REIMBURSE_MAX_OPEN_DRAFTS", raising=False)
    monkeypatch.delenv("REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", raising=False)
    monkeypatch.delenv("REIMBURSE_DRAFT_TTL_DAYS", raising=False)
    assert upload_quota.max_open_drafts() == 10
    assert upload_quota.max_upload_bytes_per_day() == 100 * 1024 * 1024
    assert upload_quota.draft_ttl_days() == 30
    for bad in ("0", "-3", "abc", ""):
        monkeypatch.setenv("REIMBURSE_MAX_OPEN_DRAFTS", bad)
        assert upload_quota.max_open_drafts() == 10
    monkeypatch.setenv("REIMBURSE_MAX_OPEN_DRAFTS", "2")
    assert upload_quota.max_open_drafts() == 2


def test_default_limit_is_ten_open_drafts(client):
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    for _ in range(10):
        assert _post(client, 0).status_code == 303
    r = _post(client, 0)
    assert r.status_code == 429
    assert "Terlalu banyak draf terbuka (maks 10)" in r.text


def test_open_draft_limit_is_per_employee_and_env_overridable(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_MAX_OPEN_DRAFTS", "2")
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    assert _post(client, 0).status_code == 303
    assert _post(client, 0).status_code == 303
    r = _post(client, 0)
    assert r.status_code == 429 and "maks 2" in r.text
    assert login_as(client, "EMP-003", "/employee").status_code == 303  # another employee is unaffected
    assert _post(client, 0).status_code == 303
    monkeypatch.setenv("REIMBURSE_MAX_OPEN_DRAFTS", "3")
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    assert _post(client, 0).status_code == 303


def test_daily_bytes_refused_before_anything_is_written(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", str(4 * len(PDF)))
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    assert _post(client, 3).status_code == 303
    written = _on_disk()
    assert len(written) == 3
    r = _post(client, 2)  # 3 + 2 files > 4 files' worth: the whole request is refused
    assert r.status_code == 429 and "Batas unggah harian" in r.text
    assert _on_disk() == written, "a refused request must write nothing"
    assert len(web.DRAFTS) == 1
    assert _post(client, 1).status_code == 303  # exactly the remaining quota is still accepted
    assert _post(client, 1).status_code == 429
    assert login_as(client, "EMP-003", "/employee").status_code == 303
    assert _post(client, 2).status_code == 303  # another employee has their own quota


def test_refused_uploads_are_not_counted(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", str(2 * len(PDF)))
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    assert _post(client, 3).status_code == 429
    assert upload_quota.bytes_used_today("EMP-001") == 0
    assert _post(client, 2).status_code == 303
    assert upload_quota.bytes_used_today("EMP-001") == 2 * len(PDF)


def test_quota_window_is_rolling_24h(monkeypatch):
    monkeypatch.setenv("REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", "100")
    upload_quota.reset_ledger()
    upload_quota.record_upload("EMP-001", 100, now=1000.0)
    with pytest.raises(Exception) as exc:
        upload_quota.check_daily_bytes("EMP-001", 1, now=1000.0 + 3600)
    assert exc.value.status_code == 429
    upload_quota.check_daily_bytes("EMP-001", 100, now=1000.0 + 24 * 3600 + 1)
    upload_quota.reset_ledger()


def test_forward_endpoint_counts_and_refuses(client, monkeypatch):
    monkeypatch.setenv("REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", str(len(PDF) + 5))
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    data = {"subject": "Receipt", "att_type": "receipt"}
    ok = client.post("/employee/forward", data=data, files={"file": ("r.pdf", PDF, "application/pdf")},
                     follow_redirects=False)
    assert ok.status_code == 303
    before = _on_disk()
    r = client.post("/employee/forward", data=data, files={"file": ("r.pdf", PDF, "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 429
    assert _on_disk() == before


def test_item_file_upload_counts_and_refuses(client, monkeypatch):
    claim = {"id": "CLM-Q-1", "employee_id": "EMP-001", "status": "submitted", "purpose": "x",
             "trip": {"origin": "Jakarta", "destination": "Surabaya", "start_date": "2026-10-02",
                      "end_date": "2026-10-04"},
             "items": [{"id": "IT-1", "category": "taxi", "date": "2026-10-02", "description": "t",
                        "amount": "50000", "receipt": False}], "batch": web.WEB_BATCH}
    web.store.claims.append(claim)
    monkeypatch.setenv("REIMBURSE_MAX_UPLOAD_BYTES_PER_DAY", str(len(PDF) + 5))
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    url = "/employee/claims/CLM-Q-1/items/IT-1/files"
    ok = client.post(url, files={"file": ("r.pdf", PDF, "application/pdf")}, follow_redirects=False)
    assert ok.status_code == 303
    before = _on_disk()
    r = client.post(url, files={"file": ("r2.pdf", PDF, "application/pdf")}, follow_redirects=False)
    assert r.status_code == 429
    assert _on_disk() == before


def _age(draft, days):
    draft["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _stored(draft):
    return {f["stored"] for c in draft["cards"] for f in c["files"]}


def test_prune_removes_old_draft_and_files_keeps_fresh_and_submitted(client):
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    ids = []
    for _ in range(3):
        r = _post(client, 2)
        assert r.status_code == 303
        ids.append(r.headers["location"].rsplit("/", 1)[1])
    old, fresh, submitted_like = (web.DRAFTS[i] for i in ids)
    old_files, fresh_files, sub_files = _stored(old), _stored(fresh), _stored(submitted_like)
    assert len(old_files) == 2
    _age(old, 45)
    _age(fresh, 2)
    # A submitted draft leaves core.DRAFTS and its files belong to the claim: pruning must not touch them.
    web.DRAFTS.pop(submitted_like["id"])
    outside = web.UPLOAD_DIR.parent / "outside.txt"  # a path that climbs out of UPLOAD_DIR is never deleted
    outside.write_text("keep")
    old["cards"][0]["files"][0]["stored"] = "../outside.txt"
    escaped = next(iter(old_files - _stored(old)))  # the file that no longer has a name inside the draft

    assert upload_flow.prune_stale_drafts() == 1
    assert old["id"] not in web.DRAFTS and fresh["id"] in web.DRAFTS
    left = set(_on_disk())
    assert fresh_files <= left and sub_files <= left
    assert (old_files - {escaped}).isdisjoint(left)
    assert outside.read_text() == "keep"
    assert upload_flow.prune_stale_drafts() == 0


def test_prune_respects_ttl_env_and_stamps_unstamped_drafts(client, monkeypatch):
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    d = web.DRAFTS[_post(client, 1).headers["location"].rsplit("/", 1)[1]]
    _age(d, 5)
    assert upload_flow.prune_stale_drafts() == 0  # default TTL is 30 days
    monkeypatch.setenv("REIMBURSE_DRAFT_TTL_DAYS", "3")
    d2 = web.DRAFTS[_post(client, 1).headers["location"].rsplit("/", 1)[1]]
    d2.pop("updated_at")  # a draft kept before the stamp existed is stamped, not deleted on first sight
    assert upload_flow.prune_stale_drafts() == 1
    assert d["id"] not in web.DRAFTS and d2["id"] in web.DRAFTS and "updated_at" in d2


def test_prune_runs_on_draft_creation_at_most_hourly(client, monkeypatch):
    calls = []
    monkeypatch.setattr(upload_flow, "_last_prune", 0.0)
    real = upload_flow.prune_stale_drafts
    monkeypatch.setattr(upload_flow, "prune_stale_drafts", lambda now=None: (calls.append(1), real(now))[1])
    assert login_as(client, "EMP-001", "/employee").status_code == 303
    assert _post(client, 0).status_code == 303
    assert _post(client, 0).status_code == 303
    assert len(calls) == 1
