# Employee Q&A (flag `EMPLOYEE_QA`, default off)

Employees ask about policy, about what their own claim is missing, and about their own claim's status.
The feature is read-only: it never changes a claim and never promises an outcome.

## Turning it on

| Env var | Default | Meaning |
|---|---|---|
| `EMPLOYEE_QA` | off | `1`/`true` shows the Q&A box on `/employee/claims/{id}` and enables `POST /employee/qa`. When it is off, the endpoint returns 404. |
| `REIMBURSE_QA_RULEBOOK` | `agent/reimburse_agent/qa/rulebook.json` | Path to the rulebook JSON (same schema). |
| `REIMBURSE_QA_MAX_QUESTIONS` | 10 | Questions per session. After the cap the reply is a fixed "limit reached" message. |
| `REIMBURSE_QA_MAX_TOKENS` | 300 | Per-answer cap. Passed to the model as `max_tokens`, and output is cut after generation. |
| `REIMBURSE_QA_MODEL` | falls back to `REIMBURSE_AGENT_MODEL` | Set a cheap model here for the optional `LLMQAModel` adapter (`qa/llm.py`). Tests and the web demo use the offline router. |
| `REIMBURSE_DEMO_SESSION_KEY` | random per process | HMAC key for the demo session cookie. |

## Identity and permissions
- The employee comes from the signed demo session cookie (`rb_demo_session`), set with the demo picker. It is never read from the message text.
- No tool takes `employee_id`. Undeclared arguments are dropped and recorded.
- If an employee asks about someone else's claim, `my_claim` returns the same bytes as for a claim that does not exist. The page `/employee/claims/{id}` returns 404 in both cases.
- `my_claim_check` replaces the sensitive findings `duplicate_suspected`, `receipt_amount_mismatch` and `multiple_receipts` with "Sedang ditinjau finance" / "Under finance review". It also leaves out approvable and pending totals.

## Answer guards (run after generation; any failure gives the safe "Saya tidak tahu" answer with the finance contact)
1. Token cap.
2. Leak check: no other employee's IDs or names.
3. Every Rp figure must appear in tool output (`reimburse_agent/money.py`, the same check the eval uses).
4. No outcome promises ("pasti disetujui", "will be approved", ...).
5. The answer must cite a source (`POL-n`, a claim ID or an item ID).

Before generation: the injection detector runs on the message. High-risk input and action requests (approve, change, delete) are refused and logged.

## Audit
New event types are `qa_request`, `qa_answer` (with `sources` and the guard result) and `qa_refusal`, all with actor `human:<employee_id>`. Payloads are PII-masked by `make_event`. Migration `agent/migrations/004_qa_events.sql` extends the CHECK constraint.

## Rulebook
The official company rulebook is **not** in this repo, so `qa/rulebook.json` is a **synthetic** rulebook (POL-1..POL-10, ID+EN, fictional figures). To use a real one, point `REIMBURSE_QA_RULEBOOK` at a JSON file with the same schema. Search is BM25 (`retrieval.bm25`) and returns the top 2 hits.

## Known limits
- The question cap and history are in memory and per process. The cookie key is random per process unless set.
- The web demo uses the deterministic offline router. The LLM adapter is only stub-tested; it has not been run against a live key.
- Response times may differ for a not-owned claim versus a missing one (possible timing side channel).
