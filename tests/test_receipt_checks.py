"""Receipt amount mismatch and multiple receipts for one item (synthetic data in a temp copy)."""

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from reimburse_buddy.data_store import DataStore
from reimburse_buddy.decision import draft_decision
from reimburse_buddy.models import Decision, ItemStatus
from reimburse_buddy.rules import check_claim

DATA = Path(__file__).parents[1] / "data"
CID = "CLM-2026-0110"  # hero; IT-5 is the taxi without a receipt (EMP-001)


def _rcpt(mid, no, amount=None):
    a = {"name": f"{mid}.jpg", "type": "receipt", "claim_id": CID, "item_id": "IT-5", "receipt_no": no}
    if amount is not None:
        a["amount"] = amount
    return {"id": mid, "owner_id": "EMP-001", "from": "rina.wulandari@nusantara-niaga.example",
            "date": "2026-10-24", "subject": f"receipt {mid}", "body": "receipt", "attachments": [a],
            "mailbox": "reimburse@nusantara-niaga.example"}


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "data"
    shutil.copytree(DATA, d)
    inbox = json.loads((d / "inbox.json").read_text())
    claim = next(c for c in json.loads((d / "claims.json").read_text()) if c["id"] == CID)
    amt = next(i["amount"] for i in claim["items"] if i["id"] == "IT-5")
    inbox += [_rcpt("MSG-9901", "TX-1", amt), _rcpt("MSG-9902", "TX-2", str(int(amt) + 50000)),
              _rcpt("MSG-9903", "TX-3")]
    (d / "inbox.json").write_text(json.dumps(inbox))
    return DataStore(d)


def item(chk, iid="IT-5"):
    return next(i for i in chk.items if i.item_id == iid)


def test_matching_amount_accepted(store):
    it = item(check_claim(store, CID, ["MSG-9901"]))
    assert "receipt_missing" not in it.gaps and "MSG-9901" in it.evidence_used
    assert "receipt_amount_mismatch" not in it.gaps


def test_receipt_without_amount_is_not_compared(store):
    it = item(check_claim(store, CID, ["MSG-9903"]))
    assert it.gaps == [g for g in it.gaps if g not in ("receipt_missing", "receipt_amount_mismatch")]


def test_amount_mismatch_blocks_item(store):
    chk = check_claim(store, CID, ["MSG-9902"])
    it = item(chk)
    assert "receipt_amount_mismatch" in it.gaps
    assert it.status == ItemStatus.NEEDS_EVIDENCE and it.approvable == Decimal(0)
    assert chk.decision == Decision.NEEDS_INFO
    f = next(f for f in it.findings if f.gap == "receipt_amount_mismatch")
    assert Decimal(f.params["receipt_amount"]) == it.claimed + 50000 and Decimal(f.params["item_amount"]) == it.claimed


def test_multiple_receipts_flagged_even_if_one_matches(store):
    it = item(check_claim(store, CID, ["MSG-9901", "MSG-9902"]))
    assert "multiple_receipts" in it.gaps and "receipt_amount_mismatch" not in it.gaps
    assert it.approvable == 0 and set(it.evidence_used) >= {"MSG-9901", "MSG-9902"}


def test_reviewer_can_resolve(store):
    it = item(check_claim(store, CID, ["MSG-9901", "MSG-9902"], {"IT-5": "approve_full"}))
    assert it.status == ItemStatus.OK and it.approvable == it.claimed


def test_note_renders_new_findings(store):
    chk = check_claim(store, CID, ["MSG-9902"])
    note = draft_decision(store, chk, language="both")
    assert "MSG-9902" in note and "Perlu ditinjau" in note
