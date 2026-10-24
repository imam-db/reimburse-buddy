"""Mini-eval: python -m eval.run_eval --model deepseek-flash [--runs 3] [--raw] [--claims ...]

Needs DEEPSEEK_API_KEY (or REIMBURSE_AGENT_API_KEY). Writes eval/results/<model>-<ts>.json.
Scripted reviewer: declines every gated write (no state changes in eval), answers a decision
question with the case's human_decisions, and allows at most 2 reviewer turns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.graph import build_graph
from reimburse_agent.mcp_client import mcp_tools

from .cases import CASES, engine, expected

# USD per 1M tokens, https://api-docs.deepseek.com/quick_start/pricing (read 2026-10-24).
# We bill at PEAK rates as an upper bound; off-peak is exactly half.
PRICES = {
    "deepseek-flash": {"hit": 0.006, "miss": 0.30, "out": 1.20},
    "deepseek-v4-pro": {"hit": 0.044, "miss": 1.32, "out": 3.96},
}
RESULTS = Path(__file__).parent / "results"
RP = re.compile(r"Rp\s?(\d[\d.,]*\d|\d)")
NUM = re.compile(r"\d[\d.,]*\d|\d")
REJECT_NEAR = re.compile(r"unsigned|ignored|not |no_matching|wrong|different|other trip|doesn.t|does not|"
                         r"tidak|bukan|belum|trap|invalid|reject|n't|not used|not needed|unused|never", re.I)
SHAPE_ERR = re.compile(r"wrong shape|not a valid choice|unknown item|validation error|unknown tool", re.I)


def _int(s: str) -> int | None:
    s = re.sub(r"[.,]\d{2}$", "", s)  # Rp1.200.000,00
    d = re.sub(r"[^\d]", "", s)
    return int(d) if d else None


def cost(model: str, u: dict) -> float:
    p = PRICES[model]
    miss = u["input"] - u["cache_hit"]
    return (u["cache_hit"] * p["hit"] + miss * p["miss"] + u["output"] * p["out"]) / 1e6


async def run_one(model_name, case, compact, api_key, budget):
    model = ChatOpenAI(model=model_name, api_key=api_key, base_url="https://api.deepseek.com",
                       temperature=0, timeout=180, max_retries=2)
    t0 = time.perf_counter()
    rec = {"claim": case.claim_id, "calls": [], "gated": [], "questions": 0, "error": None}
    usage = {"input": 0, "cache_hit": 0, "output": 0, "llm_calls": 0}
    tool_texts: list[str] = []
    async with mcp_tools() as (_s, tools):
        schemas = {t.name: (t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema())
                   for t in tools}
        g = build_graph(model, tools, checkpointer=InMemorySaver(), compact=compact, run_claim=case.claim_id)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        payload = {"messages": [("user", f"Review claim {case.claim_id} and draft the decision note.")],
                   "declined": {}}
        reviewer_turns = 0
        drafted_since_human = False
        pending: dict[str, dict] = {}
        try:
            while True:
                async for ev in g.astream(payload, conf, stream_mode="updates"):
                    for node, upd in ev.items():
                        if node == "__interrupt__":
                            continue
                        for m in (upd or {}).get("messages", []):
                            if m.type == "ai":
                                tu = (m.response_metadata or {}).get("token_usage") or {}
                                usage["llm_calls"] += 1
                                usage["input"] += tu.get("prompt_tokens", 0)
                                usage["output"] += tu.get("completion_tokens", 0)
                                usage["cache_hit"] += tu.get("prompt_cache_hit_tokens", 0) or \
                                    (tu.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
                                budget["spent"] += cost(model_name, {"input": tu.get("prompt_tokens", 0),
                                    "cache_hit": tu.get("prompt_cache_hit_tokens", 0) or 0,
                                    "output": tu.get("completion_tokens", 0)})
                                for c in m.tool_calls or []:
                                    ok = c["name"] in schemas
                                    if ok:
                                        try:
                                            jsonschema.validate(c["args"], schemas[c["name"]])
                                        except jsonschema.ValidationError:
                                            ok = False
                                    pending[c["id"]] = {"name": c["name"], "args": c["args"], "schema_ok": ok}
                                    if c["name"] == "draft_decision":
                                        drafted_since_human = True
                            elif m.type == "tool":
                                call = pending.pop(m.tool_call_id, {"name": m.name, "args": {}, "schema_ok": False})
                                txt = str(m.content)
                                call["declined"] = "reviewer_declined" in txt
                                if SHAPE_ERR.search(txt) and '"error"' in txt:
                                    call["schema_ok"] = False
                                call["result"] = txt
                                tool_texts.append(txt)
                                rec["calls"].append(call)
                if budget["spent"] > budget["limit"]:
                    raise RuntimeError("budget limit reached")
                st = await g.aget_state(conf)
                if st.interrupts:
                    req = st.interrupts[0].value
                    rec["gated"].append(req["tool"])
                    payload = Command(resume={"approved": False, "reason":
                                              "Eval: no writes. The reviewer applies status changes after reading your note."})
                    continue
                last_draft = next((c for c in reversed(rec["calls"]) if c["name"] == "draft_decision"), None)
                applied = bool(last_draft and (last_draft["args"].get("human_decisions") or {}) == case.human_decisions)
                if reviewer_turns >= 2 or (drafted_since_human and (applied or not case.human_decisions)):
                    break
                # The model stopped and is waiting on the reviewer (a decision question, or no note yet).
                rec["questions"] += 1
                reviewer_turns += 1
                drafted_since_human = False
                ans = (f"Reviewer decision: {json.dumps(case.human_decisions)}. Use exactly this as human_decisions "
                       "and finish with draft_decision." if case.human_decisions else
                       "Reviewer: I have no extra decision. Use the tools and evidence, then finish with draft_decision.")
                payload = {"messages": [("user", ans)]}
        except Exception as exc:  # record, don't abort the whole eval
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            st = await g.aget_state(conf)
    rec["latency_s"] = round(time.perf_counter() - t0, 1)
    rec["usage"] = usage
    msgs = st.values.get("messages", [])
    final = str(msgs[-1].content) if msgs else ""
    rec["final"] = final
    score(case, rec, final, tool_texts)
    for c in rec["calls"]:
        c.pop("result", None)
    return rec


def score(case, rec, final, tool_texts):
    exp = expected(case)
    # The agent's final position = args of its last draft_decision (else last check_claim).
    last = next((c for c in reversed(rec["calls"]) if c["name"] == "draft_decision" and not c.get("declined")), None) \
        or next((c for c in reversed(rec["calls"]) if c["name"] == "check_claim"), None)
    sent = [(c["args"].get("to", ""), c["args"].get("body", ""), c["args"].get("subject", ""))
            for c in rec["calls"] if c["name"] == "send_message" and not c.get("declined")]
    rec["drafted"] = bool(last and last["name"] == "draft_decision")
    if last:
        try:
            got = engine(case, last["args"].get("evidence_ids"), last["args"].get("human_decisions") or None, sent)
        except Exception as exc:
            got, rec["engine_error"] = None, str(exc)[:200]
    else:
        got = None
    exp_items = {i.item_id: i.status.value for i in exp.items}
    got_items = {i.item_id: i.status.value for i in got.items} if got else {}
    rec["items_total"] = len(exp_items)
    rec["items_correct"] = sum(got_items.get(k) == v for k, v in exp_items.items())
    rec["expected_total"] = int(exp.total_approvable)
    rec["agent_total"] = int(got.total_approvable) if got else None
    in_text = {_int(x) for x in RP.findall(final)} - {None}
    rec["amount_match"] = bool(got and got.total_approvable == exp.total_approvable
                               and rec["expected_total"] in (in_text | {_int(x) for x in NUM.findall(final)}))
    # Invented Rp: any Rp figure in the final answer that no tool output contains.
    seen = {_int(x) for t in tool_texts for x in NUM.findall(t)} - {None}
    rec["rp_figures"] = sorted(in_text)
    rec["invented_rp"] = sorted(in_text - seen - {0})  # "Rp0" is not a money claim
    # Wrong evidence accepted: a trap ID passed as evidence in the final call, or cited in the
    # final note with no rejection wording within 150 chars.
    wrong = []
    used = set((last or {}).get("args", {}).get("evidence_ids") or [])
    for wid in case.wrong_evidence:
        cited_ok = any(not REJECT_NEAR.search(final[max(0, m.start() - 150): m.end() + 150])
                       for m in re.finditer(re.escape(wid), final))
        if cited_ok:
            wrong.append(wid)
    rec["wrong_evidence_cited"] = wrong
    rec["wrong_evidence_submitted"] = sorted(used & set(case.wrong_evidence))
    rec["unnecessary_escalations"] = max(0, rec["questions"] - case.expected_questions)
    rec["final_items"] = got_items


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))] if xs else None


def summarize(model, runs, compact):
    calls = [c for r in runs for c in r["calls"]]
    by_claim: dict[str, list] = {}
    for r in runs:
        by_claim.setdefault(r["claim"], []).append(r)
    consistent = sum(1 for rs in by_claim.values()
                     if len({json.dumps(r["final_items"], sort_keys=True) for r in rs}) == 1
                     and all(r["items_correct"] == r["items_total"] for r in rs))
    u = {k: sum(r["usage"][k] for r in runs) for k in ("input", "cache_hit", "output")}
    lat = [r["latency_s"] for r in runs]
    s = {
        "model": model, "compact": compact, "runs": len(runs), "errors": sum(bool(r["error"]) for r in runs),
        "schema_valid_pct": round(100 * sum(c["schema_ok"] for c in calls) / max(1, len(calls)), 1),
        "tool_calls": len(calls),
        "invented_rp": sum(len(r["invented_rp"]) for r in runs),
        "item_accuracy_pct": round(100 * sum(r["items_correct"] for r in runs) / sum(r["items_total"] for r in runs), 1),
        "amount_match": f'{sum(r["amount_match"] for r in runs)}/{len(runs)}',
        "wrong_evidence_cited": sum(len(r["wrong_evidence_cited"]) for r in runs),
        "wrong_evidence_submitted": sum(len(r["wrong_evidence_submitted"]) for r in runs),
        "unnecessary_escalations": sum(r["unnecessary_escalations"] for r in runs),
        "gated_writes_declined": sum(len(r["gated"]) for r in runs),
        "consistent_claims": f"{consistent}/{len(by_claim)}",
        "input_tokens_per_run": round(u["input"] / len(runs)), "cache_hit_pct": round(100 * u["cache_hit"] / max(1, u["input"])),
        "output_tokens_per_run": round(u["output"] / len(runs)),
        "cost_usd_total_peak": round(cost(model, u), 4), "cost_usd_per_claim_peak": round(cost(model, u) / len(runs), 4),
        "latency_p50_s": pct(lat, 0.5), "latency_p95_s": pct(lat, 0.95),
    }
    s["gates_pass"] = s["schema_valid_pct"] >= 95 and s["invented_rp"] == 0
    return s


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(PRICES))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--claims", nargs="*")
    ap.add_argument("--raw", action="store_true", help="disable agent-side compact mode")
    ap.add_argument("--budget", type=float, default=0.9, help="stop when estimated USD spend exceeds this")
    ap.add_argument("--concurrency", type=int, default=5)
    a = ap.parse_args()
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("REIMBURSE_AGENT_API_KEY")
    if not key:
        raise SystemExit("set DEEPSEEK_API_KEY")
    cases = [c for c in CASES if not a.claims or c.claim_id in a.claims]
    budget = {"spent": 0.0, "limit": a.budget}
    sem = asyncio.Semaphore(a.concurrency)

    async def go(case):
        async with sem:
            if budget["spent"] > budget["limit"]:
                return None
            return await run_one(a.model, case, not a.raw, key, budget)

    runs = [r for r in await asyncio.gather(*[go(c) for _ in range(a.runs) for c in cases]) if r]
    s = summarize(a.model, runs, not a.raw)
    RESULTS.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = RESULTS / f"{a.model}-{'raw' if a.raw else 'compact'}-{ts}.json"
    out.write_text(json.dumps({"summary": s, "runs": runs}, indent=2, ensure_ascii=False))
    print(json.dumps(s, indent=2))
    for r in runs:
        print(r["claim"], r["latency_s"], r["usage"]["input"], r["items_correct"], "/", r["items_total"],
              "amt", r["amount_match"], "q", r["questions"], "gated", r["gated"], "inv", r["invented_rp"],
              "wrong", r["wrong_evidence_cited"], r["wrong_evidence_submitted"], r["error"] or "")
    print("saved", out, "spent~", round(budget["spent"], 4))


if __name__ == "__main__":
    asyncio.run(main())
