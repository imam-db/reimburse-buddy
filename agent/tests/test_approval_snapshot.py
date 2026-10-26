"""The approval binds to what the reviewer saw (fake tools / fake model, no key).

args_hash covers evidence_ids and human_decisions plus, for claim-affecting actions, the engine
snapshot; the executor re-runs check_claim and refuses when the approved basis changed.
"""

import uuid

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from reimburse_agent import audit as A
from reimburse_agent.graph import EVIDENCE_TOOL, build_graph
from reimburse_agent.mcp_client import mcp_tools

from test_approval_hardening import ENV, HERO
from test_graph_interrupt import Scripted, call

TOOL = EVIDENCE_TOOL
ARGS = {"claim_id": "CLM-2026-0110", "item_id": "IT-5", "gap_code": "receipt_missing",
        "evidence_ids": ["MSG-1"], "human_decisions": {"IT-3": "approve_capped"}}
CHECK = {"claim_id": "CLM-2026-0110", "decision": "needs_info", "total_approvable": "4840000"}


def _rid():
    return f"snapshot-{uuid.uuid4().hex[:8]}"


def _approve(log, run_id, action_id, tool, args, engine=None):
    log.append(A.make_event(run_id, "approval", action_id, actor="human:reviewer", action_id=action_id,
                            args_hash=A.args_hash(tool, args, engine=engine),
                            payload={"tool": tool, "args_only_hash": A.args_hash(tool, args),
                                     **({"engine": engine} if engine is not None else {})}))


def _events(log, run_id, types=None):
    return [e for e in log.list(run_id) if types is None or e["event_type"] in types]


# --- hash covers the approved basis ---------------------------------------------------------------------
def test_args_hash_differs_when_evidence_or_decisions_differ():
    base = {"claim_id": "CLM-1", "item_id": "IT-1", "gap_code": "receipt_missing"}
    h = A.args_hash(TOOL, base)
    assert h != A.args_hash(TOOL, {**base, "evidence_ids": ["MSG-1"]})
    assert h != A.args_hash(TOOL, {**base, "human_decisions": {"IT-1": "approve_full"}})
    assert h == A.args_hash(TOOL, {**base, "action_id": "a1"})  # action_id stays excluded


def test_evidence_ids_are_compared_as_a_set_not_an_order():
    base = {"claim_id": "CLM-1", "item_id": "IT-1", "gap_code": "receipt_missing"}
    assert A.args_hash(TOOL, {**base, "evidence_ids": ["MSG-2", "MSG-1"]}) == \
        A.args_hash(TOOL, {**base, "evidence_ids": ["MSG-1", "MSG-2"]})
    assert A.args_hash(TOOL, {**base, "evidence_ids": ["MSG-1"]}) != \
        A.args_hash(TOOL, {**base, "evidence_ids": ["MSG-2"]})


def test_engine_snapshot_is_part_of_the_approval_hash():
    engine = A.engine_snapshot(CHECK, ARGS["evidence_ids"], ARGS["human_decisions"])
    assert engine == {"status": "needs_info", "approvable_amount": "4840000",
                      "evidence_ids": ["MSG-1"], "human_decisions": {"IT-3": "approve_capped"}}
    snap = A.approval_snapshot(TOOL, ARGS, engine)
    assert snap["args"]["evidence_ids"] == ["MSG-1"]
    assert snap["engine"] == engine
    assert A.args_hash(TOOL, ARGS, engine=engine) != A.args_hash(TOOL, ARGS)
    assert A.args_hash(TOOL, ARGS, engine=engine) != \
        A.args_hash(TOOL, ARGS, engine={**engine, "status": "approve"})


