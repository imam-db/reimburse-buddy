"""CLI: uv run python run.py --claim CLM-2026-0110

Reads model settings from env (see reimburse_agent/config.py). Pauses for a y/n answer before
any write or decision tool. Use --auto-decline to stop at the first such pause (for scripted runs).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.config import build_chat_model, load_config
from reimburse_agent.graph import build_graph
from reimburse_agent.mcp_client import mcp_tools


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--claim", default="CLM-2026-0110")
    p.add_argument("--prompt", default="Review claim {claim} and draft the decision note.")
    p.add_argument("--auto-decline", action="store_true", help="decline every gated call without asking")
    p.add_argument("--max-steps", type=int, default=40)
    p.add_argument("--raw", action="store_true", help="send raw tool outputs (disable compact mode)")
    a = p.parse_args()

    cfg = load_config()
    print(f"model: {cfg!r}")
    async with mcp_tools() as (_session, tools):
        graph = build_graph(build_chat_model(cfg), tools, checkpointer=InMemorySaver(), compact=not a.raw)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": a.max_steps}
        payload: object = {"messages": [("user", a.prompt.format(claim=a.claim))], "declined": {}}
        tokens = {"input": 0, "output": 0}
        while True:
            async for ev in graph.astream(payload, conf, stream_mode="updates"):
                for node, upd in ev.items():
                    if node == "__interrupt__":
                        continue
                    for m in (upd or {}).get("messages", []):
                        um = getattr(m, "usage_metadata", None) or {}
                        tokens["input"] += um.get("input_tokens", 0)
                        tokens["output"] += um.get("output_tokens", 0)
                        for c in getattr(m, "tool_calls", []) or []:
                            print(f"-> {c['name']}({json.dumps(c['args'], ensure_ascii=False)})")
                        if m.type == "tool":
                            print(f"<- {m.name}: {str(m.content)[:200]}")
            state = await graph.aget_state(conf)
            if not state.interrupts:
                break
            req = state.interrupts[0].value
            print(f"\nINTERRUPT: approve {req['tool']}({json.dumps(req['args'], ensure_ascii=False)})?")
            if a.auto_decline:
                payload = Command(resume={"approved": False, "reason": "scripted run: auto-declined"})
                print("auto-declined")
            else:
                ok = input("[y/N] ").strip().lower() == "y"
                payload = Command(resume={"approved": ok, "tool_call_id": req["tool_call_id"],
                                          "reason": "" if ok else "reviewer said no"})
        print("\nFINAL:\n" + str(state.values["messages"][-1].content))
        print(f"\ntokens: {tokens}")


if __name__ == "__main__":
    asyncio.run(main())
