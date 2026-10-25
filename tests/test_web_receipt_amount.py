"""Web upload forwards the amount read from the file, not the item amount (no OCR needed: a fake extract)."""

from reimburse_web import app as web
from reimburse_web import upload_flow as uf


def _file(amount):
    return {"name": "taxi.jpg", "doc_type": "taxi_receipt", "doc_type_source": "ocr", "stored": "x.jpg", "sha256": "f1",
            "extract": {"fields": {"amount": {"value": amount}, "date": {"value": None}}}}


def test_forward_uses_extracted_amount_and_employee_sees_review():
    web.reset_all()
    try:
        cid = "CLM-2026-0110"
        claim = web.store.get_claim(cid)
        item = next(i for i in claim["items"] if i["id"] == "IT-5")
        mid = uf._forward("EMP-001", cid, item, _file(int(item["amount"]) - 30000), claim["trip"])
        att = next(m for m in web.store.inbox if m["id"] == mid)["attachments"][0]
        assert att["amount"] == int(item["amount"]) - 30000 and att["amount_required"] is True
        web.review(cid)["pending"].pop(mid, None)  # as if a reviewer accepted the file
        web.review(cid)["evidence"].append(mid)
        res = next(i for i in web.run_check(cid).items if i.item_id == "IT-5")
        assert "receipt_amount_mismatch" in res.gaps
        page = uf.employee_claim(cid).body.decode()
        assert uf.REVIEWING in page
    finally:
        web.reset_all()
