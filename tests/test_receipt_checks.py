"""Receipt amount checks (synthetic data in a temp copy of data/).

The engine compares every receipt the mailbox holds for (claim, item), not only the ones passed in."""

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from reimburse_buddy.amounts import normalize_attachment, parse_idr
from reimburse_buddy.data_store import DataStore
from reimburse_buddy.decision import draft_decision
from reimburse_buddy.models import Decision, ItemStatus
from reimburse_buddy.rules import check_claim

DATA = Path(__file__).parents[1] / "data"
CID = "CLM-2026-0110"  # hero; IT-5 is the taxi without a receipt (EMP-001)
AMT = next(int(i["amount"]) for c in json.loads((DATA / "claims.json").read_text()) if c["id"] == CID
           for i in c["items"] if i["id"] == "IT-5")


def rcpt(mid, no=None, amount=None, item="IT-5", **extra):
    a = {"name": f"{mid}.jpg", "type": "receipt", "claim_id": CID, "item_id": item, **extra}
    if no:
        a["receipt_no"] = no
    if amount is not None:
        a["amount"] = amount
    return {"id": mid, "owner_id": "EMP-001", "from": "rina.wulandari@nusantara-niaga.example",
            "date": "2026-10-24", "subject": f"receipt {mid}", "body": "receipt", "attachments": [a],
            "mailbox": "reimburse@nusantara-niaga.example"}


@pytest.fixture
def make(tmp_path):
    def _make(*msgs, required=False):
        d = tmp_path / f"d{len(list(tmp_path.iterdir()))}"
        shutil.copytree(DATA, d)
        inbox = json.loads((d / "inbox.json").read_text()) + list(msgs)
        (d / "inbox.json").write_text(json.dumps(inbox))
        pol = json.loads((d / "policy.json").read_text())
        pol["receipt_amount_required"] = required
        (d / "policy.json").write_text(json.dumps(pol))
        return DataStore(d)
    return _make


def item(chk, iid="IT-5"):
    return next(i for i in chk.items if i.item_id == iid)


def test_parse_idr_strict():
    assert parse_idr("Rp 120.000") == 120000 and parse_idr("230.000") == 230000
    assert parse_idr("120.000,00") == 120000 and parse_idr("Rp120000") == 120000 and parse_idr(150000) == 150000
    for bad in ("120,000", "12.5", "1.20.000", "", "Rp", "-5000", 0, 12.0, True, None):
        with pytest.raises(ValueError):
            parse_idr(bad)


def test_attachment_schema_only_integer_amount():
    assert normalize_attachment({"type": "receipt", "amount": "Rp 120.000"})["amount"] == 120000
    a = normalize_attachment({"type": "receipt", "amount": "120,000"})
    assert "amount" not in a and a["amount_error"]
    assert normalize_attachment({"type": "receipt", "total": 5000})["amount_error"]


def test_matching_amount_accepted(make):
    it = item(check_claim(make(rcpt("MSG-9901", "TX-1", AMT)), CID, ["MSG-9901"]))
    assert it.gaps == [] and it.status == ItemStatus.OK and "MSG-9901" in it.evidence_used


def test_passing_only_the_matching_receipt_still_flags_the_other(make):
    # Review finding 1: the engine looks the item's receipts up itself.
    store = make(rcpt("MSG-9901", "TX-1", AMT), rcpt("MSG-9902", "TX-2", AMT + 50000))
    chk = check_claim(store, CID, ["MSG-9901"])
    assert item(chk).gaps == ["multiple_receipts"] and chk.decision == Decision.NEEDS_INFO


def test_item_above_receipt_needs_review_and_capped_pays_receipt(make):
    store = make(rcpt("MSG-9902", "TX-2", AMT - 20000))
    chk = check_claim(store, CID, ["MSG-9902"])
    it = item(chk)
    assert it.gaps == ["receipt_amount_mismatch"] and it.approvable == 0 and chk.decision == Decision.NEEDS_INFO
    f = next(f for f in it.findings if f.gap == "receipt_amount_mismatch")
    assert Decimal(f.params["receipt_amount"]) == AMT - 20000 and Decimal(f.params["item_amount"]) == AMT
    capped = item(check_claim(store, CID, ["MSG-9902"], {"IT-5": "approve_capped"}))
    assert capped.approvable == AMT - 20000 and capped.status == ItemStatus.PARTIAL
    full = item(check_claim(store, CID, ["MSG-9902"], {"IT-5": "approve_full"}))
    assert full.approvable == AMT


def test_item_below_receipt_is_ok_with_note(make):
    store = make(rcpt("MSG-9902", "TX-2", AMT + 50000))
    chk = check_claim(store, CID, ["MSG-9902"])
    it = item(chk)
    assert it.gaps == [] and it.approvable == AMT
    assert any(f.key == "receipt_higher" for f in it.findings)
    assert "MSG-9902" in draft_decision(store, chk, language="en")


def test_agreeing_copies_are_not_flagged(make):
    store = make(rcpt("MSG-9901", "TX-1", AMT), rcpt("MSG-9902", "TX-1", AMT), rcpt("MSG-9903", "TX-9", AMT))
    assert item(check_claim(store, CID, ["MSG-9901", "MSG-9902", "MSG-9903"])).gaps == []


def test_multiple_receipts_need_a_named_receipt(make):
    store = make(rcpt("MSG-9901", "TX-1", AMT), rcpt("MSG-9902", "TX-2", AMT + 50000))
    for choice in ("approve_full", "approve_capped", "use:MSG-0000"):
        with pytest.raises(ValueError, match="use:"):
            check_claim(store, CID, ["MSG-9901"], {"IT-5": choice})
    chk = check_claim(store, CID, ["MSG-9901"], {"IT-5": "use:MSG-9902"})
    assert item(chk).approvable == AMT and item(chk).human_decision == "use:MSG-9902"
    note = draft_decision(store, chk, language="both")
    assert "use:MSG-9902" in note and "pakai struk MSG-9902" in note


def test_missing_amount_only_matters_when_required(make):
    assert item(check_claim(make(rcpt("MSG-9903", "TX-3")), CID, ["MSG-9903"])).gaps == []
    it = item(check_claim(make(rcpt("MSG-9903", "TX-3"), required=True), CID, ["MSG-9903"]))
    assert it.gaps == ["receipt_amount_mismatch"] and any(f.key == "receipt_amount_missing" for f in it.findings)


def test_web_uploads_require_an_amount_even_when_policy_does_not(make):
    it = item(check_claim(make(rcpt("MSG-9903", amount_required=True)), CID, ["MSG-9903"]))
    assert "receipt_amount_mismatch" in it.gaps


def test_unreadable_amount_needs_review(make):
    it = item(check_claim(make(rcpt("MSG-9904", "TX-4", "120,000")), CID, ["MSG-9904"]))
    assert any(f.key == "receipt_amount_unreadable" for f in it.findings) and it.approvable == 0


def test_one_receipt_on_two_items_is_flagged(make):
    # IT-3 in the hero claim is a receipt-required item; give both the same file.
    store = make(rcpt("MSG-9905", amount=AMT, sha256="abc"), rcpt("MSG-9906", amount=AMT, item="IT-3", sha256="abc"))
    assert "receipt_shared" in item(check_claim(store, CID, ["MSG-9905", "MSG-9906"])).gaps


def test_note_renders_new_findings(make):
    store = make(rcpt("MSG-9902", "TX-2", AMT - 20000))
    note = draft_decision(store, check_claim(store, CID, ["MSG-9902"]), language="both")
    assert "MSG-9902" in note and "Perlu ditinjau" in note
