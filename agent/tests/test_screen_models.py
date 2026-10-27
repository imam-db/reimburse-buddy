"""SumoPod screening harness, offline: no network, no key."""

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from eval import screen_models as S


def test_infra_errors_retry_then_stop_after_two_in_a_row(monkeypatch):
    calls = []

    async def fake_run_one(name, case, key, budget, cap, model=None):
        calls.append(case.id)
        return {"case": case.id, "error": "APITimeoutError: Request timed out.", "calls": [], "metrics": {}}

    import eval.run_hard_eval as R
    monkeypatch.setattr(R, "run_one", fake_run_one)
    monkeypatch.setattr(S, "gateway_model", lambda *a, **k: None)
    args = SimpleNamespace(runs=1, max_usd=1.0, token_cap=1000)
    row = asyncio.run(S.screen_model("m", args, S.CostMeter(), {"usd": 0.0}))
    assert row["runs"] == 0 and row["infra_errors"] == 2 and row["stopped"] == "2 infra errors in a row"
    assert len(calls) == 2  # not scored, and nothing after the stop


def test_budget_cap_stops_before_the_first_call(monkeypatch):
    monkeypatch.setattr(S, "gateway_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call expected")))
    row = asyncio.run(S.screen_model("m", SimpleNamespace(runs=1, max_usd=0.5, token_cap=1), S.CostMeter(), {"usd": 0.5}))
    assert row["stopped"] == "budget cap reached" and row["runs"] == 0


def test_cost_meter_reads_the_litellm_header():
    m = S.CostMeter()
    gen = ChatGeneration(message=AIMessage(content="", response_metadata={"headers": {"X-Litellm-Response-Cost": "0.00123"}}))
    asyncio.run(m.on_llm_end(LLMResult(generations=[[gen], [ChatGeneration(message=AIMessage(content=""))]])))
    assert m.usd == 0.00123 and m.calls == 2 and m.missing == 1


def test_ranking_order_safety_first():
    base = {"unsafe": 0, "false_approvals": 0, "correct": 10, "protocol": 1.0, "cost_usd": 1.0, "latency_s": 5}
    rows = [{**base, "model": "cheap_unsafe", "unsafe": 1, "cost_usd": 0.01},
            {**base, "model": "false_approver", "false_approvals": 1},
            {**base, "model": "good_expensive", "cost_usd": 2.0},
            {**base, "model": "good_cheap"}]
    assert [r["model"] for r in sorted(rows, key=S.rank_key)] == ["good_cheap", "good_expensive", "false_approver", "cheap_unsafe"]


def test_risky_schema_and_infra_classification():
    assert S.risky_schema({"properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}, "d": {"type": "object"}}}) == ["anyOf", "dict"]
    assert S.is_infra("InternalServerError: 503") and not S.is_infra("BadRequestError: anyOf is not supported")


def test_estimate_has_a_total_and_worst_case():
    e = S.estimate(17, 1)
    assert set(e["per_model"]) == set(S.MODELS) and 0 < e["total_avg"] < e["total_worst"]
