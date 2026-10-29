"""Employee Q&A: one question in, one checked answer out.

Order of operations for every question (all in code, whatever model is plugged in):
  1. Per-session question cap.                                   -> `limit` event, fixed reply
  2. qa_request is logged (masked like every audit payload: NIK, phone, secrets).
  3. Pre-checks on the employee's text, which is a question and never an instruction:
     the injection detector (high hits) and an action-request pattern ("setujui klaim saya", "abaikan aturan",
     "ubah status") -> polite refusal + qa_refusal; a question about other people -> scope reply + qa_refusal.
  4. The model answers using only QAToolbox (read tools, session-scoped).
  5. Post-generation guards, in order: token cap, leak (another employee's name/ID or a claim ID the session
     does not own), invented Rp (reimburse_agent.money), outcome promises, missing citation. A failed guard
     replaces the whole answer with the "Saya tidak tahu" fallback; the guard name is logged.
  6. qa_answer is logged with the sources the answer used.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .. import injection
from ..audit import AuditLog, JsonlAuditLog, make_event
from ..money import invented_rp
from .policy import Rulebook
from .tools import READ_TOOLS, ClaimData, QAToolbox, Render, Session

TRUE = {"1", "true", "yes", "on"}


def enabled(env: dict | None = None) -> bool:
    return str((os.environ if env is None else env).get("EMPLOYEE_QA", "")).strip().lower() in TRUE


@dataclass(frozen=True)
class QAConfig:
    max_questions: int = 10      # per session
    max_tokens: int = 300        # per answer (passed to the model; also a hard cut, ~4 chars per token)
    model: str = ""              # cheap model name for the LLM adapter (REIMBURSE_QA_MODEL); "" = offline router
    max_tool_calls: int = 6

    @classmethod
    def from_env(cls, env: dict | None = None) -> "QAConfig":
        env = os.environ if env is None else env
        return cls(max_questions=int(env.get("REIMBURSE_QA_MAX_QUESTIONS", 10)),
                   max_tokens=int(env.get("REIMBURSE_QA_MAX_TOKENS", 300)),
                   model=env.get("REIMBURSE_QA_MODEL", ""))


# --- language -----------------------------------------------------------------------------------------
ID_WORDS = frozenset("""saya aku apa apakah yang berapa klaim kurang kenapa mengapa ditahan plafon di untuk tidak bisa sudah
belum siapa lagi bulan ini tampilkan semua abaikan setujui gimana bagaimana kapan mana harian uang dong ya kok
dan atau ke dari dengan perlu harus status kebijakan pasal tolong mohon""".split())
EN_WORDS = frozenset("""what what's whats my is the how much why who else this month show all ignore approve please for per
night claim claims missing cap policy status held hold need does do can rate allowance which""".split())


# A word in both lists (e.g. "status") says nothing about the language; counting it made Indonesian questions
# look mixed and doubled every answer with the English text.
SHARED_WORDS = ID_WORDS & EN_WORDS


def detect_lang(text: str) -> str:
    words = re.findall(r"[a-z']+", (text or "").lower())
    i = sum(w in ID_WORDS and w not in SHARED_WORDS for w in words)
    e = sum(w in EN_WORDS and w not in SHARED_WORDS for w in words)
    if i and e:
        return "mixed"
    return "en" if e > i else "id"


def say(lang: str, id_text: str, en_text: str) -> str:
    if lang == "en":
        return en_text
    if lang == "mixed":
        return f"{id_text}\n\n(EN) {en_text}"
    return id_text


# --- fixed replies ------------------------------------------------------------------------------------
def contact(rb: Rulebook) -> str:
    c = rb.finance_contact or {}
    return f"{c.get('name', 'Tim Finance')}: {c.get('email', '')}".strip()


def dont_know(lang: str, rb: Rulebook) -> str:
    return say(lang, f"Saya tidak tahu. Hal ini tidak diatur di kebijakan klaim yang saya punya. Silakan hubungi {contact(rb)}.",
               f"I don't know. The claim policy I have does not cover this. Please contact {contact(rb)}.")


def refusal(lang: str, rb: Rulebook) -> str:
    return say(lang, "Maaf, saya hanya bisa menjawab pertanyaan tentang kebijakan klaim dan klaim Anda sendiri. Saya tidak bisa "
               f"menyetujui, mengubah, atau memproses klaim, dan tidak bisa mengabaikan aturan. Keputusan akhir ada di finance ({contact(rb)}).",
               "Sorry, I can only answer questions about the claim policy and your own claims. I can't approve, change or "
               f"process claims, and I can't set the rules aside. Finance makes the final decision ({contact(rb)}).")


def scope_other(lang: str, rb: Rulebook) -> str:
    return say(lang, "Maaf, saya hanya bisa membahas klaim milik Anda sendiri, jadi saya tidak bisa memberi informasi apa pun "
               f"tentang klaim atau perjalanan karyawan lain. Untuk hal lain, hubungi {contact(rb)}.",
               "Sorry, I can only discuss your own claims, so I can't share anything about other employees' claims or trips. "
               f"For anything else, contact {contact(rb)}.")


def limit_reply(lang: str, rb: Rulebook) -> str:
    return say(lang, f"Batas pertanyaan untuk sesi ini sudah tercapai. Silakan hubungi {contact(rb)}.",
               f"You've reached the question limit for this session. Please contact {contact(rb)}.")


FINAL_SAY = ("Keputusan akhir ada di finance.", "Finance makes the final decision.")


def final_say(lang: str) -> str:
    return " / ".join(FINAL_SAY) if lang == "mixed" else say(lang, *FINAL_SAY)

# --- pre-checks -------------------------------------------------------------------------------------
ACTION_RE = re.compile(
    r"\b(?:setujui|setujuin|approve|acc(?:in)?|sahkan|cairkan|bayar(?:kan)?|transfer(?:kan)?|hapus|delete|batalkan|cancel"
    r"|tandai\b[^.?!\n]{0,30}\b(?:lunas|disetujui|beres)|mark\b[^.?!\n]{0,30}\b(?:paid|approved)"
    r"|(?:ubah|ganti|naikkan|edit|update|change|raise|increase)\b[^.?!\n]{0,20}\b(?:status|nominal|jumlah|amount|plafon|cap|klaim|claim)"
    r"|abaikan|lewati|ignore|bypass|override)\b", re.I)
OTHERS_RE = re.compile(
    r"\b(?:siapa(?:\s+saja)?\s+(?:lagi|lain|yang)|orang lain|karyawan lain|pegawai lain|rekan|teman|kolega|tim saya|bawahan"
    r"|who else|anyone else|other (?:employees?|people|staff|colleagues?)|colleagues?|someone else|my team|everyone)\b", re.I)


def precheck(question: str) -> tuple[str, list[dict]] | None:
    hits = injection.scan(question, "QA-MSG")
    if injection.is_high(hits) or ACTION_RE.search(question):
        return "action_or_injection", hits
    if OTHERS_RE.search(question):
        return "other_employees", hits
    return None


# --- guards -----------------------------------------------------------------------------------------
PROMISE_RE = re.compile(
    r"\b(?:akan|pasti|dijamin|tentu)\s+(?:di)?(?:setujui|cair|dibayar|diganti|lolos)|\bpasti\s+(?:disetujui|cair)"
    r"|\bwill\s+(?:definitely\s+|surely\s+)?be\s+(?:approved|paid|reimbursed)|\bguarantee[sd]?\b|\bfor sure\b", re.I)
ID_RE = re.compile(r"\b(?:CLM|EMP)-[A-Za-z0-9-]+\b")
CITE_RE = re.compile(r"\b(?:POL-\d+|CLM-[A-Za-z0-9-]+|IT-\d+)\b")


@dataclass
class Guarded:
    text: str
    guard: str | None = None
    detail: list = field(default_factory=list)


def apply_guards(text: str, tb: QAToolbox, others: list[str], lang: str, rb: Rulebook, max_tokens: int) -> Guarded:
    text = (text or "").strip()
    cut = max_tokens * 4
    if len(text) > cut:
        text = text[:cut].rsplit(" ", 1)[0] + " …"
    fallback = dont_know(lang, rb)
    own = tb.own_claim_ids()
    bad_ids = sorted({m for m in ID_RE.findall(text) if m != tb.session.employee_id and m not in own})
    bad_names = sorted({n for n in others if n and re.search(r"\b" + re.escape(n) + r"\b", text, re.I)})
    if bad_ids or bad_names:
        return Guarded(fallback, "leak", bad_ids + ["<name>"] * len(bad_names))
    inv = invented_rp(text, tb.outputs())
    if inv:
        return Guarded(fallback, "invented_rp", inv)
    if PROMISE_RE.search(text):
        return Guarded(fallback, "promise", [PROMISE_RE.search(text).group(0)])
    outs = " ".join(tb.outputs())
    cited = [c for c in CITE_RE.findall(text) if c in outs]
    if not cited and contact(rb) not in text:
        return Guarded(fallback, "no_citation")
    return Guarded(text)


# --- models -----------------------------------------------------------------------------------------
class QAModel(Protocol):
    def answer(self, question: str, lang: str, tools: QAToolbox, *, context_claim_id: str | None,
               max_tokens: int) -> str: ...


CLAIM_IN_TEXT = re.compile(r"\bCLM-[A-Za-z0-9-]+\b", re.I)
CHECK_RE = re.compile(r"kurang|lengkap|missing|incomplete|ditahan|tahan|status|kenapa|mengapa|why|held|hold|pending|"
                      r"diproses|cair|disetujui|diterima|approved|accepted|need|perlu|masalah|problem|issue", re.I)
LIST_RE = re.compile(r"(?:semua|daftar|list|all)\s+(?:klaim|claims?)|klaim saya apa saja|my claims", re.I)


class OfflineQAModel:
    """Deterministic, no-LLM answerer: routes by keywords, then phrases tool output with fixed templates.
    Used for the demo, offline tests and screenshots. It only ever sees QAToolbox."""

    def answer(self, question, lang, tools, *, context_claim_id=None, max_tokens=300) -> str:
        rb = tools.rulebook
        m = CLAIM_IN_TEXT.search(question)
        cid = m.group(0).upper() if m else context_claim_id
        if CHECK_RE.search(question) and not LIST_RE.search(question):
            claims = [] if cid else json.loads(tools.call("my_claims"))["claims"]
            targets = [cid] if cid else [c["claim_id"] for c in claims][:3]
            parts = [self._check(json.loads(tools.call("my_claim_check", {"claim_id": t})), lang) for t in targets]
            if not parts:
                return say(lang, f"Anda belum punya klaim. Hubungi {contact(rb)} bila perlu.",
                           f"You have no claims yet. Contact {contact(rb)} if needed.")
            if any(p is None for p in parts):
                return self._not_found(lang, rb)
            out = "\n\n".join(parts)
            if not cid and len(claims) > len(targets):
                n = len(claims) - len(targets)
                out += "\n\n" + say(lang, f"{n} klaim lain tidak ditampilkan; buka klaimnya untuk detail.",
                                    f"{n} more claim{'s' if n != 1 else ''} not shown; open a claim for details.")
            return out + "\n\n" + final_say(lang)
        if LIST_RE.search(question):
            claims = json.loads(tools.call("my_claims"))["claims"]
            rows = "\n".join(f"- {c['claim_id']}: {c['destination']} {c['start_date']}–{c['end_date']} ({c['status']})" for c in claims)
            return say(lang, f"Klaim Anda ({len(claims)}):\n{rows}", f"Your claims ({len(claims)}):\n{rows}") + \
                "\n\n" + final_say(lang)
        if m:
            c = json.loads(tools.call("my_claim", {"claim_id": cid}))
            if c.get("error"):
                return self._not_found(lang, rb)
            items = "\n".join(f"- {i['item_id']} {i['category']} {i['date']}: Rp{int(i['amount']):,}".replace(",", ".")
                              for i in c["items"])
            return say(lang, f"Klaim {c['claim_id']} ({c['destination']}, {c['start_date']}–{c['end_date']}), status {c['status']}:\n{items}",
                       f"Claim {c['claim_id']} ({c['destination']}, {c['start_date']}–{c['end_date']}), status {c['status']}:\n{items}") + \
                "\n\n" + final_say(lang)
        arts = json.loads(tools.call("search_policy", {"query": question}))["articles"]
        if not arts:
            return dont_know(lang, rb)
        top = arts[:1] if len(arts) < 2 else arts[:2]
        id_txt = "\n".join(f"Menurut Pasal {a['pasal']} ({a['article_id']}, {a['title_id']}): {a['text_id']}" for a in top)
        en_txt = "\n".join(f"Per Article {a['pasal']} ({a['article_id']}, {a['title_en']}): {a['text_en']}" for a in top)
        return say(lang, id_txt, en_txt)

    @staticmethod
    def _not_found(lang, rb):
        return say(lang, f"Saya tidak menemukan klaim itu di antara klaim Anda. Periksa nomor klaimnya, atau hubungi {contact(rb)}.",
                   f"I couldn't find that claim among your claims. Check the claim number, or contact {contact(rb)}.")

    @staticmethod
    def _check(chk: dict, lang: str) -> str | None:
        if chk.get("error"):
            return None
        open_items = [i for i in chk["items"] if i["status"] != "ok"]
        out = {}
        for lg, st, note, none in (("id", "status_id", "note_id", "Pemeriksaan otomatis tidak menemukan kekurangan."),
                                   ("en", "status_en", "note_en", "The automated check found nothing missing.")):
            head = f"{'Klaim' if lg == 'id' else 'Claim'} {chk['claim_id']}: {chk[st]}."
            lines = [f"- {i['item_id']} ({i['category']}): {i[note]}" for i in open_items]
            out[lg] = head + ("\n" + "\n".join(lines) if lines else " " + none)
        return say(lang, out["id"], out["en"])


# --- service ----------------------------------------------------------------------------------------
class QAService:
    def __init__(self, data: ClaimData, *, log: AuditLog | None = None, rulebook: Rulebook | None = None,
                 model: QAModel | None = None, config: QAConfig | None = None, render: Render | None = None,
                 employees: list[dict] | None = None):
        self.data = data
        self.log = log if log is not None else JsonlAuditLog()
        self.rulebook = rulebook or Rulebook.load()
        self.model = model or OfflineQAModel()
        self.config = config or QAConfig.from_env()
        self.render = render
        self.employees = employees or []
        self._asked: dict[str, int] = {}
        self.last_toolbox: QAToolbox | None = None

    def tool_names(self) -> tuple[str, ...]:
        return READ_TOOLS

    def remaining_questions(self, session: Session) -> int:
        """Read-only view for the web layer: questions left in this session. Never resets the counter."""
        return max(0, self.config.max_questions - self._asked.get(session.session_id, 0))

    def ask(self, session: Session, question: str, *, context_claim_id: str | None = None) -> dict:
        self.last_toolbox = None
        run = f"qa-{session.session_id}"
        actor = f"human:{session.employee_id}"
        n = self._asked.get(session.session_id, 0) + 1
        self._asked[session.session_id] = n
        ref = f"q{n}"
        question = (question or "")[:1000]
        lang = detect_lang(question)
        rb = self.rulebook
        if n > self.config.max_questions:
            self.log.append(make_event(run, "limit", ref, actor="system", payload={"what": "qa_questions_per_session",
                                                                                  "max": self.config.max_questions}))
            return self._out(limit_reply(lang, rb), lang, [], refused="limit")
        self.log.append(make_event(run, "qa_request", ref, actor=actor, payload={
            "question": question, "lang": lang, "context_claim_id": context_claim_id, "employee_id": session.employee_id}))
        pre = precheck(question)
        if pre:
            reason, hits = pre
            text = refusal(lang, rb) if reason == "action_or_injection" else scope_other(lang, rb)
            self.log.append(make_event(run, "qa_refusal", ref, actor=actor, payload={
                "reason": reason, "rules": sorted({h["rule_id"] for h in hits}),
                "masked": [h["masked"] for h in hits][:5], "reply": text}))
            return self._out(text, lang, [], refused=reason)
        tb = QAToolbox(session, self.data, rb, self.render, max_calls=self.config.max_tool_calls)
        self.last_toolbox = tb  # for tests and the debug view; never sent to the employee
        try:
            raw = self.model.answer(question, lang, tb, context_claim_id=context_claim_id,
                                    max_tokens=self.config.max_tokens)
        except Exception as exc:  # a model/provider failure must still produce a safe, logged answer
            raw, err = "", type(exc).__name__
        else:
            err = None
        others = [e["name"] for e in self.employees if e.get("id") != session.employee_id]
        g = apply_guards(raw, tb, others, lang, rb, self.config.max_tokens)
        sources = sorted({c for c in CITE_RE.findall(g.text) if c in " ".join(tb.outputs())})
        self.log.append(make_event(run, "qa_answer", ref, actor="agent", sources=sources, payload={
            "answer": g.text, "lang": lang, "guard": g.guard, "guard_detail": g.detail[:10], "model_error": err,
            "tools": [{"tool": c["tool"], "args": c["args"], "dropped_args": c["dropped_args"]} for c in tb.calls][:10]}))
        return self._out(g.text, lang, sources, guard=g.guard, tools=[c["tool"] for c in tb.calls])

    @staticmethod
    def _out(text, lang, sources, refused=None, guard=None, tools=()) -> dict:
        return {"answer": text, "lang": lang, "sources": sources, "refused": refused, "guard": guard, "tools": list(tools)}


def new_session(employee_id: str) -> Session:
    return Session(employee_id=employee_id, session_id=uuid.uuid4().hex[:16])
