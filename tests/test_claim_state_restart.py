"""A restart keeps the claim state (ADR 0010 §4).

`restart()` below is what a deploy does to the process: no store, no review state, no drafts. The stored
rows are then replayed on top of the JSON seed, and every page has to read the same as before.

The double here stores rows per epoch exactly like `PostgresClaimRows` does, and replays them through the
real `claim_state.replay`, so the order under test is the order that ships. The `postgres` test runs the
same scenario against a real branch and is skipped without `DATABASE_URL`.
"""

import copy
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import auth, claim_state, plans, upload_flow

from reimburse_agent import audit as A
from reimburse_agent.office.store import FilePlanStore
from reimburse_buddy.data_store import DataStore

from conftest import install_test_users, login_as

TRIP = dict(purpose="Client visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")


class StoringRows(claim_state.MemoryClaimRows):
    """One row per fact, kept per epoch, replayed through the real `replay`."""

    def __init__(self):
        self.epoch = 1
        self.rows: dict[str, dict] = {"claims": {}, "messages": {}, "reviews": {}, "drafts": {},
                                      "outbox": {}, "requests": {}}
        self.changes: list[tuple[int, dict]] = []
        self.files: dict[str, bytes] = {}

    def _put(self, kind: str, key: str, value: dict) -> None:
        self.rows[kind][key] = (self.epoch, copy.deepcopy(value))

    def add_claim(self, claim):
        self._put("claims", claim["id"], claim)
        return claim["id"]

    def add_message(self, message):
        self._put("messages", message["id"], message)
        return message["id"]

    def update_message(self, message):
        self._put("messages", message["id"], message)

    def save_review(self, claim_id, review):
        self._put("reviews", claim_id, review)

    def save_draft(self, draft):
        self._put("drafts", draft["id"], draft)

    def mark_draft_submitted(self, draft_id):
        self.rows["drafts"].pop(draft_id, None)

    def add_outbox(self, message):
        self._put("outbox", message["id"], message)
        return message["id"]

    def save_approval_request(self, request):
        self._put("requests", request["id"], request)

    def add_status_change(self, entry):
        self.changes.append((self.epoch, dict(entry)))

    def save_file(self, stored_as, data, owner_id, content_type="", sha256=""):
        self.files[stored_as] = data

    def read_file(self, stored_as):
        return self.files.get(stored_as)

    @contextmanager
    def transaction(self):
        yield

    def bump_epoch(self):
        self.epoch += 1
        return self.epoch

    def _current(self, kind: str) -> list[dict]:
        return [row for epoch, row in self.rows[kind].values() if epoch == self.epoch]

    def load(self) -> dict:
        return {"claims": self._current("claims"), "messages": self._current("messages"),
                "changes": [row for epoch, row in self.changes if epoch == self.epoch],
                "reviews": [(key, row) for key, (epoch, row) in self.rows["reviews"].items()
                            if epoch == self.epoch],
                "drafts": self._current("drafts"), "outbox": self._current("outbox"),
                "requests": self._current("requests")}

    def hydrate(self, store, review, drafts):
        claim_state.replay(store, review, drafts, self.load())


