"""Pluggable document extraction for the drop-first upload flow.

An extractor turns one uploaded file into draft fields, each with a value, a confidence (0-1) and a source.
Default (REIMBURSE_EXTRACTOR=ocr): Tesseract OCR plus heuristics for Indonesian receipts.
REIMBURSE_EXTRACTOR=dual also runs RapidOCR (optional `rapidocr` extra) and flags every field the two engines
disagree on; REIMBURSE_EXTRACTOR=rapidocr uses RapidOCR alone. Without RapidOCR installed both fall back to
Tesseract. Optional: a vision model behind an OpenAI-compatible API (set REIMBURSE_EXTRACTOR=vision,
VISION_API_BASE, VISION_MODEL, VISION_API_KEY).
Anything unreadable comes back as an empty draft for manual input. Nothing here decides money:
the rule engine still computes every approved amount.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

DOC_TYPES = ("hotel_receipt", "taxi_receipt", "meal_receipt", "flight_receipt", "boarding_pass", "other")
FIELDS = ("doc_type", "date", "amount", "merchant", "flight_no", "nights", "passenger")
LOW_CONFIDENCE = 0.6
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6, "jul": 7, "agu": 8, "agt": 8,
          "aug": 8, "sep": 9, "okt": 10, "oct": 10, "nov": 11, "des": 12, "dec": 12}
AIRLINES = {"garuda": "GA", "citilink": "QG", "lion": "JT", "batik": "ID", "airasia": "QZ", "nusa air": "QZ",
            "super air jet": "IU", "pelita": "IP", "sriwijaya": "SJ", "nam air": "IN", "wings": "IW"}
FLIGHT_RE = re.compile(r"\b(GA|QG|JT|ID|QZ|IU|IP|SJ|IN|IW)\s?-?(\d{2,4})\b")
NUM = r"(\d{1,3}(?:[.,] ?\d{3})+|\d{4,9})(?:[.,]00)?"  # OCR often inserts a space after the dot
NUM_RE = re.compile(NUM)
# Labelled amount lines. Order matters: "TOTAL PPN" / "TOTAL DISKON" are tax/discount lines, not the total.
LABELS = [("subtotal", r"SUB\s*-?\s*TOTAL"), ("tax", r"(TOTAL\s+)?(PPN|PAJAK|TAX|PB1)"),
          ("service", r"(SERVICE|SERVIS|SVC)(\s*CHARGE)?"), ("discount", r"(TOTAL\s+)?(DISKON|DISCOUNT|POTONGAN)"),
          ("cash", r"TUNAI|CASH|BAYAR\s+TUNAI|DIBAYAR"), ("change", r"KEMBALI(AN)?|CHANGE"),
          ("total", r"GRAND\s*TOTAL|TOTAL\s*BAYAR|TOTAL|JUMLAH|AMOUNT\s*DUE")]
LABEL_RES = [(k, re.compile(r"^\s*(" + p + r")\b", re.I)) for k, p in LABELS]
RP_RE = re.compile(r"\bRp\.?\s?" + NUM, re.I)


@dataclass
class Field:
    value: str | int | None = None
    confidence: float = 0.0
    source: str = "none"  # ocr | vision | pdf_text | self_declared | none

    @property
    def low(self) -> bool:
        return self.value not in (None, "") and self.confidence < LOW_CONFIDENCE


@dataclass
class Draft:
    fields: dict[str, Field] = field(default_factory=lambda: {k: Field() for k in FIELDS})
    text: str = ""
    error: str = ""
    disagreements: list[str] = field(default_factory=list)  # fields two OCR engines read differently

    def to_json(self) -> dict:
        return {"fields": {k: asdict(v) for k, v in self.fields.items()}, "text": self.text, "error": self.error,
                "disagreements": list(self.disagreements)}


class Extractor(Protocol):
    name: str

    def extract(self, path: Path) -> Draft: ...


# --- heuristics ----------------------------------------------------------------
def to_int(num: str) -> int:
    return int(re.sub(r"[.,\s]", "", num))


def parse_date(text: str) -> tuple[str, int] | None:
    """Return (ISO date, char offset) of the first date in text."""
    pats = [(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](20\d\d)\b", "dmy"),
            (r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d\d)\b(?![/\-.]?\d)", "dmy2"), (r"\b(20\d\d)-(\d{2})-(\d{2})\b", "ymd"),
            (r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(20\d\d)\b", "dMy")]
    best = None
    for p, kind in pats:
        for m in re.finditer(p, text):
            a, b, c = m.groups()
            try:
                if kind == "dmy":
                    d, mo, y = int(a), int(b), int(c)
                elif kind == "dmy2":  # thermal receipts print 03/10/26
                    d, mo, y = int(a), int(b), 2000 + int(c)
                elif kind == "ymd":
                    y, mo, d = int(a), int(b), int(c)
                else:
                    d, mo, y = int(a), MONTHS[b.lower()[:3]], int(c)
            except KeyError:
                continue
            if 1 <= mo <= 12 and 1 <= d <= 31 and (best is None or m.start() < best[1]):
                best = (f"{y:04d}-{mo:02d}-{d:02d}", m.start())
            break
    return best


def amount_candidates(text: str) -> list[tuple[str, int, int]]:
    """(label, value, char offset) for each labelled money line; the value is the line's last amount."""
    out, pos = [], 0
    for ln in text.splitlines(keepends=True):
        nums = list(NUM_RE.finditer(ln))
        if nums:
            for label, rx in LABEL_RES:
                if rx.search(ln):
                    out.append((label, to_int(nums[-1].group(1)), pos + nums[-1].start()))
                    break
        pos += len(ln)
    return out


