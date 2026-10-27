"""Drop-first employee flow: trip header -> drop files -> editable draft cards -> one submit.

Every card field records where its value came from: `ocr` / `pdf_text` / `vision` (extracted) or
`self_declared` (typed or changed by the employee). The provenance travels with the claim item and the
forwarded attachment so the reviewer sees it. Approved amounts still come only from check_claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from reimburse_buddy.amounts import normalize_attachment
from reimburse_buddy.gaps import B_ONLY
from reimburse_buddy.models import rupiah
from reimburse_buddy.rules import check_claim

from . import app as core
from .extract import DOC_TYPES, FIELDS, LOW_CONFIDENCE, Draft, get_extractor

router = APIRouter()
e = core.e
EXTRACTED = ("ocr", "pdf_text", "vision")
DOC_TO_CATEGORY = {"hotel_receipt": "hotel", "taxi_receipt": "taxi", "meal_receipt": "meal",
                   "flight_receipt": "flight", "boarding_pass": "flight", "other": ""}
CARD_FIELDS = ("category",) + tuple(f for f in FIELDS if f not in ("doc_type", "passenger"))
# Upper bound of a plausible amount per item (whole Rupiah); hotel is per night. Above it -> needs review.
# Override with env REIMBURSE_PLAUSIBLE_MAX='{"taxi": 1500000}'.
PLAUSIBLE_MAX = {"taxi": 1_000_000, "meal": 1_000_000, "hotel": 5_000_000, "flight": 15_000_000,
                 "per_diem": 2_000_000, "late_checkout": 3_000_000}
MAX_FILES = 20


# --- draft model -------------------------------------------------------------
def _field(value=None, confidence=0.0, source="none") -> dict:
    return {"value": value, "confidence": confidence, "source": source}


def _card_from_files(files: list[dict]) -> dict:
    """Combine the extractions of the card's files: for each field, the most confident non-empty value."""
    fields = {k: _field() for k in CARD_FIELDS}
    for f in files:
        ex = f["extract"]["fields"]
        cat = DOC_TO_CATEGORY.get(f["doc_type"], "")
        if cat and (fields["category"]["value"] in (None, "") or f["doc_type"] != "boarding_pass"):
            fields["category"] = _field(cat, ex["doc_type"]["confidence"], ex["doc_type"]["source"])
        for k in CARD_FIELDS[1:]:
            v = ex.get(k, {})
            if v.get("value") not in (None, "") and v["confidence"] > fields[k]["confidence"]:
                fields[k] = dict(v)
    return {"id": uuid.uuid4().hex[:6], "files": files, "fields": fields, "errors": []}


def _file_entry(stored: str, name: str, draft: Draft, sha256: str = "") -> dict:
    j = draft.to_json()
    return {"id": uuid.uuid4().hex[:6], "name": name, "stored": stored, "extract": j, "sha256": sha256,
            "doc_type": j["fields"]["doc_type"]["value"] or "other",
            "doc_type_source": j["fields"]["doc_type"]["source"], "error": j["error"]}


async def _save_and_extract(upload: UploadFile, prefix: str) -> dict:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in core.ALLOWED_UPLOAD_EXT:
        raise HTTPException(400, f"File type {ext or '(none)'} not allowed; use {', '.join(sorted(core.ALLOWED_UPLOAD_EXT))}")
    data = await upload.read(core.MAX_UPLOAD_BYTES + 1)
    if len(data) > core.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{upload.filename} is larger than 5 MB")
    core.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload.filename).name)[:80]
    stored = f"{prefix}_{uuid.uuid4().hex[:6]}_{safe}"
    path = core.UPLOAD_DIR / stored
    path.write_bytes(data)
    return _file_entry(stored, safe, get_extractor().extract(path), hashlib.sha256(data).hexdigest())


def get_draft(did: str) -> dict:
    d = core.DRAFTS.get(did)
    if not d:
        raise HTTPException(404, f"Unknown draft {did}")
    return d


# --- pages ---------------------------------------------------------------------
DROP_JS = """<script>
const z=document.getElementById('drop'),i=document.getElementById('files'),l=document.getElementById('picked');
function show(){l.textContent=i.files.length?Array.from(i.files).map(f=>f.name).join(', '):'';}
z.addEventListener('dragover',ev=>{ev.preventDefault();z.classList.add('over');});
z.addEventListener('dragleave',()=>z.classList.remove('over'));
z.addEventListener('drop',ev=>{ev.preventDefault();z.classList.remove('over');i.files=ev.dataTransfer.files;show();});
i.addEventListener('change',show);
</script>"""


