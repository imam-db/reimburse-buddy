"""Cross-validation of uploaded evidence (PR #10). Confidence is a display hint; these rules decide needs_review."""
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reimburse_buddy.triage import triage_claims
from reimburse_web import app as web
from reimburse_web.extract import parse_date, parse_text, select_amount
from reimburse_web.upload_flow import evidence_review_reasons

SAMPLES = Path(__file__).resolve().parents[1] / "web" / "sample_uploads"
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
TRIP = {"start_date": "2026-10-02", "end_date": "2026-10-04"}


def file(doc_type="taxi_receipt", source="ocr", error="", **ex):
    fields = {k: {"value": v, "confidence": 0.9, "source": "ocr"} for k, v in ex.items()}
    return {"doc_type": doc_type, "doc_type_source": source, "error": error, "extract": {"fields": fields}}


TAXI = {"id": "IT-1", "category": "taxi", "date": "2026-10-03", "amount": "230000"}


def reasons(f, item=TAXI, passenger="Rina Wulandari", dup=False):
    return evidence_review_reasons(f, item, TRIP, passenger, dup)


# --- each rule in isolation ---------------------------------------------------
def test_matching_file_is_verified():
    assert reasons(file(amount=230000, date="2026-10-03")) == []


def test_amount_or_date_differs_from_file():
    assert reasons(file(amount=230000, date="2026-10-03"), dict(TAXI, amount="2300000")) == \
        ["mismatch_with_file", "implausible_amount"]
    assert reasons(file(amount=230000, date="2026-10-02"), TAXI) == ["mismatch_with_file"]


def test_flight_no_differs_from_file():
    item = {"id": "IT-1", "category": "flight", "date": "2026-10-02", "amount": "1120000", "flight_no": "QZ 7521"}
    f = file("flight_receipt", amount=1120000, date="2026-10-02", flight_no="QZ 7520")
    assert reasons(f, item) == ["mismatch_with_file"]
    assert reasons(f, dict(item, flight_no="qz7520")) == []  # spacing/case do not matter


def test_file_date_outside_trip():
    assert "file_date_outside_trip" in reasons(file(amount=230000, date="2026-09-28"))


def test_doc_type_vs_category():
    assert reasons(file("meal_receipt", amount=230000, date="2026-10-03")) == ["doc_type_mismatch"]


def test_implausible_amount_threshold_is_configurable(monkeypatch):
    f = file(amount=1200000, date="2026-10-03")
    item = dict(TAXI, amount="1200000")
    assert reasons(f, item) == ["implausible_amount"]
    monkeypatch.setenv("REIMBURSE_PLAUSIBLE_MAX", '{"taxi": 1500000}')
    assert reasons(f, item) == []


def test_hotel_plausibility_is_per_night():
    hotel = {"id": "IT-1", "category": "hotel", "date": "2026-10-02", "amount": "9000000", "nights": 2}
    assert reasons(file("hotel_receipt", amount=9000000, date="2026-10-02"), hotel) == []


def test_boarding_pass_passenger_or_flight():
    item = {"id": "IT-1", "category": "flight", "date": "2026-10-02", "amount": "1120000", "flight_no": "QZ 7520"}
    bp = file("boarding_pass", date="2026-10-02", flight_no="QZ 7520", passenger="RINA WULANDARI")
    assert reasons(bp, item) == []
    assert reasons(bp, item, passenger="Bagus Pratama") == ["boarding_pass_mismatch"]


def test_unreadable_and_self_declared_and_duplicate():
    assert reasons(file(error="No receipt fields found")) == ["unreadable_file"]
    assert reasons(file(source="self_declared", amount=230000, date="2026-10-03")) == ["self_declared_only"]
    assert reasons(file(date="2026-10-03")) == ["self_declared_only"]  # file shows no amount
    assert reasons(file(amount=230000, date="2026-10-03"), dup=True) == ["duplicate_file"]


# --- amount selection and dates -----------------------------------------------
def test_total_with_item_count_and_cash_is_not_cash():
    t = "SUBTOTAL 163.000\nPPN 16.300\nSERVICE 8.150\nTOTAL (3 item) 187.450\nTUNAI 200.000\nKEMBALI 12.550"
    assert select_amount(t)[0] == 187450 and select_amount(t)[2] == 1.0


def test_total_ppn_and_total_diskon_are_not_the_total():
    t = "SUBTOTAL 140.000\nDISKON 14.000\nPPN 12.600\nTOTAL 138.600\nTOTAL DISKON 14.000\nTOTAL PPN 12.600"
    assert select_amount(t)[:1] == (138600,)


def test_cash_minus_change_confirms_total():
    assert select_amount("Argo Rp 185.000\nTOTAL Rp 185.000\nTUNAI Rp 300.000\nKEMBALI Rp 115.000") == \
        (185000, select_amount("Argo Rp 185.000\nTOTAL Rp 185.000\nTUNAI Rp 300.000\nKEMBALI Rp 115.000")[1], 1.0)


def test_inconsistent_total_is_low_confidence():
    d = parse_text("RESTO CONTOH\nTanggal 02/10/2026\nSUBTOTAL 100.000\nPPN 10.000\nTOTAL 150.000", None, "ocr")
    assert d.fields["amount"].value == 150000 and d.fields["amount"].low


def test_no_total_never_picks_cash():
    assert select_amount("Argo Rp 185.000\nTUNAI Rp 300.000")[0] == 185000


def test_two_digit_year():
    assert parse_date("03/10/26 21:40")[0] == "2026-10-03"
    assert parse_date("Ref 12/34/5678") is None


