"""Dual-engine OCR (Tesseract + RapidOCR): disagreement -> needs_review, and Tesseract-only fallback."""
import builtins
import shutil
from pathlib import Path

import pytest

from reimburse_web import extract
from reimburse_web.extract import Draft, Field, boxes_to_lines, combine, get_extractor
from reimburse_web.upload_flow import _file_entry, evidence_review_reasons

SAMPLES = Path(__file__).resolve().parents[1] / "web" / "sample_uploads"
TRIP = {"start_date": "2026-10-02", "end_date": "2026-10-04"}
TAXI = {"id": "IT-1", "category": "taxi", "date": "2026-10-03", "amount": "230000"}


def draft(**vals):
    d = Draft(text="x")
    for k, v in vals.items():
        d.fields[k] = Field(v, 0.95, "ocr")
    return d


BASE = dict(doc_type="taxi_receipt", date="2026-10-03", amount=230000, merchant="TAKSI KOTA BIRU")


def test_engines_agree_no_flag():
    d = combine(draft(**BASE), draft(**dict(BASE, merchant="Taksi Kota Biru")))  # case/punctuation ignored
    assert d.disagreements == []
    f = _file_entry("s", "taxi.jpg", d)
    assert evidence_review_reasons(f, TAXI, TRIP, "Rina", False) == []


def test_amount_disagreement_goes_to_needs_review():
    d = combine(draft(**BASE), draft(**dict(BASE, amount=2300000)))
    assert d.disagreements == ["amount"]
    assert d.fields["amount"].value == 230000 and d.fields["amount"].low  # Tesseract value kept, highlighted
    f = _file_entry("s", "taxi.jpg", d)
    assert evidence_review_reasons(f, TAXI, TRIP, "Rina", False) == ["ocr_engine_disagreement"]


def test_field_read_by_only_one_engine_is_a_disagreement():
    d = combine(draft(**BASE), draft(**{k: v for k, v in BASE.items() if k != "date"}))
    assert d.disagreements == ["date"]


def test_one_engine_failing_uses_the_other():
    bad = Draft(error="unreadable")
    assert combine(bad, draft(**BASE)).fields["amount"].value == 230000
    assert combine(draft(**BASE), bad).disagreements == []


def test_glued_date_and_time_lines_are_rebuilt():
    boxes = [[[37, 152], [435, 152], [435, 185], [37, 185]], [[192, 363], [356, 363], [356, 395], [192, 395]],
             [[35, 362], [123, 362], [123, 393], [35, 393]]]
    text, conf = boxes_to_lines(boxes, ["Tanggal 03-10-202609:12", "Rp 230.000", "TOTAL"], [1.0, 0.9, 1.0])
    assert text == "Tanggal 03-10-2026 09:12\nTOTAL Rp 230.000"
    assert conf[1] == pytest.approx(0.95)
    assert extract.parse_date(text)[0] == "2026-10-03"
    box = [[[0, 0], [9, 0], [9, 9], [0, 9]]]
    assert boxes_to_lines(box, ["03/10/2621:40"], [1.0])[0] == "03/10/26 21:40"
    assert boxes_to_lines(box, ["Date 02 0ct 2026 Boarding 06:55"], [1.0])[0] == "Date 02 Oct 2026 Boarding 06:55"


@pytest.mark.parametrize("mode", ["dual", "rapidocr"])
def test_falls_back_to_tesseract_without_rapidocr(monkeypatch, mode):
    real = builtins.__import__

    def no_rapidocr(name, *a, **kw):
        if name.split(".")[0] == "rapidocr":
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_rapidocr)
    monkeypatch.setenv("REIMBURSE_EXTRACTOR", mode)
    assert not extract.rapidocr_available()
    assert isinstance(get_extractor(), extract.TesseractExtractor)


def test_default_is_tesseract(monkeypatch):
    monkeypatch.delenv("REIMBURSE_EXTRACTOR", raising=False)
    assert isinstance(get_extractor(), extract.TesseractExtractor)


@pytest.mark.skipif(shutil.which("tesseract") is None or not extract.rapidocr_available(),
                    reason="needs tesseract and rapidocr")
def test_dual_reads_sample_taxi_without_disagreement(monkeypatch):
    monkeypatch.setenv("REIMBURSE_EXTRACTOR", "dual")
    d = get_extractor().extract(SAMPLES / "taxi_kota_biru_0310.jpg")
    assert d.fields["date"].value == "2026-10-03" and d.fields["amount"].value == 230000
    assert d.disagreements == []
