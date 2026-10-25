"""Version B only: query cleanup for the lexical mailbox search, and find_messages, a ranked search over the
same mailbox (BM25 + a small local multilingual embedding model, fused with reciprocal rank fusion).

The server's search_reimburse_mailbox is a substring OR search capped at 5 hits; src/ is not changed. So
find_messages builds its candidate pool through that same tool: one probe per single character a-z0-9
(every message contains some of them; each probe returns up to 5), deduplicated by message ID. The fixture
mailboxes hold at most 10 messages per employee and the sweep recovers all of them (tests/test_retrieval.py);
a much larger mailbox could lose recall, which the result reports as pool_size.
If the embedding model cannot be loaded (no network, no fastembed), ranking is BM25 only and the result
says so in "ranker"."""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
PROBES = "abcdefghijklmnopqrstuvwxyz0123456789"
TOP_K = 5  # same as the lexical search, so one find_messages call reads as much as one search
RRF_K = 60
STOP = {"the", "a", "an", "for", "of", "to", "and", "or", "in", "on", "at", "my", "is", "please", "send",
        "mohon", "kirim", "yang", "dan", "di", "ke", "untuk", "dari", "dengan"}

ID_RE = re.compile(r"\b(?:CLM|IT|MSG|EVT|MEMO|EMP)-[\w-]+\b", re.I)
TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}(?:\s*(?:am|pm|wib|wita|wit))?\b", re.I)
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def clean_query(query: str) -> str:
    """Strip matcher noise before a substring search: IDs are compacted to one token (IT-3 -> it3, so the
    fragment 'it' no longer matches 'visit' or 'with' and '3' no longer matches every digit), ISO dates
    likewise (2026-10-14 -> 20261014), and clock times are dropped ('15:00' -> nothing; its '00' matched
    every amount)."""
    q = TIME_RE.sub(" ", str(query))
    q = ID_RE.sub(lambda m: re.sub(r"[^a-z0-9]", "", m.group(0).lower()), q)
    q = DATE_RE.sub(lambda m: m.group(0).replace("-", ""), q)
    return " ".join(q.split())


def tokens(text: str) -> list[str]:
    text = ID_RE.sub(lambda m: " " + re.sub(r"[^a-z0-9]", "", m.group(0).lower()) + " ", str(text))
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP and len(t) > 1]


def bm25(query: str, docs: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    toks = [tokens(d) for d in docs]
    n = len(docs) or 1
    avg = sum(map(len, toks)) / n or 1
    df = Counter(t for d in toks for t in set(d))
    out = []
    for d in toks:
        tf = Counter(d)
        s = 0.0
        for t in set(tokens(query)):
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
        out.append(s)
    return out


_EMBEDDER = None
_EMBED_ERR: str | None = None


def embedder():
    """The local embedding model, or None (reason in _EMBED_ERR). Loaded once; disable with RB_NO_EMBED=1."""
    global _EMBEDDER, _EMBED_ERR
    if _EMBEDDER is None and _EMBED_ERR is None:
        if os.environ.get("RB_NO_EMBED"):
            _EMBED_ERR = "disabled by RB_NO_EMBED"
        else:
            try:
                from fastembed import TextEmbedding
                cache = os.environ.get("FASTEMBED_CACHE_PATH", os.path.expanduser("~/.cache/fastembed"))
                _EMBEDDER = TextEmbedding(EMBED_MODEL, cache_dir=cache)
            except Exception as e:  # no package, no network, bad download
                _EMBED_ERR = f"{type(e).__name__}: {e}"[:200]
                log.warning("find_messages: embedding model unavailable, BM25 only (%s)", _EMBED_ERR)
    return _EMBEDDER


def cosine_scores(query: str, docs: list[str]) -> list[float] | None:
    m = embedder()
    if m is None or not docs:
        return None
    import numpy as np
    v = np.array(list(m.embed([query, *docs])))
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    return [float(x) for x in v[1:] @ v[0]]


def rrf(*scores: list[float]) -> list[float]:
    fused = [0.0] * len(scores[0])
    for s in scores:
        order = sorted(range(len(s)), key=lambda i: -s[i])
        for rank, i in enumerate(order):
            fused[i] += 1 / (RRF_K + rank + 1)
    return fused


def doc_text(m: dict) -> str:
    return " ".join([m.get("subject", ""), m.get("snippet", ""), m.get("from", ""), *m.get("attachments", [])])


async def message_pool(search: Callable[[dict], Awaitable[list[dict]]], employee_id: str) -> list[dict]:
    pool: dict[str, dict] = {}
    for ch in PROBES:
        for r in await search({"employee_id": employee_id, "query": ch}):
            if isinstance(r, dict) and r.get("id"):
                pool.setdefault(r["id"], r)
    return list(pool.values())


async def find_messages(search, employee_id: str, query: str, date_from: str | None = None,
                        date_to: str | None = None, k: int = TOP_K) -> dict:
    """Top-k messages for query among the employee's messages dated in [date_from, date_to]."""
    pool = await message_pool(search, employee_id)
    cand = [m for m in pool if (not date_from or m.get("date", "") >= date_from)
            and (not date_to or m.get("date", "") <= date_to)]
    docs = [doc_text(m) for m in cand]
    lex = bm25(query, docs)
    sem = cosine_scores(query, docs)
    if sem is None:
        score, ranker = lex, f"bm25 only (embedding model unavailable: {_EMBED_ERR})"
        keep = [i for i in range(len(cand)) if lex[i] > 0]
    else:
        score, ranker = rrf(lex, sem), f"bm25+embedding rrf ({EMBED_MODEL.split('/')[-1]})"
        keep = list(range(len(cand)))
    order = sorted(keep, key=lambda i: (-score[i], cand[i].get("date", "")), reverse=False)[:k]
    results = [{kk: cand[i].get(kk) for kk in ("id", "date", "from", "subject", "snippet", "attachments")}
               for i in order]
    return {"results": results, "ranker": ranker, "pool_size": len(pool), "in_window": len(cand)}
