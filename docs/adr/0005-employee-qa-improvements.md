# 0005. Employee Q&A: answer quality and usability

- Status: Accepted
- Date: 2026-10-28

## Context

The employee Q&A box (flag `EMPLOYEE_QA`, see [docs/employee-qa.md](../employee-qa.md)) sits on the
`/employee` portal and on each claim page. It has a scrollable chat log and a pending state
(PR #41). It answers with the deterministic `OfflineQAModel` over a synthetic rulebook (POL-1..POL-10)
and the employee's own claims. Asking a range of questions on the dev server (EMP-001) showed:

1. **Wrong policy answer instead of "I don't know".** `Rulebook.search` returns any article with a
   BM25 score above 0. "Berapa plafon taksi?" matches on "plafon" and gets the hotel cap (POL-4) plus
   POL-2. Taxi is not in the rulebook, so the honest answer is "not covered, contact finance".
2. **Indonesian questions answered in two languages.** `detect_lang` counts "status" (and other
   words) in both `ID_WORDS` and `EN_WORDS`. "Status klaim saya?" is therefore `mixed`, and the answer
   is the Indonesian text followed by "(EN) ...". That doubles its length.
3. **Silent truncation.** "Status klaim saya?" / "Apa yang kurang?" without a claim checks only the
   first 3 claims (`[:3]` in `OfflineQAModel.answer`) and does not say that more exist.
4. **Sources are raw IDs.** The line reads
   `CLM-2026-0101, CLM-2026-0110, CLM-2026-0116, IT-1, IT-3, IT-4, IT-5, POL-4, POL-6, POL-9`.
   Item IDs mean nothing without their claim, and nothing is a link.
5. **The question cap is invisible.** After 10 questions (`REIMBURSE_QA_MAX_QUESTIONS`) every answer
   is the fixed "limit reached" reply, yet the input stays enabled and nothing says how many are left.
6. **Fixed quick buttons.** The same two buttons ("Apa yang kurang?", "Berapa plafon hotel?") show on
   the portal and on every claim page, always with mixed ID/EN labels.
7. **Dead-end "I don't know".** The fallback names the finance e-mail address as plain text, with no
   link and no claim reference.
8. **Stale docs.** `docs/employee-qa.md` still says the box is only on `/employee/claims/{id}`.

## Decision

Work items, in priority order. Each one ships with tests in `tests/test_web_qa.py` and/or the agent
Q&A tests, and must keep every existing test green.

### P1: answer correctness

1. **Relevance floor for policy search.** `Rulebook.search` drops articles whose score is below a
   minimum. It also ignores generic question words when scoring. At least these do not count:
   `berapa`, `plafon`, `cap`, `limit`, `maksimal`, `boleh`, `apa`, `how`, `much`. The floor is set so that
   the checks below pass. If nothing passes, the answer is the existing `dont_know` reply.
   - "Berapa plafon hotel?" → POL-4.
   - "Berapa uang harian?" / "per diem?" → POL-5.
   - "Berapa plafon taksi?" → `dont_know`, no POL source.
   - "Kapan batas waktu pengajuan?" → `dont_know` (not in the synthetic rulebook).
2. **Language detection ignores shared words.** A word in both `ID_WORDS` and `EN_WORDS` counts for
   neither. "Status klaim saya?" → `id` and "What is my claim status?" → `en`. A question with only
   shared or unknown words defaults to `id`, as today.
3. **No silent truncation.** When the employee has more claims than are checked, the answer says so:
   "N klaim lain tidak ditampilkan; buka klaimnya untuk detail" / "N more claims not shown; open a
   claim for details". The cap of 3 stays.

### P2: usability

4. **Readable sources.**
   - A `POL-n` source shows as "Pasal n, <title>" in the answer's language.
   - A `CLM-…` source links to `/employee/claims/<id>`, and only for the employee's own claims (it
     already is: sources come from tool output).
   - Item IDs (`IT-n`) are not listed as separate sources. They still appear in the answer text.
   - The audit event keeps the raw `sources` list unchanged.
5. **Visible question cap.**
   - The box shows "Sisa pertanyaan: n dari 10 / Questions left: n of 10".
   - Once the cap is reached, the text input and quick buttons are disabled. The box explains that
     the limit is reached and gives the finance contact.
   - The server-side check stays as it is. Expose the count with a read-only accessor on `QAService`;
     do not reach into `_asked` from the web layer.
6. **Context-aware quick buttons.**
   - Portal: "Status klaim saya", "Apa yang kurang?", "Berapa plafon hotel?", "Uang harian".
   - Claim page: "Apa yang kurang di klaim ini?", "Status klaim ini", "Berapa plafon hotel?".
   - Labels are Indonesian only. The engine already answers English questions typed in the box.
   - Keep the two separate forms (Enter in the text box submits the text, not a quick button).
7. **Useful fallback.** On a `dont_know` reply, refusal or limit reply, the finance contact is a
   `mailto:` link. The subject includes the claim ID when the box is on a claim page. This is a link
   only; nothing is sent by the app.
8. **"New conversation" button.** It clears the history shown for the current session only. It does
   not reset the question cap and does not touch the audit log.

### P3: docs

9. **Update `docs/employee-qa.md`.** Cover where the box shows (portal and claim page), the cap
   display, the relevance floor and the quick buttons. Add this record to `docs/adr/README.md`
   when its status changes.

## Out of scope (need a separate decision)

- **Answer feedback (helpful / not helpful).** This needs a new audit event type and therefore a
  migration that extends the CHECK constraint. Migrations and `audit.py` are protected, so it needs
  its own record and the owner's approval.
- **Rulebook content.** Adding articles for local transport (taxi), submission deadlines and so on is
  a policy question for finance, not a code change. The rulebook is synthetic until the real one is
  supplied (`REIMBURSE_QA_RULEBOOK`).
- **Answers without a full page reload** (fetch plus partial update). This is possible later as
  progressive enhancement, but not needed while the offline router answers instantly.
- **Persisting history and the cap across restarts.** Both stay in memory per process (a known limit).
- **The live LLM adapter** (`qa/llm.py`). It is still only stub-tested.
- **Dark-mode readability of the whole app.** The base CSS in `app.py` sets a text color but no
  background, so it affects every page, not just Q&A.

## Open question for the owner

`auth.current_employee` lets any role open `/employee` as its own `employee_id`, but `qa.session_of`
returns no Q&A identity for manager and finance sessions. A manager therefore sees their own claims
on the portal but no Q&A box. Should managers and finance get Q&A on their own claims? This touches
identity rules (ADR 0001/0002), so it is not part of the work above.

## Consequences

- Fewer confident but wrong answers. Some questions that got a (wrong) policy answer before now get
  "I don't know". This is the intended trade.
- Shorter answers for Indonesian questions.
- The web layer gains a read-only view of the per-session question count.

## Notes for the implementer

- **Base:** branch from `main` after PR #41 is merged.
- **Files in scope:**
  - `agent/reimburse_agent/qa/engine.py` and `policy.py` (items 1–3, 5 accessor);
  - `web/reimburse_web/qa.py` (items 4–8);
  - the Q&A tests;
  - `docs/employee-qa.md`.
- **Do not touch:** the rule engine, the plan_graph gate, `graph.py`, `audit.py` or migrations.
- **Secrets:** never read or print `.env.local`, DSNs or keys.
- **Commits:** plain commit messages with no AI attribution lines. Do not push; the owner reviews and
  opens the PR.
- **Before handing back:** run the full suite (`uv run pytest -q tests`) and report the result line.
