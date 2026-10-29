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
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from reimburse_buddy.amounts import normalize_attachment
from reimburse_buddy.gaps import B_ONLY
from reimburse_buddy.models import rupiah
from reimburse_buddy.rules import check_claim

from . import app as core
from . import auth
from . import plans  # noqa: F401  # puts agent/ on sys.path
from . import ui
from .labels import label
from reimburse_agent.qa.tools import UNDER_REVIEW, is_sensitive_item  # noqa: E402  # same masking as Q&A
from reimburse_buddy.models import to_jsonable  # noqa: E402
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
FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}


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


def get_draft(did: str, emp: str) -> dict:
    """The draft if `emp` owns it; someone else's draft is a 404 exactly like a missing one."""
    d = core.DRAFTS.get(did)
    if not d or d.get("emp") != emp:
        raise HTTPException(404, f"Unknown draft {did}")
    return d


# --- pages ---------------------------------------------------------------------
DROP_JS = r"""<script>(function(){
const z=document.getElementById('drop'),i=document.getElementById('files'),l=document.getElementById('picked');
const pick=document.getElementById('pick-files'),submit=document.getElementById('read-files');
if(!z||!i||!l)return;
const MAX=__MAX__, MAXB=__MAXB__, OK=/\.(pdf|png|jpe?g)$/i;
let files=[];
function size(n){return n>=1048576?(n/1048576).toFixed(1)+' MB':Math.max(1,Math.round(n/1024))+' KB';}
function bad(f){if(!OK.test(f.name))return "__BADTYPE__";if(f.size>MAXB)return "__TOOBIG__";return "";}
function render(){
l.innerHTML='';
files.forEach(function(f,idx){
const li=document.createElement('li'),why=bad(f);
li.appendChild(document.createTextNode(f.name+' ('+size(f.size)+') '));
if(why){const e=document.createElement('span');e.className='err';e.textContent=why;li.appendChild(e);}
const b=document.createElement('button');b.type='button';b.textContent="__REMOVE__";
b.addEventListener('click',function(){files.splice(idx,1);sync();});
li.appendChild(b);l.appendChild(li);
});
submit.disabled=files.length>MAX||files.some(function(f){return bad(f);});
}
function sync(){const dt=new DataTransfer();files.forEach(function(f){dt.items.add(f);});i.files=dt.files;render();}
function add(list){Array.from(list).forEach(function(f){files.push(f);});sync();}
z.addEventListener('dragover',function(ev){ev.preventDefault();z.classList.add('over');});
z.addEventListener('dragleave',function(){z.classList.remove('over');});
z.addEventListener('drop',function(ev){ev.preventDefault();z.classList.remove('over');add(ev.dataTransfer.files);});
if(pick)pick.addEventListener('click',function(){i.click();});
i.addEventListener('change',function(){add(i.files);});
})();</script>"""
DROP_JS = (DROP_JS.replace("__MAXB__", str(core.MAX_UPLOAD_BYTES)).replace("__MAX__", str(MAX_FILES))
           .replace("__BADTYPE__", label("ui_file_bad_type")).replace("__TOOBIG__", label("ui_file_too_big"))
           .replace("__REMOVE__", label("ui_delete")))

BUSY_READ_JS = """<script>(function(){
var f=document.getElementById('drop-form'),b=document.getElementById('read-files'),i=document.getElementById('files');
if(!f||!b||!i)return;
f.addEventListener('submit',function(){
var n=i.files.length,t="__TEXT__";
b.textContent=t.replace("{n}",n);b.disabled=true;f.setAttribute("aria-busy","true");
});
})();</script>""".replace("__TEXT__", label("ui_busy_read"))

BUSY_SUBMIT_JS = """<script>(function(){
var f=document.getElementById('draft-form'),b=document.getElementById('submit-claim');
if(!f||!b)return;
f.addEventListener('submit',function(e){
var s=e.submitter;
if(!s||s.value!=='submit')return;
var h=document.createElement('input');h.type='hidden';h.name='action';h.value='submit';f.appendChild(h);
b.textContent="__TEXT__";b.disabled=true;f.setAttribute("aria-busy","true");
});
})();</script>""".replace("__TEXT__", label("ui_busy_submit"))

