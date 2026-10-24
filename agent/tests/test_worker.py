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
        {"tool_calls": [{"name": "update_claim_status", "args": {"claim_id": "CLM-2026-0110", "status": "on_hold"}}]},
        {"content": "done"}]))
    code, evs = run_worker(["--claim", "CLM-2026-0110", "--fake-script", str(f)], stdin='{"approved": true}\n')
    types = [e["type"] for e in evs]
    assert code == 0 and types == ["start", "interrupt", "step", "final"]
    assert evs[1]["tool"] == "update_claim_status" and not evs[2]["declined"]


def test_token_limit_guard(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps([{"content": "x"}]))
    code, evs = run_worker(["--claim", "C", "--fake-script", str(f), "--max-tokens", "-1"])
    assert code == 3 and evs[-1]["code"] == "token_limit"
