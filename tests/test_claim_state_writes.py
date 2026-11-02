"""Every web write reaches the claim-state store, and a store that fails leaves memory alone (ADR 0010 §3).

The store is a double that records the call order, so these tests prove *which* function each route uses
without a database. The rule under test is "database first, then memory": when the store raises, the route
answers 503 and nothing in `store.claims`, `store.inbox`, `store.outbox`, `store.approval_requests`,
`store.status_log`, `REVIEW` or `DRAFTS` moved.
"""

import copy
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import claim_state, upload_flow

HERO = "CLM-2026-0110"
TRIP = dict(purpose="Client visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")


class FakeClaimRows(claim_state.MemoryClaimRows):
    """An in-memory stand-in that records every call and can fail on a named one."""

    def __init__(self, fail_on=()):
        self.calls: list[str] = []
        self.fail_on = set(fail_on)
        self.depth = 0
        self.max_depth = 0
        self.claims: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.reviews: dict[str, dict] = {}
        self.drafts: dict[str, dict] = {}
        self.outbox: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.status_changes: list[dict] = []
        self.files: dict[str, bytes] = {}

    def _hit(self, name: str) -> None:
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(f"{name} is down")

    def add_claim(self, claim):
        self._hit("add_claim")
        self.claims[claim["id"]] = copy.deepcopy(claim)
        return claim["id"]

    def add_message(self, message):
        self._hit("add_message")
        self.messages[message["id"]] = copy.deepcopy(message)
        return message["id"]

    def update_message(self, message):
        self._hit("update_message")
        self.messages[message["id"]] = copy.deepcopy(message)

    def save_review(self, claim_id, review):
        self._hit("save_review")
        self.reviews[claim_id] = copy.deepcopy(review)

    def save_draft(self, draft):
        self._hit("save_draft")
        self.drafts[draft["id"]] = copy.deepcopy(draft)

    def mark_draft_submitted(self, draft_id):
        self._hit("mark_draft_submitted")
        self.drafts.pop(draft_id, None)

    def add_outbox(self, message):
        self._hit("add_outbox")
        self.outbox[message["id"]] = copy.deepcopy(message)
        return message["id"]

    def save_approval_request(self, request):
        self._hit("save_approval_request")
        self.requests[request["id"]] = copy.deepcopy(request)

    def add_status_change(self, entry):
        self._hit("add_status_change")
        self.status_changes.append(dict(entry))

    def save_file(self, stored_as, data, owner_id, content_type="", sha256=""):
        self._hit("save_file")
        self.files[stored_as] = data

    def read_file(self, stored_as):
        return self.files.get(stored_as)

    @contextmanager
    def transaction(self):
        self.calls.append("transaction")
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        try:
            yield
        finally:
            self.depth -= 1


@pytest.fixture
def rows():
    fake = FakeClaimRows()
    claim_state.set_rows(fake)
    yield fake
    claim_state.set_rows(None)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")  # gate off: the acting employee comes from the environment
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def snapshot():
    """Everything a failed write must leave untouched.

    An empty `REVIEW` entry is not state: `app.review()` uses `setdefault`, so any read of a claim page
    leaves one behind. Only entries with something in them are compared.
    """
    empty = {"evidence": [], "decisions": {}, "pending": {}, "evidence_log": []}
    review = {cid: r for cid, r in web.REVIEW.items() if r != empty}
    return {
        "claims": copy.deepcopy(web.store.claims),
        "inbox": copy.deepcopy(web.store.inbox),
        "outbox": copy.deepcopy(web.store.outbox),
        "requests": copy.deepcopy(web.store.approval_requests),
        "status_log": copy.deepcopy(web.store.status_log),
        "review": copy.deepcopy(review),
        "drafts": copy.deepcopy(web.DRAFTS),
    }


# --- a draft that submits without OCR ---------------------------------------------------------
def _file(name="taxi.png", amount="150000", day="2026-10-02", doc_type="taxi_receipt", stored="s_taxi.png"):
    fields = {key: {"value": None, "confidence": 0.0, "source": "none"}
              for key in ("doc_type", "amount", "date", "flight_no", "passenger")}
    fields["doc_type"] = {"value": doc_type, "confidence": 1.0, "source": "ocr"}
    fields["amount"] = {"value": amount, "confidence": 1.0, "source": "ocr"}
    fields["date"] = {"value": day, "confidence": 1.0, "source": "ocr"}
    return {"id": "f1", "name": name, "stored": stored, "extract": {"fields": fields, "error": None},
            "sha256": "sha-of-" + stored, "doc_type": doc_type, "doc_type_source": "ocr", "error": None}