FOCUS_JS = """<script>(function(){
var h=location.hash;if(!h)return;var el=document.getElementById(h.slice(1));if(!el)return;
try{el.focus({preventScroll:true});}catch(e){el.focus();}
if(!matchMedia('(prefers-reduced-motion: reduce)').matches&&el.scrollIntoView)el.scrollIntoView({block:'nearest'});
})();</script>"""

CATEGORY_JS = """<script>(function(){
document.querySelectorAll('[data-card]').forEach(function(card){
var sel=card.querySelector("select[data-role=category]");if(!sel)return;
function sync(){var cat=sel.value;
card.querySelectorAll('.cat-field[data-cat]').forEach(function(f){
var on=f.getAttribute('data-cat')===cat;f.hidden=!on;
f.querySelectorAll('input,select,textarea').forEach(function(x){x.disabled=!on;});});}
sel.addEventListener('change',sync);});
})();</script>"""

DATE_MIN_JS = """<script>(function(){
var s=document.getElementById('start-date'),e=document.getElementById('end-date');
if(!s||!e)return;
function sync(){e.min=s.value;if(e.value&&s.value&&e.value<s.value)e.value='';}
s.addEventListener('change',sync);sync();
})();</script>"""


@router.get("/employee/new", response_class=HTMLResponse)
def new_claim(request: Request):
    emp = auth.current_employee(request)
    if not emp:
        return core.page(label("ui_new_claim"), auth.sign_in_notice())
    me = core.store.get_employee(emp)
    steps = [label("ui_step_trip"), label("ui_step_upload"), label("ui_step_verify")]
    body = f"""<p><a href="/employee">&larr; {e(label('ui_my_claims'))}</a> &middot; {e(me['name'])}</p>
{ui.stepper(steps, 0)}
<form method="post" action="/employee/drafts" enctype="multipart/form-data" id="drop-form">
<div class="box"><h2>{e(label('ui_step_trip'))}</h2>
<label>{e(label('ui_field_purpose'))} <input name="purpose" size="40" placeholder="{e(label('ui_purpose_placeholder'))}" required></label>
<label>{e(label('ui_field_destination'))} <input name="destination" placeholder="{e(label('ui_destination_placeholder'))}" required></label>
<div class="date-row">
<label>{e(label('ui_field_start_date'))} <input type="date" name="start_date" id="start-date" required></label>
<label>{e(label('ui_field_end_date'))} <input type="date" name="end_date" id="end-date" required></label>
</div></div>
<div class="box"><h2>{e(label('ui_step_drop'))}</h2>
<label id="drop" class="drop">{e(label('ui_drop_hint').format(max=MAX_FILES))}
<input id="files" class="visually-hidden" type="file" name="files" multiple accept=".pdf,.png,.jpg,.jpeg"></label>
<p><button type="button" id="pick-files">{e(label('ui_pick_files'))}</button></p>
<ul id="picked" class="picked"></ul>
<p class="note">{e(label('ui_extraction_note'))}</p>
<button type="submit" id="read-files">{e(label('ui_read_files'))}</button></div></form>{DROP_JS}{BUSY_READ_JS}{DATE_MIN_JS}"""
    return core.page(label("ui_new_claim"), body)


@router.post("/employee/drafts")
async def create_draft(request: Request, purpose: str = Form(...), destination: str = Form(...),
                       start_date: str = Form(...), end_date: str = Form(...), files: list[UploadFile] = File([])):
    emp = core.require_employee(request)
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
        return f"<span class='tag warn' title='{e(src)}'>{e(label(src))}</span>"
    if src in EXTRACTED:
        cls = "low" if f["confidence"] < LOW_CONFIDENCE else "ok"
        return f"<span class='tag {cls}' title='{e(src)}'>{e(label('extracted'))} ({e(label(src))} {f['confidence']:.2f})</span>"
    return ""


def _err(f: dict) -> str:
    """One fixed Indonesian sentence for any extraction error; the raw error stays in `title` only."""
    if not f["error"]:
        return ""
    return (f'<div class="err" title="{e(f["error"])}">'
            f'{e(label("ui_could_not_read"))} {e(label("ui_fill_manual"))}</div>')


CAT_FIELD = {"flight_no": "flight", "nights": "hotel"}


