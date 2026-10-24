import json
import shutil
from pathlib import Path

import pytest

from reimburse_web.extract import TesseractExtractor, get_extractor, parse_text

SAMPLES = Path(__file__).resolve().parents[1] / "web" / "sample_uploads"
needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def test_parse_text_hotel():
    d = parse_text("HOTEL CONTOH INDAH\nCheck-in 02/10/2026\nCheck-out 04/10/2026\nTOTAL Rp2. 700.000", None, "ocr")
    f = d.fields
    assert f["doc_type"].value == "hotel_receipt" and f["amount"].value == 2700000
    assert f["date"].value == "2026-10-02" and f["nights"].value == 2 and f["merchant"].value == "HOTEL CONTOH INDAH"


def test_parse_text_taxi_mentioning_hotel_is_taxi():
    d = parse_text("TAKSI CONTOH\nDari Hotel Contoh ke Bandara\n03-10-2026\nTotal Rp 230.000", None, "ocr")
    assert d.fields["doc_type"].value == "taxi_receipt" and d.fields["amount"].value == 230000


def test_parse_text_boarding_pass():
    d = parse_text("BOARDING PASS\nPassenger RINA WULANDARI\nFlight QZ 7520\nDate 02 Oct 2026", None, "ocr")
    assert d.fields["doc_type"].value == "boarding_pass" and d.fields["flight_no"].value == "QZ 7520"
    assert d.fields["date"].value == "2026-10-02"


def test_too_little_text_gives_empty_card():
    d = parse_text("x", None, "ocr")
    assert d.error and all(f.value in (None, "") for f in d.fields.values() if f is not d.fields["doc_type"])


def test_default_extractor_is_ocr(monkeypatch):
    monkeypatch.delenv("REIMBURSE_EXTRACTOR", raising=False)
    assert get_extractor().name == "ocr"
    monkeypatch.setenv("REIMBURSE_EXTRACTOR", "vision")
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    assert get_extractor().name == "ocr"  # vision is only used when configured


@needs_tesseract
def test_ocr_accuracy_on_samples():
    truth = json.loads((SAMPLES / "truth.json").read_text())
    ex, hit, total = TesseractExtractor(), 0, 0
    for name, want in truth.items():
        got = ex.extract(SAMPLES / name).fields
        for k, v in want.items():
            total += 1
            hit += got[k].value == v
            if got[k].value != v and k != "doc_type":
                assert got[k].low or name == "taxi_pelangi_blurry.jpg", f"{name}:{k} wrong but confident"
    assert hit / total >= 0.9


@needs_tesseract
def test_unreadable_photo_is_empty_card():
    d = TesseractExtractor().extract(SAMPLES / "unreadable_photo.jpg")
    assert d.fields["amount"].value is None and d.fields["doc_type"].value in (None, "other")


@needs_tesseract
def test_ocr_accuracy_on_heldout_samples():
    """Held-out tricky layouts (TOTAL (3 item), cash larger than total, 2-digit year, TOTAL DISKON last)."""
    folder = SAMPLES / "heldout"
    truth = json.loads((folder / "truth.json").read_text())
    ex = TesseractExtractor()
    got = [(ex.extract(folder / n).fields[k].value, v) for n, want in truth.items() for k, v in want.items()]
    assert sum(a == b for a, b in got) / len(got) >= 0.9
    assert all(ex.extract(folder / n).fields["amount"].value == w["amount"] for n, w in truth.items())
