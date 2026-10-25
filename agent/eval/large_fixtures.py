"""Larger frozen synthetic mailboxes for the retrieval eval (Claude review 8, step 4b). Synthetic data only.

    python -m eval.large_fixtures      # (re)writes eval/fixtures/large_a and eval/fixtures/large_b

Each set has 5 claims, one per gap family, each owned by a different employee whose reimburse@ mailbox
holds 40-80 messages over Aug-Oct 2026: the one message a gap needs, plus distractors (other trips' receipts,
same-vendor receipts on other dates, other meetings moved, deadlines "dimajukan", promos with the claim's
words, "besok" chatter, newsletters), code-switching and slang. Every text is hand-written; a seeded RNG only
picks and combines them. Set A (tuning) and set B (held-out report) use disjoint paraphrase lists, so set B
results are not tuned on. Only the RNG seed and the paraphrase list differ between the sets.

Families: taxi_body (receipt named only in the body), reschedule_later (a moved meeting justifies a late
checkout), reschedule_earlier (a meeting moved earlier contradicts the calendar: review), slang_reply (the
receipt arrives as a slang reply to the ask), quoted (the receipt mail's key sentence sits in a quoted reply,
beyond the 160-character snippet the server returns).
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import date, timedelta
from pathlib import Path

from .hard_fixtures import HARD, MAILBOX

FIXTURES = Path(__file__).parent / "fixtures"
LARGE_A, LARGE_B = FIXTURES / "large_a", FIXTURES / "large_b"
DOMAIN = "nusantara-niaga.example"

# ---- hand-written paraphrases: index 0 = set A, index 1 = set B (disjoint) ----
TAXI_SUBJ = (["foto", "ini ya", "susulan", "(no subject)"], ["bukti", "nyusul", "scan", "fwd"])
TAXI_BODY = (["Ini bukti bayar argo dari {a} ke {b} hari {day}, maaf baru kirim.",
              "Pak, terlampir nota taksi waktu saya dari {a} menuju {b} ({day}).",
              "Receipt for the cab ride {a} to {b} on {day}, sorry for the delay."],
             ["Nota mobil dari {a} ke {b} pas {day} kemarin ya, lupa forward.",
              "Ongkos taxi {a} -> {b} hari {day}, struknya di lampiran.",
              "Attached the fare slip for my ride {a} to {b} ({day})."])
LATER = (["Jadwal ketemu klien {who} digeser ke jam 3 ya, kira-kira selesai setengah lima. Tempat tetap.",
          "Meeting sama {who} besok mundur jadi sore, mulai 15.00 sampai 16.30.",
          "FYI the session with {who} got pushed to 3pm, wrapping up around 4:30."],
         ["Pertemuan dengan {who} diundur ke pukul tiga sore, perkiraan beres jam setengah lima.",
          "Klien {who} minta ketemuannya dipindah ke jam 15, selesai sekitar 16.30.",
          "Heads up: {who} moved our meeting to the afternoon, 15:00-16:30."])
EARLIER = (["Review dengan {who} besok dimajukan ke pagi, jam delapan sampai sepuluh. Siang sudah bebas.",
            "Meeting {who} jadi pagi aja ya, 08.00-10.00, siangnya kosong.",
            "The {who} review was brought forward to 8-10am tomorrow, afternoon is free now."],
           ["Agenda sama {who} dimajukan, mulai jam 8 pagi selesai jam 10.",
            "Ketemu {who} pindah ke pagi hari (08.00 s/d 10.00); sore sudah tidak ada acara.",
            "{who} asked to do it early: 8:00 to 10:00 tomorrow, nothing after lunch."])
SLANG = (["nih bon yg kmrn bro, td ketinggalan dlm tas", "wkwk sori lupa, ni fotonya", "udh ya, cek lampiran aja"],
         ["sori bgt baru inget, ni notanya", "ini ya kak yg diminta kemarin", "dah tak attach, maap telat"])
QUOTE_LEAD = (["Siap Pak, nanti siang saya mampir ke ruangan buat bahas jadwal kunjungan berikutnya. Oh iya, soal makan "
               "siang tim minggu depan saya sudah tanya ke beberapa tempat dan semuanya bisa reservasi untuk dua belas orang.",
               "Thanks, noted on the team lunch. I also checked the room booking for next Thursday and it is confirmed for "
               "the whole afternoon, projector included, so we can run the dry run there before the client visit."],
              ["Oke Bu, untuk rapat koordinasi Kamis saya sudah kirim undangan ke semua kepala cabang, tinggal tunggu "
               "konfirmasi dari Medan dan Balikpapan. Materi presentasi juga sudah saya taruh di folder bersama.",
               "Sure, I'll join the onboarding session for the new interns on Monday and bring the printed handbooks. "
               "Also, the parking passes for the visitors are ready at the front desk, just ask for Mbak Dewi."])
QUOTE_KEY = (["> struk taksi {a} ke {b} tanggal {d} saya lampirkan di sini ya", "> attaching the cab receipt {a} to {b}, {d}"],
             ["> bukti bayar taksi {a} - {b} ({d}) ada di lampiran", "> ini nota taksinya, {a} ke {b} tgl {d}"])
QUOTE_SUBJ = (["Re: Re: makan siang tim", "Re: ruang rapat Kamis"], ["Re: Re: rapat koordinasi", "Re: onboarding intern"])

# distractors
PROMO = (["Promo taksi bandara 20% bulan depan", "Kuitansi digital kini tersedia di aplikasi", "Hotel weekend deals: late checkout gratis",
          "Diskon argo malam untuk pelanggan setia"],
         ["Voucher taxi ke airport, cuma minggu ini", "Struk elektronik otomatis terkirim ke email", "Staycation promo: free late check-out",
          "Cashback ride ke bandara"])
PROMO_BODY = (["Newsletter: nikmati potongan harga perjalanan. Tidak ada lampiran.", "Marketing email. Unsubscribe kapan saja."],
              ["Info promo pelanggan. Bukan tagihan.", "Pesan otomatis, mohon tidak membalas."])
OTHER_MOVE = (["Weekly sync tim sales digeser ke Rabu jam 2.", "Training compliance diundur ke bulan depan.",
               "The vendor demo moved to Friday morning."],
              ["Rapat bulanan cabang dipindah ke ruang 3B, jam tetap.", "Sesi coaching mundur seminggu.",
               "Townhall got rescheduled to next Tuesday."])
DEADLINE = (["Deadline laporan kuartal dimajukan ke Jumat ini.", "Batas input timesheet dimajukan jadi tanggal 25."],
            ["Pengumpulan forecast dimajukan ke Senin.", "Cut-off klaim bulan ini dimajukan dua hari."])
BESOK = (["besok jadi makan siang bareng?", "besok aku WFH ya", "tomorrow's standup is at 9 as usual"],
         ["besok bawa charger cadangan ya", "besok ada yg ke kantor pusat?", "see you at tomorrow's 1:1"])
CHAT = (["Tolong cek draft proposal yang saya share tadi.", "Laptop sudah balik dari service, thanks IT.",
         "Minggu depan cuti dua hari ya, sudah approve atasan.", "Can you resend the price list for Q4?",
         "Gaji sudah masuk, slip ada di portal.", "Kontrak distributor sudah ditandatangani, scan nyusul.",
         "Meja rapat lantai 5 lagi direnovasi.", "ok noted, makasih banyak"],
        ["Mohon review deck untuk presentasi klien.", "Printer lantai 3 rusak lagi, sudah lapor GA.",
         "Izin pulang cepat hari Jumat, ada urusan keluarga.", "Please share the updated org chart.",
         "Reimburse bulan lalu sudah cair, terima kasih.", "PO sudah terbit, nomornya di sistem.",
         "AC ruang meeting kecil bocor.", "siap, laksanakan"])
CHAT_SUBJ = (["proposal", "laptop", "cuti", "price list", "slip gaji", "kontrak", "info", "ok"],
             ["deck", "printer", "izin", "org chart", "pencairan", "PO", "AC", "siap"])

PEOPLE = {"EMP-001": "rina.wulandari", "EMP-002": "budi.santoso", "EMP-003": "sari.kusumawati",
          "EMP-004": "yoga.firmansyah", "EMP-005": "putri.maharani"}
CLIENTS = (["PT Arunika Niaga", "PT Bahari Sejahtera"], ["PT Cakra Mandiri", "PT Delta Samudra"])
CITIES = (["Surabaya", "Makassar"], ["Medan", "Balikpapan"])
DAYNAME = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]


class Gen:
    def __init__(self, s: int, seed: int):
        self.s, self.rng, self.msgs, self.events, self.claims, self.replies, self.truth = s, random.Random(seed), [], [], [], [], {}
        self.base = 6000 if s == 0 else 7000

    def pick(self, pool):
        return self.rng.choice(pool[self.s])

    def msg(self, emp, d, subject, body, attachments=(), sender=None, **kw):
        m = {"id": None, "owner_id": emp, "from": sender or f"{PEOPLE[emp]}@{DOMAIN}", "date": d.isoformat(),
             "subject": subject, "body": body, "attachments": list(attachments), "mailbox": MAILBOX,
             "forwarded_by": f"{PEOPLE[emp]}@{DOMAIN}", **kw}
        self.msgs.append(m)
        return m

    def rand_day(self):
        return date(2026, 8, 1) + timedelta(days=self.rng.randrange(0, 84))

    def distractors(self, emp, n, cid, city, trip_day):
        r = self.rng
        for _ in range(n):
            kind = r.choices(["chat", "promo", "other_rcpt", "same_vendor", "other_move", "deadline", "besok"],
                             [10, 3, 2, 2, 2, 1, 2])[0]
            d = self.rand_day()
            if kind == "chat":
                i = r.randrange(len(CHAT[self.s]))
                self.msg(emp, d, CHAT_SUBJ[self.s][i], CHAT[self.s][i])
            elif kind == "promo":
                self.msg(emp, d, self.pick(PROMO), self.pick(PROMO_BODY), sender=f"promo@taksi-{r.randint(1, 9)}.example")
            elif kind == "other_rcpt":  # a receipt for an older claim
                old = f"CLM-2026-0{r.randint(500, 599)}"
                self.msg(emp, d, self.pick(TAXI_SUBJ), self.pick(TAXI_BODY).format(a="kantor", b="gudang", day=DAYNAME[d.weekday()]),
                         [{"name": f"IMG_{r.randint(1000, 9999)}.jpg", "type": "receipt", "claim_id": old, "item_id": "IT-1",
                           "receipt_no": f"TX-{old}", "amount": r.randrange(40, 200) * 1000}])
            elif kind == "same_vendor":  # right route words, wrong date and claim
                d2 = trip_day - timedelta(days=r.randint(20, 60))
                old = f"CLM-2026-0{r.randint(500, 599)}"
                self.msg(emp, d2, self.pick(TAXI_SUBJ), self.pick(TAXI_BODY).format(a="bandara", b="hotel", day=DAYNAME[d2.weekday()]),
                         [{"name": f"IMG_{r.randint(1000, 9999)}.jpg", "type": "receipt", "claim_id": old, "item_id": "IT-2",
                           "receipt_no": f"TX-{old}-2", "amount": r.randrange(60, 200) * 1000}])
            elif kind == "other_move":
                eid = f"EVT-{self.base + 900 + len(self.events)}"
                self.events.append({"id": eid, "owner_id": emp, "title": "Internal sync", "date": d.isoformat(), "start": "10:00",
                                    "end": "11:00", "location": "Jakarta", "type": "business"})
                self.msg(emp, d - timedelta(days=1), "jadwal", self.pick(OTHER_MOVE),
                         meeting_update={"event_id": eid, "start": "14:00", "end": "15:00"})
            elif kind == "deadline":
                self.msg(emp, d, "deadline", self.pick(DEADLINE))
            else:
                self.msg(emp, d, "besok", self.pick(BESOK))

    def claim(self, n, emp, city, start, items, purpose):
        cid = f"CLM-2026-{(6 if self.s == 0 else 7)}{n:03d}"
        end = start + timedelta(days=1)
        fl = [{"id": "IT-1", "category": "flight", "date": start.isoformat(), "description": f"outbound flight GA {600 + n}",
               "amount": "1300000", "flight_no": f"GA {600 + n}", "leg": "outbound", "receipt": True,
               "receipt_no": f"TKT-{cid}-IT-1", "boarding_pass": True},
              {"id": "IT-2", "category": "flight", "date": end.isoformat(), "description": f"return flight GA {700 + n}",
               "amount": "1300000", "flight_no": f"GA {700 + n}", "leg": "return", "receipt": True,
               "receipt_no": f"TKT-{cid}-IT-2", "boarding_pass": True}]
        self.claims.append({"id": cid, "employee_id": emp, "status": "submitted", "submitted_at": "2026-10-24", "batch": "2026-LARGE",
                            "purpose": purpose, "trip": {"origin": "Jakarta", "destination": city, "start_date": start.isoformat(),
                                                         "end_date": end.isoformat()}, "items": fl + [f(cid, start, end) for f in items]})
        return cid, end

    def build(self):
        r, cities, clients = self.rng, CITIES[self.s], CLIENTS[self.s]
        fam = {}
        # 1 taxi_body
        emp, city, start = "EMP-005", cities[0], date(2026, 10, 13)
        cid, end = self.claim(1, emp, city, start, [lambda c, s, e: {"id": "IT-3", "category": "taxi", "date": s.isoformat(),
                                                                         "description": "Taxi airport to hotel", "amount": "135000", "receipt": False}],
                              f"Supplier visit in {city} (fictional)")
        m = self.msg(emp, start + timedelta(days=2), self.pick(TAXI_SUBJ), self.pick(TAXI_BODY).format(a="bandara", b="penginapan", day=DAYNAME[start.weekday()]),
                     [{"name": f"IMG_{r.randint(1000, 9999)}.jpg", "type": "receipt", "claim_id": cid, "item_id": "IT-3",
                       "receipt_no": f"TX-{cid}-3", "amount": 135000}])
        fam["taxi_body"] = (cid, emp, ["IT-3"], m, None)
        self.distractors(emp, r.randint(45, 75), cid, city, start)
        # 2 reschedule_later
        emp, city, start = "EMP-003", cities[0], date(2026, 10, 15)
        cid, end = self.claim(2, emp, city, start, [
            lambda c, s, e: {"id": "IT-3", "category": "hotel", "date": s.isoformat(), "description": f"Hotel {city}, 1 night",
                             "amount": "700000", "nights": 1, "receipt": True, "receipt_no": f"HTL-{c}"},
            lambda c, s, e: {"id": "IT-4", "category": "late_checkout", "date": e.isoformat(), "description": "Late check-out until 16:00",
                             "amount": "300000", "checkout_time": "16:00", "location": city, "receipt": True, "receipt_no": f"LC-{c}"}],
            f"Client meeting in {city} (fictional)")
        ev = {"id": f"EVT-{self.base + 1}", "owner_id": emp, "title": f"Meeting with {clients[0]}", "date": end.isoformat(),
              "start": "10:00", "end": "12:00", "location": city, "type": "business"}
        self.events.append(ev)
        m = self.msg(emp, start, "Re: besok", self.pick(LATER).format(who=clients[0]),
                     meeting_update={"event_id": ev["id"], "start": "15:00", "end": "16:30"})
        fam["reschedule_later"] = (cid, emp, ["IT-4"], m, ev)
        self.distractors(emp, r.randint(45, 75), cid, city, start)
        # 3 reschedule_earlier (review)
        emp, city, start = "EMP-002", cities[1], date(2026, 10, 8)
        cid, end = self.claim(3, emp, city, start, [
            lambda c, s, e: {"id": "IT-3", "category": "hotel", "date": s.isoformat(), "description": f"Hotel {city}, 1 night",
                             "amount": "650000", "nights": 1, "receipt": True, "receipt_no": f"HTL-{c}"},
            lambda c, s, e: {"id": "IT-4", "category": "late_checkout", "date": e.isoformat(), "description": "Late check-out until 15:00",
                             "amount": "250000", "checkout_time": "15:00", "location": city, "receipt": True, "receipt_no": f"LC-{c}"}],
            f"Partner review in {city} (fictional)")
        ev = {"id": f"EVT-{self.base + 2}", "owner_id": emp, "title": f"Partner review with {clients[1]}", "date": end.isoformat(),
              "start": "13:00", "end": "14:30", "location": city, "type": "business"}
        self.events.append(ev)
        m = self.msg(emp, start, f"Review {clients[1].split()[-1]}", self.pick(EARLIER).format(who=clients[1]),
                     meeting_update={"event_id": ev["id"], "start": "08:00", "end": "10:00"})
        fam["reschedule_earlier"] = (cid, emp, ["IT-4"], m, ev)
        self.distractors(emp, r.randint(45, 75), cid, city, start)
        # 4 slang_reply (arrives after the ask)
        emp, city, start = "EMP-004", cities[1], date(2026, 10, 9)
        cid, end = self.claim(4, emp, city, start, [lambda c, s, e: {"id": "IT-3", "category": "taxi", "date": e.isoformat(),
                                                                         "description": "Taxi hotel to airport", "amount": "98000", "receipt": False}],
                              f"Distributor visit in {city} (fictional)")
        reply = {"id": None, "owner_id": emp, "from": f"{PEOPLE[emp]}@{DOMAIN}", "date": "2026-10-24", "subject": "wkwk maap telat",
                 "body": self.pick(SLANG), "attachments": [{"name": f"scan{r.randint(1, 99):04d}.pdf", "type": "receipt", "claim_id": cid,
                                                            "item_id": "IT-3", "receipt_no": f"TX-{cid}-3", "amount": 98000}],
                 "mailbox": MAILBOX, "forwarded_by": f"{PEOPLE[emp]}@{DOMAIN}"}
        fam["slang_reply"] = (cid, emp, ["IT-3"], reply, None)
        self.distractors(emp, r.randint(45, 75), cid, city, start)
        # 5 quoted (key sentence beyond the snippet)
        emp, city, start = "EMP-001", cities[0], date(2026, 10, 20)
        cid, end = self.claim(5, emp, city, start, [lambda c, s, e: {"id": "IT-3", "category": "taxi", "date": e.isoformat(),
                                                                         "description": "Taxi hotel to airport", "amount": "120000", "receipt": False}],
                              f"Sales visit in {city} (fictional)")
        body = self.pick(QUOTE_LEAD) + "\n\n" + self.pick(QUOTE_KEY).format(a="hotel", b="bandara", d=end.strftime("%d/%m"))
        m = self.msg(emp, end + timedelta(days=1), self.pick(QUOTE_SUBJ), body,
                     [{"name": "lampiran1.jpg", "type": "receipt", "claim_id": cid, "item_id": "IT-3", "receipt_no": f"TX-{cid}-3", "amount": 120000}])
        fam["quoted"] = (cid, emp, ["IT-3"], m, None)
        self.distractors(emp, r.randint(45, 75), cid, city, start)
        # IDs in date order, so an ID says nothing about relevance
        self.msgs.sort(key=lambda m: (m["date"], r.random()))
        for i, m in enumerate(self.msgs):
            m["id"] = f"MSG-{self.base + 100 + i}"
        reply["id"] = f"MSG-{self.base + 99}"
        self.replies.append({"id": f"RPL-{self.base + 1}", "to": "EMP-004", "claim_id": fam["slang_reply"][0], "item_id": "IT-3",
                             "gap_code": "receipt_missing", "keywords_any": ["IT-3"], "reply": reply})
        for name, (cid, emp, items, m, ev) in fam.items():
            self.truth[name] = {"claim_id": cid, "employee_id": emp, "items": items, "needed": m["id"],
                                "event": ev["id"] if ev else None, "via_reply": name == "slang_reply"}
        return self


def write(out: Path, s: int, seed: int):
    g = Gen(s, seed).build()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for f in ("employees", "policy", "memos"):
        shutil.copy(HARD / f"{f}.json", out / f"{f}.json")
    dump = lambda n, v: (out / f"{n}.json").write_text(json.dumps(v, indent=2, ensure_ascii=False) + "\n")
    dump("inbox", g.msgs), dump("calendar", g.events), dump("claims", g.claims), dump("replies", g.replies), dump("truth", g.truth)
    return g


def main():
    for out, s, seed in ((LARGE_A, 0, 8101), (LARGE_B, 1, 8202)):
        g = write(out, s, seed)
        per = {e: sum(m["owner_id"] == e for m in g.msgs) for e in sorted({m["owner_id"] for m in g.msgs})}
        print(out.name, len(g.msgs), per)


if __name__ == "__main__":
    main()