def _input(card: dict, k: str, allowed_cats: list[str], cat) -> str:
    f = card["fields"][k]
    v = "" if f["value"] is None else str(f["value"])
    low = " class='low'" if f["source"] in EXTRACTED and f["confidence"] < LOW_CONFIDENCE and v else ""
    name = f"c_{card['id']}_{k}"
    extra = CAT_FIELD.get(k)
    applies = extra is None or cat == extra
    off = "" if applies else " disabled"
    if k == "category":
        ctl = f"<select name='{name}' data-role='category'{low}>{core.options([('', label('ui_choose'))] + [(c, label(c)) for c in allowed_cats], v)}</select>"
    elif k == "date":
        ctl = f"<input type='date' name='{name}' value='{e(v)}'{low}>"
    elif k == "amount":
        shown = f"{int(v):,}".replace(",", ".") if v.isdigit() else v
        ctl = (f"<span class='amount-field'><span class='prefix' aria-hidden='true'>{e(label('ui_currency_prefix'))}</span>"
               f"<input name='{name}' value='{e(shown)}' size=12 inputmode='numeric' "
               f"placeholder='{e(label('ui_amount_placeholder'))}'{low}></span>")
    elif k == "nights":
        ctl = f"<input name='{name}' value='{e(v)}' size=3{low}{off}>"
    else:
        ctl = f"<input name='{name}' value='{e(v)}' size=22{low}{off}>"
    attrs = f" class='cat-field' data-cat='{extra}'" if extra else ""  # only category-only fields toggle
    hidden = "" if applies else " hidden"
    return f"<label{attrs}{hidden}>{e(label('ui_field_' + k))} {_badge(f)}<br>{ctl}</label>"


@router.get("/employee/drafts/{did}", response_class=HTMLResponse)
def draft_page(did: str, request: Request):
    emp = auth.current_employee(request)
    if not emp:
        return core.page(label("ui_draft"), auth.sign_in_notice())
    d = get_draft(did, emp)
    h = d["header"]
    allowed = [c for c in core.store.policy["allowed_categories"] if c not in ("late_checkout",)]
    others = lambda cid: [(c["id"], f"card {n + 1}") for n, c in enumerate(d["cards"]) if c["id"] != cid]  # noqa: E731
    cards_html = ""
    for n, c in enumerate(d["cards"]):
        cat = c["fields"]["category"]["value"]
        keys = ["category", "date", "amount", "merchant", "flight_no", "nights"]
        files = ""
        moves = ""
        for f in c["files"]:
            url = f"/employee/drafts/{e(did)}/files/{e(f['id'])}"
            ext = Path(f["name"]).suffix.lower()
            if ext in (".png", ".jpg", ".jpeg"):
                preview = (f"<a class='file-preview' href='{url}' target='_blank' rel='noopener'>"
                           f"<img class='thumb' loading='lazy' src='{url}' alt='Pratinjau {e(f['name'])}'></a>")
            elif ext == ".pdf":
                preview = (f"<a class='file-preview' href='{url}' target='_blank' rel='noopener'>"
                           f"<span class='doc-icon' aria-hidden='true'>PDF</span></a>")
            else:
                preview = ""
            files += (f"<li class='file-row'>{preview}{e(f['name'])} <select name='f_{f['id']}_doc_type' aria-label='Jenis dokumen untuk {e(f['name'])}'>{core.options([(t, label(t)) for t in DOC_TYPES], f['doc_type'])}</select>"
                      f" {_badge({'source': f['doc_type_source'], 'confidence': f['extract']['fields']['doc_type']['confidence']})}"
                      f"{_err(f)}</li>")
            if len(d["cards"]) > 1:
                moves += (f"<div class='manage-row'><span class=note>{e(f['name'])}</span> "
                          f"<select name='move_{f['id']}' aria-label='Pindahkan berkas ke kartu lain'>{core.options([('', label('ui_move_to'))] + others(c['id']))}</select>"
                          f"<button name=action value='move:{f['id']}'>{e(label('ui_move'))}</button></div>")
        merge = (f"<div class='manage-row'><select name='merge_{c['id']}' aria-label='Gabungkan kartu ke kartu lain'>{core.options([('', label('ui_merge_into'))] + others(c['id']))}</select>"
                 f"<button name=action value='merge:{c['id']}'>{e(label('ui_merge'))}</button></div>") if len(d["cards"]) > 1 else ""
        split = f"<button name=action value='split:{c['id']}'>{e(label('ui_split'))}</button>" if len(c["files"]) > 1 else ""
        manage = (f"<details class='manage'><summary>{e(label('ui_manage_card'))}</summary>"
                  f"{moves}{merge}{split}"
                  f"<button class='btn quiet-danger' name=action value='delete:{c['id']}' onclick='return confirm(&quot;Hapus item ini?&quot;)'>{e(label('ui_delete'))}</button>"
                  f"</details>")
        errs = "".join(f"<div class=err>{e(x)}</div>" for x in c["errors"])
        cards_html += (f"<div class='card' id='card-{e(c['id'])}' data-card='{e(c['id'])}' tabindex='-1'><b>{e(label('ui_card'))} {n + 1}</b>{errs}{''.join(_input(c, k, allowed, cat) for k in keys)}"
                       f"<div class=note>{e(label('ui_col_files'))}</div><ul>{files or '<li class=note>' + e(label('ui_no_file_yet')) + '</li>'}</ul>"
                       f"{manage}</div>")
    errs = "".join(f"<p class=err>{e(x)}</p>" for x in d["errors"])
    steps = [label("ui_step_trip"), label("ui_step_upload"), label("ui_step_verify")]
    body = f"""<p>{e(h['purpose'])} &middot; {e(h['destination'])} {core.date_html(h['start_date'])} &rarr; {core.date_html(h['end_date'])}</p>
{ui.stepper(steps, 2)}
<p class="note">{e(label('ui_check_note').format(flag=label('self_declared')))}</p>{errs}
{_summary(d)}
<form method="post" action="/employee/drafts/{e(did)}" id="draft-form"><div class="cards" id="draft-cards" tabindex="-1">{cards_html}</div>
<p class="draft-add">{ui.button(label("ui_add_item"), variant="secondary", name="action", value="add")}</p>
{_submit_bar(d)}</form>
<div class="draft-spacer" aria-hidden="true"></div>{BUSY_SUBMIT_JS}{FOCUS_JS}{CATEGORY_JS}"""
    return core.page(label("ui_check_title"), body)


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


