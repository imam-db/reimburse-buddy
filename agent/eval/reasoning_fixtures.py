"""Reasoning-track fixtures (Claude review 8, step 4d). Synthetic data only; hand-written texts.

    python -m eval.reasoning_fixtures     # (re)writes eval/fixtures/reasoning

Each case is paired with a control the keyword/ranking heuristic should pass. Every case carries a ground-truth
label (the outcome a careful reviewer reaches) and a one-line rationale in eval/fixtures/reasoning/labels.json.
LABELS_NEED_REVIEW: Imam should review these labels independently before they are used to score an LLM.

The scripted reviewer answers with the label's decision only when the run stops and asks (decide_if_asked), so a
policy that routes a hard case to review passes, and one that settles it wrongly on its own fails.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .hard_fixtures import HARD, MAILBOX

REASONING = Path(__file__).parent / "fixtures" / "reasoning"
LABELS_NEED_REVIEW = True
DOMAIN = "nusantara-niaga.example"
NAMES = ["Andi Pratama", "Bayu Saputra", "Citra Lestari", "Dewi Anggraini", "Eko Nugroho", "Fajar Hidayat",
         "Gita Purnama", "Hendra Wijaya", "Indah Permata", "Joko Susilo", "Kartika Sari"]

FILLER = [("rapat", "Notulen rapat mingguan sudah saya upload."), ("cuti", "Saya cuti hari Jumat."),
          ("laptop", "Laptop sudah balik dari service."), ("promo taksi", "Diskon taksi bandara 20%. Tanpa lampiran."),
          ("Re: proposal", "Draft proposal v3 terlampir di drive."), ("besok", "besok jadi makan siang?"),
          ("slip", "Slip gaji ada di portal."), ("hotel deals", "Late check-out gratis untuk member. Newsletter.")]


def build():
    emps, claims, inbox, cal, replies, labels = [], [], [], [], [], {}
    n = [0]

    def emp(i):
        e = {"id": f"EMP-2{i:02d}", "name": NAMES[i - 1], "department": "Sales", "grade": "staff",
             "manager_id": "EMP-100", "email": f"pegawai{i}@{DOMAIN}"}
        emps.append(e)
        for j, (s, b) in enumerate(FILLER):  # 8 fillers: the mailbox is bigger than one search page
            msg(e, f"2026-09-{10 + j:02d}", s, b)
        return e

    def msg(e, d, subj, body, att=(), **kw):
        n[0] += 1
        m = {"id": f"MSG-5{n[0]:03d}", "owner_id": e["id"], "from": e["email"], "date": d, "subject": subj, "body": body,
             "attachments": list(att), "mailbox": MAILBOX, "forwarded_by": e["email"], **kw}
        inbox.append(m)
        return m["id"]

    def rcpt(cid, iid, no, amt, name="IMG.jpg"):
        return {"name": name, "type": "receipt", "claim_id": cid, "item_id": iid, "receipt_no": no, "amount": amt}

    def claim(i, e, items, purpose, start="2026-10-13", end="2026-10-14", city="Surabaya"):
        cid = f"CLM-2026-8{i:03d}"
        fl = [{"id": "IT-1", "category": "flight", "date": start, "description": "outbound flight GA 410", "amount": "1300000",
               "flight_no": "GA 410", "leg": "outbound", "receipt": True, "receipt_no": f"TKT-{cid}-1", "boarding_pass": True},
              {"id": "IT-2", "category": "flight", "date": end, "description": "return flight GA 417", "amount": "1300000",
               "flight_no": "GA 417", "leg": "return", "receipt": True, "receipt_no": f"TKT-{cid}-2", "boarding_pass": True}]
        claims.append({"id": cid, "employee_id": e["id"], "status": "submitted", "submitted_at": "2026-10-24", "batch": "2026-REASON",
                       "purpose": purpose, "trip": {"origin": "Jakarta", "destination": city, "start_date": start, "end_date": end},
                       "items": fl + [f(cid) for f in items]})
        return cid

    def label(case, cid, good, decisions, outcome, why, control_of=None):
        labels[case] = {"claim_id": cid, "good_evidence": good, "human_decisions": decisions, "label": outcome,
                        "rationale": why, "control_of": control_of, "needs_imam_review": LABELS_NEED_REVIEW}

    hotel = lambda c: {"id": "IT-3", "category": "hotel", "date": "2026-10-13", "description": "Hotel Surabaya, 1 night",
                       "amount": "700000", "nights": 1, "receipt": True, "receipt_no": f"HTL-{c}"}
    lc = lambda c: {"id": "IT-4", "category": "late_checkout", "date": "2026-10-14", "description": "Late check-out until 16:00",
                    "amount": "300000", "checkout_time": "16:00", "location": "Surabaya", "receipt": True, "receipt_no": f"LC-{c}"}

    def event(i, e):
        ev = {"id": f"EVT-8{i:03d}", "owner_id": e["id"], "title": "Meeting with PT Arunika Niaga", "date": "2026-10-14",
              "start": "10:00", "end": "12:00", "location": "Surabaya", "type": "business"}
        cal.append(ev)
        return ev["id"]

    # R1 temporal: moved, then moved back. Control: moved only.
    e = emp(1); cid = claim(1, e, [hotel, lc], "Client meeting (fictional)"); ev = event(1, e)
    m1 = msg(e, "2026-10-12", "Re: besok", "Meeting Arunika digeser ke jam 3 sore, selesai setengah lima.",
             meeting_update={"event_id": ev, "start": "15:00", "end": "16:30"})
    m2 = msg(e, "2026-10-13", "Re: Re: besok", "Eh ternyata balik ke jadwal awal ya, jam 10 sampai 12. Maaf bolak-balik.",
             meeting_update={"event_id": ev, "start": "10:00", "end": "12:00"})
    label("R1", cid, [ev, m1, m2], {"IT-4": "reject"}, "reject IT-4",
          "The later message moves the meeting back to 10:00-12:00, so a 16:00 checkout is not justified; using only the first move would approve it.")
    e = emp(2); cid = claim(2, e, [hotel, lc], "Client meeting (fictional)"); ev = event(2, e)
    m1 = msg(e, "2026-10-12", "Re: besok", "Meeting Arunika digeser ke jam 3 sore, selesai setengah lima.",
             meeting_update={"event_id": ev, "start": "15:00", "end": "16:30"})
    label("R1c", cid, [ev, m1], {}, "approve all", "One move to 15:00-16:30 justifies the 16:00 checkout.", "R1")
    # R1x cancelled (no structured update: the engine cannot represent a cancellation)
    e = emp(3); cid = claim(3, e, [hotel, lc], "Client meeting (fictional)"); ev = event(3, e)
    m1 = msg(e, "2026-10-12", "Re: besok", "Meeting Arunika digeser ke jam 3 sore, selesai setengah lima.",
             meeting_update={"event_id": ev, "start": "15:00", "end": "16:30"})
    m2 = msg(e, "2026-10-13", "batal", "Pak, kliennya batalin pertemuan besok, jadi nggak ada meeting sama sekali.")
    label("R1x", cid, [ev, m1, m2], {"IT-4": "reject"}, "reject IT-4",
          "The meeting was cancelled the day before, so nothing justifies staying until 16:00; the cancellation is free text only.")
    # R2 two same-day taxi receipts told apart by route. Control: one receipt.
    taxi = lambda c: {"id": "IT-3", "category": "taxi", "date": "2026-10-14", "description": "Taxi hotel to client office (PT Arunika)",
                      "amount": "85000", "receipt": False}
    e = emp(4); cid = claim(4, e, [taxi], "Client visit (fictional)")
    ra = msg(e, "2026-10-14", "struk", "Taksi dari hotel ke kantor PT Arunika pagi tadi.", [rcpt(cid, "IT-3", "TX-A1", 85000)])
    rb = msg(e, "2026-10-14", "struk 2", "Taksi malam dari hotel ke mall, urusan pribadi, salah lampir.", [rcpt(cid, "IT-3", "TX-A2", 150000)])
    label("R2", cid, [ra], {"IT-3": f"use:{ra}"}, f"pay IT-3 from {ra} (85,000)",
          f"Both receipts are dated the same day; only {ra}'s route (hotel to the client office) matches the item, {rb} is a personal ride.")
    e = emp(5); cid = claim(5, e, [taxi], "Client visit (fictional)")
    ra = msg(e, "2026-10-14", "struk", "Taksi dari hotel ke kantor PT Arunika pagi tadi.", [rcpt(cid, "IT-3", "TX-B1", 85000)])
    label("R2c", cid, [ra], {}, "approve all", "One receipt, matching route and amount.", "R2")
    # R3 partial reply. Control: full reply.
    taxi2 = lambda c: {"id": "IT-3", "category": "taxi", "date": "2026-10-13", "description": "Taxi airport to hotel", "amount": "120000", "receipt": False}
    hotel2 = lambda c: {"id": "IT-4", "category": "hotel", "date": "2026-10-13", "description": "Hotel Surabaya, 1 night",
                        "amount": "700000", "nights": 1, "receipt": False}

    def reply(e, cid, rid, body, atts):
        n[0] += 1
        r = {"id": f"MSG-5{n[0]:03d}", "owner_id": e["id"], "from": e["email"], "date": "2026-10-24", "subject": "Re: bukti", "body": body,
             "attachments": atts, "mailbox": MAILBOX, "forwarded_by": e["email"]}
        replies.append({"id": rid, "to": e["id"], "claim_id": cid, "item_id": "IT-3", "gap_code": "receipt_missing",
                        "keywords_any": ["IT-3", "IT-4"], "reply": r})
        return r["id"]
    e = emp(6); cid = claim(6, e, [taxi2, hotel2], "Supplier visit (fictional)")
    rid = reply(e, cid, "RPL-8006", "ini struknya, yang hotel nyusul ya", [rcpt(cid, "IT-3", "TX-C1", 120000)])
    label("R3", cid, [rid], {}, "IT-3 ok; IT-4 still needs its receipt (not approvable yet)",
          "The reply carries only the taxi receipt and says the hotel one will follow; the hotel item stays open.")
    e = emp(7); cid = claim(7, e, [taxi2, hotel2], "Supplier visit (fictional)")
    rid = reply(e, cid, "RPL-8007", "ini struk taksi sama hotelnya ya", [rcpt(cid, "IT-3", "TX-D1", 120000), rcpt(cid, "IT-4", "HTL-D1", 700000, "hotel.pdf")])
    label("R3c", cid, [rid], {}, "approve all", "Both receipts arrived in one reply.", "R3")
    # R4 free-text reason vs policy (no calendar event either way). Control: a business reason.
    e = emp(8); cid = claim(8, e, [hotel, lc], "Client meeting (fictional)")
    m = msg(e, "2026-10-14", "late checkout", "Maaf late check-out, ketiduran habis begadang nonton bola.")
    label("R4", cid, [m], {"IT-4": "reject"}, "reject IT-4", "Oversleeping is a personal reason; policy pays late checkout only for business.")
    e = emp(9); cid = claim(9, e, [hotel, lc], "Client meeting (fictional)")
    m = msg(e, "2026-10-14", "late checkout", "Late check-out karena nunggu klien tanda tangan kontrak di lobby hotel sampai jam 4.")
    label("R4c", cid, [m], {"IT-4": "approve_full"}, "approve IT-4 after review",
          "Waiting for a client signature is a business reason, but no calendar event backs it, so a reviewer decides (label: approve).", "R4")
    # R5 indirect reference. Control: explicit reference.
    taxi3 = lambda c: {"id": "IT-3", "category": "taxi", "date": "2026-10-13", "description": "Taxi hotel to client office", "amount": "90000", "receipt": False}
    e = emp(10); cid = claim(10, e, [taxi3], "Client visit (fictional)", "2026-10-12", "2026-10-13")
    m = msg(e, "2026-10-15", "fwd", "Here's the receipt from Tuesday's ride to the client.", [rcpt(cid, "IT-3", "TX-E1", 90000, "IMG_7781.jpg")])
    label("R5", cid, [m], {}, "approve all", "2026-10-13 is a Tuesday and the client ride is IT-3; the message names neither the date nor the item.")
    e = emp(11); cid = claim(11, e, [taxi3], "Client visit (fictional)", "2026-10-12", "2026-10-13")
    m = msg(e, "2026-10-15", f"receipt {cid} IT-3", f"Taxi receipt for {cid} IT-3, hotel to client office, 13 Oct.", [rcpt(cid, "IT-3", "TX-F1", 90000)])
    label("R5c", cid, [m], {}, "approve all", "The message names the claim, item and route.", "R5")
    return emps, claims, inbox, cal, replies, labels


def main():
    emps, claims, inbox, cal, replies, labels = build()
    if REASONING.exists():
        shutil.rmtree(REASONING)
    REASONING.mkdir(parents=True)
    for f in ("policy", "memos"):
        shutil.copy(HARD / f"{f}.json", REASONING / f"{f}.json")
    base = json.loads((HARD / "employees.json").read_text())
    dump = lambda n, v: (REASONING / f"{n}.json").write_text(json.dumps(v, indent=2, ensure_ascii=False) + "\n")
    dump("employees", base + emps), dump("claims", claims), dump("inbox", inbox), dump("calendar", cal)
    dump("replies", replies), dump("labels", labels)
    print(len(claims), "claims,", len(inbox), "messages")


if __name__ == "__main__":
    main()
