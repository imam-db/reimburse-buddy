"""Web <-> agent wiring: run agent/investigate_worker.py as a subprocess in the agent's own uv env.

Why a subprocess and not an import: the agent pulls in langgraph, langchain-openai and
langchain-mcp-adapters (which pins its own `mcp`). Importing it here would force those into the
root uv.lock that the hackathon MCP server runs from. A subprocess keeps agent/ on its own lockfile;
the only contract is the JSON-lines protocol documented in investigate_worker.py.

Guards (per web process): at most REIMBURSE_INVESTIGATE_MAX_PER_MIN runs started per minute
(default 3), one live run at a time, and REIMBURSE_AGENT_MAX_TOKENS tokens per run (default 250k,
enforced in the worker).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

AGENT_DIR = Path(os.environ.get("REIMBURSE_AGENT_DIR", Path(__file__).resolve().parents[2] / "agent"))
NO_KEY_MESSAGE = ("No model key configured. Set DEEPSEEK_API_KEY (or REIMBURSE_AGENT_API_KEY) in the "
                  "environment of the web server, then restart it. Nothing was run.")


def key_configured() -> bool:
    return bool((os.environ.get("REIMBURSE_AGENT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip())


class RateLimited(RuntimeError):
    pass


class Run:
    def __init__(self, claim_id: str, cmd: list[str]):
        self.id = uuid.uuid4().hex[:10]
        self.claim_id = claim_id
        self.events: list[dict] = []
        self.lock = threading.Lock()
        # stderr is dropped (uv progress noise); errors arrive as "error" events.
        self.proc = subprocess.Popen(cmd, cwd=AGENT_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self.lock:
                self.events.append(ev)
        self.proc.wait()
        with self.lock:
            if not any(e["type"] in ("final", "error") for e in self.events):
                self.events.append({"type": "error", "code": "exited",
                                    "message": f"Agent process exited with code {self.proc.returncode}."})

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.events)

    @property
    def pending(self) -> dict | None:
        ev = self.snapshot()
        if ev and ev[-1]["type"] == "interrupt":
            return ev[-1]
        return None

    @property
    def done(self) -> bool:
        return any(e["type"] in ("final", "error") for e in self.snapshot())

    def resume(self, approved: bool, reason: str = "") -> bool:
        """Answer the pending interrupt. Returns False if nothing is waiting."""
        req = self.pending
        if req is None:
            return False
        with self.lock:
            self.events.append({"type": "decision", "tool": req["tool"], "approved": approved, "reason": reason})
        self.proc.stdin.write(json.dumps({"approved": approved, "reason": reason}) + "\n")
        self.proc.stdin.flush()
        return True

    def wait(self, timeout: float = 60, until=None) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.done or (until and until(self)):
                return
            time.sleep(0.05)
        raise TimeoutError("agent run did not reach the expected state")


class Investigator:
    def __init__(self, max_per_min: int | None = None, fake_script: str | None = None):
        self.max_per_min = max_per_min or int(os.environ.get("REIMBURSE_INVESTIGATE_MAX_PER_MIN", "3"))
        self.fake_script = fake_script  # tests only
        self.started: deque[float] = deque()
        self.runs: dict[str, Run] = {}

    def command(self, claim_id: str) -> list[str]:
        cmd = ["uv", "--directory", str(AGENT_DIR), "run", "--no-dev", "python", "investigate_worker.py",
               "--claim", claim_id]
        return cmd + (["--fake-script", self.fake_script] if self.fake_script else [])

    def start(self, claim_id: str) -> Run:
        now = time.time()
        while self.started and now - self.started[0] > 60:
            self.started.popleft()
        if any(not r.done for r in self.runs.values()):
            raise RateLimited("Another investigation is still running. Finish or decline it first.")
        if len(self.started) >= self.max_per_min:
            raise RateLimited(f"Rate limit: at most {self.max_per_min} investigations per minute.")
        self.started.append(now)
        run = Run(claim_id, self.command(claim_id))
        self.runs[run.id] = run
        return run

    def stop_all(self) -> None:
        for r in self.runs.values():
            if r.proc.poll() is None:
                r.proc.kill()
        self.runs.clear()
        self.started.clear()