def _card_errors(c: dict, h: dict) -> list[str]:
    """Validation errors for one card, without mutating it (the banner and _validate share this)."""
    allowed = core.store.policy["allowed_categories"]
    f = {k: v["value"] for k, v in c["fields"].items()}
    errors = []
    if f["category"] not in allowed:
        errors.append(label("ui_err_category"))
    if not f["date"] or not (h["start_date"] <= str(f["date"]) <= h["end_date"]):
        errors.append(label("ui_err_date"))
    if not isinstance(f["amount"], int) or f["amount"] <= 0:
        errors.append(label("ui_err_amount"))
    if f["category"] == "flight" and not f["flight_no"]:
        errors.append(label("ui_err_flight_no"))
    if f["category"] == "hotel" and not str(f["nights"] or "").isdigit():
        errors.append(label("ui_err_nights"))
    return errors


def _validate(d: dict) -> bool:
    h, ok = d["header"], True
    for c in d["cards"]:
        c["errors"] = _card_errors(c, h)
        ok = ok and not c["errors"]
    if not d["cards"]:
        d["errors"] = [label("ui_err_no_item")]
        return False
    d["errors"] = []
    return ok


def _summary(d: dict) -> str:
    """Banner above the cards: unreadable files and incomplete card numbers (linked to their anchor)."""
    files = [f for c in d["cards"] for f in c["files"]]
    unreadable = [f for f in files if f["error"]]
    incomplete = [n for n, c in enumerate(d["cards"], 1) if _card_errors(c, d["header"])]
    if not unreadable and not incomplete:
        return ""
    parts = []
    if unreadable:
        parts.append(e(label("ui_summary_unreadable").format(bad=len(unreadable), total=len(files))))
    if incomplete:
        links = ", ".join(f"<a href='#card-{e(d['cards'][n - 1]['id'])}'>{n}</a>" for n in incomplete)
        parts.append(e(label("ui_summary_incomplete")).replace("{cards}", links))
    return ui.alert_html(" ".join(parts), tone="warn")


def _draft_total(d: dict) -> Decimal:
    """Sum of the card amounts that parse as whole Rupiah; unreadable cards contribute nothing."""
    return sum((Decimal(c["fields"]["amount"]["value"]) for c in d["cards"]
                if isinstance(c["fields"]["amount"]["value"], int) and c["fields"]["amount"]["value"] > 0),
               Decimal("0"))