@router.get("/employee/new", response_class=HTMLResponse)
def new_claim(emp: str = "EMP-001"):
    me = core.store.get_employee(emp)
    body = f"""<p><a href="/employee?emp={e(emp)}">&larr; My claims</a> &middot; {e(me['name'])}</p>
<form method="post" action="/employee/drafts" enctype="multipart/form-data"><input type="hidden" name="emp" value="{e(emp)}">
<div class="box"><h2>1. Trip</h2>Purpose <input name="purpose" size="40" required> Destination <input name="destination" value="Surabaya" required>
<br>Dates <input type="date" name="start_date" required> &rarr; <input type="date" name="end_date" required></div>
<div class="box"><h2>2. Drop your receipts and boarding passes</h2>
<label id="drop" class="drop">Drag photos or PDFs here, or click to choose (up to {MAX_FILES} files, 5 MB each)
<input id="files" type="file" name="files" multiple accept=".pdf,.png,.jpg,.jpeg" style="display:none"></label>
<div id="picked" class="note"></div>
<p class="note">Extraction: {e(get_extractor().name)}. You can check and fix every field on the next screen.</p>
<button type="submit">Read files</button></div></form>{DROP_JS}"""
    return core.page("New claim", body)


@router.post("/employee/drafts")
async def create_draft(emp: str = Form(...), purpose: str = Form(...), destination: str = Form(...),
                       start_date: str = Form(...), end_date: str = Form(...), files: list[UploadFile] = File([])):
    core.store.get_employee(emp)
    if end_date < start_date:
        raise HTTPException(400, "The trip end date is before the start date")
    files = [f for f in files if f.filename]
    if len(files) > MAX_FILES:
        raise HTTPException(400, f"Drop at most {MAX_FILES} files at once")
    did = uuid.uuid4().hex[:8]
    entries = [await _save_and_extract(f, f"D{did}") for f in files]
    cards = [_card_from_files([x]) for x in entries]
    # A boarding pass and an e-ticket for the same flight become one card.
    by_flight: dict[str, dict] = {}
    merged = []
    for c in cards:
        fn = c["fields"]["flight_no"]["value"]
        if c["fields"]["category"]["value"] == "flight" and fn:
            if fn in by_flight:
                by_flight[fn]["files"] += c["files"]
                for k, v in c["fields"].items():
                    if by_flight[fn]["fields"][k]["value"] in (None, ""):
                        by_flight[fn]["fields"][k] = v
                continue
            by_flight[fn] = c
        merged.append(c)
    core.DRAFTS[did] = {"id": did, "emp": emp, "header": {"purpose": purpose, "destination": destination,
                                                          "start_date": start_date, "end_date": end_date},
                        "cards": merged or [_card_from_files([])], "errors": []}
    return RedirectResponse(f"/employee/drafts/{did}", status_code=303)


def _badge(f: dict) -> str:
    src = f["source"]
    if src == "self_declared":
        return "<span class='tag warn'>self_declared</span>"
    if src in EXTRACTED:
        cls = "low" if f["confidence"] < LOW_CONFIDENCE else "ok"
        return f"<span class='tag {cls}'>extracted ({e(src)} {f['confidence']:.2f})</span>"
    return ""


def _err(f: dict) -> str:
    return f"<div class=err>Could not read this file: {e(f['error'])}</div>" if f["error"] else ""


def _input(card: dict, k: str, allowed_cats: list[str]) -> str:
    f = card["fields"][k]
    v = "" if f["value"] is None else str(f["value"])
    low = " class='low'" if f["source"] in EXTRACTED and f["confidence"] < LOW_CONFIDENCE and v else ""
    name = f"c_{card['id']}_{k}"
    if k == "category":
        ctl = f"<select name='{name}'{low}>{core.options([('', 'choose...')] + [(c, c) for c in allowed_cats], v)}</select>"
    elif k == "date":
        ctl = f"<input type='date' name='{name}' value='{e(v)}'{low}>"
    elif k == "amount":
        shown = f"{int(v):,}".replace(",", ".") if v.isdigit() else v
        ctl = f"<input name='{name}' value='{e(shown)}' size=12 inputmode='numeric' placeholder='1.350.000'{low}>"
    elif k == "nights":
        ctl = f"<input name='{name}' value='{e(v)}' size=3{low}>"
    else:
        ctl = f"<input name='{name}' value='{e(v)}' size=22{low}>"
    return f"<label>{e(k.replace('_', ' '))} {_badge(f)}<br>{ctl}</label>"


