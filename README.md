# reimburse-buddy

An office agent that audits business-travel expense claims, built for the
**Tencent Cloud Hackathon 2026 (Office Agent track)** on **Tencent WorkBuddy**.

The agent reads a claim, plans its review, spots gaps, goes looking for evidence
(inbox, calendar, approval memos), revises its plan on screen, asks a human only when
it has to, and drafts a decision. Every Rupiah figure comes from a deterministic Python
rule engine, never from the language model.

> All data in this repository is synthetic. Company, people, hotels and emails are fictional.

## Architecture

```mermaid
flowchart LR
    U[Reviewer] <-->|chat, questions| WB
    subgraph WB[Tencent WorkBuddy]
        S[Skill: claim-auditor<br/>plan, revise, ask human]
    end
    WB <-->|MCP over stdio| M
    subgraph M[reimburse-buddy MCP server - Python]
        T[Mock office tools<br/>get_claim, list_claims, get_policy<br/>search_inbox, get_calendar, find_approval_memo]
        R[Rule engine - Decimal<br/>check_claim, draft_decision]
    end
    T --> D[(data/*.json<br/>synthetic)]
    R --> D
```

- **WorkBuddy** does the reasoning: planning, tool choice, plan revision and the human-in-the-loop.
- **Python** does what must be exact: data access and policy arithmetic.

## Tools

| Tool | Purpose |
|---|---|
| `list_claims(status)` | Claims waiting for review |
| `get_claim(claim_id)` | Claim, trip, items and employee |
| `get_policy()` | Travel policy: city tiers, hotel caps, per diem, receipt rules |
| `search_inbox(employee_id, query)` | Mock mailbox search (boarding passes, receipts) |
| `get_calendar(employee_id, date_from, date_to)` | Mock calendar |
| `find_approval_memo(employee_id, city, date)` | Hotel-cap exception memos |
| `check_claim(claim_id, evidence_ids, human_decisions)` | Deterministic rule check |
| `triage_claims(batch)` | Split a batch into auto-cleared claims and exceptions, with a time-saved estimate |
| `draft_decision(...)` | Reviewer-ready decision note |
| `send_message(to, body, subject, claim_id)` | (Mock write) Email an employee; goes to the outbox |
| `request_approval(manager_id, claim_id, item_id, reason)` | (Mock write) Ask the claimant's manager; returns a pending request |
| `update_claim_status(claim_id, status, note)` | (Mock write) `on_hold`, `approved`, `partially_approved`, `rejected` |
| `get_outbox()` | Everything the agent sent or changed this session |

Write tools are mocks: state lives in memory for one server process. `send_message` is a
closed loop: scripted replies in `data/replies.json` land in the employee's mailbox right away,
so the agent can `search_inbox` and pass the reply's ID to `check_claim`.

Rules in `check_claim`:

- hotel nightly cap by city tier (or a signed memo), and hotel nights inside the trip
- per diem per trip day
- receipt required; a receipt found in the inbox counts
- boarding pass must match flight number, date and passenger name
- expense dates within the trip
- duplicates keyed on receipt number (no distinct receipt numbers means `duplicate_suspected`, not auto-reject)
- late check-out justified by a business meeting in the same city that ends after the standard check-out
  time, with the requested check-out at most 2 hours after the meeting (fee capped at 50% of the nightly cap)
- claim-level warning when an outbound flight has no return flight

Unresolved problems come back as **gap codes** (`receipt_missing`, `boarding_pass_missing`,
`hotel_over_cap`, `hotel_nights_mismatch`, `late_checkout_unjustified`, `duplicate_suspected`,
`return_flight_missing`). The skill maps each code to a tool. `human_decisions`
(`approve_full` / `approve_capped` / `reject`) can settle any item, in any category.
`ignored_evidence` lists IDs that did not count and why (`unknown_id`, `belongs_to_other_employee`, `memo_unsigned`).

## Demo scenarios

| # | Claim | What happens |
|---|---|---|
| Triage | batch `2026-W43` | `triage_claims` checks 10 claims: 7 clean ones are auto-cleared, 3 exceptions are investigated. Time-saved estimate: ~105 min (15 min per auto-cleared claim, a stated synthetic assumption). |
| Hero | `CLM-2026-0110` | Four problems in one claim. (a) Searching "boarding pass" only finds another trip; the airline email says "Check-in successful: QZ 7520", so the agent retries with the flight number. (b) The only hotel memo, `MEMO-2026-038`, is **unsigned**, so `check_claim` ignores it (`memo_unsigned`) and the agent asks the reviewer. (c) The calendar says the meeting ended 11:30, but an email moved it to 13:00-15:00; together they justify the 16:30 check-out. (d) The taxi receipt is missing; the agent emails Rina and her reply carries the receipt. |
| 1 | `CLM-2026-0101` | Flight has no boarding pass. Agent searches the inbox, finds `MSG-5001`, claim approved. |
| 2a | `CLM-2026-0102` | Jakarta hotel over cap. Agent finds signed memo `MEMO-2026-041`, approved. |
| 2b | `CLM-2026-0103` | Medan hotel over cap, no memo. Agent asks the reviewer; capped amount approved. |
| 3 | `CLM-2026-0104` | Late check-out fee. Calendar shows a contract signing until 15:30, so the fee is justified. A post-trip dinner is rejected and a suspected duplicate taxi goes to the reviewer. |

Run them without WorkBuddy:

```bash
uv run python -m reimburse_buddy.demo boarding-pass   # or triage, hero, hotel-memo, hotel-ask, late-checkout, all
```

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/imam-db/reimburse-buddy.git
cd reimburse-buddy
uv sync
uv run pytest            # rule tests + a real MCP stdio round-trip
uv run reimburse-buddy-mcp   # starts the server on stdio (WorkBuddy normally does this)
```

## Plug into WorkBuddy

1. Copy `.workbuddy/mcp.json` into your project's `.workbuddy/` folder (or merge it into
   `~/.workbuddy/mcp.json`) and replace `/ABSOLUTE/PATH/TO/reimburse-buddy` with your clone path.
   On Windows use a path like `C:\\Users\\you\\reimburse-buddy`.
2. Restart WorkBuddy and confirm the `reimburse-buddy` server shows its 13 tools.
3. Add the skill in `skills/claim-auditor/SKILL.md` to WorkBuddy's skills.
4. Try: *"Review claim CLM-2026-0101."*

To point the server at another dataset, set `REIMBURSE_BUDDY_DATA=/path/to/data` in the `env` block.

## Project layout

```
src/reimburse_buddy/
  server.py      MCP server (FastMCP, stdio)
  rules.py       deterministic rule engine
  decision.py    decision note renderer
  data_store.py  JSON data access + mock tools
  models.py      result types, Decimal helpers
  triage.py      batch triage
  demo.py        scripted scenarios
data/            synthetic employees, policy, claims, inbox, calendar, memos
skills/claim-auditor/SKILL.md
.workbuddy/mcp.json
tests/
```

## License

MIT