def _submit_bar(d: dict) -> str:
    """The sticky bar: item count, claimed total and incomplete-card count, then save/submit."""
    incomplete = len([c for c in d["cards"] if _card_errors(c, d["header"])])
    summary = (f'{e(label("ui_bar_items").format(n=len(d["cards"])))} &middot; '
               f'{e(label("ui_bar_total").format(total=rupiah(_draft_total(d))))} &middot; '
               f'{e(label("ui_bar_incomplete").format(n=incomplete))}')
    return (f'<div class="draft-bar" id="draft-bar">'
            f'<p class="bar-summary">{summary}</p>'
            f'{ui.button(label("ui_save_changes"), variant="secondary", name="action", value="save")}'
            f'{ui.button(label("ui_submit_claim"), variant="primary", name="action", value="submit", id="submit-claim")}'
            f'</div>')


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
    d = get_draft(did, core.require_employee(request))
    form = await request.form()
    _apply_edits(d, form)
    action = str(form.get("action", "save"))
    verb, _, arg = action.partition(":")
    cards = d["cards"]
    find = lambda cid: next((c for c in cards if c["id"] == cid), None)  # noqa: E731
    anchor = ""
    if verb == "delete" and find(arg):
        cards.remove(find(arg))
        anchor = "draft-cards"
    elif verb == "split" and (c := find(arg)) and len(c["files"]) > 1:
        i = cards.index(c)
        cards[i:i + 1] = [_card_from_files([f]) for f in c["files"]]
        anchor = f"card-{cards[i]['id']}"
    elif verb == "merge" and (c := find(arg)) and (t := find(str(form.get(f"merge_{arg}", "")))):
        t["files"] += c["files"]
        for k, v in c["fields"].items():
            if t["fields"][k]["value"] in (None, ""):
                t["fields"][k] = v
        cards.remove(c)
        anchor = f"card-{t['id']}"
    elif verb == "move":
        src = next((c for c in cards if any(f["id"] == arg for f in c["files"])), None)
        t = find(str(form.get(f"move_{arg}", "")))
        if src and t and t is not src:
            f = next(f for f in src["files"] if f["id"] == arg)
            src["files"].remove(f)
            t["files"].append(f)
            anchor = f"card-{src['id']}"
    elif verb == "add":
        card = _card_from_files([])
        cards.append(card)
        anchor = f"card-{card['id']}"
    elif verb == "submit":
        if _validate(d):
            cid = _submit(d)
            return RedirectResponse(f"/employee/claims/{cid}?msg=Submitted", status_code=303)
    if verb == "save":
        _validate(d)
        first = next((c["id"] for c in cards if c["errors"]), "")
        anchor = f"card-{first}" if first else ""
    target = f"/employee/drafts/{did}" + (f"#{anchor}" if anchor else "")
    return RedirectResponse(target, status_code=303)


@router.get("/employee/drafts/{did}/files/{fid}")
def draft_file(did: str, fid: str, request: Request):
    """Serve one uploaded draft file to its owner. The path comes only from the stored name."""
    emp = core.require_employee(request)
    d = get_draft(did, emp)
    f = next((x for c in d["cards"] for x in c["files"] if x["id"] == fid), None)
    if not f:
        raise HTTPException(404, f"Unknown file {fid}")
    root = core.UPLOAD_DIR.resolve()
    path = (root / f["stored"]).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(404, f"Unknown file {fid}")
    media = FILE_TYPES.get(path.suffix.lower())
    if not media:
        raise HTTPException(404, f"Unknown file {fid}")
    name = str(f["name"]).replace('"', "")
    return FileResponse(path, media_type=media, headers={
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{name}"',
        "Cache-Control": "private, no-store",
    })


AWAITING = label("ui_state_awaiting")
REVIEWING = UNDER_REVIEW["note_id"].rstrip(".")


@dataclass(frozen=True)
class ItemState:
    """What the employee sees for one claim item. `key` is rejected | review | wait | action | done."""
    key: str
    tone: str
    text: str  # the whole tag text, icon included
    masked: bool  # sensitive finding or B-only gap: nothing more specific than REVIEWING, no mailbox files
    pending: bool  # an uploaded file still awaits a reviewer


