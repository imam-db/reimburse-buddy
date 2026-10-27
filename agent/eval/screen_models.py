"""SumoPod model screening harness. PREPARED, NOT RUN: the default is a dry run that makes no network call.

    uv run --extra embed python -m eval.screen_models                       # dry run: plan, frozen conditions, estimate
    uv run --extra embed python -m eval.screen_models --live --max-usd 2.00 # paid: needs LLM_GATEWAY_API_KEY

Gateway: https://ai.sumopod.com/v1 (OpenAI-compatible, LiteLLM). The key is read from LLM_GATEWAY_API_KEY and is
never printed or written. Order of work in a live run:
  1. preflight per model: force each bound tool once (tool_choice = that tool); record schema rejections
     (HTTP 400 naming dict/anyOf/schema), missing tool calls and schema-invalid args. A model whose preflight
     rejects a schema is screened anyway but flagged.
  2. screening: the no-LLM baseline reference row ($0), then every model on the same cases, 1 run each.
  3. gateway control: deepseek-flash direct (api.deepseek.com, DEEPSEEK_API_KEY) vs via SumoPod on 2 cases.
Frozen conditions (recorded per results file): system prompt bytes (sha256), tool schemas (sha256), git commit,
fixture hash, budgets, temperature 0, parallel tool calls off.
Infra errors (timeouts, connection errors, 429, 5xx) are retried up to 2 times, counted, and not scored; a model
stops after 2 infra errors in a row. Ranking: safety (unsafe runs) > false approvals > correctness > protocol
reliability (schema-valid call rate, filled_by_graph, preflight rejections) > cost (X-Litellm-Response-Cost
header; token estimate only if the header is missing) > latency.
Hard budget cap: --max-usd is required with --live; the run refuses to start if the estimate exceeds it and
stops as soon as the measured spend reaches it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_openai import ChatOpenAI

from reimburse_agent.graph import FindArgs, GapSearchArgs, load_system_prompt
from reimburse_agent.mcp_client import mcp_tools

from .fixture_hash import fixture_hash
from .hard_cases import CASES, LARGE_CASES
from .run_eval import RESULTS

BASE_URL = "https://ai.sumopod.com/v1"
KEY_ENV = "LLM_GATEWAY_API_KEY"
MODELS = ["deepseek-flash", "glm-5.3-flash", "MiniMax-M3.1-Flash-Preview", "qwen3.8-flash"]
# USD per 1M tokens (input, output), from a third-party price list (aigratis.my.id, read 2026-10-25); estimate only.
PRICE = {"deepseek-flash": (0.15, 0.60), "glm-5.3-flash": (0.07, 0.25), "MiniMax-M3.1-Flash-Preview": (0.03, 0.12),
         "qwen3.8-flash": (0.15, 0.47)}
# Token assumption per run: #17/#18 flash averages (about 45k in / 3k out per case), worst case = the token cap.
AVG_IN, AVG_OUT, TOKEN_CAP = 45_000, 3_000, 60_000
PREFLIGHT_IN, PREFLIGHT_OUT = 9_000, 300  # per forced tool call: system prompt + all schemas
SCREEN_CASES = [c.id for c in CASES] + [c.id for c in LARGE_CASES["reasoning"]]
CONTROL_CASES = ["5b", "11"]
INFRA = ("APITimeoutError", "APIConnectionError", "RateLimitError", "InternalServerError", "ServiceUnavailable",
         "Timeout", "502", "503", "504", "429")
SCHEMA_REJECT = ("anyOf", "dict", "schema", "additionalProperties", "Invalid schema", "invalid_request_error")


class ScreenChat(ChatOpenAI):
    """Parallel tool calls off on every bind (the graph binds once per run)."""

    def bind_tools(self, tools, **kw):
        kw.setdefault("parallel_tool_calls", False)
        return super().bind_tools(tools, **kw)


class CostMeter(AsyncCallbackHandler):
    """Sums X-Litellm-Response-Cost from response headers; counts calls without the header."""

    def __init__(self):
        self.usd, self.calls, self.missing = 0.0, 0, 0

    async def on_llm_end(self, response, **kw):
        for gens in response.generations:
            for g in gens:
                h = {k.lower(): v for k, v in ((getattr(g, "message", None) and g.message.response_metadata.get("headers")) or {}).items()}
                self.calls += 1
                if "x-litellm-response-cost" in h:
                    self.usd += float(h["x-litellm-response-cost"])
                else:
                    self.missing += 1


def gateway_model(name: str, meter: CostMeter, direct: bool = False) -> ChatOpenAI:
    key = os.environ.get("DEEPSEEK_API_KEY" if direct else KEY_ENV)
    if not key:
        raise SystemExit(f"set {'DEEPSEEK_API_KEY' if direct else KEY_ENV}")  # never echo the value
    return ScreenChat(model=name, api_key=key, base_url="https://api.deepseek.com" if direct else BASE_URL,
                      temperature=0, timeout=120, max_retries=0, include_response_headers=True, callbacks=[meter])


def is_infra(err: str | None) -> bool:
    return bool(err) and any(k in err for k in INFRA)


async def tool_schemas() -> dict:
    async with mcp_tools() as (_s, tools):
        out = {t.name: (t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema()) for t in tools}
    out["search_for_gap"] = GapSearchArgs.model_json_schema()
    out["find_messages"] = FindArgs.model_json_schema()
    return out


def risky_schema(s) -> list[str]:
    """Schema features some gateways/models reject: anyOf, free-form dict (object without properties)."""
    found = set()

    def walk(x):
        if isinstance(x, dict):
            if "anyOf" in x or "oneOf" in x:
                found.add("anyOf")
            if x.get("type") == "object" and not x.get("properties") and x.get("additionalProperties", True) is not False:
                found.add("dict")
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(s)
    return sorted(found)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def frozen(schemas: dict, args) -> dict:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.strip())
    return {"system_prompt_sha256": sha(load_system_prompt().encode()), "tool_schemas_sha256": sha(json.dumps(schemas, sort_keys=True).encode()),
            "commit": commit, "worktree_dirty": dirty, "fixtures_sha256": fixture_hash()["sha256"], "temperature": 0,
            "parallel_tool_calls": False, "token_cap": args.token_cap, "budget_usd": args.max_usd, "cases": SCREEN_CASES,
            "runs": args.runs, "base_url": BASE_URL}


def estimate(n_tools: int, runs: int) -> dict:
    out, total, worst = {}, 0.0, 0.0
    n = len(SCREEN_CASES) * runs
    for m, (pi, po) in PRICE.items():
        pre = n_tools * (PREFLIGHT_IN * pi + PREFLIGHT_OUT * po) / 1e6
        avg = n * (AVG_IN * pi + AVG_OUT * po) / 1e6
        cap = n * (TOKEN_CAP * pi + 4_000 * po) / 1e6
        out[m] = {"preflight": round(pre, 4), "screen_avg": round(avg, 4), "screen_worst": round(cap, 4)}
        total += pre + avg
        worst += pre + cap
    ctl = len(CONTROL_CASES) * 2 * (AVG_IN * 0.30 + AVG_OUT * 1.20) / 1e6  # direct DeepSeek peak price for the direct half
    return {"per_model": out, "gateway_control": round(ctl, 4), "total_avg": round(total + ctl, 3),
            "total_worst": round(worst + 2 * ctl, 3), "assumptions": f"{AVG_IN} in / {AVG_OUT} out per run, cap {TOKEN_CAP}; "
            "prices from a third-party list, not cache-discounted; the measured header cost is what counts"}


async def preflight(name: str, schemas: dict, meter: CostMeter) -> dict:
    """Force each tool once; record rejections. One request per tool, no graph, no tool execution."""
    from langchain_core.tools import StructuredTool
    import jsonschema
    model = gateway_model(name, meter)
    tools = [StructuredTool.from_function(func=lambda **k: "", name=n, description=f"{n} (preflight)", args_schema=s) for n, s in schemas.items()]
    rows = {}
    for t in tools:
        try:
            msg = await model.bind_tools(tools, tool_choice={"type": "function", "function": {"name": t.name}}).ainvoke(
                f"Preflight: call {t.name} once with plausible arguments for claim CLM-2026-0301, employee EMP-003.")
            calls = msg.tool_calls or []
            ok = bool(calls) and calls[0]["name"] == t.name
            valid = None
            if ok:
                try:
                    jsonschema.validate(calls[0]["args"], schemas[t.name]); valid = True
                except jsonschema.ValidationError:
                    valid = False
            rows[t.name] = {"called": ok, "args_valid": valid, "rejected": False, "risky": risky_schema(schemas[t.name])}
        except Exception as e:  # noqa: BLE001 — recorded, not raised
            err = f"{type(e).__name__}: {e}"[:300]
            rows[t.name] = {"called": False, "args_valid": None, "rejected": any(k in err for k in SCHEMA_REJECT) and not is_infra(err),
                            "infra": is_infra(err), "error": err, "risky": risky_schema(schemas[t.name])}
    return rows


def rank_key(r: dict):
    return (r["unsafe"], r["false_approvals"], -r["correct"], -r["protocol"], r["cost_usd"], r["latency_s"])


async def screen_model(name: str, args, meter: CostMeter, spent: dict, direct: bool = False, cases=None) -> dict:
    from .run_hard_eval import run_one
    by_id = {c.id: c for c in CASES + LARGE_CASES["reasoning"]}
    runs, infra, streak, stopped = [], 0, 0, None
    for cid in cases or SCREEN_CASES:
        for _ in range(args.runs):
            for attempt in range(3):
                if spent["usd"] + meter.usd >= args.max_usd:
                    stopped = "budget cap reached"
                    break
                model = gateway_model(name, meter, direct)
                rec = await run_one(name, by_id[cid], "", {"spent": 0.0, "limit": 1e9}, args.token_cap, model=model)
                if is_infra(rec.get("error")):
                    infra += 1; streak += 1
                    if streak >= 2:
                        stopped = "2 infra errors in a row"
                        break
                    continue
                streak = 0
                runs.append(rec)
                break
            if stopped:
                break
        if stopped:
            break
    return summarize(name, runs, infra, meter, stopped)


def summarize(name, runs, infra, meter, stopped) -> dict:
    ms = [r["metrics"] for r in runs]
    calls = [c for r in runs for c in r["calls"] if not c.get("auto")]
    return {"model": name, "runs": len(runs), "infra_errors": infra, "stopped": stopped,
            "unsafe": sum(not m.get("safety", True) for m in ms), "false_approvals": sum(len(m.get("false_approvals") or []) for m in ms),
            "correct": sum(bool(m.get("correct")) for m in ms), "pass": sum(bool(m.get("pass")) for m in ms),
            "protocol": round(sum(bool(c.get("schema_ok")) for c in calls) / len(calls), 3) if calls else 0.0,
            "filled_by_graph": sum(bool(c.get("filled_by_graph")) for c in calls),
            "cost_usd": round(meter.usd, 5), "cost_header_missing": meter.missing,
            "latency_s": round(sum(r.get("latency_s", 0) for r in runs) / len(runs), 1) if runs else 0.0,
            "per_case": {r["case"]: r["metrics"].get("severity") for r in runs}}


async def baseline_row() -> dict:
    from .run_hard_eval import run_one
    by_id = {c.id: c for c in CASES + LARGE_CASES["reasoning"]}
    runs = [await run_one("baseline", by_id[cid], "", {"spent": 0, "limit": 0}) for cid in SCREEN_CASES]
    return summarize("baseline (no LLM)", runs, 0, CostMeter(), None)


async def main_async(args):
    schemas = await tool_schemas()
    est = estimate(len(schemas), args.runs)
    plan = {"frozen": frozen(schemas, args), "estimate_usd": est, "models": MODELS, "control_cases": CONTROL_CASES,
            "schema_risks": {n: risky_schema(s) for n, s in schemas.items() if risky_schema(s)}}
    if not args.live:
        if args.baseline:
            plan["baseline_reference"] = await baseline_row()
        print(json.dumps(plan, indent=2))
        print("dry run: no network call made. Add --live --max-usd X to run (paid).")
        return plan
    if args.max_usd is None:
        raise SystemExit("--live needs --max-usd (hard budget cap)")
    if est["total_worst" if args.strict_cap else "total_avg"] > args.max_usd:
        raise SystemExit(f"estimate {est} exceeds --max-usd {args.max_usd}; raise the cap or cut cases")
    spent = {"usd": 0.0}
    out = {**plan, "preflight": {}, "rows": [await baseline_row()]}
    for m in MODELS:
        meter = CostMeter()
        out["preflight"][m] = await preflight(m, schemas, meter)
        row = await screen_model(m, args, meter, spent)
        row["preflight_rejections"] = sum(v.get("rejected", False) for v in out["preflight"][m].values())
        spent["usd"] += meter.usd
        out["rows"].append(row)
    ctl = []
    for direct in (True, False):
        meter = CostMeter()
        ctl.append({"direct": direct, **await screen_model("deepseek-flash", args, meter, spent, direct, CONTROL_CASES)})
        spent["usd"] += meter.usd
    out["gateway_control"] = ctl
    out["ranking"] = [r["model"] for r in sorted(out["rows"][1:], key=rank_key)]
    out["spent_usd"] = round(spent["usd"], 4)
    RESULTS.mkdir(exist_ok=True)
    p = RESULTS / f"screen-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    p.write_text(json.dumps(out, indent=2, default=str))
    print("saved", p, "spent", out["spent_usd"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="make paid calls (default: dry run)")
    ap.add_argument("--max-usd", type=float, help="hard budget cap for the whole live run")
    ap.add_argument("--strict-cap", action="store_true", help="refuse if the worst-case estimate exceeds the cap")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--token-cap", type=int, default=TOKEN_CAP)
    ap.add_argument("--baseline", action="store_true", help="dry run: include the $0 baseline reference row")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
