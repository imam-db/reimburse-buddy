"""investigate_worker.py JSON-lines protocol, with a scripted model (no key)."""
import json
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
HERO = "CLM-2026-0110"
WRITE = {"tool_calls": [{"name": "update_claim_status", "args": {"claim_id": HERO, "status": "on_hold"}, "id": "w1"}]}


def run_worker(args, stdin=""):
    p = subprocess.run([sys.executable, "investigate_worker.py", *args], cwd=AGENT, input=stdin,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]


def drive(args, on_interrupt, timeout=120, on_question=None):
    """Run the worker; on each interrupt event, write the lines on_interrupt(ev) returns (None = stay silent)."""
    p = subprocess.Popen([sys.executable, "investigate_worker.py", *args], cwd=AGENT, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    evs = []
    try:
        for line in p.stdout:
            if not line.startswith("{"):
                continue
            ev = json.loads(line)
            evs.append(ev)
            if ev["type"] == "interrupt":
                for out in on_interrupt(ev) or []:
                    p.stdin.write(json.dumps(out) + "\n")
                    p.stdin.flush()
            if ev["type"] == "question" and on_question:
                for out in on_question(ev) or []:
                    p.stdin.write(json.dumps(out) + "\n")
                    p.stdin.flush()
            if ev["type"] in ("final", "error"):
                break
        p.wait(timeout=timeout)
    finally:
        p.kill()
    return p.returncode, evs


def script(tmp_path, *msgs):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(list(msgs)))
    return str(f)


def test_no_key_emits_message_and_exits():
    code, evs = run_worker(["--claim", HERO])
    assert code == 2 and evs == [{"type": "error", "code": "no_key", "message": evs[0]["message"]}]
    assert "DEEPSEEK_API_KEY" in evs[0]["message"]


def test_interrupt_then_approve_with_worker_id_and_rule_engine_view(tmp_path):
    code, evs = drive(["--claim", HERO, "--fake-script", script(tmp_path, WRITE, {"content": "done"})],
                      lambda ev: [{"approval_id": ev["approval_id"], "approved": True}])
    types = [e["type"] for e in evs]
    assert code == 0 and types == ["start", "interrupt", "answer_applied", "step", "final"]
    intr = evs[1]
    assert intr["approval_id"] != intr["tool_call_id"] and len(intr["approval_id"]) == 32
    assert intr["current_decision"]["total_approvable"] == "4840000"
    assert evs[2]["approved"] is True and not evs[3]["declined"]


def test_mismatched_and_malformed_answers_are_rejected(tmp_path):
    code, evs = drive(["--claim", HERO, "--fake-script", script(tmp_path, WRITE, {"content": "ok"})],
                      lambda ev: [{"approval_id": "stale", "approved": True}, {"approved": True},
                                  {"approval_id": ev["approval_id"], "approved": "yes"},
                                  {"approval_id": ev["approval_id"], "approved": False, "reason": "no"}])
    rej = [e for e in evs if e["type"] == "answer_rejected"]
    assert code == 0 and len(rej) == 3 and rej[0]["approval_id"] == "stale"
    assert next(e for e in evs if e["type"] == "step")["declined"]