def _card(files=(), category="taxi", amount=150000, day="2026-10-02"):
    card = upload_flow._card_from_files([])
    card["files"] = list(files)
    card["fields"]["category"] = {"value": category, "confidence": 1.0, "source": "self_declared"}
    card["fields"]["date"] = {"value": day, "confidence": 1.0, "source": "self_declared"}
    card["fields"]["amount"] = {"value": amount, "confidence": 1.0, "source": "self_declared"}
    card["fields"]["merchant"] = {"value": "Kota Biru", "confidence": 1.0, "source": "self_declared"}
    return card


def draft_with(files=(), did="draft01"):
    for f in files:  # the bytes are on disk: extraction and duplicate detection read them there
        web.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        (web.UPLOAD_DIR / f["stored"]).write_bytes(b"bytes of " + f["stored"].encode())
    d = {"id": did, "emp": "EMP-001", "header": dict(TRIP), "cards": [_card(files)], "errors": []}
    web.DRAFTS[did] = d
    return d


def submit_form(d):
    form = {"action": "submit"}
    for c in d["cards"]:
        for key, value in c["fields"].items():
            form[f"c_{c['id']}_{key}"] = "" if value["value"] is None else str(value["value"])
        for f in c["files"]:
            form[f"f_{f['id']}_doc_type"] = f["doc_type"]
    return form


# --- which function each write point uses -----------------------------------------------------
def test_the_text_form_stores_the_claim_first(client, rows):
    r = client.post("/employee/claims", data={**TRIP, "items": "taxi, 2026-10-02, Station, 150000, no"})
    assert r.status_code == 200
    assert rows.calls[0] == "add_claim"
    assert "CLM-2026-W001" in rows.claims


def test_forwarding_one_file_stores_the_message_and_the_bytes(client, rows):
    draft_with([_file()])
    client.post("/employee/claims", data={**TRIP, "items": "taxi, 2026-10-02, Station, 150000, no"})
    r = client.post("/employee/forward", data={"subject": "Taxi", "claim_id": "CLM-2026-W001",
                                               "att_type": "receipt", "item_id": "IT-1"},
                    files={"file": ("taxi.png", b"synthetic receipt", "image/png")})
    assert r.status_code == 200
    assert {"save_file", "add_message"} <= set(rows.calls)
    assert rows.files and b"synthetic receipt" in rows.files.values()


def test_submitting_a_draft_writes_everything_in_one_transaction(client, rows):
    d = draft_with([_file()])
    r = client.post(f"/employee/drafts/{d['id']}", data=submit_form(d), follow_redirects=False)
    assert r.status_code == 303
    for name in ("add_claim", "add_message", "save_review", "mark_draft_submitted", "save_file"):
        assert name in rows.calls, name
    assert rows.max_depth == 1  # one transaction, not a transaction per fact
    assert rows.claims and rows.messages and rows.reviews
    assert d["id"] not in web.DRAFTS and d["id"] not in rows.drafts


def test_accepting_evidence_saves_the_review_and_the_message(client, rows):
    client.post(f"/admin/claims/{HERO}/evidence", data={"evidence_id": "MSG-5201"})
    assert "save_review" in rows.calls and HERO in rows.reviews
    assert "MSG-5201" in rows.reviews[HERO]["evidence"]


def test_a_verdict_on_evidence_updates_the_message_and_the_review(client, rows):
    upload_flow.forward_files("EMP-001", HERO, {"start_date": "2026-10-02", "end_date": "2026-10-04"},
                              [({"id": "IT-5", "category": "taxi", "date": "2026-10-03", "amount": "140000",
                                 "provenance": {}}, _file(stored="s_evidence.png"))])
    mid = next(iter(rows.messages))
    rows.calls.clear()
    client.post(f"/admin/claims/{HERO}/evidence/{mid}/review", data={"verdict": "accept"})
    assert {"update_message", "save_review"} <= set(rows.calls)
    assert rows.messages[mid]["attachments"][0]["evidence_status"] == "accepted_by_reviewer"


def test_a_decision_saves_the_review(client, rows):
    client.post(f"/admin/claims/{HERO}/decide", data={"item_id": "IT-3", "choice": "approve_capped"})
    assert "save_review" in rows.calls
    assert rows.reviews[HERO]["decisions"]["IT-3"] == "approve_capped"


