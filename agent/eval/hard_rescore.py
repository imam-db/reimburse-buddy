"""Re-score saved hard-eval results offline (no model calls) with the current scoring and engine.

    python -m eval.hard_rescore eval/results/hard-*.json

Old result files did not keep tool outputs, so the run's tool calls are replayed in-process against a
fresh copy of the case's fixture data (deterministic, same tools the MCP server exposes) to rebuild
them. Writes <name>-rescored.json next to the input; the original file is left untouched."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

from reimburse_agent.graph import GAP_SEARCH_TOOL, GapSearchArgs

from .hard_cases import CASES
from .hard_score import score
from .run_hard_eval import summarize

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from reimburse_buddy import server  # noqa: E402
from reimburse_buddy.data_store import DataStore  # noqa: E402

TOOLS = {n: getattr(server, n) for n in (
    "list_claims", "get_claim", "get_policy", "search_reimburse_mailbox", "get_calendar", "find_approval_memo",
    "check_claim", "triage_claims", "approve_cleared_batch", "draft_decision", "send_message",
    "request_missing_evidence", "request_approval", "update_claim_status", "get_outbox")}


def rebuild_tool_texts(case, rec) -> list[str]:
    server.store = DataStore(case.data)
    out = []
    for c in rec["calls"]:
        if c.get("declined"):
            out.append(json.dumps({"error": "reviewer_declined"}))
            continue
        fn = TOOLS.get(c["name"])
        if fn is None or c.get("error"):
            out.append(json.dumps({"error": "tool error"}))
            continue
        try:
            res = fn(**(c.get("args") or {}))
            out.append(res if isinstance(res, str) else json.dumps(res, default=str, ensure_ascii=False))
        except Exception as exc:  # the live run saw an error too
            out.append(json.dumps({"error": str(exc)}))
    return out


def rescore(path: Path) -> dict:
    d = json.loads(path.read_text())
    cases = {c.id: c for c in CASES}
    for r in d["runs"]:
        case = cases[r["case"]]
        for c in r["calls"]:  # graph-filled args were logged as malformed before review 5
            if c.get("model_args") is not None:
                c["filled_by_graph"] = True
                if c["name"] == GAP_SEARCH_TOOL:
                    try:
                        jsonschema.validate(c["model_args"], GapSearchArgs.model_json_schema())
                        c["schema_ok"] = True
                    except jsonschema.ValidationError:
                        c["schema_ok"] = False
        texts = r.get("tool_texts") or rebuild_tool_texts(case, r)
        srv = {"changed_status": r.get("server_changed", []), "outbox": r["metrics"].get("outbox", 0)}
        r["metrics_before"] = {k: r["metrics"].get(k) for k in ("pass", "why", "invented_rp")}
        r["metrics"] = score(case, r, r["final"], texts, srv)
    d["summary_before"] = {k: d["summary"].get(k) for k in ("pass", "invented_rp", "correct_stop")}
    d["summary"] = summarize(d["summary"]["model"], d["runs"])
    d["rescored"] = ("offline, tiered scoring (safety gate / correctness / efficiency), current engine; review 5: "
                     "final-set evidence precision, model_followed_explicit_instruction, filled_by_graph")
    out = path.with_name(path.stem + "-rescored.json")
    out.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=str))
    return d


if __name__ == "__main__":
    for p in sys.argv[1:]:
        d = rescore(Path(p))
        s = d["summary"]
        print(p, "before", d["summary_before"], "after", {k: s.get(k) for k in ("pass", "safety", "false_approvals", "correct",
                                                                            "efficiency_avg", "invented_rp",
                                                                            "evidence_precision_avg",
                                                                            "final_evidence_precision_avg",
                                                                            "model_followed_explicit_instruction")})
        for row in s["cases"]:
            print("  ", row["case"], row["pass"], "safety", row["safety"], "false_appr", row.get("false_approvals"), "correct", row["correct"],
                  "eff", row["efficiency_avg"], "prec", row["evidence_precision_avg"],
                  "final_prec", row["final_evidence_precision_avg"], "followed", row["model_followed_explicit_instruction"],
                  row["why"])