def test_closed_stdin_cancels(tmp_path):
    p = subprocess.Popen([sys.executable, "investigate_worker.py", "--claim", HERO, "--approval-deadline",
                          "1", "--fake-script", script(tmp_path, WRITE, {"content": "ok"})], cwd=AGENT,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate(timeout=120)
    evs = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    assert evs[-1]["code"] == "cancelled"


def test_no_answer_within_deadline_is_declined(tmp_path):
    code, evs = drive(["--claim", HERO, "--approval-deadline", "1",
                       "--fake-script", script(tmp_path, WRITE, {"content": "ok"})], lambda ev: None)
    types = [e["type"] for e in evs]
    assert "auto_declined" in types and "answer_applied" not in types and types[-1] == "final"
    assert next(e for e in evs if e["type"] == "step")["declined"]


def test_run_clock_pauses_during_approval(tmp_path):
    # 2s approval wait > 1.5s run deadline: the run must still finish (the wait isn't agent time)
    import time

    def slow(ev):
        time.sleep(2)
        return [{"approval_id": ev["approval_id"], "approved": False}]
    code, evs = drive(["--claim", HERO, "--run-deadline", "1.5",
                       "--fake-script", script(tmp_path, WRITE, {"content": "ok"})], slow)
    assert code == 0 and evs[-1]["type"] == "final"


def test_cancel_line_stops_run(tmp_path):
    code, evs = run_worker(["--claim", HERO, "--fake-script", script(tmp_path, WRITE, {"content": "ok"})],
                           stdin='{"cancel": true}\n')
    assert code == 5 and evs[-1]["code"] == "cancelled"
    assert not any(e["type"] == "step" for e in evs)


def test_run_deadline(tmp_path):
    read = {"tool_calls": [{"name": "check_claim", "args": {"claim_id": HERO}, "id": "r1"}]}
    code, evs = run_worker(["--claim", HERO, "--run-deadline", "0.001",
                            "--fake-script", script(tmp_path, read, read, read, {"content": "x"})])
    assert code == 4 and evs[-1]["code"] == "deadline"


def test_token_limit_guard(tmp_path):
    code, evs = run_worker(["--claim", "C", "--fake-script", script(tmp_path, {"content": "x"}), "--max-tokens", "-1"])
    assert code == 3 and evs[-1]["code"] == "token_limit"


def test_token_report_carries_the_cache_split(tmp_path):
    """spend.cost_of() bills cache-hit input at its own (cheaper) price, so the worker has to report the
    three counters, not just a total."""
    code, evs = run_worker(["--claim", HERO, "--fake-script", script(tmp_path, {"content": "done"})])
    assert code == 0
    final = next(e for e in evs if e["type"] == "final")
    assert set(final["tokens"]) == {"input", "cache_hit", "output"}
    code, evs = run_worker(["--claim", "C", "--fake-script", script(tmp_path, {"content": "x"}), "--max-tokens", "-1"])
    assert set(evs[-1]["tokens"]) == {"input", "cache_hit", "output"}


def test_question_reply_carries_reviewer_decisions_into_the_graph(tmp_path):
    ask = {"content": "Should I cap the hotel IT-3?"}
    same = {"tool_calls": [{"name": "check_claim", "id": "k1", "args": {
        "claim_id": HERO, "human_decisions": {"IT-3": "approve_capped"}}}]}
    other = {"tool_calls": [{"name": "check_claim", "id": "k2", "args": {
        "claim_id": HERO, "human_decisions": {"IT-3": "approve_full"}}}]}
    first = {"tool_calls": [{"name": "check_claim", "id": "k0", "args": {"claim_id": HERO}}]}
    reply = lambda ev: [{"question_id": ev["question_id"], "reply": "",
                         "decisions": {"IT-3": "approve_capped", "bad key": "x", "IT-4": "approve_everything"}}]
    code, evs = drive(["--claim", HERO, "--fake-script", script(tmp_path, first, ask, same, other, {"content": "done"})],
                      lambda ev: None, on_question=reply)
    assert code == 0
    q = next(e for e in evs if e["type"] == "question")
    assert "IT-3" in {i["item_id"] for i in q["items"]}
    applied = next(e for e in evs if e["type"] == "reply_applied")
    assert applied["decisions"] == {"IT-3": "approve_capped"}  # malformed pairs dropped
    steps = [e for e in evs if e["type"] == "step"]
    assert "human_decisions_not_from_reviewer" not in steps[1]["result_summary"]  # matches the reviewer
    assert "human_decisions_not_from_reviewer" in steps[2]["result_summary"]  # model's own value refused


def test_no_question_without_question_mark(tmp_path):
    code, evs = run_worker(["--claim", HERO, "--fake-script", script(tmp_path, {"content": "All done."})])
    assert code == 0 and not any(e["type"] == "question" for e in evs)