# --- flows ---------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.delenv("REIMBURSE_EXTRACTOR", raising=False)
    monkeypatch.delenv("REIMBURSE_PLAUSIBLE_MAX", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def drop(client, names, emp="EMP-001", start="2026-10-02", end="2026-10-04"):
    trip = dict(emp=emp, purpose="Client visit", destination="Surabaya", start_date=start, end_date=end)
    files = [("files", (Path(n).name, (SAMPLES / n).read_bytes())) for n in names]
    r = client.post("/employee/drafts", data=trip, files=files, follow_redirects=False)
    return web.DRAFTS[r.headers["location"].rsplit("/", 1)[1]]


def submit(client, d, **overrides):
    form = {}
    for c in d["cards"]:
        for k, v in c["fields"].items():
            form[f"c_{c['id']}_{k}"] = "" if v["value"] is None else str(v["value"])
        for f in c["files"]:
            form[f"f_{f['id']}_doc_type"] = f["doc_type"]
    form.update(overrides)
    r = client.post(f"/employee/drafts/{d['id']}", data=dict(form, action="submit"), follow_redirects=False)
    assert "/employee/claims/" in r.headers["location"], d.get("errors") or [c["errors"] for c in d["cards"]]
    return r.headers["location"].split("/")[3].split("?")[0]


def test_txt_uploads_are_rejected(client):
    r = client.post("/employee/drafts", data=dict(emp="EMP-001", purpose="x", destination="Surabaya",
                                                  start_date="2026-10-02", end_date="2026-10-04"),
                    files=[("files", ("taxi.txt", b"TAKSI\nTOTAL Rp 230.000", "text/plain"))])
    assert r.status_code == 400


@needs_tesseract
def test_claude_exploit_two_identical_receipts_one_amount_one_date_edited(client):
    """Reproduces the review exploit: before PR #10 this claim was approve, Rp2.300.000, and bulk-cleared."""
    d = drop(client, ["taxi_kota_biru_0310.jpg", "taxi_kota_biru_0310.jpg"])
    a, b = (c["id"] for c in d["cards"])
    cid = submit(client, d, **{f"c_{a}_amount": "2.300.000", f"c_{b}_date": "2026-10-04"})
    pend = web.review(cid)["pending"]
    assert set(web.pending_items(cid)) == {"IT-1", "IT-2"}
    assert {"mismatch_with_file", "implausible_amount", "duplicate_file"} <= set(pend["MSG-W001"]["reasons"])
    assert {"mismatch_with_file", "duplicate_file"} <= set(pend["MSG-W002"]["reasons"])
    chk = web.run_check(cid)
    assert chk.decision.value == "needs_info" and chk.total_approvable == 0
    assert cid not in triage_claims(web.store, web.WEB_BATCH)["auto_cleared"]
    client.post("/admin/batch/approve", data={"batch": web.WEB_BATCH, "claim_ids": [cid], "reviewer_confirmed": "true"})
    client.post(f"/admin/claims/{cid}/status", data={"status": "approved", "reviewer_confirmed": "true"})
    assert web.store.get_claim(cid)["status"] == "submitted"
    assert "mismatch_with_file" in client.get(f"/admin/claims/{cid}").text


@needs_tesseract
def test_same_file_in_a_later_claim_is_duplicate(client):
    cid1 = submit(client, drop(client, ["taxi_kota_biru_0310.jpg"]))
    assert web.pending_items(cid1) == {}
    cid2 = submit(client, drop(client, ["taxi_kota_biru_0310.jpg"]))
    assert web.review(cid2)["pending"]["MSG-W002"]["reasons"] == ["duplicate_file"]


@needs_tesseract
def test_boarding_pass_metadata_comes_from_the_file_wrong_trip_trap(client):
    """A pass from the 2 Oct trip cannot clear a 20 Oct flight, even with the right flight number typed."""
    d = drop(client, ["boarding_pass_qz7520.png", "eticket_qz7520.pdf"], start="2026-10-20", end="2026-10-22")
    c = d["cards"][0]["id"]
    cid = submit(client, d, **{f"c_{c}_date": "2026-10-20"})
    att = next(m for m in web.store.inbox if m["id"] == "MSG-W001")["attachments"][0]
    assert (att["type"], att["date"], att["flight"], att["passenger"]) == \
        ("boarding_pass", "2026-10-02", "QZ 7520", "RINA WULANDARI")
    assert {"mismatch_with_file", "file_date_outside_trip"} <= set(web.review(cid)["pending"]["MSG-W001"]["reasons"])
    # Even if a reviewer accepts both files, the pass's own date does not match the flight: still missing.
    for mid in ("MSG-W001", "MSG-W002"):
        client.post(f"/admin/claims/{cid}/evidence/{mid}/review", data={"verdict": "accept"})
    assert "boarding_pass_missing" in web.run_check(cid).items[0].gaps


@needs_tesseract
def test_someone_elses_boarding_pass(client):
    d = drop(client, ["boarding_pass_qz7520.png", "eticket_qz7520.pdf"], emp="EMP-002")
    cid = submit(client, d)
    assert "boarding_pass_mismatch" in web.review(cid)["pending"]["MSG-W001"]["reasons"]
    assert "boarding_pass_missing" in web.run_check(cid).items[0].gaps


@needs_tesseract
def test_honest_claim_still_clears(client):
    d = drop(client, ["taxi_kota_biru_0310.jpg", "boarding_pass_qz7520.png", "eticket_qz7520.pdf"])
    cid = submit(client, d)
    assert web.pending_items(cid) == {}
    assert web.run_check(cid).decision.value == "approve"