@router.get("/employee/drafts/{did}", response_class=HTMLResponse)
def draft_page(did: str):
    d = get_draft(did)
    h = d["header"]
    allowed = [c for c in core.store.policy["allowed_categories"] if c not in ("late_checkout",)]
    others = lambda cid: [(c["id"], f"card {n + 1}") for n, c in enumerate(d["cards"]) if c["id"] != cid]  # noqa: E731
    cards_html = ""
    for n, c in enumerate(d["cards"]):
        cat = c["fields"]["category"]["value"]
        keys = ["category", "date", "amount", "merchant"] + (["flight_no"] if cat == "flight" else []) + \
               (["nights"] if cat == "hotel" else [])
        files = ""
        for f in c["files"]:
            mv = (f"<select name='move_{f['id']}'>{core.options([('', 'move to...')] + others(c['id']))}</select>"
                  f"<button name=action value='move:{f['id']}'>Move</button>") if len(d["cards"]) > 1 else ""
            files += (f"<li>{e(f['name'])} <select name='f_{f['id']}_doc_type'>{core.options([(t, t) for t in DOC_TYPES], f['doc_type'])}</select>"
                      f" {_badge({'source': f['doc_type_source'], 'confidence': f['extract']['fields']['doc_type']['confidence']})}"
                      f"{_err(f)} {mv}</li>")
        merge = (f"<select name='merge_{c['id']}'>{core.options([('', 'merge into...')] + others(c['id']))}</select>"
                 f"<button name=action value='merge:{c['id']}'>Merge</button>") if len(d["cards"]) > 1 else ""
        split = f"<button name=action value='split:{c['id']}'>Split</button>" if len(c["files"]) > 1 else ""
        errs = "".join(f"<div class=err>{e(x)}</div>" for x in c["errors"])
        cards_html += (f"<div class=card><b>Card {n + 1}</b>{errs}{''.join(_input(c, k, allowed) for k in keys)}"
                       f"<div class=note>Files</div><ul>{files or '<li class=note>No file: add one after submitting.</li>'}</ul>"
                       f"{merge} {split} <button name=action value='delete:{c['id']}'>Delete</button></div>")
    errs = "".join(f"<p class=err>{e(x)}</p>" for x in d["errors"])
    body = f"""<p>{e(h['purpose'])} &middot; {e(h['destination'])} {e(h['start_date'])} &rarr; {e(h['end_date'])}</p>
<p class="note">3. Check the drafted items. Yellow fields were read with low confidence. Anything you type or change is
marked self_declared for the reviewer.</p>{errs}
<form method="post" action="/employee/drafts/{e(did)}"><div class="cards">{cards_html}</div>
<p><button name=action value="save">Save changes</button> <button name=action value="add">Add an item without a file</button>
<button name=action value="submit" style="font-weight:bold">Submit claim</button></p></form>"""
    return core.page("Check your items", body)


def _apply_edits(d: dict, form) -> None:
    for c in d["cards"]:
        for k in CARD_FIELDS:
            name = f"c_{c['id']}_{k}"
            if name not in form:
                continue
            raw = str(form[name]).strip()
            cur = c["fields"][k]["value"]
            new: object = raw
            if k == "amount" and raw:
                try:
                    new = int(core.parse_amount(raw))
                except ValueError:
                    new = raw  # validated on submit
            elif k == "nights" and raw.isdigit():
                new = int(raw)
            if (cur if cur is not None else "") != new and str(cur if cur is not None else "") != raw.replace(".", "") or \
                    (cur in (None, "") and raw):
                if str(cur) != str(new):
                    c["fields"][k] = _field(new if raw else None, 1.0 if raw else 0.0, "self_declared" if raw else "none")
        for f in c["files"]:
            v = form.get(f"f_{f['id']}_doc_type")
            if v in DOC_TYPES and v != f["doc_type"]:
                f["doc_type"], f["doc_type_source"] = v, "self_declared"


