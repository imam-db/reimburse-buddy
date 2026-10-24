"""Generate synthetic receipts and boarding passes for the drop-first upload demo.

Everything here is fictional. Run: uv run python web/sample_uploads/make_samples.py
Writes the files next to this script plus truth.json (the expected extraction per file).
"""

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def receipt(lines, title, size=(760, 980), fs=26):
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.text((40, 40), title, fill="black", font=font(BOLD, fs + 8))
    y = 110
    for ln in lines:
        d.text((40, y), ln, fill="black", font=font(MONO, fs))
        y += fs + 16
    return img


SAMPLES = {
    "hotel_majapahit_kencana.png": (
        "HOTEL MAJAPAHIT KENCANA",
        ["Jl. Fiktif No. 12, Surabaya", "Invoice HMK-20441", "Guest: Rina Wulandari",
         "Check-in : 02/10/2026", "Check-out: 04/10/2026", "Room Deluxe  2 malam x 1.350.000",
         "", "TOTAL      Rp2.700.000", "Terima kasih"],
        {"doc_type": "hotel_receipt", "date": "2026-10-02", "amount": 2700000, "merchant": "HOTEL MAJAPAHIT KENCANA",
         "nights": 2}),
    "taxi_kota_biru_0310.jpg": (
        "TAKSI KOTA BIRU",
        ["Struk Perjalanan", "Tanggal 03-10-2026  09:12", "Dari: Hotel Majapahit Kencana",
         "Ke  : Kantor PT Samudra Logistik", "Argo      Rp 215.000", "Tol       Rp 15.000",
         "TOTAL     Rp 230.000"],
        {"doc_type": "taxi_receipt", "date": "2026-10-03", "amount": 230000, "merchant": "TAKSI KOTA BIRU"}),
    "boarding_pass_qz7520.png": (
        "BOARDING PASS",
        ["Nusa Air (fictional)", "Passenger: RINA WULANDARI", "Flight  QZ 7520", "From CGK Jakarta  To SUB Surabaya",
         "Date 02 Oct 2026   Boarding 06:55", "Seat 14C   Gate B3"],
        {"doc_type": "boarding_pass", "date": "2026-10-02", "flight_no": "QZ 7520"}),
    "meal_sari_rasa_2010.png": (
        "RUMAH MAKAN SARI RASA",
        ["Jl. Contoh 5, Bandung", "20/10/2026 12:41", "Nasi timbel        65.000", "Ayam bakar         85.000",
         "Es teh 2x         35.000", "JUMLAH            185.000"],
        {"doc_type": "meal_receipt", "date": "2026-10-20", "amount": 185000, "merchant": "RUMAH MAKAN SARI RASA"}),
}


# Held-out set: tricky layouts NOT used to tune the parser. Measure separately.
HELDOUT = {
    "meal_total_3_item_tunai.png": (
        "WARUNG KOPI SENJA",
        ["Jl. Fiktif 9, Surabaya", "Tgl 03/10/2026 19:05", "Kopi susu 2x        56.000", "Roti bakar          42.000",
         "Mie goreng          65.000", "SUBTOTAL           163.000", "PPN 10%             16.300", "SERVICE 5%           8.150",
         "TOTAL (3 item)     187.450", "TUNAI              200.000", "KEMBALI             12.550"],
        {"doc_type": "meal_receipt", "date": "2026-10-03", "amount": 187450, "merchant": "WARUNG KOPI SENJA"}),
    "taxi_2digit_year_cash.jpg": (
        "TAKSI LINTAS KOTA",
        ["No. 88213", "03/10/26  21:40", "Bandara Juanda - Hotel", "Argo         Rp 185.000",
         "TOTAL        Rp 185.000", "TUNAI        Rp 300.000", "KEMBALI      Rp 115.000"],
        {"doc_type": "taxi_receipt", "date": "2026-10-03", "amount": 185000, "merchant": "TAKSI LINTAS KOTA"}),
    "meal_total_diskon_last.png": (
        "RESTORAN BUMI LAUT",
        ["Jl. Contoh 21, Surabaya", "Tanggal 02/10/26", "Ikan bakar         120.000", "Nasi 2x             20.000",
         "SUBTOTAL           140.000", "DISKON              14.000", "PPN                 12.600", "TOTAL              138.600",
         "TOTAL DISKON        14.000", "TOTAL PPN           12.600"],
        {"doc_type": "meal_receipt", "date": "2026-10-02", "amount": 138600, "merchant": "RESTORAN BUMI LAUT"}),
}


def make_heldout() -> None:
    out = OUT / "heldout"
    out.mkdir(exist_ok=True)
    truth = {}
    for name, (title, lines, t) in HELDOUT.items():
        img = receipt(lines, title, size=(760, 1100))
        img.save(out / name, quality=90) if name.endswith(".jpg") else img.save(out / name)
        truth[name] = t
    (out / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")


def main() -> None:
    truth = {}
    for name, (title, lines, t) in SAMPLES.items():
        img = receipt(lines, title)
        img.save(OUT / name, quality=90) if name.endswith(".jpg") else img.save(OUT / name)
        truth[name] = t
    # E-ticket as a scanned PDF (no text layer), so the OCR path for PDFs is exercised.
    tk = receipt(["Nusa Air (fictional)", "Booking ref NA8KQ2", "Passenger RINA WULANDARI", "Flight QZ 7520 CGK-SUB",
                  "Departure 02/10/2026 07:40", "Fare            1.020.000", "Tax & fees        100.000",
                  "TOTAL Rp1.120.000"], "E-TICKET RECEIPT")
    tk.save(OUT / "eticket_qz7520.pdf")
    truth["eticket_qz7520.pdf"] = {"doc_type": "flight_receipt", "date": "2026-10-02", "amount": 1120000,
                                   "flight_no": "QZ 7520", "merchant": "E-TICKET RECEIPT"}
    # A crumpled, low-contrast photo: extraction should be partial with low confidence.
    rnd = random.Random(7)
    img = receipt(["Taksi Pelangi", "Tgl 21/10/2026", "TOTAL Rp 150.000"], "TAKSI PELANGI", fs=22)
    img = img.rotate(4, expand=True, fillcolor="white").filter(ImageFilter.GaussianBlur(1.6))
    px = img.load()
    for _ in range(25000):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, y] = (150, 150, 150)
    img.convert("L").point(lambda v: 110 + v // 3).save(OUT / "taxi_pelangi_blurry.jpg", quality=40)
    truth["taxi_pelangi_blurry.jpg"] = {"doc_type": "taxi_receipt", "date": "2026-10-21", "amount": 150000,
                                        "merchant": "TAKSI PELANGI"}
    # Pure noise: nothing readable, so the card stays empty for manual input.
    noise = Image.effect_noise((600, 800), 60).convert("RGB")
    noise.save(OUT / "unreadable_photo.jpg")
    truth["unreadable_photo.jpg"] = {"doc_type": "other"}
    (OUT / "truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    print(f"wrote {len(truth)} samples to {OUT}")


if __name__ == "__main__":
    main()
    make_heldout()
