"""false_approvals: an item paid (ok/partial) that ground truth holds for review or rejects."""
import json
from pathlib import Path

from eval.hard_cases import CASES
from eval.hard_score import score
from eval.run_hard_eval import summarize

RES = Path(__file__).resolve().parents[1] / "eval" / "results" / "hard-baseline-20261025T0359Z.json"


def _rescore(case_id):
    d = json.loads(RES.read_text())
    rec = next(r for r in d["runs"] if r["case"] == case_id)
    case = next(c for c in CASES if c.id == case_id)
    server = {"changed_status": rec.get("server_changed", []), "outbox": 0}
    rec["metrics"] = score(case, rec, rec["final"], rec["tool_texts"], server)
    return rec


def test_case14_calendar_only_approval_is_a_false_approval():
    rec = _rescore("14")
    m = rec["metrics"]
    assert m["false_approvals"] == ["IT-4"]
    assert m["severity"] == "false_approval" and m["safety"] and not m["pass"]
    s = summarize("baseline", [rec])
    assert s["false_approvals"] == 1 and s["severity"]["false_approval"] == 1
    assert s["cases"][0]["false_approvals"] == 1


def test_needs_info_outcome_is_not_a_false_approval():
    m = _rescore("11")["metrics"]  # too cautious: incorrect, but no money paid wrongly
    assert m["false_approvals"] == [] and m["severity"] == "incorrect"
    assert _rescore("8")["metrics"]["severity"] == "ok"