def _validate(d: dict) -> bool:
    h, ok = d["header"], True
    allowed = core.store.policy["allowed_categories"]
    for c in d["cards"]:
        c["errors"] = []
        f = {k: v["value"] for k, v in c["fields"].items()}
        if f["category"] not in allowed:
            c["errors"].append("Choose a category.")
        if not f["date"] or not (h["start_date"] <= str(f["date"]) <= h["end_date"]):
            c["errors"].append("Date must fall within the trip.")
        if not isinstance(f["amount"], int) or f["amount"] <= 0:
            c["errors"].append("Amount must be whole Rupiah, e.g. 1.350.000.")
        if f["category"] == "flight" and not f["flight_no"]:
            c["errors"].append("Flight number is required for flights.")
        if f["category"] == "hotel" and not str(f["nights"] or "").isdigit():
            c["errors"].append("Number of nights is required for hotels.")
        ok = ok and not c["errors"]
    if not d["cards"]:
        d["errors"] = ["Add at least one item."]
        return False
    d["errors"] = []
    return ok


def plausible_max(category: str) -> int | None:
    limits = dict(PLAUSIBLE_MAX, **json.loads(os.environ.get("REIMBURSE_PLAUSIBLE_MAX", "{}")))
    return limits.get(category)


def _norm(x) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(x or "").upper())


def used_hashes(exclude_claim: str = "") -> dict[str, tuple[str, str]]:
    """sha256 -> (claim_id, item_id) for every file already forwarded, across all claims."""
    out = {}
    for m in core.store.inbox:
        for a in m.get("attachments", []):
            if a.get("sha256") and a.get("linked_claim") != exclude_claim:
                out.setdefault(a["sha256"], (a.get("linked_claim"), a.get("linked_item")))
    return out


def evidence_review_reasons(f: dict, item: dict, trip: dict, passenger: str, dup: bool) -> list[str]:
    """Cross-validation of one uploaded file against its item. Any reason -> needs_review.

    Extraction confidence is only a display hint; these rules decide. Empty list = verified by the file.
    """
    if f.get("error"):
        return ["unreadable_file"]
    ex = {k: v.get("value") for k, v in f["extract"]["fields"].items()}
    bp = f["doc_type"] == "boarding_pass"
    reasons = []
    if f.get("doc_type_source") == "self_declared" or ex.get("flight_no" if bp else "amount") in (None, ""):
        reasons.append("self_declared_only")  # the file itself does not show the doc type / amount
    if DOC_TO_CATEGORY.get(f["doc_type"]) != item["category"]:
        reasons.append("doc_type_mismatch")
    mism = []
    if not bp and ex.get("amount") not in (None, "") and str(ex["amount"]) != str(item["amount"]):
        mism.append("amount")
    if ex.get("date") and ex["date"] != item["date"]:
        mism.append("date")
    if ex.get("flight_no") and item.get("flight_no") and _norm(ex["flight_no"]) != _norm(item["flight_no"]):
        mism.append("flight_no")
    if mism:
        reasons.append("mismatch_with_file")
    if ex.get("date") and not (trip["start_date"] <= ex["date"] <= trip["end_date"]):
        reasons.append("file_date_outside_trip")
    cap = plausible_max(item["category"])
    if cap and int(item["amount"]) > cap * max(1, int(item.get("nights") or 1) if item["category"] == "hotel" else 1):
        reasons.append("implausible_amount")
    if bp and (_norm(ex.get("passenger")) != _norm(passenger) or not ex.get("flight_no")):
        reasons.append("boarding_pass_mismatch")
    if dup:
        reasons.append("duplicate_file")
    if f["extract"].get("disagreements"):
        reasons.append("ocr_engine_disagreement")  # Tesseract and RapidOCR read this file differently
    return reasons