# --- executor refuses a changed basis -------------------------------------------------------------------
async def test_executor_refuses_changed_evidence_ids_and_runs_nothing():
    log, run_id, action_id = A.JsonlAuditLog(), _rid(), "a1"
    engine = A.engine_snapshot(CHECK, ARGS["evidence_ids"], ARGS["human_decisions"])
    _approve(log, run_id, action_id, TOOL, ARGS, engine)
    runs = []

    async def run(args):
        runs.append(args)
        return {"ok": True}

    async def engine_check(args, approved_engine):
        return approved_engine

    changed = {**ARGS, "evidence_ids": ["MSG-2"]}
    res = await A.execute_approved(log, run_id, action_id, TOOL, changed, run, engine_check=engine_check)
    assert res["status"] == "refused" and "args differ" in res["reason"]
    assert runs == []
    assert [e["event_type"] for e in _events(log, run_id)] == ["approval", "refusal"]


async def test_executor_refuses_changed_human_decisions_and_runs_nothing():
    log, run_id, action_id = A.JsonlAuditLog(), _rid(), "a1"
    engine = A.engine_snapshot(CHECK, ARGS["evidence_ids"], ARGS["human_decisions"])
    _approve(log, run_id, action_id, TOOL, ARGS, engine)
    runs = []

    async def run(args):
        runs.append(args)
        return {"ok": True}

    async def engine_check(args, approved_engine):
        return approved_engine

    changed = {**ARGS, "human_decisions": {"IT-3": "approve_full"}}
    res = await A.execute_approved(log, run_id, action_id, TOOL, changed, run, engine_check=engine_check)
    assert res["status"] == "refused" and "args differ" in res["reason"]
    assert runs == []


@pytest.mark.parametrize("changed", [
    {"status": "reject", "approvable_amount": "0"},
    {"status": "needs_info", "approvable_amount": "1200000"},
])
async def test_executor_refuses_when_the_engine_result_changed(changed):
    log, run_id, action_id = A.JsonlAuditLog(), _rid(), "a1"
    engine = A.engine_snapshot(CHECK, ARGS["evidence_ids"], ARGS["human_decisions"])
    _approve(log, run_id, action_id, TOOL, ARGS, engine)
    runs = []

    async def run(args):
        runs.append(args)
        return {"ok": True}

    async def engine_check(args, approved_engine):
        return {**approved_engine, **changed}

    res = await A.execute_approved(log, run_id, action_id, TOOL, ARGS, run, engine_check=engine_check)
    assert res["status"] == "refused" and res["reason"] == A.ENGINE_DRIFT
    assert runs == []
    refusals = _events(log, run_id, {"refusal"})
    assert len(refusals) == 1 and refusals[0]["payload"]["reason"] == A.ENGINE_DRIFT
    assert refusals[0]["action_id"] == action_id


async def test_executor_runs_once_when_args_and_engine_result_match():
    log, run_id, action_id = A.JsonlAuditLog(), _rid(), "a1"
    engine = A.engine_snapshot(CHECK, ARGS["evidence_ids"], ARGS["human_decisions"])
    _approve(log, run_id, action_id, TOOL, ARGS, engine)
    runs = []

    async def run(args):
        runs.append(args)
        return {"ok": True}

    async def engine_check(args, approved_engine):
        return approved_engine

    first = await A.execute_approved(log, run_id, action_id, TOOL, ARGS, run, engine_check=engine_check)
    second = await A.execute_approved(log, run_id, action_id, TOOL, ARGS, run, engine_check=engine_check)
    assert first["status"] == "executed" and second["status"] == "duplicate"
    assert runs == [ARGS]
    ex = _events(log, run_id, {"execution"})
    assert len(ex) == 1 and ex[0]["args_hash"] == A.args_hash(TOOL, ARGS, engine=engine)
    assert A.unmatched_executions(log.list(run_id)) == []


