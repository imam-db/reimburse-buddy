"""Judge prep, offline: httpx MockTransport stands in for the API; no network, no key."""

import asyncio
import json

import httpx

from eval import judge_score as S
from reimburse_agent import judge_questions as Q
from reimburse_agent.judge import JevJudge, NullJudge, flags_for, message_state, review_flags, Answer

MSG = {"from": "Rina", "date": "2026-10-13", "subject": "geser", "body": "review mundur jam 5"}
CTX = {"meeting": {"title": "Client review", "scheduled_time": "14:00-16:00"}}


def mock(p=0.9, status=200, seen=None):
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        if seen is not None:
            seen.append((req, body))
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"answers": {q: {"type": "noul", "noul": p} for q in body["questions"]},
                                         "usage": {"input_tokens": 200, "cost": 0.00001}})
    return httpx.MockTransport(handler)


def test_payload_shape_one_message_and_weekday_in_code():
    seen = []
    j = JevJudge(api_key="k", transport=mock(seen=seen))
    a = asyncio.run(j.ask(MSG, "meeting_moved", CTX))
    req, body = seen[0]
    assert a.p == 0.9 and a.cost == 0.00001
    assert str(req.url) == Q.URL and req.headers["authorization"] == "Bearer k"
    assert body["model"] == "jev-1.13.0" and list(body["questions"]) == ["meeting_moved"]
    assert body["questions"]["meeting_moved"]["type"] == "noul"
    assert body["state"]["message"]["sent_on"] == "Tuesday, 2026-10-13" and body["state"]["meeting"] == CTX["meeting"]


def test_cache_by_message_and_question():
    seen = []
    j = JevJudge(api_key="k", transport=mock(seen=seen))
    asyncio.run(j.ask(MSG, "meeting_moved", CTX))
    again = asyncio.run(j.ask(MSG, "meeting_moved", CTX))
    asyncio.run(j.ask(MSG, "meeting_cancelled", CTX))
    assert again.cached and again.cost == 0 and len(seen) == 2


def test_failure_is_an_error_never_a_no():
    a = asyncio.run(JevJudge(api_key="k", transport=mock(status=503)).ask(MSG, "meeting_moved", CTX))
    assert a.p is None and "HTTPStatusError" in a.error
    assert asyncio.run(JevJudge(api_key=None).ask(MSG, "meeting_moved", CTX)).error  # no key, no call
    assert flags_for([a]) == []  # FLAG_ON_ERROR is off: deterministic guards stay primary


def test_escalation_only_truth_table():
    f = flags_for([Answer("a", 0.9), Answer("b", 0.5), Answer("c", 0.1)])
    assert [(x["question"], x["why"]) for x in f] == [("a", "yes"), ("b", "unsettled")]
    assert asyncio.run(review_flags(NullJudge(), MSG, "late_checkout", CTX)) == []


def test_off_by_default(monkeypatch):
    from reimburse_agent.judge import get_judge
    monkeypatch.delenv(Q.ENABLE_ENV, raising=False)
    assert get_judge().name == "null"
    monkeypatch.setenv(Q.ENABLE_ENV, "jev")
    assert get_judge().name == "jev"


def test_questions_avoid_choice_and_numbers_in_wording():
    for q in Q.QUESTIONS.values():
        assert q["type"] == "noul" and set(q["criteria"]) == {"true", "false"}
        assert not any(ch.isdigit() for ch in q["instructions"])


def test_judge_set_shape():
    items = S.load()
    assert 60 <= len(items) <= 100 and len({i["id"] for i in items}) == len(items)
    assert {i["register"] for i in items} == {"id_formal", "id_slang", "en"}
    assert all(i["question"] in Q.QUESTIONS and i["label"] in (0, 1) for i in items)
    assert sum(i["hard_negative"] for i in items) >= 15 and 0.3 < sum(i["label"] for i in items) / len(items) < 0.6


def test_metrics():
    assert S.auroc([1, 1, 0, 0], [0.9, 0.8, 0.2, 0.1]) == 1.0 and S.auroc([1, 0], [0.5, 0.5]) == 0.5
    items = [{"id": "x", "label": 1, "register": "en", "hard_negative": False, "hard_positive": False},
             {"id": "y", "label": 0, "register": "en", "hard_negative": True, "hard_positive": False}]
    m = S.metrics(items, {"x": [0.2, 0.8], "y": [0.1, None]})
    assert m["errors"] == 1 and m["miss_rate"] == 0.0 and m["false_escalation"] == 0.0
    assert m["repeat_flag_flips"] == 1 and abs(m["repeat_mean_abs_diff"] - 0.6) < 1e-9
    assert S.estimate(62, 3)["jev"] < 0.01


def test_dry_run_sends_nothing(capsys, monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert S.main([]) == 0 and "dry run" in capsys.readouterr().out


async def test_graph_hook_only_adds_flags(monkeypatch):
    """RB_JUDGE on (stubbed judge, no network): the scan asks about one candidate message per call and can only
    add hits; with the similarity/term hits disabled, the judge's flag alone sends case 14 goes to the reviewer and nothing is approved."""
    import reimburse_agent.graph as G
    from test_calendar_guard import C14, CID, Scripted, _msgs, run_one
    from langchain_core.messages import AIMessage

    asked = []

    class Stub:
        name = "stub"

        async def ask(self, message, question, context):
            asked.append((message["id"], question, context))
            return Answer(question, 0.95)

    monkeypatch.setattr(G, "get_judge", lambda: Stub())
    monkeypatch.setattr(G, "contradiction_hits", lambda res: [])  # the judge is the only signal left
    turns = [[("get_claim", {"claim_id": CID})], [("check_claim", {"claim_id": CID, "evidence_ids": ["EVT-7341"]})],
             [("check_claim", {"claim_id": CID, "evidence_ids": ["EVT-7341", "MSG-9341"]})]]
    rec = await run_one("deepseek-flash", C14, "", {"spent": 0.0, "limit": 1.0},
                        model=Scripted(messages=iter(_msgs(turns, "j") + [AIMessage(content="Done.")] * 3)))
    assert {m for m, _, _ in asked} == {"MSG-9341"} and all("meeting" in c for _, _, c in asked)
    assert rec["metrics"]["false_approvals"] == [] and rec["reviewer_turns"] >= 1