def item_state(res, pending: dict) -> ItemState:
    """The one place that decides the employee-facing state of an item (claim page and portal).

    `res` is the item's ItemResult, `pending` is core.pending_items(cid). Only document gaps are
    "action": uploading a file is the only thing the employee can do. A masked item never gets more
    specific than REVIEWING. A rejected item shows only "Ditolak", never a gap, and stays masked for files
    (a reviewer's rejection is itself a sensitive finding in the Q&A masking)."""
    gaps = [str(g) for g in res.gaps]
    masked = is_sensitive_item(to_jsonable(res)) or any(g in B_ONLY for g in gaps)
    waiting = res.item_id in pending
    if res.status.value == "rejected":
        return ItemState("rejected", "bad", "✕ " + label("rejected"), masked, waiting)
    if masked:
        return ItemState("review", "info", "◷ " + REVIEWING, True, waiting)
    if waiting:
        return ItemState("wait", "info", "◷ " + AWAITING, False, True)
    docs = [g for g in gaps if g in core.DOCUMENT_GAPS]
    if docs:
        names = ", ".join(dict.fromkeys(label(g) for g in docs))
        return ItemState("action", "warn", "! " + label("ui_needs_action") + ": " + names, False, False)
    if gaps:
        names = ", ".join(dict.fromkeys(label(g) for g in gaps))
        return ItemState("wait", "info", "◷ " + label("ui_state_decision") + ": " + names, False, False)
    return ItemState("done", "ok", "✓ " + label("ui_state_complete"), False, False)


UPLOAD_JS = """<script>(function(){
document.querySelectorAll('form.upload input[type=file]').forEach(function(i){
var n=i.closest('form').querySelector('.file-name');if(!n)return;
i.addEventListener('change',function(){n.textContent=i.files.length?i.files[0].name:n.dataset.empty;});
});
})();</script>"""


def upload_control(cid: str, iid: str, primary: bool) -> str:
    """The upload form of one item. The real file input is visually hidden inside a button-styled label;
    the chosen file name is filled in by UPLOAD_JS when scripts run, and the form works without it."""
    empty = label("ui_no_file_chosen")
    return (f"<form class='upload' method=post action='/employee/claims/{e(cid)}/items/{e(iid)}/files' "
            f"enctype='multipart/form-data'>"
            f"<label class='btn file-pick'>{e(label('ui_pick_file'))}"
            f"<input class='visually-hidden' type=file name=file accept='.pdf,.png,.jpg,.jpeg' required "
            f"aria-label='{e(label('ui_file_for_item').format(item=iid))}'></label>"
            f"<span class='note file-name' data-empty='{e(empty)}'>{e(empty)}</span>"
            f"{ui.button(label('ui_upload'), variant='primary' if primary else 'secondary')}</form>")


def item_files(emp: str, cid: str, iid: str, res, state: ItemState, pending: dict) -> str:
    """The files cell: web uploads for the item, plus the attachments of the mailbox messages the engine
    used as its evidence. Only messages the signed-in employee owns, and never mailbox files for a masked
    item (its uploads stay, in the neutral tone). Chip tone: ok = used by the engine, warn = awaiting a
    reviewer, neutral = anything else."""
    origin = core.review(cid).get("origin", {})
    used, waiting = set(res.evidence_used), set(pending.get(iid, []))
    rank = {"ok": 0, "warn": 1, "": 2}
    found: dict[str, str] = {}

    def add(name: str, tone: str) -> None:
        if name not in found or rank[tone] < rank[found[name]]:
            found[name] = tone

    for m in core.store.inbox:
        if m.get("owner_id") != emp:
            continue
        atts = m.get("attachments", [])
        uploaded = origin.get(m["id"]) == "employee_upload"
        if uploaded:
            for a in atts:
                if a.get("linked_claim") == cid and a.get("linked_item") == iid:
                    tone = "" if state.masked else "warn" if m["id"] in waiting else "ok" if m["id"] in used else ""
                    add(a["name"], tone)
        elif m["id"] in used and not state.masked:
            for a in atts:
                add(a["name"], "ok")
    if not found:
        return f"<span class=note>{e(label('none'))}</span>"
    chips = [ui.badge(f"{n} ({label('ui_file_pending')})" if t == "warn" else n, t) for n, t in found.items()]
    return f'<div class="chips">{"".join(chips)}</div>'


