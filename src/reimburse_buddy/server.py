"""Local stdio MCP server exposing mock office tools and the rule engine to WorkBuddy."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .data_store import DataStore
from .decision import draft_decision as _draft
from .models import to_jsonable
from .rules import check_claim as _check

mcp = FastMCP("reimburse-buddy")
store = DataStore()


@mcp.tool()
def list_claims(status: str = "submitted") -> list[dict]:
    """List expense claims with the given status (default: submitted)."""
    return [
        {"id": c["id"], "employee_id": c["employee_id"], "purpose": c["purpose"],
         "destination": c["trip"]["destination"], "submitted_at": c["submitted_at"]}
        for c in store.claims if not status or c["status"] == status
    ]


@mcp.tool()
def get_claim(claim_id: str) -> dict:
    """Get one claim with its trip, items and employee details."""
    claim = dict(store.get_claim(claim_id))
    claim["employee"] = store.get_employee(claim["employee_id"])
    return claim


@mcp.tool()
def get_policy() -> dict:
    """Get the company travel policy (caps, per diem, receipt rules)."""
    return store.policy


@mcp.tool()
def search_inbox(employee_id: str, query: str = "") -> list[dict]:
    """Search an employee's mailbox. All words in `query` must match (subject, body, sender, attachments)."""
    return store.search_inbox(employee_id, query)


@mcp.tool()
def get_calendar(employee_id: str, date_from: str, date_to: str) -> list[dict]:
    """Get an employee's calendar events between two ISO dates (inclusive)."""
    return store.get_calendar(employee_id, date_from, date_to)


@mcp.tool()
def find_approval_memo(employee_id: str, city: str = "", date: str = "") -> list[dict]:
    """Find approval memos for an employee, optionally filtered by city and a date they must cover.
    Only memos with signed=true count as approval."""
    return store.find_approval_memo(employee_id, city or None, date or None)


@mcp.tool()
def check_claim(claim_id: str, evidence_ids: list[str] | None = None,
                human_decisions: dict[str, str] | None = None) -> dict:
    """Run the deterministic policy rules on a claim. ALWAYS use this for any money figure.

    evidence_ids: IDs you found (inbox MSG-..., calendar EVT-..., memo MEMO-...).
    human_decisions: {item_id: approve_full | approve_capped | reject}; works for any item category.
    Items with status needs_evidence list gap codes (e.g. boarding_pass_missing, hotel_over_cap,
    late_checkout_unjustified, duplicate_suspected). `ignored_evidence` explains IDs that did not count."""
    return to_jsonable(_check(store, claim_id, evidence_ids, human_decisions))


@mcp.tool()
def draft_decision(claim_id: str, evidence_ids: list[str] | None = None,
                   human_decisions: dict[str, str] | None = None, note: str = "") -> str:
    """Draft the final decision note for a reviewer. Re-runs check_claim, so numbers stay consistent."""
    return _draft(store, _check(store, claim_id, evidence_ids, human_decisions), note)


@mcp.tool()
def send_message(to: str, body: str, subject: str = "", claim_id: str = "") -> dict:
    """(Mock) Email an employee, e.g. to ask for a missing receipt. Goes to the outbox.
    Scripted replies arrive in that employee's mailbox; check with search_inbox."""
    return store.send_message(to, body, subject, claim_id)


@mcp.tool()
def request_approval(manager_id: str, claim_id: str, item_id: str, reason: str = "") -> dict:
    """(Mock) Ask the claimant's manager to approve one item. Returns a pending request."""
    return store.request_approval(manager_id, claim_id, item_id, reason)


@mcp.tool()
def update_claim_status(claim_id: str, status: str, note: str = "") -> dict:
    """(Mock) Set claim status: submitted, on_hold, approved, partially_approved or rejected."""
    return store.update_claim_status(claim_id, status, note)


@mcp.tool()
def get_outbox() -> dict:
    """(Mock) Show sent messages, approval requests and status changes made in this session."""
    return {"messages": store.outbox, "approval_requests": store.approval_requests,
            "status_changes": store.status_log}


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
