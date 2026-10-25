"""Offline token estimate for batch case 3 (no key, no network): replay a saved live run's model tool calls,
per exception claim, through the batch graph with a scripted model, and sum a chars/4 estimate of every
prompt the model would have been sent. Compares the review-5 sub-run (slim=False) with the slim one.

    python -m eval.estimate_batch_tokens eval/results/hard-deepseek-flash-20261025T0210Z.json [run_index]
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent.batch import SUBRUN_TOKEN_BUDGET, build_batch_graph
from reimburse_agent.graph import estimate_tokens
from reimburse_agent.mcp_client import mcp_tools

from .hard_cases import CASES

SERVED = {"triage_claims", "get_policy"}


PER: dict[str, int] = {}
CALLS: list[int] = []  # estimated tokens per model call (module-level: pydantic copies field defaults)


class Counting(GenericFakeChatModel):
    """Serves each sub-run its own claim's replayed calls (keyed by the claim in the sub-run prompt), so a
    sub-run that ends early (auto-draft) never spills its leftovers into the next claim."""

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        first = next((str(m.content) for m in messages if m.type == "human"), "")
        cid = first.split()[2] if first.startswith("Review claim") else "parent"
        queue = SCRIPTS.get(cid) or []
        msg = queue.pop(0) if queue else AIMessage(content=f"{cid} done.")
        n = estimate_tokens(messages) + estimate_tokens([msg])
        CALLS.append(n)
        PER[cid] = PER.get(cid, 0) + n
        return ChatResult(generations=[ChatGeneration(message=msg)])


SCRIPTS: dict[str, list] = {}


def scripts_from(rec, drop_served: bool) -> dict[str, list]:
    """Live model tool calls grouped by the claim they were about (claimless calls join the current claim)."""
    out: dict[str, list] = {}
    cur, i = None, 0
    for c in rec["calls"]:
        if c.get("auto") or c.get("by"):
            continue
        args = c.get("model_args") or c["args"]
        cur = args.get("claim_id") or cur
        if cur is None or (drop_served and c["name"] in SERVED):
            continue
        i += 1
        out.setdefault(cur, []).append(AIMessage(content="", tool_calls=[{"name": c["name"], "args": args, "id": f"r{i}"}]))
    return out


async def estimate(rec, slim: bool, drop_served: bool, budget):
    case = next(c for c in CASES if c.id == "3")
    CALLS.clear(); PER.clear()
    SCRIPTS.clear(); SCRIPTS.update(scripts_from(rec, drop_served))
    model = Counting(messages=iter([]))
    env = {"REIMBURSE_ENABLE_EVIDENCE_TOOL": "1", "REIMBURSE_BUDDY_DATA": str(case.data)}
    async with mcp_tools(extra_env=env) as (_s, tools):
        g = build_batch_graph(model, tools, case.batch, checkpointer=InMemorySaver(), slim=slim,
                              subrun_token_budget=budget)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 200}
        payload = {"messages": [("user", case.prompt)]}
        while True:
            await g.ainvoke(payload, conf)
            st = await g.aget_state(conf)
            if not st.interrupts:
                break
            req = st.interrupts[0].value
            payload = Command(resume={"approved": True, "tool_call_id": req["tool_call_id"]})
    v = st.values
    return {"llm_calls": len(CALLS), "est_tokens": sum(CALLS),
            "claims_done": [s["claim_id"] for s in v.get("summaries", [])],
            "per_claim": dict(PER), "max_call": max(CALLS, default=0), "budget_stops": sum("used its model budget" in str(s.get("stopped_with"))
                                                                   for s in v.get("summaries", [])),
            "drafted": sum(bool(s["draft"]) for s in v.get("summaries", []))}


async def main(path, idx):
    d = json.load(open(path))
    rec = [r for r in d["runs"] if r["case"] == "3"][idx]
    print("live:", rec["usage"])
    for name, slim, drop, budget in [("review-5 sub-run", False, False, None),
                                     ("slim, model still calls triage/policy (served)", True, False, None),
                                     ("slim, model skips triage/policy", True, True, None),
                                     ("slim + default per-sub-run budget", True, False, SUBRUN_TOKEN_BUDGET)]:
        print(f"{name}: {await estimate(rec, slim, drop, budget)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 1))
