"""Plan schema, the deterministic request reader, who/when resolution and staging validation.

A plan is a small dict (JSON-serialisable, stored for the confirmation page):

    {"run_id", "plan_id", "version", "request", "requester",
     "actions": [{"action_id", "where": "email"|"calendar"|"task", "tool", "args", "args_hash",
                  "who": [EMP-ID], "what": {"type", "subject"|"title", "body"},
                  "when": {"start", "duration_min", "local": {EMP-ID: "Fri 30 Oct 2026 13:00 WIB"}} | None,
                  "why": [source IDs], "influenced_fields": {field: echo_check result}}],
     "open_questions": [...], "assumptions": [{"id", "en"}], "notices": [...], "superseded": [action_id]}

Who is decided by code: 0 or 2+ directory matches for a name -> one question, exactly 1 -> no question.
When is proposed, never asked: a missing or partial time becomes the nearest allowed slot (operating rules,
computed from the run's frozen start time), recorded as an assumption the reviewer sees.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import injection as inj
from ..audit import args_hash
from . import rules as R
from .directory import HONORIFICS, Directory

WHERE_TOOL = {"email": "send_email", "calendar": "create_calendar_event", "task": "create_task"}
MAX_PARTICIPANTS = 3
DEFAULT_MEETING_MIN = 60
DIRECTORY_DOMAINS = frozenset({"nusantara-niaga.example"})

DAYS = {"senin": 0, "monday": 0, "selasa": 1, "tuesday": 1, "rabu": 2, "wednesday": 2, "kamis": 3,
        "thursday": 3, "jumat": 4, "jum'at": 4, "friday": 4, "sabtu": 5, "saturday": 5, "minggu": 6, "sunday": 6}
PARTS = {"pagi": 9, "morning": 9, "siang": 12, "midday": 12, "noon": 12, "lunch": 12, "sore": 15,
         "afternoon": 15}
DAY_RE = re.compile(r"\b(" + "|".join(re.escape(d) for d in DAYS) + r")\b", re.I)
PART_RE = re.compile(r"\b(" + "|".join(PARTS) + r")\b", re.I)
CLOCK_RE = re.compile(r"\b(?:jam|pukul|at)\s*(\d{1,2})(?:[.:](\d{2}))?\b|\b(\d{1,2})[.:](\d{2})\b", re.I)
REL_RE = re.compile(r"\b(besok|tomorrow|lusa)\b", re.I)
# "ke Pak Budi", "dengan Bu Sari", "with Budi Santoso", "untuk EMP-202"
PERSON_RE = re.compile(r"\b(?:ke|kepada|dengan|sama|untuk|to|with|for)\s+((?:EMP-\d{3,})|(?:(?:pak|bu|ibu|bapak|mas|mbak)\s+)?"
                       r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)")
NOT_NAMES = {"Jumat", "Senin", "Selasa", "Rabu", "Kamis", "Friday", "Monday", "Tuesday", "Wednesday", "Thursday",
             "Finance", "Semua", "All"}
# A request aimed at a whole group is never resolved to "everyone": it becomes one question that must be
# answered with an explicit directory ID, exactly like an ambiguous name.
BROAD_TARGET = "__broad_audience__"
BROAD_RE = re.compile(r"\b(?:all (?:staff|employees|users|contacts|managers)|everyone|every employee"
                      r"|semua (?:karyawan|staf|staff|pegawai|orang|kontak|manajer)"
                      r"|seluruh (?:karyawan|staf|staff|pegawai|orang))\b", re.I)


# --- reading the request (deterministic default; an LLM proposer can replace it) -----------------------
def parse_when(text: str) -> dict | None:
    d, p, c, rel = DAY_RE.search(text), PART_RE.search(text), CLOCK_RE.search(text), REL_RE.search(text)
    if not (d or p or c or rel):
        return None
    hint: dict = {"text": " ".join(m.group(0) for m in (rel, d, p, c) if m)}
    if d:
        hint["weekday"] = DAYS[d.group(1).lower()]
    if rel:
        hint["days_ahead"] = 2 if rel.group(1).lower() == "lusa" else 1
    if c:
        h, m = (c.group(1), c.group(2)) if c.group(1) else (c.group(3), c.group(4))
        hint["hour"], hint["minute"] = int(h), int(m or 0)
    elif p:
        hint["hour"], hint["minute"] = PARTS[p.group(1).lower()], 0
    return hint


def parse_names(text: str) -> list[str]:
    out = []
    for m in PERSON_RE.finditer(text):
        name = m.group(1).strip()
        last = name.split()[-1]
        if last in NOT_NAMES:
            continue
        if name not in out:
            out.append(name)
    return out


def clauses(text: str) -> list[str]:
    parts = re.split(r"(?:,\s*)?\b(?:terus|lalu|kemudian|dan juga|then|and then|also)\b|[;.]\s+", text, flags=re.I)
    return [p.strip(" ,—-\"'“”") for p in parts if p and p.strip(" ,—-\"'“”")]


def read_request(request: str, documents: list[dict] | None = None) -> list[dict]:
    """Intents from the operator's request: [{"where", "names", "when_hint", "topic", "why"}].
    Names carry over to a later clause that names nobody ("send the minutes to Budi, then book a follow-up")."""
    doc_ids = [d["id"] for d in documents or []]
    intents, last_names = [], []
    for cl in clauses(request):
        low = cl.lower()
        if re.search(r"\b(jadwal\w*|meeting|rapat|follow-?up|book|schedule|undang|invite)\b", low):
            where = "calendar"
        elif re.search(r"\b(kirim|send|email|e-mail|forward|teruskan)\b", low):
            where = "email"
        elif re.search(r"\b(task|tugas|todo|to-do|ingatkan|remind)\b", low):
            where = "task"
        else:
            continue
        names = [n for n in parse_names(cl) if not BROAD_RE.search(n)]
        if not names:
            names = [BROAD_TARGET] if BROAD_RE.search(cl) else list(last_names)
        last_names = names or last_names
        topic = ("notulen" if re.search(r"notulen|minutes", low) else
                 "follow-up" if re.search(r"follow-?up", low) else cl[:80])
        intents.append({"where": where, "names": names, "when_hint": parse_when(cl) if where != "email" else None,
                        "topic": topic, "why": list(doc_ids)})
    return intents


def revise_intent(intent: dict, note: str) -> dict:
    """Apply a reviewer's rejection note to one intent: a named person or a time in the note replaces the old one."""
    out = dict(intent)
    names = parse_names(note) or re.findall(r"\bEMP-\d{3,}\b", note)
    if names:
        out["names"] = names
    hint = parse_when(note)
    if hint and out["where"] == "calendar":
        out["when_hint"] = hint
    out["note"] = note
    return out


