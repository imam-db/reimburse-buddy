import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web

SAMPLES = Path(__file__).resolve().parents[1] / "web" / "sample_uploads"
pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
TRIP = dict(emp="EMP-001", purpose="Client visit", destination="Surabaya", start_date="2026-10-02", end_date="2026-10-04")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.delenv("REIMBURSE_EXTRACTOR", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def drop(client, names):
    files = [("files", (n, (SAMPLES / n).read_bytes())) for n in names]
    r = client.post("/employee/drafts", data=TRIP, files=files, follow_redirects=False)
    assert r.status_code == 303
    return web.DRAFTS[r.headers["location"].rsplit("/", 1)[1]]


def fields(card):
    return {k: v["value"] for k, v in card["fields"].items()}


def form_of(d):
    out = {}
    for c in d["cards"]:
        for k, v in c["fields"].items():
            val = v["value"]
            out[f"c_{c['id']}_{k}"] = "" if val is None else str(val)
        for f in c["files"]:
            out[f"f_{f['id']}_doc_type"] = f["doc_type"]
    return out


def test_drop_page_renders(client):
    r = client.get("/employee/new")
    assert r.status_code == 200 and 'id="drop"' in r.text and "multiple" in r.text


def test_drop_makes_cards_and_pairs_flight_files(client):
    d = drop(client, ["hotel_majapahit_kencana.png", "taxi_kota_biru_0310.jpg", "boarding_pass_qz7520.png",
                      "eticket_qz7520.pdf", "unreadable_photo.jpg"])
    cards = [fields(c) for c in d["cards"]]
    assert len(cards) == 4
    assert cards[0]["amount"] == 2700000 and cards[0]["nights"] == 2
    assert cards[2]["flight_no"] == "QZ 7520" and len(d["cards"][2]["files"]) == 2
    assert cards[3]["amount"] is None  # unreadable -> empty card for manual input
    assert d["cards"][0]["fields"]["amount"]["source"] == "ocr"
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert "extracted (ocr" in page and "Could not read this file" in page


def test_edits_are_self_declared_and_submit_forwards_files(client):
    d = drop(client, ["hotel_majapahit_kencana.png", "taxi_kota_biru_0310.jpg", "unreadable_photo.jpg"])
    form = form_of(d)
    blank = d["cards"][2]["id"]
    form.update({f"c_{blank}_category": "meal", f"c_{blank}_date": "2026-10-03", f"c_{blank}_amount": "95.000",
                 f"c_{blank}_merchant": "Warung"})
    client.post(f"/employee/drafts/{d['id']}", data=dict(form, action="save"))
    meal = d["cards"][2]["fields"]
    assert meal["amount"] == {"value": 95000, "confidence": 1.0, "source": "self_declared"}
    assert d["cards"][0]["fields"]["amount"]["source"] == "ocr"  # untouched stays extracted
    r = client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action="submit"), follow_redirects=False)
    cid = r.headers["location"].split("/")[3].split("?")[0]
    claim = web.store.get_claim(cid)
    assert claim["batch"] == web.WEB_BATCH and len(claim["items"]) == 3
    assert claim["items"][2]["provenance"]["amount"] == "self_declared"
    fwd = [m for m in web.store.inbox if m["id"].startswith("MSG-W")]
    assert len(fwd) == 3 and fwd[0]["attachments"][0] == fwd[0]["attachments"][0] | {
        "type": "receipt", "claim_id": cid, "item_id": "IT-1"}
    chk = web.run_check(cid)
    assert all("receipt_missing" not in i.gaps for i in chk.items)
    page = client.get(r.headers["location"]).text
    assert "forwarded to" in page and "Add file" in page
    admin = client.get(f"/admin/claims/{cid}").text
    assert "amount: self_declared" in admin and "amount: ocr" in admin
    # Triage of the W43 batch is unaffected.
    assert "10 claims, 7 cleared, 3 exceptions" in client.get("/admin").text


def test_missing_evidence_then_add_file(client):
    d = drop(client, ["taxi_kota_biru_0310.jpg"])
    client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action="add"))
    new = d["cards"][1]["id"]
    form = dict(form_of(d), **{f"c_{new}_category": "taxi", f"c_{new}_date": "2026-10-04", f"c_{new}_amount": "120000",
                                f"c_{new}_merchant": "Taksi"})
    r = client.post(f"/employee/drafts/{d['id']}", data=dict(form, action="submit"), follow_redirects=False)
    cid = r.headers["location"].split("/")[3].split("?")[0]
    assert "receipt missing" in client.get(f"/employee/claims/{cid}").text
    client.post(f"/employee/claims/{cid}/items/IT-2/files",
                files={"file": ("taxi2.jpg", (SAMPLES / "taxi_pelangi_blurry.jpg").read_bytes())})
    assert all("receipt_missing" not in i.gaps for i in web.run_check(cid).items)


def test_submit_validation_and_card_actions(client):
    d = drop(client, ["taxi_kota_biru_0310.jpg", "meal_sari_rasa_2010.png"])
    a, b = (c["id"] for c in d["cards"])
    # meal is dated 20 Oct, outside the 2-4 Oct trip
    client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action="submit"))
    assert d["id"] in web.DRAFTS and "Date must fall within the trip." in d["cards"][1]["errors"]
    client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action=f"merge:{b}", **{f"merge_{b}": a}))
    assert len(d["cards"]) == 1 and len(d["cards"][0]["files"]) == 2
    client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action=f"split:{a}"))
    assert len(d["cards"]) == 2
    client.post(f"/employee/drafts/{d['id']}", data=dict(form_of(d), action=f"delete:{d['cards'][1]['id']}"))
    assert len(d["cards"]) == 1


def test_flight_needs_flight_no(client):
    d = drop(client, ["eticket_qz7520.pdf"])
    form = form_of(d)
    form[f"c_{d['cards'][0]['id']}_flight_no"] = ""
    client.post(f"/employee/drafts/{d['id']}", data=dict(form, action="submit"))
    assert "Flight number is required for flights." in d["cards"][0]["errors"]