def select_amount(text: str) -> tuple[int, int, float] | None:
    """Pick the receipt total by arithmetic consistency: (value, offset, confidence factor).

    TUNAI/CASH, KEMBALI, DISKON and TOTAL PPN/DISKON lines are never the total. A TOTAL is consistent when
    subtotal + tax + service - discount == total, or cash - change == total. Factor 1.0 = consistent,
    0.9 = a lone TOTAL with nothing to check against, 0.4 = no consistent candidate (low confidence).
    """
    c = amount_candidates(text)
    get = lambda k: [v for lab, v, _ in c if lab == k]  # noqa: E731
    totals = [(v, off) for lab, v, off in c if lab == "total"]
    sub, tax, svc, disc, cash, chg = (get(k) for k in ("subtotal", "tax", "service", "discount", "cash", "change"))
    checks = []
    if sub:
        checks.append(sub[-1] + sum(tax) + sum(svc) - sum(disc))
    if cash and chg:
        checks.append(cash[-1] - chg[-1])
    for v, off in reversed(totals):
        if v in checks:
            return v, off, 1.0
    if totals:
        v, off = totals[-1]
        return v, off, (0.9 if not checks else 0.4)
    if checks:  # no TOTAL label: derived from the arithmetic
        off = next(o for lab, _, o in c if lab in ("subtotal", "cash"))
        return checks[0], off, 0.7
    excluded = {o for lab, _, o in c if lab in ("cash", "change", "discount", "tax")}
    rps = [(to_int(x.group(1)), x.start(1)) for x in RP_RE.finditer(text) if x.start(1) not in excluded]
    if rps:
        v, off = max(rps)
        return v, off, 0.5
    return None


def guess_type(t: str) -> str:
    t = t.lower()
    if "boarding" in t:
        return "boarding_pass"
    if "e-ticket" in t or "eticket" in t or "booking ref" in t or ("fare" in t and FLIGHT_RE.search(t.upper())):
        return "flight_receipt"
    if any(k in t for k in ("taksi", "taxi", "argo", "taks!")):  # before hotel: taxi slips name hotels as pickup
        return "taxi_receipt"
    if any(k in t for k in ("check-out", "check out", "hotel", "malam", "room", "kamar")):
        return "hotel_receipt"
    if any(k in t for k in ("rumah makan", "restoran", "restaurant", "cafe", "warung", "nasi", "makan")):
        return "meal_receipt"
    return "other"