def _forward(emp: str, cid: str, item: dict, f: dict, trip: dict, dup: bool = False) -> str:
    """Forward one file to the reimburse@ mailbox, linked to its claim item. Returns the message ID."""
    me = core.store.get_employee(emp)
    mid = f"MSG-W{len([m for m in core.store.inbox if m['id'].startswith('MSG-W')]) + 1:03d}"
    prov = dict(item.get("provenance", {}), doc_type=f["doc_type_source"])
    ex = {k: v.get("value") for k, v in f["extract"]["fields"].items()}
    if f["doc_type"] == "boarding_pass":
        # Metadata comes from the file itself, never from the item, so a pass from another trip cannot match.
        att = {"name": f["name"], "type": "boarding_pass", "flight": ex.get("flight_no") or "",
               "date": ex.get("date") or "", "passenger": ex.get("passenger") or ""}
    else:
        # The amount comes from the file (OCR / extraction), never from the item, so the engine can compare them.
        att = {"name": f["name"], "type": "receipt", "claim_id": cid, "item_id": item["id"], "amount_required": True}
        if ex.get("amount") not in (None, ""):
            att["amount"] = ex["amount"]
        att = normalize_attachment(att)
    reasons = evidence_review_reasons(f, item, trip, me["name"], dup)
    att.update(stored_as=f["stored"], sha256=f.get("sha256", ""), linked_claim=cid, linked_item=item["id"],
               provenance=prov, self_declared=any(v == "self_declared" for v in prov.values()),
               evidence_status="needs_review" if reasons else "extracted",
               review_reason=reasons[0] if reasons else "", review_reasons=reasons)
    core.store.inbox.append({"id": mid, "owner_id": emp, "from": me["email"], "date": date.today().isoformat(),
                             "subject": f"Claim {cid} {item['id']}: {f['doc_type'].replace('_', ' ')}", "body": "",
                             "attachments": [att], "mailbox": core.MAILBOX, "forwarded_by": me["email"]})
    rv = core.review(cid)
    if reasons:  # kept out of check_claim until a reviewer accepts it
        rv["pending"][mid] = {"item_id": item["id"], "reason": ", ".join(reasons), "reasons": reasons, "state": "pending"}
    else:
        rv["evidence"].append(mid)
    rv.setdefault("origin", {})[mid] = "employee_upload"
    return mid


def forward_files(emp: str, cid: str, trip: dict, pairs: list[tuple[dict, dict]]) -> None:
    """Forward (item, file) pairs. A file hash used by another item, here or in any earlier claim, is a duplicate."""
    seen = used_hashes()
    per_hash: dict[str, set] = {}
    for it, f in pairs:
        per_hash.setdefault(f.get("sha256", ""), set()).add(it["id"])
    for it, f in pairs:
        h = f.get("sha256", "")
        prior = seen.get(h)
        dup = bool(h) and (len(per_hash[h]) > 1 or (prior is not None and prior != (cid, it["id"])))
        _forward(emp, cid, it, f, trip, dup)


def _submit(d: dict) -> str:
    h, emp = d["header"], d["emp"]
    cid = f"CLM-2026-W{len([c for c in core.store.claims if c.get('batch') == core.WEB_BATCH]) + 1:03d}"
    items = []
    for n, c in enumerate(d["cards"], 1):
        f = {k: v["value"] for k, v in c["fields"].items()}
        it = {"id": f"IT-{n}", "category": f["category"], "date": f["date"],
              "description": f["merchant"] or f["category"], "amount": str(f["amount"]),
              "receipt": False, "provenance": {k: v["source"] for k, v in c["fields"].items() if v["value"] not in (None, "")}}
        if f["category"] == "flight":
            fn = _norm(f["flight_no"])
            it.update(flight_no=re.sub(r"^([A-Z]{2})(\d+)$", r"\1 \2", fn), boarding_pass=False)
        if f["category"] == "hotel":
            it["nights"] = int(f["nights"])
        items.append((it, c["files"]))
    core.store.claims.append({"id": cid, "employee_id": emp, "status": "submitted", "submitted_at": date.today().isoformat(),
                              "purpose": h["purpose"], "trip": {"origin": "Jakarta", "destination": h["destination"],
                                                                "start_date": h["start_date"], "end_date": h["end_date"]},
                              "items": [i for i, _ in items], "batch": core.WEB_BATCH})
    trip = {"start_date": h["start_date"], "end_date": h["end_date"]}
    forward_files(emp, cid, trip, [(it, f) for it, files in items for f in files])
    core.DRAFTS.pop(d["id"], None)
    return cid


@router.post("/employee/drafts/{did}")
async def update_draft(did: str, request: Request):
    d = get_draft(did)
    form = await request.form()
    _apply_edits(d, form)
    action = str(form.get("action", "save"))
    verb, _, arg = action.partition(":")
    cards = d["cards"]
    find = lambda cid: next((c for c in cards if c["id"] == cid), None)  # noqa: E731
    if verb == "delete" and find(arg):
        cards.remove(find(arg))
    elif verb == "split" and (c := find(arg)) and len(c["files"]) > 1:
        i = cards.index(c)
        cards[i:i + 1] = [_card_from_files([f]) for f in c["files"]]
    elif verb == "merge" and (c := find(arg)) and (t := find(str(form.get(f"merge_{arg}", "")))):
        t["files"] += c["files"]
        for k, v in c["fields"].items():
            if t["fields"][k]["value"] in (None, ""):
                t["fields"][k] = v
        cards.remove(c)
    elif verb == "move":
        src = next((c for c in cards if any(f["id"] == arg for f in c["files"])), None)
        t = find(str(form.get(f"move_{arg}", "")))
        if src and t and t is not src:
            f = next(f for f in src["files"] if f["id"] == arg)
            src["files"].remove(f)
            t["files"].append(f)
    elif verb == "add":
        cards.append(_card_from_files([]))
    elif verb == "submit":
        if _validate(d):
            cid = _submit(d)
            return RedirectResponse(f"/employee/claims/{cid}?msg=Submitted", status_code=303)
    return RedirectResponse(f"/employee/drafts/{did}", status_code=303)


