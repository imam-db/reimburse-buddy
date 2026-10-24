"""Finding message templates in English and Bahasa Indonesia, keyed by finding key.

Params named in MONEY are formatted as Rupiah (Rp1.350.000) in both languages.
"""

from __future__ import annotations

from decimal import Decimal

MONEY = {"per_night", "cap", "memo_cap", "limit", "max_fee", "receipt_amount", "item_amount"}

T = {
    "date_outside": ("Expense date {date} is outside the trip {start} to {end}.",
                     "Tanggal biaya {date} di luar periode perjalanan {start} s.d. {end}."),
    "receipt_found": ("Receipt found in {src}.", "Bukti pembayaran ditemukan di {src}."),
    "receipt_missing": ("Receipt missing.", "Bukti pembayaran tidak ada."),
    "receipt_amount_mismatch": ("Receipt in {src} shows {receipt_amount}, but the item claims {item_amount}. Needs review.",
                                "Bukti di {src} menunjukkan {receipt_amount}, tetapi item diklaim {item_amount}. Perlu ditinjau."),
    "multiple_receipts": ("Several receipts were submitted for this item ({srcs}). Needs review: which one is right?",
                          "Ada beberapa bukti untuk item ini ({srcs}). Perlu ditinjau: mana yang benar?"),
    "category_not_allowed": ("Category '{category}' is not allowed by the policy (allowed: {allowed}).",
                             "Kategori '{category}' tidak diizinkan kebijakan (yang diizinkan: {allowed})."),
    "dup_same_receipt": ("Same receipt number {rno} as {other}.", "Nomor bukti {rno} sama dengan {other}."),
    "dup_suspected": ("Same category, date and amount as {other} and no distinct receipt number.",
                      "Kategori, tanggal dan nominal sama dengan {other}, tanpa nomor bukti yang berbeda."),
    "bp_found": ("Boarding pass for {flight} on {date} ({name}) found in {src}.",
                 "Boarding pass {flight} tanggal {date} ({name}) ditemukan di {src}."),
    "bp_missing": ("No boarding pass matching flight {flight}, {date}, passenger {name}.",
                   "Tidak ada boarding pass yang cocok untuk penerbangan {flight}, {date}, penumpang {name}."),
    "nights_mismatch": ("{nights} night(s) from {date} ends {checkout}, after the trip end {end}.",
                        "{nights} malam sejak {date} berakhir {checkout}, melewati akhir perjalanan {end}."),
    "hotel_within": ("{per_night}/night is within the cap of {cap}.",
                     "{per_night}/malam masih dalam batas {cap}."),
    "hotel_memo": ("{per_night}/night exceeds the tier-{tier} cap of {cap}; signed memo {memo} allows {memo_cap}/night.",
                   "{per_night}/malam melebihi batas tier-{tier} {cap}; memo bertanda tangan {memo} mengizinkan {memo_cap}/malam."),
    "hotel_over": ("{per_night}/night exceeds the tier-{tier} cap of {cap}/night for {city}.",
                   "{per_night}/malam melebihi batas tier-{tier} {cap}/malam untuk {city}."),
    "per_diem": ("Allowance {limit} ({days} days at the tier-{tier} rate).",
                 "Uang harian maksimal {limit} ({days} hari, tarif tier-{tier})."),
    "lc_ok": ("Check-out at {time} justified by a business meeting ({ids}) in {location}.",
              "Check-out pukul {time} dibenarkan oleh rapat kerja ({ids}) di {location}."),
    "lc_missing": ("Check-out at {time} on {date} in {location} is past the standard {std} and does not match "
                   "the claimed trip schedule. Needs justification.",
                   "Check-out pukul {time} tanggal {date} di {location} melewati jam standar {std} dan tidak sesuai "
                   "dengan jadwal perjalanan yang diklaim. Perlu justifikasi."),
    "human_resolved": ("Resolved by the reviewer: {choice}.", "Diputuskan oleh reviewer: {choice}."),
    "human_rejected": ("Rejected by the reviewer.", "Ditolak oleh reviewer."),
    "return_missing": ("Outbound flight claimed but no return flight. Confirm how the employee returned.",
                       "Ada penerbangan berangkat tetapi tidak ada penerbangan pulang. Konfirmasi cara karyawan kembali."),
}

RESOLVED_PREFIX = ("Resolved ({choice}): ", "Sudah diputuskan ({choice}): ")

CHOICES_ID = {"approve_full": "setujui penuh", "approve_capped": "setujui sebatas plafon", "reject": "tolak"}

IGNORED = {
    "unknown_id": ("ID does not exist", "ID tidak ditemukan"),
    "belongs_to_other_employee": ("belongs to another employee", "milik karyawan lain"),
    "memo_unsigned": ("memo was never signed, so it approves nothing", "memo belum ditandatangani, jadi tidak berlaku"),
    "no_matching_item": ("does not match any item in this claim", "tidak cocok dengan item mana pun di klaim ini"),
}


def rupiah(amount) -> str:
    return "Rp" + f"{int(Decimal(str(amount)).quantize(Decimal('1'))):,}".replace(",", ".")


def render(key: str, params: dict, lang: str = "en") -> str:
    idx = 0 if lang == "en" else 1
    p = {k: (rupiah(v) if k in MONEY else v) for k, v in params.items()}
    if lang != "en" and "choice" in p:
        p["choice"] = CHOICES_ID.get(p["choice"], p["choice"])
    return T[key][idx].format(**p)
