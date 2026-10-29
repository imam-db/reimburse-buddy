"""The claim-policy rulebook and its BM25 search (search_policy).

Default: the synthetic rulebook next to this file. REIMBURSE_QA_RULEBOOK points at another JSON file with
the same shape (title, version, finance_contact, articles[{id, number, title_id, title_en, text_id, text_en}]).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..retrieval import bm25, tokens

DEFAULT_PATH = Path(__file__).with_name("rulebook.json")
# Question words that carry no policy meaning; without them an off-policy question scores 0. "Plafon", "cap",
# "limit" and "maksimal" are generic too: the topic is what the sentence is about ("hotel", "taksi"), not the
# word for a ceiling.
QUERY_STOP = frozenset("""berapa apa apakah yang di ke dari untuk dan atau saya aku kami kita anda kamu ini itu ada adalah
bisa boleh dong ya kah nya gimana bagaimana kapan mana mau ingin tolong mohon jika kalau dengan per
plafon cap limit maksimal
what how much many is are the a an of for to in on my me i can could do does please tell about""".split())
# A body mention is not an answer: "taksi" appears in the category list (POL-2) and the receipt rule (POL-6),
# but no article is about taxis. An article is a candidate only when a remaining query word is in its title,
# and its BM25 score must clear this floor. The floor is low enough for a tiny custom rulebook (one article)
# and still drops incidental matches on long articles.
MIN_SCORE = 0.4


@dataclass(frozen=True)
class Rulebook:
    title: str
    version: str
    finance_contact: dict
    articles: tuple[dict, ...]
    path: str

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Rulebook":
        p = Path(path or os.environ.get("REIMBURSE_QA_RULEBOOK") or DEFAULT_PATH)
        d = json.loads(p.read_text(encoding="utf-8"))
        arts = tuple(d["articles"])
        for a in arts:
            missing = {"id", "number", "title_id", "title_en", "text_id", "text_en"} - set(a)
            if missing:
                raise ValueError(f"rulebook article {a.get('id')!r} lacks {sorted(missing)}")
        return cls(d.get("title", ""), d.get("version", ""), d.get("finance_contact", {}), arts, str(p))

    def search(self, query: str, k: int = 2) -> list[dict]:
        words = [w for w in re.findall(r"[\w-]+", (query or "").lower()) if w not in QUERY_STOP]
        if not words:
            return []
        docs = [f"{a['title_id']} {a['title_en']} {a['text_id']} {a['text_en']}" for a in self.articles]
        scores = bm25(" ".join(words), docs)
        wanted = set(words)
        ranked = sorted(((s, i) for i, (s, a) in enumerate(zip(scores, self.articles))
                         if s >= MIN_SCORE and wanted & set(tokens(f"{a['title_id']} {a['title_en']}"))),
                        reverse=True)[:k]
        return [{**self.articles[i], "score": round(s, 3)} for s, i in ranked]