async def test_executor_refuses_when_the_engine_cannot_be_rechecked():
    log, run_id, action_id = A.JsonlAuditLog(), _rid(), "a1"
    engine = A.engine_snapshot(CHECK, ARGS["evidence_ids"], ARGS["human_decisions"])
    _approve(log, run_id, action_id, TOOL, ARGS, engine)
    runs = []

    async def run(args):
        runs.append(args)
        return {"ok": True}

    res = await A.execute_approved(log, run_id, action_id, TOOL, ARGS, run)
    assert res["status"] == "refused" and res["reason"] == A.ENGINE_DRIFT
    assert runs == []
    assert [e["event_type"] for e in _events(log, run_id)] == ["approval", "refusal"]


async def test_executor_reruns_the_engine_with_the_executed_args():
    """The engine basis is the executed args themselves (claim_id/evidence/decisions), not a copy of
    the approved snapshot."""
    log, run_id, action_id = A.JsonlAuditLog(), _rid(), "a1"
    engine = A.engine_snapshot(CHECK, ARGS["evidence_ids"], ARGS["human_decisions"])
    _approve(log, run_id, action_id, TOOL, ARGS, engine)
    runs, seen = [], []

    async def run(args):
        runs.append(args)
        return {"ok": True}

    async def engine_check(args, _approved_engine):
        seen.append(args)
        return A.engine_snapshot(CHECK, args.get("evidence_ids"), args.get("human_decisions"))

    res = await A.execute_approved(log, run_id, action_id, TOOL, ARGS, run, engine_check=engine_check)
    assert res["status"] == "executed"
    assert seen == [ARGS] and runs == [ARGS]


# --- the graph stages the full args and engine snapshot before the interrupt -----------------------------
async def test_graph_stages_full_args_and_engine_before_the_reviewer_is_asked():
    script = [call("check_claim", {"claim_id": HERO, "evidence_ids": ["MSG-5201"]}, 1),
              call(TOOL, {"claim_id": HERO, "item_id": "IT-5", "gap_code": "receipt_missing"}, 2),
              AIMessage(content="done")]
    log = A.JsonlAuditLog()
    async with mcp_tools(extra_env=ENV) as (_s, tools):
        g = build_graph(Scripted(messages=iter(script)), tools, checkpointer=InMemorySaver(),
                        run_claim=HERO, audit=log)
        conf = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 60}
        payload = {"messages": [("user", "go")], "declined": {},
                   "reviewer_decisions": {"IT-3": "approve_capped"}, "decisions_source": "form"}
        await g.ainvoke(payload, conf)
        st = await g.aget_state(conf)
        assert st.interrupts
        req = st.interrupts[0].value
        assert req["tool"] == TOOL
        # Filled before the prompt: the reviewer's decision and the evidence the latest check used.
        assert req["args"]["human_decisions"] == {"IT-3": "approve_capped"}
        assert req["args"]["evidence_ids"] == ["MSG-5201"]
        assert req["args"]["claim_id"] == HERO and req["args"]["item_id"] == "IT-5"
        assert req["engine"]["status"] and "approvable_amount" in req["engine"]
        assert req["engine"]["evidence_ids"] == req["args"]["evidence_ids"]
        assert req["engine"]["human_decisions"] == req["args"]["human_decisions"]
        staged = [e for e in log.list() if e["event_type"] == "staged_action"][0]
        assert staged["payload"]["args"] == req["args"]
        assert staged["payload"]["engine"] == req["engine"]
        assert staged["args_hash"] == A.args_hash(TOOL, req["args"], engine=req["engine"])
        await g.ainvoke(Command(resume={"approved": True, "tool_call_id": req["tool_call_id"]}), conf)
    st = await g.aget_state(conf)
    assert [w["tool"] for w in st.values["writes"]] == [TOOL]
    ex = [e for e in log.list() if e["event_type"] == "execution"]
    assert len(ex) == 1
    assert ex[0]["payload"]["args"] == req["args"]
    assert ex[0]["payload"]["args"]["evidence_ids"] == ["MSG-5201"]
    assert A.unmatched_executions(log.list()) == []
