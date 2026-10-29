# Employee Q&A (flag `EMPLOYEE_QA`, default off)

Employees ask about policy, about what their own claim is missing, and about their own claim's status.
The feature is read-only: it never changes a claim and never promises an outcome.

## Where the box shows
With the flag on, the box is on the employee portal `/employee` (policy questions, no claim needed) and on
each own claim page `/employee/claims/{id}` (policy plus that claim). The form carries only the question and
the claim the page shows; the employee comes from the session. "Percakapan baru / New conversation" clears the
turns shown for the current session only: the question count and the audit log are untouched.

The box shows "Sisa pertanyaan: n dari 10 / Questions left: n of 10". At zero the text input and quick buttons
are disabled and the box says the limit is reached and points to finance. The server-side check is unchanged;
the web layer reads the count through `QAService.remaining_questions` (a read-only accessor).

Quick buttons are per page and Indonesian only:
- Portal: Status klaim saya, Apa yang kurang?, Berapa plafon hotel?, Uang harian.
- Claim page: Apa yang kurang di klaim ini?, Status klaim ini, Berapa plafon hotel?

The text box has its own form, so Enter submits the text and not a quick button.

## Answers
Everything from the question and the answer is escaped before it is rendered. A `POL-n` source shows as
"Pasal n, \<title\>" ("Article n" with the English title for an English answer). An own `CLM-…` source links to
`/employee/claims/{id}`. Item ids stay in the answer text and are not listed as sources. The `qa_answer` audit
event keeps the raw `sources` list.

The finance contact in a "Saya tidak tahu", refusal or limit reply is a `mailto:` link; when the turn was asked
on a claim page the subject contains the claim id. The app only renders the link and sends nothing.

"Status klaim saya?" / "Apa yang kurang?" without a claim checks up to 3 claims and says how many were not
shown ("N klaim lain tidak ditampilkan; buka klaimnya untuk detail" / "N more claims not shown; open a claim
for details").

## Language
The reply follows the question: `id`, `en` or `mixed` (Indonesian text followed by the English text). A word
that is in both language lists, such as "status", counts for neither, so "Status klaim saya?" is Indonesian
and "What is my claim status?" is English. A question with only shared or unknown words defaults to Indonesian.

## Turning it on

| Env var | Default | Meaning |
|---|---|---|
| `EMPLOYEE_QA` | off | `1`/`true` shows the Q&A box on `/employee` and `/employee/claims/{id}` and enables `POST /employee/qa`. When it is off, the endpoint returns 404. |
| `REIMBURSE_QA_RULEBOOK` | `agent/reimburse_agent/qa/rulebook.json` | Path to the rulebook JSON (same schema). |
| `REIMBURSE_QA_MAX_QUESTIONS` | 10 | Questions per session. After the cap the reply is a fixed "limit reached" message. |
| `REIMBURSE_QA_MAX_TOKENS` | 300 | Per-answer cap. Passed to the model as `max_tokens`, and output is cut after generation. |
| `REIMBURSE_QA_MODEL` | falls back to `REIMBURSE_AGENT_MODEL` | Set a cheap model here for the optional `LLMQAModel` adapter (`qa/llm.py`). Tests and the web demo use the offline router. |
| `REIMBURSE_DEMO_SESSION_KEY` | random per process | HMAC key for the login session cookie. |

## Identity and permissions
- The employee comes from the signed login session (`rb_auth`, role `employee`); there is no identity picker. With the login gate off (dev/tests, no user store) `REIMBURSE_DEV_EMPLOYEE=EMP-xxx` names the employee, a dev-only fallback ignored once the gate is on. It is never read from the message text.
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
The official company rulebook is **not** in this repo, so `qa/rulebook.json` is a **synthetic** rulebook (POL-1..POL-10, ID+EN, fictional figures). To use a real one, point `REIMBURSE_QA_RULEBOOK` at a JSON file with the same schema.

Search is BM25 (`retrieval.bm25`) over the article text and returns the top 2 hits. Generic question words
(`berapa`, `plafon`, `cap`, `limit`, `maksimal`, `boleh`, `apa`, `how`, `much`, ...) are ignored. An article is
a candidate only when a remaining question word appears in its title, and its score must reach
`MIN_SCORE = 0.4` (`qa/policy.py`). A body-only mention is not an answer: "Berapa plafon taksi?" gets
"Saya tidak tahu" even though POL-2 lists taxi as a category. This trades some recall for fewer confident
wrong answers; if nothing passes, the reply is the usual don't-know text.

## Known limits
- The question cap and history are in memory and per process. The cookie key is random per process unless set.
- The web demo uses the deterministic offline router. The LLM adapter is only stub-tested; it has not been run against a live key.
- Response times may differ for a not-owned claim versus a missing one (possible timing side channel).