def test_a_message_to_the_employee_stores_the_outbox_row_and_the_reply(client, rows):
    client.post(f"/admin/claims/{HERO}/message",
                data={"subject": "Claim CLM-2026-0110: taxi receipt",
                      "body": "Mohon kirim struk taksi tanggal 3 Okt (IT-5)."})
    assert "add_outbox" in rows.calls
    assert rows.outbox
    assert "add_message" in rows.calls  # the scripted reply the mock mailbox delivered


def test_asking_a_manager_stores_the_request(client, rows):
    client.post(f"/admin/claims/{HERO}/items/IT-3/manager", data={"reason": "Perlu konfirmasi"})
    assert "save_approval_request" in rows.calls
    assert rows.requests and next(iter(rows.requests.values()))["status"] == "pending"


def test_setting_a_status_stores_the_change_before_memory_moves(client, rows):
    client.post(f"/admin/claims/{HERO}/status", data={"status": "on_hold", "reviewer_confirmed": "true"})
    assert "add_status_change" in rows.calls
    assert rows.status_changes[0]["to"] == "on_hold"
    assert web.store.get_claim(HERO)["status"] == "on_hold"


def test_a_draft_is_stored_when_it_is_created(client, rows):
    d = draft_with([_file()])
    r = client.post(f"/employee/drafts/{d['id']}", data={"action": "save"}, follow_redirects=False)
    assert r.status_code == 303
    assert "save_draft" in rows.calls and d["id"] in rows.drafts


# --- a store that fails leaves memory alone ----------------------------------------------------
def test_a_failing_store_answers_503_and_keeps_the_claims(client, rows):
    rows.fail_on = {"add_claim"}
    before = snapshot()
    r = client.post("/employee/claims", data={**TRIP, "items": "taxi, 2026-10-02, Station, 150000, no"})
    assert r.status_code == 503
    assert snapshot() == before


def test_a_failing_store_keeps_the_review(client, rows):
    rows.fail_on = {"save_review"}
    before = snapshot()
    r = client.post(f"/admin/claims/{HERO}/evidence", data={"evidence_id": "MSG-5201"})
    assert r.status_code == 503
    assert snapshot() == before


def test_a_failing_store_keeps_the_outbox_and_undelivers_the_reply(client, rows):
    rows.fail_on = {"add_outbox"}
    before = snapshot()
    r = client.post(f"/admin/claims/{HERO}/message",
                    data={"subject": "Claim CLM-2026-0110: taxi receipt",
                          "body": "Mohon kirim struk taksi tanggal 3 Okt (IT-5)."})
    assert r.status_code == 503
    assert snapshot() == before


def test_a_failing_store_keeps_the_manager_request(client, rows):
    rows.fail_on = {"save_approval_request"}
    before = snapshot()
    r = client.post(f"/admin/claims/{HERO}/items/IT-3/manager", data={"reason": "Perlu konfirmasi"})
    assert r.status_code == 503
    assert snapshot() == before


def test_a_failing_store_keeps_the_status(client, rows):
    rows.fail_on = {"add_status_change"}
    before = snapshot()
    r = client.post(f"/admin/claims/{HERO}/status", data={"status": "on_hold", "reviewer_confirmed": "true"})
    assert r.status_code == 503
    assert snapshot() == before


def test_a_submit_that_fails_midway_leaves_no_half_claim(client, rows):
    d = draft_with([_file()])
    rows.fail_on = {"save_review"}  # the claim and its message are already written when this fails
    before = snapshot()
    r = client.post(f"/employee/drafts/{d['id']}", data=submit_form(d), follow_redirects=False)
    assert r.status_code == 503
    assert snapshot() == before
    assert d["id"] in web.DRAFTS  # the draft is still there, untouched


def test_a_failing_store_keeps_a_draft_edit(client, rows):
    d = draft_with([_file()])
    rows.fail_on = {"save_draft"}
    before = snapshot()
    form = submit_form(d)
    form["action"] = "save"
    r = client.post(f"/employee/drafts/{d['id']}", data=form, follow_redirects=False)
    assert r.status_code == 503
    assert snapshot() == before


def test_reset_demo_refuses_when_the_store_is_down(client, rows, monkeypatch):
    rows.fail_on = {"bump_epoch"}

    def boom():
        raise RuntimeError("demo_epoch is down")

    monkeypatch.setattr(rows, "bump_epoch", boom)
    before = snapshot()
    r = client.post("/admin/reset", follow_redirects=False)
    assert r.status_code == 503
    assert snapshot() == before