# --- who ------------------------------------------------------------------------------------------------
def who_question(intents: list[dict], directory: Directory, answers: dict[str, str]) -> dict | None:
    """The single question to ask next, or None. Decided by code: a name with exactly one match is never asked."""
    for it in intents:
        for name in it["names"]:
            if name in answers:
                continue
            if name == BROAD_TARGET:
                opts = sorted(directory.ids())
                listing = "; ".join(c.short() for c in (directory.get(o) for o in opts) if c)
                return {"id": "who:broad", "name": BROAD_TARGET, "options": opts,
                        "id_text": "Permintaan ini ditujukan ke banyak orang sekaligus dan tidak dikirim massal. "
                                   f"Pilih satu penerima dari direktori: {listing}.",
                        "en": "This request targets a broad audience and is never sent to everyone. "
                              f"Choose one recipient from the directory: {listing}."}
            found = directory.find(name)
            if len(found) == 1:
                continue
            shown = " ".join(w for w in name.split() if w.lower() not in HONORIFICS) or name
            if found:
                opts = [c.id for c in found]
                listing = "; ".join(c.short() for c in found)
                return {"id": f"who:{name}", "name": name, "options": opts,
                        "id_text": f"Ada {len(found)} karyawan bernama {shown}: {listing}. Yang mana yang dimaksud?",
                        "en": f"There are {len(found)} employees named {shown}: {listing}. Which one do you mean?"}
            return {"id": f"who:{name}", "name": name, "options": [],
                    "id_text": f"Tidak ada karyawan bernama {shown} di direktori. Siapa yang dimaksud (ID karyawan)?",
                    "en": f"No employee named {shown} is in the directory. Who do you mean (employee ID)?"}
    return None


def resolve_names(intents: list[dict], directory: Directory, answers: dict[str, str]) -> dict[str, str]:
    out = {}
    for it in intents:
        for name in it["names"]:
            if name in answers:
                out[name] = answers[name]
            elif name != BROAD_TARGET:
                found = directory.find(name)
                if len(found) == 1:
                    out[name] = found[0].id
    return out


def valid_answer(question: dict, answer, directory: Directory) -> str | None:
    a = str(answer or "").strip().upper()
    if question["options"]:
        return a if a in question["options"] else None
    return a if a in directory.ids() else None


