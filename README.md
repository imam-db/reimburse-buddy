# reimburse-buddy

> The hackathon submission is the WorkBuddy skill + MCP server. `web/` is a separate simulation UI (no LLM) and is not part of the judged demo.

An office agent that audits business-travel expense claims, built for the
**Tencent Cloud Hackathon 2026 (Office Agent track)** on **Tencent WorkBuddy**.

The agent reads a claim, plans its review, spots gaps, goes looking for evidence
(shared reimburse@ mailbox, calendar, approval memos), revises its plan on screen, asks a human only when
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
        T[Mock office tools<br/>get_claim, list_claims, get_policy<br/>search_reimburse_mailbox, get_calendar, find_approval_memo]
        R[Rule engine - Decimal<br/>check_claim, draft_decision]
    end
    T --> D[(data/*.json<br/>synthetic)]
    R --> D
```

- **WorkBuddy** does the reasoning: planning, tool choice, plan revision and the human-in-the-loop.
- **Python** does what must be exact: data access and policy arithmetic.

## How the AI agent works (version B)

<picture>
  <source srcset="docs/agent-how-it-works.svg" type="image/svg+xml">
  <img src="docs/agent-how-it-works.png" alt="How the Reimburse Buddy agent works">
</picture>

Source: [docs/agent-how-it-works.excalidraw](docs/agent-how-it-works.excalidraw) (open it at excalidraw.com).

- The LangGraph agent (`agent/reimburse_agent/graph.py`) reads the claim and the policy, then calls `check_claim`. Every amount and gap comes from that deterministic rule engine, never from the LLM.
- For each open gap it searches the mailbox with `search_for_gap`: 2 searches before asking the employee and 1 after. If a document is still missing, it asks the employee with the fixed-template `request_missing_evidence` and checks the claim again.
- Read-only tools run freely. Every other write or message stops at an approval gate, apart from the capped evidence request.
- Decisions that need judgment (`approve_full`, `approve_capped`, `reject`) come only from the finance reviewer, through the web question form or a reply. The graph refuses decisions the model tries to set itself.
- Once the check is clean, or every gap has been searched and asked about, the graph drafts the decision note. Finance takes the final action.
- Version B also offers `find_messages(employee_id, query, date_from, date_to)` (agent only, not a WorkBuddy tool). It collects the employee's messages through the same mailbox search, ranks them with BM25 plus a small local multilingual embedding model (fused with reciprocal rank fusion), and returns the top 5. Each call counts as one search against the gap's budget. If the model cannot be loaded, ranking falls back to BM25 and the result says so.

### What the evaluation shows so far (synthetic data, small n, no LLM runs in this round)

Retrieval was the bottleneck. With semantic retrieval, the no-LLM baseline passes 4/4 of the heuristic-hostile cases 11-14 (19/19 hard cases, 0 unsafe, 0 false approvals) and 2/5 on the held-out large set B (3/5 on tuning set A). What the LLM adds is so far untested and is measured on the reasoning track.

| suite (baseline, 1 run, $0) | ranked (BM25+embedding) | BM25 only | whole mailbox, unranked (control) |
|---|---|---|---|
| hard, 19 cases | 19/19 | 17/19 (11, 12 fail) | 19/19 |
| large set A (tuning), 5 | 3/5 | 2/5 | 5/5 |
| large set B (held out), 5 | 2/5 | 3/5 | 5/5 |
| reasoning track, 11 | 9/11 (2 false approvals) | - | 9/11 |

- The whole-mailbox control passes everything because every synthetic receipt and schedule change carries structured fields (`claim_id`, `item_id`, `meeting_update`) and the rule engine keeps only what matches. On these fixtures the loss comes from the top-5 cut, not from understanding. Real mail has no such tags.
- Large sets (`agent/eval/large_fixtures.py`): 46-68 messages per employee over Aug-Oct 2026, hand-written paraphrases combined by a seeded RNG; set A and set B share no paraphrase. Needed message recall@5 / MRR (baseline query): set A lexical 0.2/0.2, ranked 0.2/0.12; set B lexical 0.4/0.12, ranked 0.2/0.15 (`agent/eval/retrieval_metrics.py`). Every large mailbox is flagged `possibly_incomplete` (the 36 probes reach 33-47 of 46-68 messages).
- Reasoning track (`agent/eval/reasoning_fixtures.py`, labels in `fixtures/reasoning/labels.json`, to be reviewed by Imam): the baseline falsely approves R1 (meeting moved, then moved back) and R1x (moved, then cancelled in free text). R1 exposes an engine issue: `meeting_update`s are applied in evidence-list order, not message date order.
- Results: `agent/eval/results/{hard,large_a,large_b,reasoning}-baseline*.json` and `retrieval-*.json`; each records a SHA-256 of the fixtures.

### Prepared, not yet run: model screening and an optional judge

- `agent/eval/screen_models.py` screens SumoPod models on the 30 hard, large and reasoning cases. It is a dry
  run by default. `--live` needs `--max-usd` (hard cap; `--strict-cap` stops mid-model). It runs a preflight
  that forces each tool once, freezes the conditions (temperature 0, no parallel tool calls, the same token cap),
  retries infrastructure errors and drops a model after two in a row, reads the cost from the gateway header,
  ranks by unsafe actions, then false approvals, correct, protocol, cost and latency, adds the $0 baseline as
  a reference row, and runs a gateway control (deepseek-flash direct vs. via SumoPod). The dry-run estimate is
  about $0.81 (worst case $1.10), using third-party prices that have not been verified.
- `agent/reimburse_agent/judge.py` is an optional Jev (`jev-1.13.0`) judge. It is off by default; set
  `RB_JUDGE=jev`. It only escalates: a yes or unsettled answer adds a review flag. It never closes a gap,
  picks evidence or changes an amount. Each request sends one candidate message. Code computes dates and
  weekdays. Questions are Nouls, never Choices. A failed call is recorded as an error, never as "no". All
  wording and thresholds are in `judge_questions.py`. Today the judge is wired only into the contradiction
  scan of a calendar-only late checkout, and it sees the 160-character snippet, not the full body.
- `agent/eval/judge_set.jsonl` has 62 hand-written labelled pairs (formal Indonesian, slang Indonesian,
  English; 27 positives; hard negatives and injection attempts). `agent/eval/judge_score.py` compares Jev
  with a flash model using structured output. It reports AUROC, Brier, miss rate at the review threshold,
  false escalations and repeatability, overall and per register. It is a dry run by default; the estimate
  for 3 repeats is about $0.017.

### find_messages: setup and limits

- Embedding model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, loaded by fastembed 0.9.1 (pinned) from `Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q` at commit `faf4aa42` (pinned in `retrieval.py`). Install with `uv sync --extra embed`. The first run downloads about 240 MB into `RB_EMBED_CACHE`, else `FASTEMBED_CACHE_PATH`, else `~/.cache/fastembed`.
- `RB_NO_EMBED=1` skips the model: BM25 only, and the result's `ranker` says so.
- Scope: only the dedicated `reimburse@` mailbox, limited to messages the employee sent or forwarded. Personal inboxes are never read.
- Limitation: the server offers only a substring search capped at 5 results, so `find_messages` builds its pool with 36 one-character probes (a-z, 0-9). Any probe that returns 5 may have hidden more, so the pool is flagged `possibly_incomplete`; that happens for any mailbox with 5 or more messages. A contradiction scan on an incomplete pool counts as unavailable and sends the item to review. A server-side list or paged search would remove the workaround.
- Guards: a late checkout resolved by the calendar alone gets a graph-side scan (event title and date over the trip window; a hit at similarity >= 0.45, tuned on set A, or a schedule-change word sends it to review). After an evidence request, a reply that arrived since the pre-request mailbox snapshot and that the latest `check_claim` did not consider holds the draft once per mailbox version; a repeat draft goes through, flagged "new evidence found but not used". The same check runs when the model ends in plain text.

## Tools

| Tool | Purpose |
|---|---|
| `list_claims(status)` | Claims waiting for review |
| `get_claim(claim_id)` | Claim, trip, items and employee |
| `get_policy()` | Travel policy: city tiers, hotel caps, per diem, receipt rules |
| `search_reimburse_mailbox(employee_id, query)` | Search the shared reimburse@ mailbox that employees forward evidence to (boarding passes, receipts, schedule changes). Personal inboxes are never read. |
| `approve_cleared_batch(batch_id, claim_ids, reviewer_confirmed)` | (Mock write) Bulk-approve the claims triage cleared, only with explicit reviewer confirmation and the exact cleared list |
| `get_calendar(employee_id, date_from, date_to)` | Mock calendar |
| `find_approval_memo(employee_id, city, date)` | Hotel-cap exception memos |
| `check_claim(claim_id, evidence_ids, human_decisions)` | Deterministic rule check |
| `triage_claims(batch)` | Split a batch into auto-cleared claims and exceptions, with a time-saved estimate |
| `draft_decision(..., language)` | Reviewer-ready decision note: `id`, `en` or `both` (default, Bahasa Indonesia first) |
| `send_message(to, body, subject, claim_id)` | (Mock write) Email an employee; goes to the outbox |
| `request_approval(manager_id, claim_id, item_id, reason)` | (Mock write) Ask the claimant's manager; returns a pending request |
| `update_claim_status(claim_id, status, note)` | (Mock write) `on_hold`, `approved`, `partially_approved`, `rejected` |
| `get_outbox()` | Everything the agent sent or changed this session |
| `reset_demo()` | Restore all in-memory state before a new take (replies, outbox, approvals, statuses) |

The four `(Mock write)` tools are registered only when `REIMBURSE_ENABLE_WRITE_TOOLS=1`. The
WorkBuddy config (`.workbuddy/mcp.json`) does not set it, so in WorkBuddy the agent can only read and
draft; version B's agent sets it and gates every write behind a human approval.

Write tools are mocks: state lives in memory for one server process. `send_message` is a
closed loop: scripted replies in `data/replies.json` are the employee's answer and land in the reimburse@ mailbox right away,
so the agent can `search_reimburse_mailbox` and pass the reply's ID to `check_claim`.

Rules in `check_claim`:

- hotel nightly cap by city tier (or a signed memo), and hotel nights inside the trip
- per diem per trip day
- receipt required; a receipt forwarded to the reimburse mailbox counts
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
`ignored_evidence` lists IDs that did not count and why (`unknown_id`, `belongs_to_other_employee`, `memo_unsigned`,
`no_matching_item`). Wrong argument shapes return an error with a correct example, so the model can retry.

`search_reimburse_mailbox` is a ranked OR search (case-insensitive, `QZ7520` = `QZ 7520`, identifiers weigh more) that
returns the top 5 summaries. Scripted replies react to English and Indonesian requests (receipt, struk,
kuitansi, nota, bukti, taksi). The decision note's Indonesian section is fully localised and lists ignored
evidence as an audit trail.

## Employee Q&A (flag)

`EMPLOYEE_QA=1` adds a read-only Q&A box on the employee claim page. It answers policy questions, says what an employee's own claim is missing and shows its status, cites a source for every answer and keeps a full audit trail. It uses a synthetic rulebook. See [docs/employee-qa.md](docs/employee-qa.md).

## Demo scenarios

| # | Claim | What happens |
|---|---|---|
| Triage | batch `2026-W43` | `triage_claims` checks 10 claims: 7 clean ones are auto-cleared, 3 exceptions are investigated. Time-saved estimate: ~105 min (15 min per auto-cleared claim, a stated synthetic assumption). |
| Hero | `CLM-2026-0110` | Four problems in one claim. (a) Searching "boarding pass" only finds another trip; the airline email says "Check-in successful: QZ 7520", so the agent retries with the flight number. (b) The only hotel memo, `MEMO-2026-038`, is **unsigned**, so `check_claim` ignores it (`memo_unsigned`) and the agent asks the reviewer. (c) The calendar says the meeting ended 11:30, but an email moved it to 13:00-15:00; together they justify the 16:30 check-out. (d) The taxi receipt is missing; the agent emails Rina and her reply carries the receipt. |
| 1 | `CLM-2026-0101` | Flight has no boarding pass. Agent searches the reimburse mailbox, finds `MSG-5001`, claim approved. |
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
uv run --no-dev reimburse-buddy-mcp   # starts the server on stdio (WorkBuddy normally does this)
```

## Plug into WorkBuddy

1. Copy `.workbuddy/mcp.json` into your project's `.workbuddy/` folder (or merge it into
   `~/.workbuddy/mcp.json`) and replace `/ABSOLUTE/PATH/TO/reimburse-buddy` with your clone path.
   On Windows use a path like `C:\\Users\\you\\reimburse-buddy`.
2. Restart WorkBuddy and confirm the `reimburse-buddy` server shows its 14 tools.
3. Add the skill in `skills/claim-auditor/SKILL.md` to WorkBuddy's skills. It covers the gap-code-to-tool
   mapping, the fallback search strategy, how to explain `ignored_evidence`, batching questions to the
   reviewer, and post-decision actions.
4. Try: *"Triage batch 2026-W43."* or *"Review claim CLM-2026-0110."*

To point the server at another dataset, set `REIMBURSE_BUDDY_DATA=/path/to/data` in the `env` block.

## Deployment notes: audit log

Production refuses JSONL and in-memory audit logs. When `APP_ENV` is `prod` or `production`
(case-insensitive), the audit log must be Postgres:

```bash
export APP_ENV=prod
export REIMBURSE_AUDIT_DATABASE_URL='postgresql://user:pass@db.example:5432/reimburse'
```

- `REIMBURSE_AUDIT_DATABASE_URL` is required and must start with `postgres://` or `postgresql://`;
  the connection must open at startup.
- If it is missing, is not a Postgres URL, or the connection fails, the process exits with
  `audit.AuditConfigError` (a subclass of `audit.AuditError`). There is no fallback to
  `REIMBURSE_AUDIT_LOG` (JSONL) or the in-memory log.
- The web app checks this in its FastAPI startup hook, so it fails before serving the first request.
  `agent/investigate_worker.py` checks it before running, and `audit.default_log()` enforces it too.
- Outside production (`APP_ENV` unset or any other value), behaviour is unchanged: JSONL via
  `REIMBURSE_AUDIT_LOG`, otherwise a fresh in-memory log.

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
data/            synthetic employees, policy, claims, reimburse mailbox (inbox.json), calendar, memos
skills/claim-auditor/SKILL.md
.workbuddy/mcp.json
tests/
```

## Human in the loop

The agent proposes, the reviewer confirms. `triage_claims` is read-only; cleared claims change status only
through `approve_cleared_batch(..., reviewer_confirmed=true)` with the exact cleared list. Final status
changes, rejections, above-cap approvals, escalations and decision notices always wait for the reviewer.
Gathering evidence happens automatically; asking an employee for a missing document also waits for the
reviewer's approval.

In a real deployment the evidence source is a dedicated reimburse@ mailbox that employees forward
documents to, plus connectors for calendar, HR and ERP. The mock MCP tools here stand in for those.

## License

MIT

## Beyond the hackathon

### Web simulation UI (`web/`)

Not part of the judged demo. `web/` holds a small two-sided FastAPI app that imports the same core (`data_store`, `rules`, `decision`,
`triage`). It needs no LLM: the reviewer investigates and the rule engine computes every amount. A LangGraph agent (the planned
version B) will later plug in at `investigate()` and call the tools over MCP.

- Employee portal (`/employee`), drop-first: enter the trip, drop all receipts and boarding passes at once
  (`/employee/new`), then check one draft card per item. Each card field shows where it came from: extracted
  (`ocr`, `pdf_text` or `vision`, with a confidence; low confidence is highlighted) or `self_declared` (typed or
  changed by the employee). Cards can be edited, merged, split or deleted, and files moved between them. One
  submit creates the claim and forwards each file to the reimburse@ mailbox, linked to its item (files are kept
  locally in `web/uploads/`). The claim page lists evidence per item and has an "Add file" button for gaps.
- Finance admin (`/admin`): read-only triage, bulk approval only with an explicit confirmation, and a claim page
  with findings, gap codes, ignored evidence, mailbox search, calendar, memos, item decisions, a bilingual note
  preview, and a status change that needs the reviewer's tick. Each item shows provenance badges for its fields.
  Each uploaded file is cross-checked against its item; any failed rule marks it `needs_review` and keeps it
  out of the rule engine. Rules: `unreadable_file`, `self_declared_only` (the file does not show the doc type or
  amount), `mismatch_with_file` (item amount, date or flight number differs from the file),
  `file_date_outside_trip`, `doc_type_mismatch`, `implausible_amount` (per-category caps, override with
  `REIMBURSE_PLAUSIBLE_MAX`), `boarding_pass_mismatch` (passenger or flight on the pass) and `duplicate_file`
  (same file hash on another item or claim). Extraction confidence is only a display hint. Boarding pass
  metadata is taken from the file, never from the item. The item stays out of the engine until the
  reviewer clicks "Accept evidence" or "Reject evidence" (logged). Until then the item shows "pending review",
  its claim cannot be bulk-approved or moved past `on_hold`, and the employee sees "Menunggu verifikasi finance /
  Awaiting finance review". "Reset demo data" restores the seed data.

```bash
uv sync
uv run uvicorn --app-dir web reimburse_web.app:app --port 8000
# open http://127.0.0.1:8000/admin
```

#### File extraction

Extraction is pluggable (`web/reimburse_web/extract.py`). It only drafts fields; the rule engine still decides
every amount. The default reads files locally with Tesseract plus regex rules; PDFs use `pdftotext`, falling back
to rasterising with `pdf2image`. Photos are read with two page layouts and the better-supported parse wins.
The amount is chosen from labelled lines (SUBTOTAL, PPN, SERVICE, DISKON, TOTAL, TUNAI, KEMBALI): cash, change,
discount and tax lines are never the total, and a TOTAL that matches subtotal + tax + service - discount or
cash - change is preferred; no consistent TOTAL means low confidence. Dates with a 2-digit year (03/10/26) are
read. An unreadable file becomes an empty card for manual input. Only PDF, PNG and JPEG are accepted.

```bash
sudo apt-get install tesseract-ocr tesseract-ocr-ind poppler-utils   # Debian/Ubuntu
brew install tesseract tesseract-lang poppler                       # macOS
```

A second OCR engine, [RapidOCR](https://github.com/RapidAI/RapidOCR) (PaddleOCR models on onnxruntime, CPU only,
Apache-2.0), is optional. `REIMBURSE_EXTRACTOR` picks the extractor:

| Value | What runs |
|---|---|
| `ocr` (default) | Tesseract only |
| `dual` | Tesseract and RapidOCR on every photo. If they read a field differently (doc type, date, amount, merchant, flight number, nights, passenger), or only one of them reads it, the Tesseract value is kept but shown as low confidence, and the file goes to `needs_review` with reason `ocr_engine_disagreement` |
| `rapidocr` | RapidOCR only |
| `vision` | the vision extractor below |

PDFs with a text layer are read by `pdftotext` in every mode. If RapidOCR is not installed, `dual` and `rapidocr`
fall back to Tesseract. RapidOCR rebuilds lines from its word boxes and splits a date glued to a time
(`03-10-202609:12`).

```bash
uv sync --group rapidocr                 # adds rapidocr + onnxruntime (models ship with the package)
REIMBURSE_EXTRACTOR=dual uv run uvicorn --app-dir web reimburse_web.app:app --port 8000
```

RapidOCR is not in the default `dev` group, so `uv sync` and the MCP server never install it. Install it with
`uv sync --group rapidocr`; without it the RapidOCR test skips and `dual` / `rapidocr` fall back to Tesseract.

On 10 clean synthetic images we generated (38 fields), Tesseract read 37/38 fields and RapidOCR 38/38; dual mode
flagged the one Tesseract error and no other file. This shows the pipeline works end to end. It says nothing about
accuracy on real receipts.

Reproduce with `uv run python web/sample_uploads/measure_accuracy.py ocr rapidocr dual`. Timings are mean seconds
per file after a warm-up file, measured on a cloud VM with an Intel Xeon CPU (no GPU), and vary by machine:

| Extractor | Tuning set (7 files, 26 fields) | Held-out set (3 files, 12 fields) | Files flagged for review |
|---|---|---|---|
| Tesseract | 25/26, 1.22 s/file | 12/12, 0.67 s/file | n/a |
| RapidOCR | 26/26, 0.48 s/file | 12/12, 0.41 s/file | n/a |
| Dual | 25/26, 1.58 s/file | 12/12, 1.08 s/file | 1 (tuning: the blurry taxi merchant, the one Tesseract miss) |

An optional vision extractor calls any OpenAI-compatible chat completions API. It is off by default and the key
is read from the environment only:

```bash
export REIMBURSE_EXTRACTOR=vision VISION_API_BASE=https://api.example.com/v1 VISION_MODEL=your-model
export VISION_API_KEY=...   # never commit this
```

Synthetic sample uploads live in `web/sample_uploads/` (`make_samples.py` regenerates them with Pillow,
`truth.json` holds the expected fields). `web/sample_uploads/heldout/` is a separate set of tricky layouts
(TOTAL (3 item), cash larger than the total, 2-digit year, TOTAL DISKON last) not used to tune the parser.
`uv run python web/sample_uploads/measure_accuracy.py` reports both (see the table above). Both sets are synthetic and small.

Known limits before any public deploy: shared in-memory state, no auth, local uploads (basic 5 MB / type
limits only), and a self-declared field is only flagged, not verified. Dual mode detects OCR misreads, not edited
receipts; PDFs, single-engine reads and near-duplicate files are not cross-checked yet (planned after the hackathon).

Known limitation: the cross-checks compare the item with what OCR read from the same file, so a convincingly
forged or edited image still passes; OCR heuristics are tuned on synthetic receipts and need real, anonymised
receipts (and a vision fallback) before production use.
