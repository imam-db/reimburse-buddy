"""Harder eval: python -m eval.run_hard_eval --model deepseek-flash [--runs 3] [--cases 1 3 10a] [--budget 1.0]

Needs DEEPSEEK_API_KEY. Each run starts a fresh MCP server on an eval-only fixture dir
(REIMBURSE_BUDDY_DATA=eval/fixtures/hard or altered), so default data/ is never touched.
The scripted reviewer answers every approval per the case's policy (hard_cases.review) and replies
to a stopped agent per hard_cases.stop_reply (at most 2 reviewer turns).
Writes eval/results/hard-<model>-<ts>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

import jsonschema
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.compact import tool_text
from reimburse_agent.batch import build_batch_graph
from reimburse_agent.graph import BUDGET_STOP, GAP_SEARCH_TOOL, GapSearchArgs, build_graph
from reimburse_agent.mcp_client import mcp_tools

from .fixture_hash import fixture_hash
from .hard_cases import CASES, review, stop_reply
from .hard_score import score
from .baseline import BaselineModel
from .run_eval import PRICES, RESULTS, RP, cost as _cost

FAIL_MSG = "upstream timeout (injected by eval, first call only)"
STATUSES = ("approved", "partially_approved", "rejected", "on_hold")


def fail_once(tool):
    """Same name/schema as `tool`; its first call raises, later calls pass through."""
    state = {"n": 0}

    async def run(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError(FAIL_MSG)
        return await tool.ainvoke(kwargs)

    return StructuredTool.from_function(coroutine=run, name=tool.name, description=tool.description,
                                        args_schema=tool.args_schema)


def usage_of(m):
    tu = (m.response_metadata or {}).get("token_usage") or {}
    return {"input": tu.get("prompt_tokens", 0) or 0, "output": tu.get("completion_tokens", 0) or 0,
            "cache_hit": tu.get("prompt_cache_hit_tokens", 0)
            or (tu.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0}


async def server_state(by_name) -> dict:
    out = await by_name["get_outbox"].ainvoke({})
    try:
        outbox = json.loads(tool_text(out))
        msgs = outbox.get("messages", outbox.get("outbox", [])) if isinstance(outbox, dict) else outbox
        n = len(msgs)
    except ValueError:
        n, msgs = -1, None
    changed = []
    for st in STATUSES:
        rows = json.loads(tool_text(await by_name["list_claims"].ainvoke({"status": st})) or "[]")
        rows = rows if isinstance(rows, list) else [rows]
        changed += [r["id"] for r in rows if isinstance(r, dict) and "id" in r]
    return {"outbox": n, "outbox_msgs": msgs, "changed_status": sorted(changed)}


def cost(model_name: str, u: dict) -> float:
    return 0.0 if model_name in ("baseline", "oracle") else _cost(model_name, u)


TOKEN_CAP = 60_000  # per run (input + output); a run past it stops and is scored as is


def use_sqlite_cache(path: str) -> None:
    """Optional LangChain LLM cache (temperature 0 + byte-identical system prompt => repeat prompts hit it)."""
    try:
        from langchain_community.cache import SQLiteCache
    except ImportError as exc:  # optional dependency
        raise SystemExit("--cache needs langchain-community: uv pip install langchain-community") from exc
    from langchain_core.globals import set_llm_cache
    set_llm_cache(SQLiteCache(database_path=path))


async def run_one(model_name, case, api_key, budget, token_cap=TOKEN_CAP, model=None, model_search=True):
    """model: tests pass a fake chat model; "baseline" is the no-LLM heuristic policy (eval/baseline.py,
    reviewer answers via the structured form); otherwise DeepSeek via the OpenAI-compatible API."""
    if model is None and model_name == "baseline":
        model = BaselineModel()
    model = model or ChatOpenAI(model=model_name, api_key=api_key, base_url="https://api.deepseek.com",
                                temperature=0, timeout=180, max_retries=2)
    t0 = time.perf_counter()
    # questions: turn-ending agent messages that ask something ("?"), not the scripted reviewer's replies
    rec = {"case": case.id, "claim": case.claim_id, "calls": [], "gated": [], "questions": 0, "reviewer_turns": 0,
           "error": None, "drafted_since_human": False}
    usage = {"input": 0, "cache_hit": 0, "output": 0, "llm_calls": 0}
    tool_texts: list[str] = []
    env = {"REIMBURSE_BUDDY_DATA": str(case.data)}
    async with mcp_tools(extra_env=env) as (_s, tools):
        raw = {t.name: t for t in tools}
        if case.fail_tool:
            tools = [fail_once(t) if t.name == case.fail_tool else t for t in tools]
        schemas = {t.name: (t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema())
                   for t in tools}
        schemas[GAP_SEARCH_TOOL] = GapSearchArgs.model_json_schema()  # graph-only tool: validate the model's own args
        if case.batch:
            g = build_batch_graph(model, tools, case.batch, checkpointer=InMemorySaver())
        else:
            g = build_graph(model, tools, checkpointer=InMemorySaver(), run_claim=case.claim_id,
                            model_search=model_search)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        payload = {"messages": [("user", case.prompt)], "declined": {}}
        pending: dict[str, dict] = {}
        seen_props: dict[tuple, int] = {}
        rec["confirmations"] = []
        turns = 0
        queue: list[str] = []  # batch: claims not yet run; a sub-run's events belong to queue[0]
        try:
            while True:
                async for ns, ev in g.astream(payload, conf, stream_mode="updates", subgraphs=True):
                    for node, upd in ev.items():
                        if node == "__interrupt__" or not isinstance(upd, dict):
                            continue
                        if not ns and "queue" in upd:
                            queue = list(upd["queue"] or [])
                        run_claim = (queue[0] if queue else None) if case.batch and ns else \
                            (None if case.batch else case.claim_id)
                        props = upd.get("graph_proposals")
                        if props is not None:  # gated actions the graph proposed itself (never in model history)
                            k = (tuple(ns), node)
                            for p in props[seen_props.get(k, 0):]:
                                rec["calls"].append({"id": p["id"], "name": p["tool"], "args": p["args"], "schema_ok": True,
                                                     "turn": usage["llm_calls"], "auto": True, "by": "graph_action",
                                                     "declined": not p["approved"], "error": bool(p.get("error")),
                                                     "ok": p["approved"] and not p.get("error"), "run_claim": run_claim})
                            seen_props[k] = len(props)
                        if node in ("graph_actions", "finish", "approve", "triage", "claim_run", "intake"):
                            continue  # graph-made text, no LLM call
                        if node == "auto_draft":  # graph-made draft + closing text: no LLM call, no tokens
                            for d in (upd or {}).get("auto_drafts", [])[-1:]:
                                rec["calls"].append({"id": f"auto-draft-{d['check_id']}", "name": "draft_decision",
                                                     "args": d["args"], "schema_ok": True, "turn": usage["llm_calls"],
                                                     "auto": True, "declined": False, "error": False, "ok": False,
                                                     "run_claim": run_claim, "budget": bool(d.get("budget"))})
                                tool_texts.append(d["content"])
                                rec["drafted_since_human"] = True
                            continue
                        for m in (upd or {}).get("messages", []):
                            if m.type == "ai" and node == "agent" and m.name == BUDGET_STOP:
                                rec["budget_stops"] = rec.get("budget_stops", 0) + 1  # graph text, no LLM call
                                continue
                            if m.type == "ai" and node == "agent":
                                if node == "agent" and not m.tool_calls and "?" in str(m.content):
                                    rec["questions"] += 1
                                u = usage_of(m)
                                usage["llm_calls"] += 1
                                for k in ("input", "output", "cache_hit"):
                                    usage[k] += u[k]
                                budget["spent"] += cost(model_name, u)
                                if usage["input"] + usage["output"] > token_cap:
                                    raise RuntimeError(f"token cap {token_cap} reached")
                                for c in m.tool_calls or []:
                                    ok = c["name"] in schemas
                                    if ok:
                                        try:
                                            jsonschema.validate(c["args"], schemas[c["name"]])
                                        except jsonschema.ValidationError:
                                            ok = False
                                    pending[c["id"]] = {"id": c["id"], "name": c["name"], "args": c["args"],
                                                        "schema_ok": ok, "turn": usage["llm_calls"],
                                                        "run_claim": run_claim}
                                    if c["name"] == "draft_decision":
                                        rec["drafted_since_human"] = True
                            elif m.type == "tool":
                                call = pending.pop(m.tool_call_id, {"id": m.tool_call_id, "name": m.name, "args": {},
                                                                    "schema_ok": False, "turn": -1})
                                txt = str(m.content)
                                call["declined"] = "reviewer_declined" in txt
                                call["error"] = txt.startswith('{"error"') and not call["declined"]
                                call["ok"] = '"ok":true' in txt.replace(" ", "")
                                tool_texts.append(txt)
                                rec["calls"].append(call)
                if budget["spent"] > budget["limit"]:
                    raise RuntimeError("budget limit reached")
                if usage["input"] + usage["output"] > token_cap:
                    raise RuntimeError(f"token cap {token_cap} reached")
                st = await g.aget_state(conf)
                if st.interrupts:
                    req = st.interrupts[0].value
                    ok, reason = review(case, req)
                    if req["tool"] == "confirm_reviewer_decisions":
                        rec["confirmations"].append({"decisions": req["args"].get("decisions"), "prompt": req.get("prompt"),
                                                     "approved": ok})
                        payload = Command(resume={"approved": ok, "tool_call_id": req["tool_call_id"], "reason": reason})
                        continue
                    rec["gated"].append({"tool": req["tool"], "args": req["args"], "tool_call_id": req["tool_call_id"],
                                         "approved": ok, "reason": reason})
                    payload = Command(resume={"approved": ok, "tool_call_id": req["tool_call_id"], "reason": reason})
                    continue
                rec["reviewer_decisions_state"] = dict(st.values.get("reviewer_decisions", {}))
                reply = stop_reply(case, rec) if turns < 2 else None
                if reply is None:
                    break
                turns += 1
                rec["reviewer_turns"] += 1
                rec["drafted_since_human"] = False
                payload = {"messages": [("user", reply)]}
                if model_name == "baseline" and case.human_decisions and case.reviewer_says and case.reviewer_says in reply:
                    # the baseline has no language understanding: the reviewer uses the structured form
                    payload.update(reviewer_decisions=dict(case.human_decisions), decisions_source="form")
        except Exception as exc:  # record, don't abort the whole eval
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            st = await g.aget_state(conf)
        server = await server_state(raw)
    vals = st.values if st else {}
    eff = vals.get("effective_args", {})
    for c in rec["calls"]:  # score what really ran: the graph injects reviewer decisions, strips ignored IDs
        if c["id"] in eff and eff[c["id"]] != c["args"]:
            c["model_args"], c["args"] = c["args"], eff[c["id"]]
            c["filled_by_graph"] = True  # graph injected/stripped/filled args; not a malformed model call
    # graph-made writes, with the run they came from (batch: the sub-run's own summary, never the args)
    owned = [(s["claim_id"], w) for s in vals.get("summaries", []) for w in s.get("writes", [])] if case.batch \
        else [(case.claim_id, w) for w in vals.get("writes", [])]
    for i, (rc, w) in enumerate((rc, w) for rc, w in owned if w.get("by") == "graph"):
        rec["calls"].append({"id": f"graph-write-{i}", "name": w["tool"], "args": w["args"], "schema_ok": True,
                             "turn": -1, "auto": True, "declined": False, "error": False, "ok": True, "run_claim": rc})
    rec["reviewer_decisions"] = vals.get("reviewer_decisions", {})
    # [claim, item, gap] evidence requests the graph's gate let through as free (free_evidence_request);
    # the scorer exempts exactly these, in single-claim runs and in batch sub-runs alike.
    rec["free_evidence"] = [list(x) for x in vals.get("free_evidence", [])]
    rec["unparsed_reviewer"] = vals.get("unparsed_reviewer", [])
    rec["model_decisions_refused"] = sum("human_decisions_not_from_reviewer" in t for t in tool_texts)
    rec["server_changed"] = server["changed_status"]
    rec["outbox_msgs"] = server.get("outbox_msgs")  # the server's own outbox: the scorer checks every message
    runs = [t["state"] for t in vals.get("threads", {}).values()] if case.batch else [vals]
    rec["budget_drafts"] = sum(bool(d.get("budget")) for v in runs for d in v.get("auto_drafts", []))
    rec["latency_s"] = round(time.perf_counter() - t0, 1)
    rec["usage"] = usage
    msgs = st.values.get("messages", [])
    final = str(msgs[-1].content) if msgs else ""
    rec["final"] = final  # full text: offline re-scoring must see every Rp figure
    rec["money_mentions"] = RP.findall(final)
    rec["tool_texts"] = tool_texts  # kept so later scoring changes can be re-applied offline
    rec["metrics"] = score(case, rec, final, tool_texts, server)
    rec["cost_usd_peak"] = round(cost(model_name, usage), 5)
    return rec


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def summarize(model, runs):
    by: dict[str, list] = {}
    for r in runs:
        by.setdefault(r["case"], []).append(r)
    rows = []
    for cid, rs in by.items():
        ms = [r["metrics"] for r in rs]
        rows.append({
            "case": cid, "pass": f"{sum(m['pass'] for m in ms)}/{len(ms)}",
            "safety": f"{sum(m.get('safety', False) for m in ms)}/{len(ms)}",
            "false_approvals": sum(len(m.get("false_approvals") or []) for m in ms),
            "correct": f"{sum(m.get('correct', False) for m in ms)}/{len(ms)}",
            "efficiency_avg": _avg([m.get("efficiency") for m in ms]),
            "unauthorized_writes": sum(m["unauthorized_writes"] for m in ms),
            "evidence_precision_avg": _avg([m.get("evidence_precision") for m in ms]),
            "final_evidence_precision_avg": _avg([m.get("final_evidence_precision") for m in ms]),
            "model_followed_explicit_instruction": _avg([m.get("model_followed_explicit_instruction") for m in ms]),
            "tool_calls_avg": round(sum(m["tool_calls"] for m in ms) / len(ms), 1),
            "llm_calls_avg": round(sum(r["usage"]["llm_calls"] for r in rs) / len(rs), 1),
            "input_tokens_avg": round(sum(r["usage"]["input"] for r in rs) / len(rs)),
            "output_tokens_avg": round(sum(r["usage"]["output"] for r in rs) / len(rs)),
            "errors": sum(bool(r["error"]) for r in rs),
            "why": sorted({m["why"] for m in ms if not m["pass"]}),
        })
    allm = [r["metrics"] for r in runs]
    inj = [r["metrics"] for r in runs if r["case"] == "1"]
    dec = [r["metrics"] for r in runs if r["case"] == "4"]
    stop = [r["metrics"] for r in runs if r["case"] == "6"]
    u = {k: sum(r["usage"][k] for r in runs) for k in ("input", "cache_hit", "output")}
    return {
        "model": model, "runs": len(runs), "pass": f"{sum(m['pass'] for m in allm)}/{len(allm)}",
        "safety": f"{sum(m.get('safety', False) for m in allm)}/{len(allm)}",
        "false_approvals": sum(len(m.get("false_approvals") or []) for m in allm),
        "false_approval_runs": f"{sum(bool(m.get('false_approvals')) for m in allm)}/{len(allm)}",
        "severity": {k: sum(m.get("severity") == k for m in allm) for k in ("unsafe", "false_approval", "incorrect", "ok")},
        "correct": f"{sum(m.get('correct', False) for m in allm)}/{len(allm)}",
        "efficiency_avg": _avg([m.get("efficiency") for m in allm]),
        "unauthorized_writes": sum(m["unauthorized_writes"] for m in allm),
        "injection_success": f"{sum(bool(m.get('injection_followed')) for m in inj)}/{len(inj)}",
        "decline_respected": f"{sum(not m.get('writes_after_decline') for m in dec)}/{len(dec)}",
        "correct_stop": f"{sum(m['pass'] for m in stop)}/{len(stop)}",
        "proposed_employee_writes_unforced": sum(m.get("proposed_employee_writes", 0)
                                                 for r, m in zip(runs, allm) if r["case"] == "2u"),
        "invented_rp": sum(len(m["invented_rp"]) for m in allm),
        "wrong_evidence_submitted": sum(len(m.get("wrong_evidence_submitted", [])) for m in allm),
        "evidence_precision_avg": _avg([m.get("evidence_precision") for m in allm]),
        "final_evidence_precision_avg": _avg([m.get("final_evidence_precision") for m in allm]),
        "model_followed_explicit_instruction": "{}/{}".format(
            sum(m.get("model_followed_explicit_instruction") or 0 for m in allm),
            sum(m.get("model_followed_explicit_instruction") is not None for m in allm)),
        "model_decisions_refused": sum(r.get("model_decisions_refused", 0) for r in runs),
        "agent_questions": sum(m.get("questions", 0) for m in allm),
        "cost_usd_total_peak": round(cost(model, u), 4), "cases": rows,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=[*sorted(PRICES), "baseline"])
    ap.add_argument("--no-model-search", action="store_true",
                    help="do not give the model the mailbox search tools (the graph's reply search still runs)")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--budget", type=float, default=1.0, help="stop when estimated USD spend exceeds this")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--token-cap", type=int, default=TOKEN_CAP, help="per-run input+output token cap")
    ap.add_argument("--cache", help="SQLite path for an optional LangChain LLM cache")
    a = ap.parse_args()
    if a.cache:
        use_sqlite_cache(a.cache)
    key = os.environ.get("DEEPSEEK_API_KEY") if a.model != "baseline" else ""
    if not key and a.model != "baseline":
        raise SystemExit("set DEEPSEEK_API_KEY")
    cases = [c for c in CASES if not a.cases or c.id in a.cases]
    budget = {"spent": 0.0, "limit": a.budget}
    sem = asyncio.Semaphore(a.concurrency)

    async def go(case):
        async with sem:
            if budget["spent"] > budget["limit"]:
                return None
            return await run_one(a.model, case, key, budget, a.token_cap, model_search=not a.no_model_search)

    runs = [r for r in await asyncio.gather(*[go(c) for _ in range(a.runs) for c in cases]) if r]
    s = summarize(a.model, runs)
    s["skipped_for_budget"] = a.runs * len(cases) - len(runs)
    RESULTS.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = RESULTS / f"hard-{a.model}{'-nosearch' if a.no_model_search else ''}-{ts}.json"
    s["fixtures_sha256"] = fixture_hash()["sha256"]
    out.write_text(json.dumps({"summary": s, "fixtures": fixture_hash(), "runs": runs}, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(s, indent=2, ensure_ascii=False))
    print("saved", out, "spent~", round(budget["spent"], 4))


if __name__ == "__main__":
    asyncio.run(main())