# --- when -----------------------------------------------------------------------------------------------
def target_time(hint: dict | None, now: datetime, zone: str, rules: R.Rules) -> datetime | None:
    if not hint:
        return None
    loc = now.astimezone(rules.tz(zone))
    day = loc.date()
    if "days_ahead" in hint:
        day = day + timedelta(days=hint["days_ahead"])
    if "weekday" in hint:
        day = day + timedelta(days=(hint["weekday"] - day.weekday()) % 7)
    hour, minute = hint.get("hour", rules.office_start.hour), hint.get("minute", 0)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=rules.tz(zone))


@dataclass
class Slot:
    start: datetime | None
    offered: list[datetime] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)


def propose_slot(hint: dict | None, now: datetime, duration_min: int, people, requester_zone: str,
                 rules: R.Rules) -> Slot:
    """Nearest allowed slot at or after the requested time (or after `now` when no time was given)."""
    earliest = now + timedelta(minutes=30)
    target = target_time(hint, now, requester_zone, rules)
    after = max(target, earliest) if target else earliest
    offered = R.next_allowed_slots(after, duration_min, people, n=3, rules=rules)
    s = Slot(offered[0] if offered else None, offered)
    if not offered:
        return s
    got = R.fmt_local(rules, s.start, requester_zone)
    if target is None:
        s.assumptions.append({"id": f"Waktu tidak disebut; diusulkan slot terdekat yang diizinkan: {got}.",
                              "en": f"No time was given; proposing the nearest allowed slot: {got}."})
    else:
        asked = R.fmt_local(rules, target, requester_zone)
        why = R.check_slot(target, duration_min, people, rules) if target >= earliest else ["it is already past"]
        if why:
            s.assumptions.append({
                "id": f"'{hint['text']}' dibaca sebagai {asked}; waktu itu tidak diizinkan ({'; '.join(why)}). "
                      f"Diusulkan {got}.",
                "en": f"'{hint['text']}' read as {asked}; that time is not allowed ({'; '.join(why)}). "
                      f"Proposing {got}."})
        else:
            s.assumptions.append({"id": f"'{hint['text']}' dibaca sebagai {asked}.",
                                  "en": f"'{hint['text']}' read as {asked}."})
    if any(p.tz != requester_zone for p in people):
        s.assumptions.append({"id": f"Zona waktu acuan: zona peminta ({requester_zone}).",
                              "en": f"Reference time zone: the requester's ({requester_zone})."})
    if s.start.weekday() == 4 or (target and target.weekday() == 4):
        s.assumptions.append({"id": rules.prayer_assumption, "en": rules.prayer_assumption})
    return s


# --- building the plan ----------------------------------------------------------------------------------
def _clean_lines(text: str) -> list[str]:
    """Lines of clean text: any line or sentence segment carrying the model-only withheld placeholder is
    dropped, so a planned email body never includes the marker (or the withheld content)."""
    out = []
    for ln in (text or "").splitlines():
        parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", ln)]
        clean = " ".join(p for p in parts if p and not inj.contains_withheld_marker(p))
        if clean:
            out.append(clean)
    return out


def _summary(documents: list[dict]) -> str:
    lines = []
    for d in documents or []:
        text = d.get("model_text", d.get("text", ""))
        for ln in _clean_lines(text)[:6]:
            lines.append(f"- {ln}")
    return "\n".join(lines)


