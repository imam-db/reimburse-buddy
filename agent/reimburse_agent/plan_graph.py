"""Challenge B office flow: request -> who/what/when/where plan -> one confirmation -> mock tools.

    start -> propose -> resolve --(question)--> clarify -> resolve
                           \\--> schedule (stage) --(invalid)--> END
                                       \\--> gate -> execute --(reject with note)--> propose (v+1) ...

* start freezes `run_started_at` once (checkpointed), scans the attached documents for injected
  instructions (notices + withheld spans), and logs the request.
* resolve asks at most one question at a time, only when a name has 0 or 2+ directory matches (code-decided).
* schedule computes slots from the frozen start time with the operating-rules tool, builds the plan, runs
  validate_plan (open questions, argument sources, rules), saves it for /admin/plans/<run_id> and logs
  staged_action per action. It runs BEFORE the gate, so a resume never recomputes a time.
* gate calls interrupt() once per plan and resumes with {"decisions": {action_id: "approve"|"reject"},
  "notes": {action_id: text}, "actor": "human:<name>"}. It only records decisions (idempotent event keys).
* execute is the only node that touches the mock tools; it re-validates every approved action and requires an
  approval in the audit log with the same action_id and args_hash, and no earlier execution.
* A rejection with a note supersedes that action and sends the note back for a new plan version (new
  action_ids). Old approvals cannot be replayed: their action_ids are not in the new plan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import injection as inj
from .audit import AuditLog, JsonlAuditLog, args_hash, make_event
from .office import plan as P
from .office import rules as R
from .office.directory import Directory
from .office.store import PlanStore
from .office.tools import MockOffice

MAX_VERSIONS = 5


class PlanState(TypedDict, total=False):
    run_id: str
    request: str
    requester: str
    documents: list[dict]
    now: str  # optional input: the run's start time (tests / replays); otherwise the clock is read once
    run_started_at: str
    version: int
    intents: list[dict]
    answers: dict
    question: dict | None
    notices: list[dict]
    flagged_spans: list[str]
    plan: dict
    operating_times: list[str]
    staging_errors: list[str]
    decisions: dict
    notes: dict
    superseded: list[str]
    results: dict
    status: str


Proposer = Callable[[str, list[dict]], list[dict]]


def build_plan_graph(*, log: AuditLog | None = None, directory: Directory | None = None,
                     rules: R.Rules | None = None, office: MockOffice | None = None,
                     store: PlanStore | None = None, proposer: Proposer | None = None,
                     clock: Callable[[], datetime] | None = None, checkpointer=None):
    log = log if log is not None else JsonlAuditLog()
    directory = directory or Directory.load()
    rules = rules or R.default_rules()
    office = office or MockOffice(directory)
    proposer = proposer or P.read_request
    clock = clock or (lambda: datetime.now(timezone.utc))

    def rec(run_id, etype, ref, **kw) -> bool:
        return log.append(make_event(run_id, etype, ref, **kw))

    def start(s: PlanState) -> dict:
        if s.get("run_started_at"):
            return {}
        run_id = s["run_id"]
        now = R.parse_iso(s["now"]) if s.get("now") else clock()
        rec(run_id, "request", "request", actor="system", payload={"request": s["request"],
            "requester": s["requester"], "run_started_at": R.iso(now)},
            sources=[d["id"] for d in s.get("documents", [])])
        notices, spans, docs = [], [], []
        for d in s.get("documents", []):
            hits = inj.scan(d["text"], d["id"], directory_domains=P.DIRECTORY_DOMAINS)
            for h in hits:
                rec(run_id, "injection_detected", f"{d['id']}:{h['rule_id']}:{h['start']}", actor="system",
                    payload={k: h[k] for k in ("source_id", "rule_id", "severity", "label", "detector_version")}
                    | {"offsets": [h["start"], h["end"]], "snippet": h["masked"]}, sources=[d["id"]])
            n = inj.notice(hits, subject=("rencana ini", "this plan"))
            if n:
                notices.append(n)
                spans += [h["snippet"] for h in hits if h["severity"] == inj.HIGH]
            docs.append({**d, "model_text": inj.withhold(d["text"], hits)})
        return {"run_started_at": R.iso(now), "version": 1, "answers": {}, "notices": notices,
                "flagged_spans": spans, "documents": docs, "superseded": [], "results": {}}

    def propose(s: PlanState) -> dict:
        if s.get("intents") and s.get("notes"):  # re-plan: only the rejected-with-note actions come back
            old = {a["action_id"]: i for i, a in enumerate(s["plan"]["actions"])}
            intents = [P.revise_intent(s["intents"][old[aid]], note) for aid, note in s["notes"].items()
                       if aid in old and note]
            return {"intents": intents, "notes": {}, "decisions": {}}
        model_docs = [{"id": d["id"], "text": d.get("model_text", d["text"])} for d in s.get("documents", [])]
        return {"intents": proposer(s["request"], model_docs)}

    def resolve(s: PlanState) -> dict:
        q = P.who_question(s["intents"], directory, s.get("answers", {}))
        return {"question": q}

    def clarify(s: PlanState) -> dict:
        q = s["question"]
        rec(s["run_id"], "clarification", f"v{s['version']}:{q['id']}", payload=q)
        answer = interrupt({"kind": "clarify", "run_id": s["run_id"], "question": q})
        emp = P.valid_answer(q, answer.get("answer") if isinstance(answer, dict) else answer, directory)
        if not emp:
            return {}  # not one of the options: resolve asks the same single question again
        return {"answers": {**s.get("answers", {}), q["name"]: emp}}

    def schedule(s: PlanState) -> dict:
        run_id, version = s["run_id"], s["version"]
        now = R.parse_iso(s["run_started_at"])
        resolved = P.resolve_names(s["intents"], directory, s.get("answers", {}))
        plan, offered = P.build_plan(run_id=run_id, version=version, request=s["request"],
                                     requester=s["requester"], intents=s["intents"], resolved=resolved, now=now,
                                     directory=directory, rules=rules, documents=s.get("documents"),
                                     notices=s.get("notices"), flagged_spans=s.get("flagged_spans"),
                                     superseded=s.get("superseded"))
        errors = P.validate_plan(plan, directory, rules, set(offered))
        rec(run_id, "plan", plan["plan_id"], payload={"plan_id": plan["plan_id"], "actions": [
            {k: a[k] for k in ("action_id", "tool", "args", "why")} for a in plan["actions"]],
            "assumptions": plan["assumptions"], "errors": errors}, sources=[d["id"] for d in s.get("documents", [])])
        if errors:
            plan["staging_errors"] = list(errors)
            rec(run_id, "refusal", f"{plan['plan_id']}:staging", actor="system", payload={"errors": errors})
            if store:
                store.save_plan(plan)  # read-only: the page shows why nothing was staged, with no approve control
            return {"plan": plan, "operating_times": offered, "staging_errors": errors, "status": "refused"}
        for a in plan["actions"]:
            rec(run_id, "staged_action", a["action_id"], action_id=a["action_id"], args_hash=a["args_hash"],
                payload={"tool": a["tool"], "args": a["args"], "plan_id": plan["plan_id"],
                         "influenced_fields": sorted(a["influenced_fields"])}, sources=a["why"])
        if store:
            store.save_plan(plan)
        return {"plan": plan, "operating_times": offered, "staging_errors": [], "status": "awaiting_approval"}

    def gate(s: PlanState) -> dict:
        plan = s["plan"]
        answer = interrupt({"kind": "plan_approval", "run_id": s["run_id"], "plan_id": plan["plan_id"],
                            "page": f"/admin/plans/{s['run_id']}", "notices": [n["text"] for n in plan["notices"]],
                            "actions": [{k: a[k] for k in ("action_id", "tool", "args", "args_hash", "when",
                                                           "influenced_fields")} for a in plan["actions"]],
                            "assumptions": plan["assumptions"]})
        answer = answer if isinstance(answer, dict) else {}
        actor = answer.get("actor") if isinstance(answer.get("actor"), str) else "human:reviewer"
        raw, notes = answer.get("decisions") or {}, answer.get("notes") or {}
        current = {a["action_id"]: a for a in plan["actions"]}
        decisions, kept_notes = {}, {}
        for aid, d in raw.items():
            if aid not in current:
                rec(s["run_id"], "refusal", f"{plan['plan_id']}:unknown:{aid}", actor="system",
                    payload={"why": "decision for an action that is not in the current plan (stale or superseded)",
                             "action_id": aid, "decision": str(d)})
        for aid, a in current.items():
            d = raw.get(aid)
            d = d if d in ("approve", "reject") else "reject"  # no decision = nothing runs
            decisions[aid] = d
            note = str(notes.get(aid) or "").strip()[:500]
            if d == "approve":
                rec(s["run_id"], "approval", aid, actor=actor, action_id=aid, args_hash=a["args_hash"],
                    payload={"tool": a["tool"], "plan_id": plan["plan_id"]})
            else:
                rec(s["run_id"], "rejection", aid, actor=actor, action_id=aid, args_hash=a["args_hash"],
                    payload={"tool": a["tool"], "plan_id": plan["plan_id"], "note": note,
                             "explicit": aid in raw})
                if note:
                    kept_notes[aid] = note
        return {"decisions": decisions, "notes": kept_notes}

    def approved(run_id: str, aid: str, h: str) -> bool:
        evs = [x for x in log.list(run_id) if x.get("action_id") == aid]
        return (any(x["event_type"] == "approval" and x["args_hash"] == h for x in evs)
                and not any(x["event_type"] in ("execution", "superseded", "rejection", "expired") for x in evs))

    def execute(s: PlanState) -> dict:
        plan, run_id = s["plan"], s["run_id"]
        results = dict(s.get("results", {}))
        for a in plan["actions"]:
            aid = a["action_id"]
            if s["decisions"].get(aid) != "approve":
                continue
            h = args_hash(a["tool"], a["args"])
            bad = P.validate_action(a, directory, rules, set(s.get("operating_times", [])))
            if bad or not approved(run_id, aid, h):
                rec(run_id, "refusal", f"{aid}:executor", actor="system", action_id=aid, args_hash=h,
                    payload={"errors": bad or ["no matching approval in the audit log"]})
                continue
            out = office.call(a["tool"], aid, a["args"])
            rec(run_id, "execution", aid, actor="system", action_id=aid, args_hash=h,
                payload={"tool": a["tool"], "result_id": out["id"]})
            results[aid] = out["id"]
        sup = list(s.get("superseded", []))
        for aid in s.get("notes", {}):
            rec(run_id, "superseded", aid, actor="system", action_id=aid,
                payload={"reason": "rejected with a note; the agent proposes a new plan", "note": s["notes"][aid]})
            sup.append(aid)
        more = bool(s.get("notes")) and s["version"] < MAX_VERSIONS
        return {"results": results, "superseded": sup, "version": s["version"] + (1 if more else 0),
                "status": "replanning" if more else "done"}

    g = StateGraph(PlanState)
    for name, fn in (("start", start), ("propose", propose), ("resolve", resolve), ("clarify", clarify),
                     ("schedule", schedule), ("gate", gate), ("execute", execute)):
        g.add_node(name, fn)
    g.add_edge(START, "start")
    g.add_edge("start", "propose")
    g.add_edge("propose", "resolve")
    g.add_conditional_edges("resolve", lambda s: "clarify" if s.get("question") else "schedule",
                            ["clarify", "schedule"])
    g.add_edge("clarify", "resolve")
    g.add_conditional_edges("schedule", lambda s: END if s.get("staging_errors") else "gate", ["gate", END])
    g.add_edge("gate", "execute")
    g.add_conditional_edges("execute", lambda s: "propose" if s.get("status") == "replanning" else END,
                            ["propose", END])
    return g.compile(checkpointer=checkpointer or MemorySaver())


def pending(graph, config) -> dict | None:
    """The open interrupt payload (clarify or plan_approval), or None when the run is finished."""
    st = graph.get_state(config)
    for t in st.tasks:
        for it in getattr(t, "interrupts", ()):
            return it.value
    return None


def run_input(run_id: str, request: str, requester: str, documents: list[dict] | None = None,
              now: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"run_id": run_id, "request": request, "requester": requester,
                         "documents": documents or []}
    if now:
        d["now"] = now
    return d
