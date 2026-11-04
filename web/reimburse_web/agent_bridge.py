"""Web <-> agent wiring: run agent/investigate_worker.py as a subprocess in the agent's own uv env.

Why a subprocess and not an import: the agent pulls in langgraph, langchain-openai and
langchain-mcp-adapters (which pins its own `mcp`). Importing it here would force those into the
root uv.lock that the hackathon MCP server runs from. A subprocess keeps agent/ on its own lockfile;
the only contract is the JSON-lines protocol documented in investigate_worker.py.

Guards (per web process): at most REIMBURSE_INVESTIGATE_MAX_PER_MIN runs started per minute
(default 3), one live run at a time, and REIMBURSE_AGENT_MAX_TOKENS tokens per run (default 250k,
enforced in the worker). The worker also enforces a run deadline and auto-declines an approval
nobody answers. Each run is its own process group so Cancel/stop kills uv, python and the MCP server.
stderr goes to a per-run log; the page shows its last lines with secrets redacted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable
import tempfile

from . import spend

AGENT_DIR = Path(os.environ.get("REIMBURSE_AGENT_DIR", Path(__file__).resolve().parents[2] / "agent"))
NO_KEY_MESSAGE = ("No model key configured. Set DEEPSEEK_API_KEY (or REIMBURSE_AGENT_API_KEY) in the "
                  "environment of the web server, then restart it. Nothing was run.")


def key_configured() -> bool:
    return bool((os.environ.get("REIMBURSE_AGENT_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip())


class RateLimited(RuntimeError):
    pass


LOG_DIR = Path(os.environ.get("REIMBURSE_AGENT_LOG_DIR", Path(tempfile.gettempdir()) / "reimburse-agent-runs"))
_SECRET = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|(?i:(api[_-]?key|authorization|bearer|token)[\"'=: ]+(bearer\s+)?)\S+)")


def redact(text: str) -> str:
    for k, v in os.environ.items():
        if v and len(v) >= 8 and (k.endswith("_API_KEY") or k.endswith("_TOKEN") or "SECRET" in k):
            text = text.replace(v, "***")
    return _SECRET.sub("***", text)


class Run:
    def __init__(self, claim_id: str, cmd: list[str], run_id: str | None = None,
                 on_tokens: Callable[[dict], None] | None = None, data_dir: str | None = None):
        self.id = run_id or uuid.uuid4().hex[:10]
        self.claim_id = claim_id
        self.data_dir = data_dir  # the run's snapshot directory, removed when the run ends
        self._started_at = time.time()
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.answered: set[str] = set()  # approval_ids already answered (duplicates ignored)
        self._on_tokens = on_tokens  # settles the reserved LLM budget, once
        self._settled = False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.log_path = LOG_DIR / f"{self.id}.stderr.log"
        self._log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, cwd=AGENT_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._log, text=True, bufsize=1, start_new_session=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _settle(self, tokens) -> None:
        """Charge the run exactly once, from the worker's own token report.

        No report (cancel, deadline, crash) leaves the reservation in place: the ledger fails safe."""
        if self._on_tokens is None or self._settled or not isinstance(tokens, dict):
            return
        self._settled = True
        try:
            self._on_tokens(tokens)
        except Exception:  # noqa: BLE001 - a ledger problem must not kill the run's output
            logging.getLogger(__name__).exception("spend: settle failed for run %s", self.id)

    def _cleanup_snapshot(self) -> None:
        """Remove this run's snapshot directory once the worker has exited (idempotent).

        The MCP server is the only reader, and it is gone by then. A failed removal is
        harmless: the startup sweep clears leftovers, and the data is synthetic."""
        path, self.data_dir = self.data_dir, None
        if not path:
            return
        try:
            from .agent_snapshot import remove_snapshot

            remove_snapshot(path)
        except Exception:  # noqa: BLE001 - cleanup must never surface as a run error
            logging.getLogger(__name__).exception("could not remove snapshot %s", path)

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "final" or (ev.get("type") == "error" and ev.get("code") == "token_limit"):
                # Charge before the event is published: `done` (read by wait() and the page) must never be
                # true while the ledger still holds only the reservation.
                self._settle(ev.get("tokens"))
            with self.lock:
                self.events.append(ev)
        self.proc.wait()
        self._log.close()
        self._cleanup_snapshot()
        with self.lock:
            if not any(e["type"] in ("final", "error") for e in self.events):
                self.events.append({"type": "error", "code": "exited",
                                    "message": f"Agent process exited with code {self.proc.returncode}."})

    @property
    def started_at(self) -> float:
        """Epoch seconds when this run was started (read-only measurement for the progress page)."""
        return self._started_at

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.events)

    CLOSES = ("answer_applied", "auto_declined", "final", "error")

    @staticmethod
    def _pending(events: list[dict]) -> dict | None:
        """The latest interrupt not yet closed by answer_applied/auto_declined/final/error.
        answer_rejected and step events don't hide it."""
        for i in range(len(events) - 1, -1, -1):
            ev = events[i]
            if ev["type"] in Run.CLOSES:
                return None
            if ev["type"] == "interrupt":
                return ev
        return None

    @property
    def pending(self) -> dict | None:
        return self._pending(self.snapshot())

    @property
    def question(self) -> dict | None:
        """The agent's open question for the reviewer, until it is answered or the run ends."""
        evs = self.snapshot()
        for i in range(len(evs) - 1, -1, -1):
            if evs[i]["type"] in ("reply_applied", "final", "error", "interrupt"):
                return None
            if evs[i]["type"] == "question":
                return evs[i]
        return None

    def reply(self, question_id: str, text: str, decisions: dict[str, str], reviewer: str = "") -> bool:
        """Answer the agent's open question; decisions become the graph's reviewer_decisions.

        `reviewer` travels with the answer so the worker can pass it on to the graph, which logs the
        decision as human:<id> instead of the anonymous "reviewer"."""
        with self.lock:
            q = None
            for ev in reversed(self.events):
                if ev["type"] in ("reply_applied", "final", "error", "interrupt"):
                    break
                if ev["type"] == "question":
                    q = ev
                    break
            if q is None or q.get("question_id") != question_id or question_id in self.answered:
                return False
            self.answered.add(question_id)
            try:
                self.proc.stdin.write(json.dumps({"question_id": question_id, "reply": text,
                                                  "decisions": decisions, "reviewer": reviewer}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                return False
        return True

    @property
    def done(self) -> bool:
        return any(e["type"] in ("final", "error") for e in self.snapshot())

    def resume(self, approval_id: str, approved: bool, reason: str = "", reviewer: str = "") -> bool:
        """Answer the pending interrupt identified by its worker-generated approval_id. False when
        that interrupt is not the one waiting (stale page, second tab, double click) or was already
        answered; nothing is sent then. The trace shows the decision only after the worker's
        answer_applied acknowledgement. `reviewer` is the signed-in EMP-ID, so the audit entry names the
        person who approved rather than the anonymous "reviewer"."""
        with self.lock:  # check and write under one lock
            req = self._pending(self.events)
            if req is None or not approval_id or req.get("approval_id") != approval_id or approval_id in self.answered:
                return False
            self.answered.add(approval_id)
            try:
                self.proc.stdin.write(json.dumps({"approval_id": approval_id, "approved": approved,
                                                  "reason": reason, "reviewer": reviewer}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                return False
        return True

    def cancel(self) -> None:
        """Stop the run: polite cancel line, then kill the whole process group."""
        with self.lock:
            try:
                self.proc.stdin.write(json.dumps({"cancel": True}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.kill()
        self._cleanup_snapshot()
        with self.lock:
            if not any(e["type"] in ("final", "error") for e in self.events):
                self.events.append({"type": "error", "code": "cancelled", "message": "Cancelled by the reviewer."})

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                if hasattr(os, "killpg"):  # POSIX: kill uv, python and the MCP server together
                    os.killpg(self.proc.pid, signal.SIGKILL)
                else:  # Windows has no process groups here; kill the child itself
                    self.proc.kill()
            except (ProcessLookupError, PermissionError, AttributeError):
                self.proc.kill()
        self._cleanup_snapshot()

    def stderr_tail(self, n: int = 20) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return redact("\n".join(lines[-n:]))

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

    def command(self, claim_id: str, data_dir: str | None = None) -> list[str]:
        cmd = ["uv", "--directory", str(AGENT_DIR), "run", "--frozen", "--no-dev", "python", "investigate_worker.py",
               "--claim", claim_id]
        if data_dir:
            cmd += ["--data-dir", str(data_dir)]
        return cmd + (["--fake-script", self.fake_script] if self.fake_script else [])

    def _command_for(self, claim_id: str, data_dir: str) -> list[str]:
        """`command()` with the snapshot flag; tolerates a test double that takes only the claim id."""
        import inspect

        if len(inspect.signature(self.command).parameters) >= 2:
            return self.command(claim_id, data_dir)
        return list(self.command(claim_id)) + ["--data-dir", str(data_dir)]

    def start(self, claim_id: str) -> Run:
        now = time.time()
        while self.started and now - self.started[0] > 60:
            self.started.popleft()
        if any(not r.done for r in self.runs.values()):
            raise RateLimited("Another investigation is still running. Finish or decline it first.")
        if len(self.started) >= self.max_per_min:
            raise RateLimited(f"Rate limit: at most {self.max_per_min} investigations per minute.")
        # Snapshot the web's claim state before spending anything (ADR 0011 §2/§3). If it cannot be
        # written the run is refused outright: starting on the seed data would silently investigate a
        # different claim state. No budget is reserved and no worker is spawned.
        from .agent_snapshot import snapshot_data_dir

        data_dir = snapshot_data_dir(claim_id)
        # Charge the daily LLM budget before spawning: the worst case of this run is reserved, and the
        # run is refused outright when it would cross the cap (spend.BudgetRefused). The day is taken
        # once, here: a run that crosses midnight WIB settles on the day it was reserved, so the
        # reservation is always replaced and never leaks into the next day's cap.
        run_id = uuid.uuid4().hex[:10]
        day = spend.today()
        try:
            spend.reserve(run_id, day=day)
        except BaseException:
            # No run will own the snapshot: remove it before the refusal leaves the handler.
            from .agent_snapshot import remove_snapshot

            remove_snapshot(data_dir)
            raise
        self.started.append(now)
        try:
            run = Run(claim_id, self._command_for(claim_id, data_dir), run_id=run_id,
                      on_tokens=lambda t: spend.settle(run_id, t, day=day), data_dir=str(data_dir))
        except BaseException:
            # The worker never started: give the reservation back, drop the snapshot, do not count it.
            self.started.pop()
            from .agent_snapshot import remove_snapshot

            remove_snapshot(data_dir)
            try:
                spend.settle(run_id, {}, day=day)
            except Exception:  # noqa: BLE001 - the original failure is what the caller must see
                pass
            raise
        self.runs[run.id] = run
        return run

    def stop_all(self) -> None:
        for r in self.runs.values():
            r.kill()  # also removes the run's snapshot directory
        self.runs.clear()
        self.started.clear()
