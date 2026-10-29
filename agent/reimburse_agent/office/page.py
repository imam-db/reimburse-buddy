"""The confirmation page for one pending plan (/admin/plans/<run_id>), as a pure HTML renderer.

Rendering the page is what "the reviewer was shown the notice" means: each injection notice on the page is
logged as `notice_shown` with the idempotency key <run_id>:notice_shown:<notice id>, so a refresh adds
nothing. Labels are Indonesian first, then English.
"""

from __future__ import annotations

from html import escape

from ..audit import AuditLog, make_event
from . import rules as R
from .directory import Directory

WHERE = {"email": "Email", "calendar": "Kalender", "task": "Tugas"}
TOOL_LABELS = {"send_email": "Email", "create_calendar_event": "Undangan rapat", "create_task": "Tugas"}
STATUS_LABELS = {"execution": "Dijalankan", "rejection": "Ditolak", "approval": "Disetujui",
                 "superseded": "Digantikan", "expired": "Kedaluwarsa"}
EXPIRED_REASON_ID = {"not answered before the deadline": "tidak dijawab sebelum batas waktu",
                     "server restarted before an answer": "server dimulai ulang sebelum ada jawaban"}
EXPIRED_FALLBACK_ID = "tidak dijawab sebelum batas waktu"
# Shown when a run cannot be resumed at all: the checkpointer has no thread for it, so the open question or
# the pending approval is gone. Nothing can be approved on this page; the way forward is a new run from the
# stored request ("Jalankan ulang").
STALE_TEXT = "Rencana ini tidak bisa dilanjutkan setelah server dimulai ulang. Gunakan Jalankan ulang."
DAYS_ID = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
MONTHS_ID = ("", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def _id_time(rules: R.Rules, dt, zone: str) -> str:
    """'Senin, 3 Nov 09.00 WIB' (Indonesian day and month names)."""
    loc = R.local(rules, dt, zone)
    return f"{DAYS_ID[loc.weekday()]}, {loc.day} {MONTHS_ID[loc.month]} {loc:%H.%M} {zone}"


def _short(text, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def e(x) -> str:
    return escape(str(x if x is not None else ""))


def log_notices_shown(plan: dict, log: AuditLog) -> int:
    n = 0
    for nt in plan.get("notices", []):
        n += log.append(make_event(plan["run_id"], "notice_shown", nt["source_id"], actor="system",
                                   payload={"text": nt["text"], "source_id": nt["source_id"],
                                            "rules": nt.get("rules", []), "where": f"/admin/plans/{plan['run_id']}",
                                            "plan_id": plan["plan_id"]},
                                   sources=[nt["source_id"]]))
    return n


def _status(events: list[dict], aid: str) -> str | None:
    for ev in reversed(events):
        if ev.get("action_id") == aid and ev["event_type"] in ("execution", "rejection", "approval", "superseded",
                                                               "expired"):
            return ev["event_type"]
    return None


def _expired_reason(events: list[dict], aids: set[str]) -> str:
    for ev in reversed(events):
        if ev["event_type"] == "expired" and ev.get("action_id") in aids:
            return str((ev.get("payload") or {}).get("reason") or "")
    return ""


def _person(directory: Directory, emp: str) -> str:
    c = directory.get(emp)
    return f"{e(c.name)} <span class='muted'>({e(emp)}, {e(c.role)}, {e(c.office)} {e(c.tz)})</span>" if c else e(emp)


def render(plan: dict, log: AuditLog, directory: Directory, rules: R.Rules, *, action_url: str | None = None,
           msg: str = "", allow_approve: bool = True, blocked_note: str | None = None,
           stale: bool = False, stylesheet_url: str | None = None, nav_html: str = "") -> str:
    """The confirmation page. `allow_approve=False` disables the Approve control (the requester, or the
    reviewer who submitted the request, may not approve it): the radio is rendered disabled so the form
    cannot submit "approve", and a note explains why (`blocked_note` overrides the default wording).
    Reject stays available. Presentation only: the gate in plan_graph still decides.

    `stale=True` means the run cannot be resumed at all (no thread in the checkpointer after a restart): the
    page says so, drops the decision form and offers "Jalankan ulang" instead.

    `stylesheet_url` links the app's single stylesheet and `nav_html` puts the site header in the body, so
    the page is rendered inside the same shell as every other screen without rewriting the markup."""
    log_notices_shown(plan, log)
    events = log.list(plan["run_id"])
    run = e(plan["run_id"])
    action_ids = {a["action_id"] for a in plan["actions"]}
    expired = {aid for aid in action_ids if _status(events, aid) == "expired"}
    refused = bool(plan.get("staging_errors"))
    link = f"<link rel=stylesheet href='{e(stylesheet_url)}'>" if stylesheet_url else ""
    out = [f"<!doctype html><html lang=id><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width, initial-scale=1'>"
           f"<title>Konfirmasi rencana {run} · Reimburse Buddy</title>"
           f"<meta name=theme-color content='#1a5fb4'>{link}</head><body>",
           "<a class='skip-link' href='#main'>Lewati ke konten</a>",
           nav_html,
           "<main id=main tabindex='-1'>",
           f"<h1>Konfirmasi rencana</h1>",
           f"<p class=muted>Peminta: {_person(directory, plan['requester'])}"
           + (f" &middot; <span id=submitted-by>Diajukan oleh: "
              f"{_person(directory, plan['submitted_by'])}</span>" if plan.get("submitted_by") else "")
           + "</p>",
           f"<details><summary>Detail rencana</summary><p class=muted>Run {run} &middot; "
           f"{e(plan['plan_id'])}</p></details>",
           "<p><b>Belum ada yang dikirim sampai Anda menyetujui.</b> Semua alat masih tiruan.</p>"]
    if not allow_approve:
        note = blocked_note or "Anda peminta rencana ini, jadi tombol Setujui dimatikan."
        out.append(f"<div class='notice' id='self-approval'><b>Anda tidak dapat menyetujui rencana ini</b>"
                   f"<p>{e(note)} Anda tetap bisa menolak atau meminta rekan lain menyetujuinya.</p></div>")
    if expired:
        reason = _expired_reason(events, expired)
        reason_id = EXPIRED_REASON_ID.get(reason, EXPIRED_FALLBACK_ID)
        also = f" <span class=muted>({e(reason)})</span>" if reason else ""
        out.append("<div class='expired' id='plan-expired'><b>Rencana ini sudah kedaluwarsa</b>"
                   f"<p>Alasan: {e(reason_id)}{also}</p>"
                   "<p>Tidak ada tindakan yang bisa disetujui lagi. Mulai rencana baru dari halaman Rencana.</p>"
                   f"<form method='post' action='/admin/intake/rerun/{run}'>"
                   "<button class='btn' type='submit' id='rerun'>Jalankan ulang</button></form></div>")
    if stale:
        out.append("<div class='expired' id='plan-stale'><b>Rencana ini tidak bisa dilanjutkan</b>"
                   f"<p>{e(STALE_TEXT)}</p>"
                   "<p>Tidak ada tindakan yang bisa disetujui di halaman ini.</p>"
                   f"<form method='post' action='/admin/intake/rerun/{run}'>"
                   "<button class='btn' type='submit' id='rerun'>Jalankan ulang</button></form></div>")
    if refused:
        errors = "".join(f"<li>{e(x)}</li>" for x in plan["staging_errors"])
        out.append("<div class='expired' id='plan-refused'><b>Rencana tidak dibuat</b>"
                   "<p>Permintaan ini tidak bisa diubah menjadi tindakan yang aman, jadi tidak ada yang "
                   "dijadwalkan atau dikirim.</p>"
                   f"<ul>{errors}</ul>"
                   "<p>Tidak ada tindakan yang bisa disetujui di halaman ini.</p></div>")
    if msg:
        out.append(f"<div class='alert' role='status' tabindex='-1'>{e(msg)}</div>")
    out.append(f"<div class=card><b>Permintaan</b><pre>{e(plan['request'])}</pre></div>")
    for nt in plan.get("notices", []):
        out.append(f"<div class=notice><b>&#9888; Peringatan injeksi</b><br>"
                   f"{e(nt['id'])}<br><i>{e(nt['en'])}</i></div>")
    if plan.get("open_questions"):
        for q in plan["open_questions"]:
            out.append(f"<div class=notice><b>Pertanyaan</b><br>{e(q['id_text'])}<br><i>{e(q['en'])}</i></div>")
    if plan.get("superseded"):
        out.append(f"<p class=muted>Menggantikan: {e(', '.join(plan['superseded']))}</p>")
    by_action: dict[str, list[dict]] = {}
    for a in plan.get("assumptions", []):
        by_action.setdefault(a.get("action_id", ""), []).append(a)
    form = action_url is not None and not expired and not refused and not stale
    if form:
        out.append(f"<form method=post action='{e(action_url)}' id='plan-form'>"
                   f"<input type=hidden name=plan_id value='{e(plan['plan_id'])}'>")
    for i, a in enumerate([] if refused else plan["actions"], 1):
        aid = a["action_id"]
        st = _status(events, aid)
        cls = " done" if st == "execution" else " rej" if st in ("rejection", "superseded") else ""
        infl = a.get("influenced_fields", {})
        names = ", ".join(directory.get(w).name if directory.get(w) else w for w in a.get("who", []))
        assume_here = by_action.get(aid, [])
        badges = ""
        if assume_here:
            badges += " <span class='tag assume-badge'>waktu diusulkan agent</span>"
        if infl:
            badges += " <span class='tag influenced' id='influenced-warning'>⚠ dipengaruhi konten mencurigakan</span>"
        if a["where"] == "email":
            sentence = f"Kirim email ke <b>{e(names)}</b> — segera setelah Anda menyetujui"
            if a["what"].get("body"):
                sentence += f" — isi: {e(_short(a['what']['body']))}"
        elif a["where"] == "calendar":
            sentence = f"Jadwalkan rapat dengan <b>{e(names)}</b>"
            req = directory.get(plan["requester"])
            if a.get("when", {}).get("start") and req:
                sentence += f" — {e(_id_time(rules, R.parse_iso(a['when']['start']), req.tz))}"
            if a["what"].get("title"):
                sentence += f" — judul: {e(a['what']['title'])}"
        else:
            sentence = f"Buat tugas untuk <b>{e(names)}</b>"
            if a["what"].get("title"):
                sentence += f" — {e(a['what']['title'])}"
        sentence += badges

        def field(name, text, pre=False):
            c = " class=influenced" if name in infl else ""
            flag = (" <span class='tag influenced'>dipengaruhi konten yang ditandai</span>") if name in infl else ""
            return f"<pre{c}>{e(text)}</pre>{flag}" if pre else f"<span{c}>{e(text)}</span>{flag}"
        rows = [f"<tr><th scope=row>Untuk</th><td>{'<br>'.join(_person(directory, w) for w in a['who'])}</td></tr>",
                f"<tr><th scope=row>Yang terjadi</th><td><span class=tag title='{e(a['tool'])}'>"
                f"{e(TOOL_LABELS.get(a['tool'], a['tool']))}</span> "
                + (field("subject", a["what"].get("subject", "")) if a["what"].get("subject")
                   else field("title", a["what"].get("title", "")))
                + field("body", a["what"].get("body", ""), pre=True) + "</td></tr>"]
        if a.get("when"):
            w = a["when"]
            loc = "".join(f"<tr><td>{_person(directory, p)}</td><td><b>{e(t)}</b></td></tr>"
                          for p, t in w["local"].items())
            alts = ", ".join(R.fmt_local(rules, R.parse_iso(x), directory.get(plan["requester"]).tz)
                             for x in w.get("alternatives", []))
            req = directory.get(plan["requester"])
            main_time = _id_time(rules, R.parse_iso(w["start"]), req.tz) if req else ""
            rows.append(f"<tr><th scope=row>Waktu</th><td>{e(w['duration_min'])} menit"
                        + (f", <b>{e(main_time)}</b>" if main_time else "")
                        + f"<div class=note>Waktu lokal setiap peserta</div>"
                        f"<table><caption class=visually-hidden>Waktu lokal setiap peserta</caption>{loc}</table>"
                        + (f"<span class=muted>Alternatif: {e(alts)}</span>" if alts else "")
                        + "</td></tr>")
        else:
            rows.append("<tr><th scope=row>Waktu</th><td>Segera setelah disetujui</td></tr>")
        rows.append(f"<tr><th scope=row>Di mana</th><td>{e(WHERE[a['where']])}</td></tr>")
        rows.append(f"<tr><th scope=row>Mengapa</th><td>{e(', '.join(a['why']) or '-')}</td></tr>")
        if assume_here:
            ids = ", ".join(x["id"] for x in assume_here)
            english = " ".join(x["en"] for x in assume_here)
            assume = (f"<div class=assume><b>Asumsi:</b> {e(ids)}"
                      f"<details><summary>English</summary><i>{e(english)}</i></details></div>")
        else:
            assume = ""
        tech = (f"<details><summary>Detail teknis</summary><p class=muted>Run {run} &middot; "
                f"plan {e(plan['plan_id'])} &middot; aksi {e(aid)} &middot; hash argumen "
                f"{e(a['args_hash'][:12])}</p></details>")
        decide = ""
        if form and st is None:
            approve_control = (
                f"<label><input type=radio name='decision:{e(aid)}' value=approve> Setujui</label> "
                if allow_approve else
                f"<label><input type=radio name='decision:{e(aid)}' value=approve disabled> "
                f"Setujui (dimatikan: Anda peminta)</label> ")
            decide = (f"<p class=\"decision\">{approve_control}"
                      f"<label><input type=radio name='decision:{e(aid)}' value=reject checked> Tolak</label></p>"
                      f"<p><label>Catatan (opsional, agen akan mengusulkan rencana baru):"
                      f"<br><textarea name='note:{e(aid)}' rows=2 cols=70></textarea></label></p>")
        elif st:
            decide = f"<p class=tag title='{e(st)}'>Status: {e(STATUS_LABELS.get(st, st))}</p>"
        out.append(f"<div class='card{cls}'><div class=\"sentence\">{sentence}</div>"
                   f"<b>Aksi {i}</b><table><caption class=visually-hidden>Rincian aksi {i}</caption>"
                   f"{''.join(rows)}</table>{assume}{tech}{decide}</div>")
    if form:
        actions = len(plan["actions"])
        out.append(f"<div class='submit-bar' id='submit-bar'>"
                   f"<span class='counts' id='decision-counts'>0 disetujui, {actions} ditolak</span>"
                   f"<button type=button id='approve-all'>Setujui semua</button>"
                   f"<button type=button id='reject-all'>Tolak semua</button>"
                   f"<button type=submit class='primary'>Kirim keputusan</button></div></form>")
        out.append("""<script>(function(){var f=document.getElementById('plan-form'),c=document.getElementById('decision-counts');
if(!f||!c)return;function refresh(){var a=f.querySelectorAll("input[value=approve]:checked").length,
r=f.querySelectorAll("input[value=reject]:checked").length;c.textContent=a+' disetujui, '+r+' ditolak';}
f.addEventListener('change',refresh);refresh();
document.getElementById('approve-all').addEventListener('click',function(){
f.querySelectorAll("input[value=approve]:not([disabled])").forEach(function(x){x.checked=true;});refresh();});
document.getElementById('reject-all').addEventListener('click',function(){
if(!confirm('Tolak semua aksi?'))return;
f.querySelectorAll("input[value=reject]").forEach(function(x){x.checked=true;});refresh();});})();</script>""")
    out.append("</main></body></html>")
    return "".join(out)