AWAITING = "Menunggu verifikasi finance / Awaiting finance review"
REVIEWING = "finance is reviewing this item"
GAP_TEXT = {**{code: REVIEWING for code in B_ONLY}, "receipt_missing": "receipt missing", "boarding_pass_missing": "boarding pass missing",
            "hotel_over_cap": "over the hotel cap: finance will review", "duplicate_suspected": "possible duplicate",
            "hotel_nights_mismatch": "nights do not match the trip", "late_checkout_unjustified": "needs justification"}


@router.get("/employee/claims/{cid}", response_class=HTMLResponse)
def employee_claim(cid: str, msg: str = ""):
    try:
        claim = core.store.get_claim(cid)
    except KeyError:
        raise HTTPException(404, f"Unknown claim {cid}")
    chk = core.run_check(cid)
    files_by_item: dict[str, list] = {}
    for m in core.store.inbox:
        for a in m.get("attachments", []):
            if a.get("claim_id") == cid and a.get("item_id"):
                files_by_item.setdefault(a["item_id"], []).append(a["name"])
    pending = core.pending_items(cid)
    used = {i.item_id: i.evidence_used for i in chk.items}
    rows = ""
    for it, res in zip(claim["items"], chk.items):
        have = files_by_item.get(it["id"], []) + [x for x in used.get(it["id"], []) if x.startswith("MSG")
                                                  and not any(x in f for f in files_by_item.get(it["id"], []))]
        missing = list(dict.fromkeys(GAP_TEXT.get(str(g), str(g).replace("_", " ")) for g in res.gaps))
        if it["id"] in pending:
            missing = [AWAITING]
        status = (", ".join(missing) if missing else ("rejected" if res.status.value == "rejected" else "evidence complete"))
        cls = "warn" if missing else ("bad" if res.status.value == "rejected" else "ok")
        add = (f"<form method=post action='/employee/claims/{e(cid)}/items/{e(it['id'])}/files' enctype='multipart/form-data'>"
               f"<input type=file name=file accept='.pdf,.png,.jpg,.jpeg' required> <button>Add file</button></form>")
        rows += (f"<tr><td>{e(it['category'])}<div class=note>{e(it['description'])}</div></td><td>{e(it['date'])}</td>"
                 f"<td class=num>{e(rupiah(res.claimed))}</td><td>{e(', '.join(have)) or '<span class=note>none</span>'}</td>"
                 f"<td><span class='tag {cls}'>{e(status)}</span></td><td>{add}</td></tr>")
    body = f"""<p><a href="/employee?emp={e(claim['employee_id'])}">&larr; My claims</a></p>
{f'<p class="tag ok">{e(msg)}: files were forwarded to {e(core.MAILBOX)} and linked to their items.</p>' if msg else ''}
<p>{e(claim['purpose'])} &middot; {e(claim['trip']['destination'])} {e(claim['trip']['start_date'])} &rarr;
{e(claim['trip']['end_date'])} &middot; status {core.status_tag(claim['status'])}</p>
<table><tr><th>Item</th><th>Date</th><th class=num>Claimed</th><th>Files</th><th>Evidence</th><th></th></tr>{rows}</table>
<p class="note">Finance reviews every claim. Amounts shown are what you claimed; finance decides what is approved.</p>"""
    return core.page(f"Claim {cid}", body)


@router.post("/employee/claims/{cid}/items/{iid}/files")
async def add_file(cid: str, iid: str, file: UploadFile = File(...)):
    claim = core.store.get_claim(cid)
    item = next((i for i in claim["items"] if i["id"] == iid), None)
    if not item:
        raise HTTPException(404, f"Unknown item {iid}")
    entry = await _save_and_extract(file, cid)
    forward_files(claim["employee_id"], cid, claim["trip"], [(item, entry)])
    return RedirectResponse(f"/employee/claims/{cid}?msg=File+added", status_code=303)