def parse_text(text: str, line_conf: dict[int, float] | None = None, source: str = "ocr") -> Draft:
    """Heuristic field parser for receipt text. line_conf maps line index -> OCR confidence 0-1."""
    dr = Draft(text=text)
    lines = text.splitlines()
    lc = line_conf or {}

    def conf_at(offset: int, factor: float = 1.0) -> float:
        idx = text.count("\n", 0, offset)
        return round(min(1.0, lc.get(idx, 0.95)) * factor, 2)

    words = re.findall(r"[A-Za-z]{3,}", text)
    if len(words) < 3:
        dr.error = "No readable text found; fill in the card by hand."
        dr.fields["doc_type"] = Field("other", 0.0, "none")
        return dr
    dt = guess_type(text)
    dr.fields["doc_type"] = Field(dt, 0.9 if dt != "other" else 0.3, source)
    # date: prefer the line after a check-in / tanggal / date / departure label
    m = re.search(r"(check[\s-]?in|tanggal|tgl|tgt|date|departure)[^\n]*", text, re.I)
    found = parse_date(m.group(0)) if m else None
    if found:
        dr.fields["date"] = Field(found[0], conf_at(m.start()), source)
    elif (found := parse_date(text)):
        dr.fields["date"] = Field(found[0], conf_at(found[1], 0.8), source)
    # amount: labelled candidates checked by arithmetic (see select_amount)
    if (am := select_amount(text)):
        dr.fields["amount"] = Field(am[0], conf_at(am[1], am[2]), source)
    if (pm := re.search(r"(passenger|penumpang|nama|name)\s*:?\s*([A-Z][A-Z .'-]{2,40})", text, re.I)):
        dr.fields["passenger"] = Field(pm.group(2).strip().upper(), conf_at(pm.start()), source)
    # flight number, or airline name as a weak hint
    if (fm := FLIGHT_RE.search(text.upper())):
        dr.fields["flight_no"] = Field(f"{fm.group(1)} {fm.group(2)}", conf_at(fm.start()), source)
    # merchant: first non-empty line
    first = next(((i, ln.strip()) for i, ln in enumerate(lines) if ln.strip()), None)
    if first and dt != "boarding_pass":
        dr.fields["merchant"] = Field(first[1], round(lc.get(first[0], 0.95) * 0.9, 2), source)
    # hotel nights from check-in / check-out
    if dt == "hotel_receipt":
        ci = re.search(r"check[\s-]?in[^\n]*", text, re.I)
        co = re.search(r"check[\s-]?out[^\n]*", text, re.I)
        if ci and co and (a := parse_date(ci.group(0))) and (b := parse_date(co.group(0))):
            from datetime import date
            n = (date.fromisoformat(b[0]) - date.fromisoformat(a[0])).days
            if 0 < n < 60:
                dr.fields["nights"] = Field(n, min(conf_at(ci.start()), conf_at(co.start())), source)
        elif (nm := re.search(r"(\d{1,2})\s*(malam|nights?)", text, re.I)):
            dr.fields["nights"] = Field(int(nm.group(1)), conf_at(nm.start(), 0.7), source)
    if dt == "other" and not any(dr.fields[k].value for k in ("date", "amount", "flight_no")):
        # Text-like noise with no receipt field at all: treat as unreadable, not as a half-filled card.
        dr.error = "No receipt fields found; fill in the card by hand."
        dr.fields = {k: Field() for k in FIELDS}
        dr.fields["doc_type"] = Field("other", 0.0, "none")
    return dr


# --- Tesseract ------------------------------------------------------------------
class TesseractExtractor:
    name = "ocr"

    def __init__(self, lang: str | None = None):
        self.lang = lang or os.environ.get("TESSERACT_LANG", "ind+eng")

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def _ocr_image(self, img, psm: int = 3) -> tuple[str, dict[int, float]]:
        import pytesseract
        langs = set(pytesseract.get_languages(config=""))
        lang = "+".join(x for x in self.lang.split("+") if x in langs) or "eng"
        data = pytesseract.image_to_data(img, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
        rows: dict[tuple, list] = {}
        for i, w in enumerate(data["text"]):
            if w.strip():
                rows.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(
                    (w, float(data["conf"][i])))
        lines, conf = [], {}
        for n, key in enumerate(sorted(rows)):
            ws = rows[key]
            lines.append(" ".join(w for w, _ in ws))
            conf[n] = sum(c for _, c in ws) / len(ws) / 100
        return "\n".join(lines), conf

    def extract(self, path: Path) -> Draft:
        try:
            if path.suffix.lower() == ".pdf":
                if shutil.which("pdftotext"):
                    txt = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                                         timeout=30).stdout
                    if len(re.findall(r"[A-Za-z]{3,}", txt)) >= 3:
                        return parse_text(txt, None, "pdf_text")
                from pdf2image import convert_from_path
                pages = convert_from_path(str(path), dpi=200, first_page=1, last_page=2)
                parts = [self._ocr_image(p) for p in pages]
                text, conf, off = "", {}, 0
                for t, c in parts:
                    conf.update({k + off: v for k, v in c.items()})
                    off += t.count("\n") + 1
                    text += t + "\n"
                return parse_text(text, conf, "ocr")
            from PIL import Image
            with Image.open(path) as img:
                img = img.convert("L")
                if img.width < 1000:
                    img = img.resize((img.width * 2, img.height * 2))
                # psm 3 (auto layout) can split a label from its right-aligned amount; psm 6 (one block) keeps
                # them on one line but suffers more on noisy photos. Parse both and keep the better-supported one.
                drafts = [parse_text(*self._ocr_image(img, psm), "ocr") for psm in (3, 6)]
            return max(drafts, key=_support)
        except Exception as ex:  # unreadable or OCR missing: empty card for manual input
            d = Draft()
            d.error = f"Could not read this file ({type(ex).__name__}); fill in the card by hand."
            d.fields["doc_type"] = Field("other", 0.0, "none")
            return d


