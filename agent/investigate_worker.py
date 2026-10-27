"""Investigation worker: one LangGraph run on one claim, spoken over JSON lines on stdin/stdout.

The web admin (web/reimburse_web/agent_bridge.py) starts this as a subprocess inside the agent's
own uv environment, so the agent's dependencies never enter the root lockfile.

    uv --directory agent run python investigate_worker.py --claim CLM-2026-0110 [--fake-script f.json]

stdout events (one JSON object per line):
    {"type": "start", "model": ..., "compact": true}
    {"type": "step", "n": 1, "tool": ..., "args": {...}, "args_summary": ..., "result_summary": ...}
    {"type": "interrupt", "tool": ..., "args": {...}, "tool_call_id": ...}   -> waits for one stdin line
    {"type": "final", "note": ..., "tokens": {...}}
    {"type": "error", "code": "no_key" | "token_limit" | "exception", "message": ...}
stdin (only after an interrupt): {"approved": true} or {"approved": false, "reason": "..."}

Model: REIMBURSE_AGENT_MODEL (default deepseek-flash), REIMBURSE_AGENT_BASE_URL (default
https://api.deepseek.com), key from REIMBURSE_AGENT_API_KEY or DEEPSEEK_API_KEY. The key is
never printed. --fake-script replays scripted AIMessages (tests; no key, no network).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.graph import build_graph
from reimburse_agent.mcp_client import mcp_tools

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"


def emit(ev: dict) -> None:
    sys.stdout.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def summarize(x, limit: int = 160) -> str:
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def api_key() -> str:
    return (os.environ.get("REIMBURSE_AGENT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()


def make_model(fake_script: str | None):
    if fake_script:
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

        class Scripted(GenericFakeChatModel):
            def bind_tools(self, tools, **kw):
                return self

        with open(fake_script, encoding="utf-8") as f:
            script = json.load(f)
        msgs = [AIMessage(content=m.get("content", ""), tool_calls=[
            {"name": c["name"], "args": c.get("args", {}), "id": c.get("id", f"call{i}-{j}")}
            for j, c in enumerate(m.get("tool_calls", []))]) for i, m in enumerate(script)]
        return Scripted(messages=iter(msgs)), "fake-scripted"
    from langchain_openai import ChatOpenAI

    name = os.environ.get("REIMBURSE_AGENT_MODEL") or DEFAULT_MODEL
    return ChatOpenAI(model=name, api_key=api_key(), temperature=0,
                      base_url=os.environ.get("REIMBURSE_AGENT_BASE_URL") or DEFAULT_BASE_URL), name


async def run(a) -> int:
    if not a.fake_script and not api_key():
        emit({"type": "error", "code": "no_key",
              "message": "No model key configured. Set DEEPSEEK_API_KEY (or REIMBURSE_AGENT_API_KEY)."})
        return 2
    model, name = make_model(a.fake_script)
    emit({"type": "start", "model": name, "compact": True, "claim": a.claim})
    tokens = {"input": 0, "output": 0}
    pending: dict[str, dict] = {}
    n = 0
    async with mcp_tools() as (_s, tools):
        graph = build_graph(model, tools, checkpointer=InMemorySaver(), compact=True)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": a.max_steps}
        payload: object = {"messages": [("user", a.prompt.format(claim=a.claim))], "declined": {}}
        while True:
            async for ev in graph.astream(payload, conf, stream_mode="updates"):
                for node, upd in ev.items():
                    if node == "__interrupt__" or not isinstance(upd, dict):
                        continue
                    for m in upd.get("messages", []):
                        um = getattr(m, "usage_metadata", None) or {}
                        tokens["input"] += um.get("input_tokens", 0)
                        tokens["output"] += um.get("output_tokens", 0)
                        for c in getattr(m, "tool_calls", None) or []:
                            pending[c["id"]] = c
                        if m.type == "tool":
                            c = pending.pop(m.tool_call_id, {"name": m.name, "args": {}})
                            n += 1
                            emit({"type": "step", "n": n, "tool": c["name"], "args": c["args"],
                                  "args_summary": summarize(c["args"], 120),
                                  "result_summary": summarize(m.content),
                                  "declined": "reviewer_declined" in str(m.content)})
                    if tokens["input"] + tokens["output"] > a.max_tokens:
                        emit({"type": "error", "code": "token_limit", "tokens": tokens,
                              "message": f"Stopped: run used more than {a.max_tokens} tokens."})
                        return 3
            state = await graph.aget_state(conf)
            if not state.interrupts:
                break
            req = state.interrupts[0].value
            emit({"type": "interrupt", **req})
            line = sys.stdin.readline()
            try:
                ans = json.loads(line) if line.strip() else {}
            except json.JSONDecodeError:
                ans = {}
            approved = bool(ans.get("approved"))
            payload = Command(resume={"approved": approved,
                                      "reason": "" if approved else (ans.get("reason") or "reviewer declined")})
        emit({"type": "final", "note": str(state.values["messages"][-1].content), "tokens": tokens})
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--claim", required=True)
    p.add_argument("--prompt", default="Review claim {claim} and draft the decision note.")
    p.add_argument("--max-steps", type=int, default=60)
    p.add_argument("--max-tokens", type=int, default=int(os.environ.get("REIMBURSE_AGENT_MAX_TOKENS", "250000")))
    p.add_argument("--fake-script")
    a = p.parse_args()
    try:
        code = asyncio.run(run(a))
    except Exception as exc:  # report, never a traceback with env in it
        emit({"type": "error", "code": "exception", "message": f"{type(exc).__name__}: {exc}"})
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
