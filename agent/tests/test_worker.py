"""investigate_worker.py JSON-lines protocol, with a scripted model (no key)."""
import json
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]


def run_worker(args, stdin=""):
    p = subprocess.run([sys.executable, "investigate_worker.py", *args], cwd=AGENT, input=stdin,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]


def test_no_key_emits_message_and_exits():
    code, evs = run_worker(["--claim", "CLM-2026-0110"])
    assert code == 2 and evs == [{"type": "error", "code": "no_key", "message": evs[0]["message"]}]
    assert "DEEPSEEK_API_KEY" in evs[0]["message"]


def test_interrupt_then_approve(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps([
        {"tool_calls": [{"name": "update_claim_status", "args": {"claim_id": "CLM-2026-0110", "status": "on_hold"}, "id": "w1"}]},
        {"content": "done"}]))
    code, evs = run_worker(["--claim", "CLM-2026-0110", "--fake-script", str(f)], stdin='{"approved": true, "tool_call_id": "w1"}\n')
    types = [e["type"] for e in evs]
    assert code == 0 and types == ["start", "interrupt", "step", "final"]
    assert evs[1]["tool"] == "update_claim_status" and not evs[2]["declined"]


def test_token_limit_guard(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps([{"content": "x"}]))
    code, evs = run_worker(["--claim", "C", "--fake-script", str(f), "--max-tokens", "-1"])
    assert code == 3 and evs[-1]["code"] == "token_limit"


WRITE = {"tool_calls": [{"name": "update_claim_status", "args": {"claim_id": "CLM-2026-0110", "status": "on_hold"},
                         "id": "w1"}]}


def script(tmp_path, *msgs):
    f = tmp_path / "s.json"
    f.write_text(json.dumps(list(msgs)))
    return str(f)


def test_mismatched_and_malformed_answers_are_rejected(tmp_path):
    stdin = ('{"approved": true, "tool_call_id": "stale"}\n' 'not json\n' '{"approved": true}\n'
             '{"approved": false, "tool_call_id": "w1", "reason": "no"}\n')
    code, evs = run_worker(["--claim", "CLM-2026-0110", "--fake-script", script(tmp_path, WRITE, {"content": "ok"})],
                           stdin=stdin)
    rej = [e for e in evs if e["type"] == "answer_rejected"]
    assert code == 0 and len(rej) == 3 and rej[0]["tool_call_id"] == "stale"
    step = next(e for e in evs if e["type"] == "step")
    assert step["declined"]  # only the matching answer (a decline) was applied


def test_closed_stdin_cancels(tmp_path):
    p = subprocess.Popen([sys.executable, "investigate_worker.py", "--claim", "CLM-2026-0110", "--approval-deadline",
                          "1", "--fake-script", script(tmp_path, WRITE, {"content": "ok"})], cwd=AGENT,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate(timeout=120)  # stdin closes right away -> treated as cancel
    evs = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    assert evs[-1]["code"] == "cancelled"


def test_no_answer_within_deadline_is_declined(tmp_path):
    p = subprocess.Popen([sys.executable, "investigate_worker.py", "--claim", "CLM-2026-0110", "--approval-deadline",
                          "1", "--fake-script", script(tmp_path, WRITE, {"content": "ok"})], cwd=AGENT,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        out = ""
        for line in p.stdout:  # keep stdin open, never answer
            out += line
            if '"final"' in line or '"error"' in line:
                break
        p.wait(timeout=60)
    finally:
        p.kill()
    evs = [json.loads(l) for l in out.splitlines() if l.startswith("{")]
    types = [e["type"] for e in evs]
    assert "auto_declined" in types and types[-1] == "final"
    assert next(e for e in evs if e["type"] == "step")["declined"]


def test_cancel_line_stops_run(tmp_path):
    code, evs = run_worker(["--claim", "CLM-2026-0110", "--fake-script", script(tmp_path, WRITE, {"content": "ok"})],
                           stdin='{"cancel": true}\n')
    assert code == 5 and evs[-1]["code"] == "cancelled"
    assert not any(e["type"] == "step" for e in evs)


def test_run_deadline(tmp_path):
    code, evs = run_worker(["--claim", "CLM-2026-0110", "--run-deadline", "0.01",
                            "--fake-script", script(tmp_path, {"content": "x"})])
    assert code == 4 and evs[-1]["code"] == "deadline"