def build_plan(*, run_id: str, version: int, request: str, requester: str, intents: list[dict],
               resolved: dict[str, str], now: datetime, directory: Directory, rules: R.Rules,
               documents: list[dict] | None = None, notices: list[dict] | None = None,
               flagged_spans: list[str] | None = None, superseded: list[str] | None = None) -> tuple[dict, list[str]]:
    """(plan, offered start times as ISO strings: the operating-rules output time args must come from)."""
    req = directory.get(requester)
    actions, assumptions, offered_all = [], [], []
    for i, it in enumerate(intents):
        who = [resolved[n] for n in it["names"] if n in resolved]
        names = ", ".join(directory.get(w).name for w in who)
        where = it["where"]
        tool = WHERE_TOOL[where]
        aid = f"{run_id}:v{version}:a{i + 1}"
        when = None
        note = f"\n\nCatatan peninjau / reviewer note: {it['note']}" if it.get("note") else ""
        if where == "email":
            subject = "Notulen rapat" if it["topic"] == "notulen" else f"Tindak lanjut: {it['topic']}"
            body = (f"Halo {names},\n\nBerikut {subject.lower()}.\n{_summary(documents)}\n\nSalam,\n{req.name}"
                    ).replace("\n\n\n", "\n\n")
            args = {"to": who, "subject": subject, "body": body}
            what = {"type": "send_email", "subject": subject, "body": body}
        elif where == "calendar":
            people = [req] + [directory.get(w) for w in who if w != requester]
            slot = propose_slot(it.get("when_hint"), now, DEFAULT_MEETING_MIN, people, req.tz, rules)
            offered_all += [R.iso(x) for x in slot.offered]
            assumptions += [dict(a, action_id=aid) for a in slot.assumptions]
            title = "Follow-up" + (f": {it['topic']}" if it["topic"] not in ("follow-up",) else "")
            body = f"Follow-up meeting with {names}." + (" Sources: " + ", ".join(it["why"]) if it["why"] else "")
            start = R.iso(slot.start) if slot.start else None
            args = {"attendees": [p.id for p in people], "title": title, "start": start,
                    "duration_min": DEFAULT_MEETING_MIN, "body": body}
            when = {"start": start, "duration_min": DEFAULT_MEETING_MIN,
                    "local": {p.id: R.fmt_local(rules, slot.start, p.tz) for p in people} if slot.start else {},
                    "alternatives": [R.iso(x) for x in slot.offered[1:]]}
            what = {"type": tool, "title": title, "body": body}
            who = [p.id for p in people]
        else:
            title = f"Tindak lanjut: {it['topic']}"
            body = f"Task for {names}." + (" Sources: " + ", ".join(it["why"]) if it["why"] else "")
            args = {"assignee": who[0] if who else None, "title": title, "body": body}
            what = {"type": tool, "title": title, "body": body}
        if note:
            args["body"] += note
            what["body"] = args["body"]
        influenced = {}
        for k in inj.FREE_TEXT_ARGS:
            v = args.get(k)
            if isinstance(v, str) and v:
                r = inj.echo_check(v, flagged_spans or [], directory_domains=DIRECTORY_DOMAINS)
                if r["influenced"]:
                    influenced[k] = r
        actions.append({"action_id": aid, "where": where, "tool": tool, "args": args,
                        "args_hash": args_hash(tool, args), "who": who, "what": what, "when": when,
                        "why": list(it["why"]), "influenced_fields": influenced})
    plan = {"run_id": run_id, "plan_id": f"{run_id}:v{version}", "version": version, "request": request,
            "requester": requester, "actions": actions, "open_questions": [], "assumptions": assumptions,
            "notices": list(notices or []), "superseded": list(superseded or [])}
    return plan, sorted(set(offered_all))


def validate_action(action: dict, directory: Directory, rules: R.Rules, operating_times: set[str]) -> list[str]:
    tool, args = action["tool"], action["args"]
    bad = []
    if tool not in WHERE_TOOL.values() or WHERE_TOOL.get(action["where"]) != tool:
        bad.append(f"{tool}: not an allowed office tool for where={action['where']!r}")
    if args_hash(tool, args) != action["args_hash"]:
        bad.append("args changed after staging (args_hash mismatch)")
    bad += inj.arg_source_violations(tool, args, directory_ids=directory.ids(), operating_times=operating_times)
    marker = inj.withheld_marker_fields(args)
    if marker:
        bad.append("withheld instruction text from a source document in " + ", ".join(marker)
                   + "; rewrite it without the withheld marker or the withheld content")
    people = args.get("to") or args.get("attendees") or ([args["assignee"]] if args.get("assignee") else [])
    if not people:
        bad.append("no recipient resolved from the directory")
    if len(people) > MAX_PARTICIPANTS:
        bad.append(f"{len(people)} participants; at most {MAX_PARTICIPANTS} are supported")
    if tool == "create_calendar_event":
        if not args.get("start"):
            bad.append("no allowed slot found by the operating-rules tool")
        elif all(directory.get(p) for p in people):
            bad += R.check_slot(R.parse_iso(args["start"]), int(args["duration_min"]),
                                [directory.get(p) for p in people], rules)
    return bad


def validate_plan(plan: dict, directory: Directory, rules: R.Rules, operating_times: set[str]) -> list[str]:
    """Staging check, in code. A plan with open questions is never staged."""
    if plan.get("open_questions"):
        return [f"plan has {len(plan['open_questions'])} open question(s); answer them before staging"]
    if not plan.get("actions"):
        return ["plan has no actions"]
    return [f"{a['action_id']}: {b}" for a in plan["actions"]
            for b in validate_action(a, directory, rules, operating_times)]