def claim_summary(claim: dict, items: list, states: list[ItemState]) -> str:
    """Status, claimed total, the count per state and one next-step sentence. Claimed amounts only:
    the engine's approvable and pending totals never reach the employee."""
    count = {k: sum(1 for s in states if s.key == k) for k in ("action", "rejected", "done")}
    waiting = sum(1 for s in states if s.key in ("review", "wait"))
    total = sum((Decimal(i["amount"]) for i in items), Decimal(0))
    cells = [ui.stat(label("ui_status"), label(claim["status"])),
             ui.stat(label("ui_total_claimed"), rupiah(total)),
             ui.stat(label("ui_count_action"), count["action"], tone="warn" if count["action"] else ""),
             ui.stat(label("ui_count_waiting"), waiting),
             ui.stat(label("ui_state_complete"), count["done"])]
    if count["rejected"]:
        cells.append(ui.stat(label("rejected"), count["rejected"], tone="danger"))
    first = next((i["id"] for i, s in zip(items, states) if s.key == "action"), "")
    if first:
        step = (f"{e(label('ui_next_action'))} "
                f"<a href='#item-{e(first)}'>{e(label('ui_next_go'))}</a>")
    elif waiting:
        step = e(label("ui_next_wait"))
    elif count["done"]:
        step = e(label("ui_next_done"))
    else:
        step = ""
    return ui.stats(cells, id="claim-summary") + (f"<p class='next-step' id='next-step'>{step}</p>" if step else "")


@router.get("/employee/claims/{cid}", response_class=HTMLResponse)
def employee_claim(cid: str, request: Request = None, msg: str = ""):  # type: ignore[assignment]  # direct calls pass no request
    from . import qa as qa_web  # Employee Q&A hook; a no-op unless EMPLOYEE_QA is on
    emp = auth.current_employee(request)
    if not emp:
        return core.page(label("ui_claim"), auth.sign_in_notice())
    claim = core.owned_claim(cid, emp)
    parts = qa_web.claim_page_parts(request, cid, claim["employee_id"])
    if isinstance(parts, HTMLResponse):
        return parts
    qa_top, qa_box = parts
    chk = core.run_check(cid)
    pending = core.pending_items(cid)
    rows: list[list[str]] = []
    states = [item_state(res, pending) for res in chk.items]
    for it, res, state in zip(claim["items"], chk.items, states):
        status, cls = state.text, state.tone
        form = upload_control(cid, it["id"], primary=state.key == "action")
        add = form if state.key == "action" else ui.details(label("ui_add_file"), form)
        files_cell = item_files(emp, cid, it["id"], res, state, pending)
        desc = str(it["description"] or "").strip()
        note = "" if desc.lower() == str(it["category"]).strip().lower() else f"<div class=note>{e(desc)}</div>"
        rows.append([f"{e(label(it['category']))}{note}", core.date_html(it["date"]),
                     e(rupiah(res.claimed)), files_cell, f"<span class='tag {cls}'>{e(status)}</span>", add])
    body = f"""<p><a href="/employee">&larr; {e(label('ui_my_claims'))}</a></p>
{ui.alert(label("ui_forwarded_note").format(flash=core.flash_text(msg), mailbox=core.MAILBOX), tone="success") if msg else ''}
<p>{e(claim['purpose'])} &middot; {e(claim['trip']['destination'])} {core.date_html(claim['trip']['start_date'])} &rarr;
{core.date_html(claim['trip']['end_date'])}</p>
{claim_summary(claim, claim["items"], states)}
{ui.table([label('ui_col_item'), label('ui_col_date'), label('ui_col_claimed'), label('ui_col_files'),
           label('ui_col_evidence'), ''], rows, num=(2,), caption=label('ui_claim'))}
<p class="note">{e(label('ui_finance_note'))}</p>{UPLOAD_JS}"""
    return core.page(f"{label('ui_claim')} {cid}", qa_top + body + qa_box)


@router.post("/employee/claims/{cid}/items/{iid}/files")
async def add_file(cid: str, iid: str, request: Request, file: UploadFile = File(...)):
    claim = core.owned_claim(cid, core.require_employee(request))
    item = next((i for i in claim["items"] if i["id"] == iid), None)
    if not item:
        raise HTTPException(404, f"Unknown item {iid}")
    entry = await _save_and_extract(file, cid)
    forward_files(claim["employee_id"], cid, claim["trip"], [(item, entry)])
    return RedirectResponse(f"/employee/claims/{cid}?msg=File+added", status_code=303)
