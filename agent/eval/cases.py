"""Five eval claims. Expected outcomes come from the rule engine itself (src/reimburse_buddy),
called directly with the evidence a careful reviewer would find, never hand-typed numbers."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from reimburse_buddy.data_store import DataStore  # noqa: E402
from reimburse_buddy.rules import check_claim  # noqa: E402


@dataclass(frozen=True)
class Case:
    claim_id: str
    employee_id: str
    why: str
    good_evidence: list[str]
    wrong_evidence: list[str]  # traps: the wrong trip's pass, the unsigned memo, a travel event
    human_decisions: dict[str, str]  # the scripted reviewer's answer if asked
    expected_questions: int  # reviewer decisions the agent legitimately needs
    messages: list[str] = field(default_factory=list)  # requests that deliver a scripted reply


CASES = [
    Case("CLM-2026-0110", "EMP-001", "hero: dead-end search, unsigned memo, moved meeting, missing receipt",
         ["MSG-5201", "EVT-7010", "MSG-5202", "MSG-5203"], ["MSG-5001", "MEMO-2026-038"],
         {"IT-3": "approve_capped"}, 1, ["taxi receipt"]),
    Case("CLM-2026-0104", "EMP-003", "late check-out via calendar, duplicate taxi, out-of-trip item",
         ["EVT-7004", "MSG-5101"], ["EVT-7005"], {}, 0, ["taxi receipt"]),
    Case("CLM-2026-0101", "EMP-001", "boarding pass in mailbox; the other trip's pass is a trap",
         ["MSG-5001"], ["MSG-5201"], {}, 0),
    Case("CLM-2026-0103", "EMP-002", "hotel over cap, no memo exists: needs the reviewer",
         [], [], {"IT-1": "approve_capped"}, 1),
    Case("CLM-2026-0111", "EMP-004", "clean claim: should approve without investigating or asking",
         [], [], {}, 0),
]


def engine(case_or_id, evidence_ids=None, human_decisions=None, sent_bodies=()):
    """Run the rule engine on a fresh store, replaying sent messages so scripted replies exist."""
    cid = case_or_id.claim_id if isinstance(case_or_id, Case) else case_or_id
    store = DataStore()
    emp = store.get_claim(cid)["employee_id"]
    for to, body, subject in sent_bodies:
        try:
            store.send_message(to or emp, body, subject, cid)
        except Exception:
            pass
    return check_claim(store, cid, evidence_ids, human_decisions)


def expected(case: Case):
    return engine(case, case.good_evidence, case.human_decisions or None,
                  [(case.employee_id, m, "") for m in case.messages])
