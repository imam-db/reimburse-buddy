"""Optional Jev judge (escalation only). Off by default: set RB_JUDGE=jev and LLM_GATEWAY_API_KEY.

The judge may only ADD review flags. It never closes a gap, never picks evidence and never changes an amount;
the rule engine and the deterministic guards decide everything else. Each request carries ONE candidate
message and the fields a person needs for the question (see judge_questions.py for wording and thresholds).
Answers are cached per (message hash, question, context hash). A call that fails or times out (3 s) is
recorded as an error and is never read as "no".
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date as _date
from typing import Protocol

from . import judge_questions as Q

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class Answer:
    question: str
    p: float | None  # probability of yes; None = abstained or failed
    error: str | None = None
    cached: bool = False
    cost: float = 0.0


class Judge(Protocol):
    name: str

    async def ask(self, message: dict, question: str, context: dict) -> Answer: ...


class NullJudge:
    """The default: abstains, adds nothing."""
    name = "null"

    async def ask(self, message: dict, question: str, context: dict) -> Answer:
        return Answer(question, None)


def message_state(message: dict) -> dict:
    """Only what a person would read. The weekday is computed in code (Jev reads dates as text)."""
    d = message.get("date") or ""
    try:
        day = DAYS[_date.fromisoformat(d[:10]).weekday()]
    except ValueError:
        day = None
    return {k: v for k, v in {"from": message.get("from"), "sent_on": f"{day}, {d}" if day else d,
                              "subject": message.get("subject"), "text": message.get("body") or message.get("snippet"),
                              "attachments": message.get("attachments")}.items() if v}


def cache_key(message: dict, question: str, context: dict) -> str:
    h = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return f"{h(message_state(message))}:{question}:{h(context)}"


class JevJudge:
    name = "jev"

    def __init__(self, api_key: str | None = None, url: str = Q.URL, model: str = Q.MODEL, timeout: float = Q.TIMEOUT_S,
                 transport=None):
        self._key = api_key or os.environ.get(Q.KEY_ENV)
        self.url, self.model, self.timeout, self._transport = url, model, timeout, transport
        self.cache: dict[str, Answer] = {}
        self.spent = 0.0

    def payload(self, message: dict, question: str, context: dict) -> dict:
        return {"model": self.model, "state": {"message": message_state(message), **context},
                "questions": {question: Q.QUESTIONS[question]}}

    async def ask(self, message: dict, question: str, context: dict) -> Answer:
        key = cache_key(message, question, context)
        if key in self.cache:
            a = self.cache[key]
            return Answer(a.question, a.p, a.error, True, 0.0)
        if not self._key:
            return Answer(question, None, f"{Q.KEY_ENV} not set")
        import httpx
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as c:
                r = await c.post(self.url, json=self.payload(message, question, context),
                                 headers={"Authorization": f"Bearer {self._key}"})
                r.raise_for_status()
                body = r.json()
            p = float(body["answers"][question]["noul"])
            cost = float((body.get("usage") or {}).get("cost") or 0.0)
        except Exception as e:  # noqa: BLE001 — never read a failure as "no"
            return Answer(question, None, f"{type(e).__name__}: {str(e)[:120]}")
        self.spent += cost
        a = Answer(question, p, None, False, cost)
        self.cache[key] = a
        return a


def get_judge() -> Judge:
    return JevJudge() if os.environ.get(Q.ENABLE_ENV) == "jev" else NullJudge()


def flags_for(answers: list[Answer]) -> list[dict]:
    """Truth table in code: yes or unsettled on any risk question -> a review flag. Escalation only."""
    out = []
    for a in answers:
        if a.p is None:
            if a.error and Q.FLAG_ON_ERROR:
                out.append({"question": a.question, "why": f"judge unavailable: {a.error}"})
            continue
        if a.p >= Q.YES:
            out.append({"question": a.question, "p": a.p, "why": "yes"})
        elif a.p > Q.NO:
            out.append({"question": a.question, "p": a.p, "why": "unsettled"})
    return out


async def review_flags(judge: Judge, message: dict, category: str, context: dict) -> list[dict]:
    """Ask every risk question for this item category about ONE message; return the review flags."""
    answers = [await judge.ask(message, q, context) for q in Q.BY_CATEGORY.get(category, [])]
    return flags_for(answers)
