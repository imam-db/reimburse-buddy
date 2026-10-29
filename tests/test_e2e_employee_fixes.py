"""ADR 0009, employee side (P2). Stub extractors only: nothing here needs Tesseract."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from reimburse_web import app as web
from reimburse_web import upload_flow

from test_upload_flow_ux import bad, drop, form_of, good, visible


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "UPLOAD_DIR", tmp_path)
    monkeypatch.setenv("REIMBURSE_DEV_EMPLOYEE", "EMP-001")
    monkeypatch.delenv("EMPLOYEE_QA", raising=False)
    web.reset_all()
    yield TestClient(web.app)
    web.reset_all()


def test_reasons_show_as_hints_before_the_first_save_and_as_errors_after(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad()})
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert "class='hint note'>Pilih kategori." in page and "class='hint note'>Isi nominal." in page
    assert "<div class=err>Isi nominal.</div>" not in page
    client.post(f"/employee/drafts/{d['id']}", data={**form_of(d), "action": "save"})
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert "<div class=err>Isi nominal.</div>" in page and "class='hint note'" not in page


def test_banner_lists_the_card_and_its_first_reason(client, monkeypatch):
    d = drop(client, monkeypatch, {"bad.png": bad(),
                                   "ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    page = client.get(f"/employee/drafts/{d['id']}").text
    banner = page.split("Lengkapi:", 1)[1].split("</div>", 1)[0]
    assert "Kartu 1</a>: kategori belum dipilih" in banner and "Kartu 2" not in banner


def test_empty_amount_and_malformed_amount_have_different_messages(client, monkeypatch):
    d = drop(client, monkeypatch, {"ok.png": good(doc_type="taxi_receipt", date="2026-10-03",
                                                  amount=150000, merchant="Taksi")})
    card = d["cards"][0]
    h = d["header"]
    card["fields"]["amount"]["value"] = None
    assert "Isi nominal." in upload_flow._card_errors(card, h)
    card["fields"]["amount"]["value"] = "abc"
    errors = upload_flow._card_errors(card, h)
    assert "Nominal harus bilangan bulat rupiah, misalnya 1.350.000." in errors and "Isi nominal." not in errors


def test_boarding_pass_only_flight_card_explains_the_missing_price(client, monkeypatch):
    d = drop(client, monkeypatch, {"bp.png": good(doc_type="boarding_pass", date="2026-10-02",
                                                  flight_no="QZ 7520")})
    page = client.get(f"/employee/drafts/{d['id']}").text
    assert "Boarding pass tidak memuat harga. Tambahkan e-ticket atau isi harga tiket." in page
    assert "Isi nominal." in page