def _support(d: Draft) -> float:
    """Sum of confidences of the filled fields: how much of the receipt the parse explains."""
    return sum(f.confidence for f in d.fields.values() if f.value not in (None, ""))


# --- Vision (optional) -------------------------------------------------------------
VISION_PROMPT = ("Extract fields from this Indonesian travel receipt or boarding pass. Reply with JSON only: "
                 '{"doc_type": one of ' + json.dumps(list(DOC_TYPES)) + ', "date": "YYYY-MM-DD" (check-in date for '
                 'hotels), "amount": whole Rupiah integer or null, "merchant": string or null, "flight_no": like '
                 '"QZ 7520" or null, "nights": integer or null, "confidence": {field: 0-1}}')


class VisionExtractor:
    """OpenAI-compatible chat-completions vision call. Configured only through environment variables."""

    name = "vision"

    def __init__(self):
        self.base = os.environ.get("VISION_API_BASE", "").rstrip("/")
        self.model = os.environ.get("VISION_MODEL", "")
        self.key = os.environ.get("VISION_API_KEY", "")

    def available(self) -> bool:
        return bool(self.base and self.model and self.key)

    def extract(self, path: Path) -> Draft:
        import urllib.request
        try:
            if path.suffix.lower() == ".pdf":
                from pdf2image import convert_from_path
                import io
                buf = io.BytesIO()
                convert_from_path(str(path), dpi=150, first_page=1, last_page=1)[0].save(buf, "PNG")
                raw, mime = buf.getvalue(), "image/png"
            else:
                raw, mime = path.read_bytes(), "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            body = {"model": self.model, "temperature": 0, "messages": [{"role": "user", "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"}}]}]}
            req = urllib.request.Request(f"{self.base}/chat/completions", json.dumps(body).encode(),
                                         {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                content = json.load(r)["choices"][0]["message"]["content"]
            data = json.loads(re.search(r"\{.*\}", content, re.S).group(0))
            conf = data.get("confidence") or {}
            d = Draft(text=content)
            for k in FIELDS:
                v = data.get(k)
                if v not in (None, ""):
                    d.fields[k] = Field(v, float(conf.get(k, 0.8)), "vision")
            if d.fields["doc_type"].value not in DOC_TYPES:
                d.fields["doc_type"] = Field("other", 0.3, "vision")
            return d
        except Exception as ex:
            d = Draft()
            d.error = f"Vision extraction failed ({type(ex).__name__}); fill in the card by hand."
            d.fields["doc_type"] = Field("other", 0.0, "none")
            return d


# --- RapidOCR (optional) and dual-engine mode ---------------------------------------
GLUED_TIME_RE = re.compile(r"(\d{1,2}[/\-.]\d{1,2}[/\-.](?:20)?\d\d)(?=\d{1,2}:\d{2})")
ZERO_MONTH_RE = re.compile(r"(?<=\d )0(ct|kt)\b", re.I)  # "02 0ct 2026": zero read for the letter O


def boxes_to_lines(boxes, txts, scores) -> tuple[str, dict[int, float]]:
    """Rebuild reading-order lines from RapidOCR word boxes.

    Boxes whose vertical centres fall within half a box height of the current line join it; each line is then
    sorted left to right. RapidOCR also glues a date to the following time ("03-10-202609:12", "03/10/2621:40"),
    which breaks the date regex, so a space is inserted there, and "0ct" after a day number becomes "Oct".
    """
    items = []
    for b, t, sc in zip(boxes, txts, scores):
        ys = [float(p[1]) for p in b]
        items.append(((min(ys) + max(ys)) / 2, max(ys) - min(ys), min(float(p[0]) for p in b), t, float(sc)))
    items.sort(key=lambda i: i[0])
    rows: list[list] = []
    for it in items:
        if rows and abs(it[0] - rows[-1][0][0]) <= max(it[1], rows[-1][0][1]) / 2:
            rows[-1].append(it)
        else:
            rows.append([it])
    lines, conf = [], {}
    for n, row in enumerate(rows):
        row.sort(key=lambda i: i[2])
        line = GLUED_TIME_RE.sub(r"\1 ", " ".join(i[3] for i in row))
        lines.append(ZERO_MONTH_RE.sub(lambda m: "O" + m.group(1), line))
        conf[n] = sum(i[4] for i in row) / len(row)
    return "\n".join(lines), conf


def rapidocr_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


class RapidOCRExtractor:
    """RapidOCR (PaddleOCR models on onnxruntime, CPU). PDFs with a text layer reuse the Tesseract path."""

    name = "rapidocr"
    _engine = None

    def __init__(self):
        self._pdf = TesseractExtractor()

    @classmethod
    def engine(cls):
        if cls._engine is None:
            import logging
            from rapidocr import RapidOCR
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            cls._engine = RapidOCR()
        return cls._engine

    def extract(self, path: Path) -> Draft:
        if path.suffix.lower() == ".pdf":
            return self._pdf.extract(path)
        try:
            r = self.engine()(str(path))
            if not r.txts:
                return parse_text("", None, "ocr")
            return parse_text(*boxes_to_lines(r.boxes, r.txts, r.scores), "ocr")
        except Exception as ex:
            d = Draft()
            d.error = f"Could not read this file ({type(ex).__name__}); fill in the card by hand."
            d.fields["doc_type"] = Field("other", 0.0, "none")
            return d


COMPARED = ("doc_type", "date", "amount", "merchant", "flight_no", "nights", "passenger")


def _same(k: str, a, b) -> bool:
    if k in ("merchant", "passenger", "flight_no"):
        return re.sub(r"[^A-Z0-9]", "", str(a).upper()) == re.sub(r"[^A-Z0-9]", "", str(b).upper())
    return str(a) == str(b)


def combine(primary: Draft, second: Draft) -> Draft:
    """Tesseract draft plus the fields where RapidOCR read something different.

    A field disagrees when both engines read a value and the values differ, or when only one engine read it.
    Disagreeing fields keep the Tesseract value but drop below LOW_CONFIDENCE, so the card highlights them.
    """
    if primary.error and not second.error:
        return second
    if second.error:
        return primary
    out = Draft(fields=dict(primary.fields), text=primary.text, error=primary.error)
    for k in COMPARED:
        a, b = primary.fields[k].value, second.fields[k].value
        if a in (None, "") and b in (None, ""):
            continue
        if a in (None, "") or b in (None, "") or not _same(k, a, b):
            out.disagreements.append(k)
            f = primary.fields[k] if a not in (None, "") else second.fields[k]
            out.fields[k] = Field(f.value, min(f.confidence, LOW_CONFIDENCE - 0.1), f.source)
    return out


class DualExtractor:
    """Run Tesseract and RapidOCR; any field they disagree on sends the file to needs_review."""

    name = "dual (tesseract + rapidocr)"

    def __init__(self):
        self.a, self.b = TesseractExtractor(), RapidOCRExtractor()

    def extract(self, path: Path) -> Draft:
        if path.suffix.lower() == ".pdf":  # one engine reads PDFs, nothing to compare
            return self.a.extract(path)
        return combine(self.a.extract(path), self.b.extract(path))


def get_extractor() -> Extractor:
    mode = os.environ.get("REIMBURSE_EXTRACTOR", "ocr")
    if mode == "vision":
        v = VisionExtractor()
        if v.available():
            return v
    if mode in ("dual", "rapidocr") and rapidocr_available():
        return DualExtractor() if mode == "dual" else RapidOCRExtractor()
    return TesseractExtractor()