@pytest.fixture
def rows():
    fake = StoringRows()
    claim_state.set_rows(fake)
    yield fake
    claim_state.set_rows(None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    install_test_users()
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    for name in ("APP_ENV", "REIMBURSE_AUDIT_DATABASE_URL", "REIMBURSE_DEV_EMPLOYEE"):
        monkeypatch.delenv(name, raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    auth.reset_attempts()
    yield TestClient(web.app)
    web.reset_all()
    plans.STATE.clear()
    auth.reset_attempts()


def restart() -> None:
    """What a new process has: the seed, then the stored rows replayed on top of it."""
    web.reset_all()
    claim_state.hydrate(web.store, web.REVIEW, web.DRAFTS)


def as_(c, emp):
    assert login_as(c, emp, "/").status_code == 303
    return c


# --- a claim that survives a restart -----------------------------------------------------------
def _file(stored="s_hotel.png", amount="4200000", day="2026-10-03"):
    fields = {key: {"value": None, "confidence": 0.0, "source": "none"}
              for key in ("doc_type", "amount", "date", "flight_no", "passenger")}
    fields["doc_type"] = {"value": "hotel_receipt", "confidence": 1.0, "source": "ocr"}
    fields["amount"] = {"value": amount, "confidence": 1.0, "source": "ocr"}
    fields["date"] = {"value": day, "confidence": 1.0, "source": "ocr"}
    return {"id": "f1", "name": "hotel.png", "stored": stored,
            "extract": {"fields": fields, "error": None}, "sha256": "sha-of-" + stored,
            "doc_type": "hotel_receipt", "doc_type_source": "ocr", "error": None}


def _draft(tmp_path, did="draft01"):
    """A hotel card over the cap, with a receipt whose amount disagrees: it lands in `pending`."""
    card = upload_flow._card_from_files([])
    card["files"] = [_file()]
    card["fields"]["category"] = {"value": "hotel", "confidence": 1.0, "source": "self_declared"}
    card["fields"]["date"] = {"value": "2026-10-03", "confidence": 1.0, "source": "self_declared"}
    card["fields"]["amount"] = {"value": 5000000, "confidence": 1.0, "source": "self_declared"}
    card["fields"]["nights"] = {"value": 2, "confidence": 1.0, "source": "self_declared"}
    card["fields"]["merchant"] = {"value": "Hotel Majapahit", "confidence": 1.0, "source": "self_declared"}
    (tmp_path / "s_hotel.png").write_bytes(b"synthetic hotel receipt")
    d = {"id": did, "emp": "EMP-001", "header": dict(TRIP), "cards": [card], "errors": []}
    web.DRAFTS[did] = d
    return d


def _submit(c, d):
    as_(c, "EMP-001")  # the employee who owns the draft
    form = {"action": "submit"}
    for card in d["cards"]:
        for key, value in card["fields"].items():
            form[f"c_{card['id']}_{key}"] = "" if value["value"] is None else str(value["value"])
        for f in card["files"]:
            form[f"f_{f['id']}_doc_type"] = f["doc_type"]
    r = c.post(f"/employee/drafts/{d['id']}", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"].split("/employee/claims/")[1].split("?")[0]


def pages(c, cid):
    """Every page the acceptance list names, as the role that may open it."""
    as_(c, "EMP-102")  # finance
    out = {"claim": c.get(f"/admin/claims/{cid}").text, "triage": c.get("/admin").text}
    as_(c, "EMP-001")  # the employee who owns the claim
    out["portal"] = c.get("/employee").text
    out["employee_claim"] = c.get(f"/employee/claims/{cid}").text
    as_(c, "EMP-100")  # the manager who was asked
    out["manager"] = c.get("/manager").text
    return out


def test_the_whole_scenario_reads_the_same_after_a_restart(client, rows, tmp_path):
    d = _draft(tmp_path)
    cid = _submit(client, d)

    as_(client, "EMP-102")  # finance accepts the evidence, decides the item, writes and asks
    mid = "MSG-W001"
    for url, data in ((f"/admin/claims/{cid}/evidence/{mid}/review", {"verdict": "accept"}),
                      (f"/admin/claims/{cid}/decide", {"item_id": "IT-1", "choice": "approve_capped"}),
                      (f"/admin/claims/{cid}/message", {"subject": f"Claim {cid}: dokumen kurang",
                                                        "body": "Mohon konfirmasi tarif hotel."}),
                      (f"/admin/claims/{cid}/items/IT-1/manager", {"reason": "Tarif di atas plafon"})):
        assert client.post(url, data=data, follow_redirects=False).status_code == 303, url
    as_(client, "EMP-100")
    r = client.post("/manager/requests/APR-001", data={"decision": "approved", "note": "Sudah disetujui"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text

    before = pages(client, cid)
    assert "MSG-W001" in before["claim"] and "Setujui sebatas plafon" in before["claim"]
    assert "Tarif di atas plafon" in before["manager"]

    restart()

    assert pages(client, cid) == before


def test_a_restart_keeps_the_claim_in_triage_and_the_portal(client, rows, tmp_path):
    cid = _submit(client, _draft(tmp_path))
    restart()
    as_(client, "EMP-102")
    triage = client.get("/admin").text
    assert "10 klaim, 7 bersih, 3 pengecualian" in triage  # the seed numbers do not move
    assert cid in triage
    as_(client, "EMP-001")
    assert cid in client.get("/employee").text


def test_a_scripted_reply_is_not_delivered_twice_after_a_restart(client, rows):
    as_(client, "EMP-102")
    body = "Mohon kirim struk taksi tanggal 3 Okt (IT-5)."
    assert client.post("/admin/claims/CLM-2026-0110/message",
                       data={"subject": "Claim CLM-2026-0110: taxi receipt", "body": body}).status_code == 200
    assert [m for m in web.store.inbox if m.get("in_reply_to")]  # the mock mailbox answered

    restart()

    # The same message again: the scripted reply was already delivered before the restart.
    as_(client, "EMP-102")
    replies = [m["id"] for m in web.store.inbox if m.get("in_reply_to")]
    r = client.post("/admin/claims/CLM-2026-0110/message",
                    data={"subject": "Claim CLM-2026-0110: taxi receipt", "body": body})
    assert r.status_code == 200
    assert [m["id"] for m in web.store.inbox if m.get("in_reply_to")] == replies  # no second delivery


def test_an_unsubmitted_draft_comes_back_but_a_submitted_one_does_not(client, rows, tmp_path):
    as_(client, "EMP-001")
    kept = _draft(tmp_path, did="kept01")
    assert client.post(f"/employee/drafts/{kept['id']}", data={"action": "save"},
                       follow_redirects=False).status_code == 303
    _submit(client, _draft(tmp_path, did="gone01"))
    restart()
    assert "kept01" in web.DRAFTS and "gone01" not in web.DRAFTS


def test_reset_demo_moves_to_a_new_epoch_and_keeps_the_seed_numbers(client, rows, tmp_path):
    cid = _submit(client, _draft(tmp_path))
    assert cid in [c["id"] for c in web.store.claims]
    seed = DataStore(web.store.data_dir)

    as_(client, "EMP-102")  # Reset demo is finance only
    r = client.post("/admin/reset", follow_redirects=False)
    assert r.status_code == 303
    assert rows.epoch == 2  # a new epoch, no row deleted
    assert cid not in [c["id"] for c in web.store.claims]
    assert len(web.store.claims) == len(seed.claims)  # the seed is untouched
    assert len(web.store.inbox) == len(seed.inbox)
    assert not web.REVIEW and not web.DRAFTS


def test_reset_demo_keeps_the_old_rows_in_the_store(client, rows, tmp_path):
    cid = _submit(client, _draft(tmp_path))
    as_(client, "EMP-102")
    client.post("/admin/reset", follow_redirects=False)
    assert cid in {key for key, (epoch, _) in rows.rows["claims"].items() if epoch == 1}


# --- the same scenario against a real branch ----------------------------------------------------
@pytest.mark.postgres
def test_postgres_keeps_the_claim_across_a_restart(tmp_path, monkeypatch):
    """Needs DATABASE_URL (the app role) and 008 applied. Skipped without it."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set")
    install_test_users()
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEMO_SESSION_KEY", "test-session-key-not-a-secret")
    monkeypatch.setenv("REIMBURSE_PLAN_STORE", str(tmp_path / "plans"))
    monkeypatch.setenv("REIMBURSE_PLAN_BACKEND", "postgres")
    monkeypatch.setenv("REIMBURSE_STATE_DATABASE_URL", dsn)
    for name in ("APP_ENV", "REIMBURSE_DEV_EMPLOYEE"):
        monkeypatch.delenv(name, raising=False)
    web.reset_all()
    plans.STATE.clear()
    plans.STATE["store"] = FilePlanStore(tmp_path / "plans")
    plans.STATE["log"] = A.JsonlAuditLog(tmp_path / "audit.jsonl")
    claim_state.reset()
    try:
        client = TestClient(web.app)
        cid = _submit(client, _draft(tmp_path))
        before = pages(client, cid)
        restart()
        assert pages(client, cid) == before
    finally:
        web.reset_all()
        plans.STATE.clear()
        claim_state.reset()
