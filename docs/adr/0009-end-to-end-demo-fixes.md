# 0009. End-to-end demo: one claim state everywhere, evidence finance can see

- Status: Accepted
- Date: 2026-10-29

## Context

ADRs 0006–0008 fixed pages one at a time. On 2026-10-29 the whole demo story was run in one pass on the
dev server (`main` at `eb8f5a0`, gate off, `EMPLOYEE_QA=1`, `REIMBURSE_EXTRACTOR=rapidocr`, 1280 px and
375 px):

1. The employee (EMP-001) drops four receipts, fixes two cards and submits `CLM-2026-W001`.
2. Finance opens triage, reviews the claim, approves the hotel up to the cap, rejects the meal
   evidence and asks the employee for the flight e-ticket.
3. The employee sees the message, the claim state and the Q&A answer.
4. Finance turns a request into a plan, answers the who-question and approves both actions.

The agent investigation was not run: it needs the model key in `.env.local`, which the reviewer may not
read. No page scrolled horizontally at 375 px. The findings are grouped by how much they hurt the demo.

**A. The same claim gets three different answers**

1. **Triage, Q&A and the claim page disagree.** For `CLM-2026-W001` after review:
   - The claim page says the taxi is complete, the boarding pass was found (MSG-W003) and the hotel was
     decided.
   - The triage card lists "Boarding pass belum ada" and "Struk belum ada" for the whole claim.
   - The employee Q&A says all four items lack a receipt, the flight lacks a boarding pass, and the hotel
     needs a signed memo.

   Cause: the claim pages call `run_check` (`app.py`), which passes the web review state (accepted
   evidence and reviewer decisions) to `check_claim`. Triage (`triage_claims` in
   `src/reimburse_buddy/triage.py`) and Q&A (`_Data.check` in `web/reimburse_web/qa.py`) call
   `check_claim(store, cid)` without it. `demo_claim_id` does the same, which is harmless there.
2. **A claim the employee just submitted is invisible to finance.** `/admin` opens on batch `2026-W43`.
   Web submissions live in batch `web-submissions`, which is reachable only from a dropdown with that
   raw name, or as one row at the bottom of "Semua klaim". The demo story breaks right after the
   employee presses "Kirim klaim".
3. **Finance cannot see the uploaded files.** The claim page shows evidence as message IDs
   (`MSG-W001`…). There is no link, no thumbnail and no route that serves an uploaded file to finance.
   The pending-evidence block asks the reviewer to "Terima bukti" or "Tolak bukti" for a receipt they
   cannot open.

**B. Finance claim page**

4. **Engine findings are in English** on the item cards:
   - "Receipt found in MSG-W001.";
   - "Rp1.350.000/night exceeds the tier-1 cap…";
   - "Resolved by the reviewer: approve_capped.";
   - provenance tags "Category: OCR".

   The decision note on the same page is already Indonesian, through `messages.render(key, params, "id")`.
5. **Misleading item status.** The hotel has its receipt and only a cap decision is open, yet it reads
   "Disetujui: Rp2.400.000 · Perlu bukti".
6. **The sticky summary is too tall.** At 1280×900 it takes about a third of the viewport. After an item
   decision the page returns to `#item-IT-1`, and the summary covers that item's header.
7. **Asking the employee is hidden.** "Tanya karyawan" (subject and body, posts to `/message`) sits inside
   "Atau selidiki manual". Its placeholder is hard-coded from the hero claim ("struk taksi tanggal
   3 Okt (IT-5)"), so it is wrong for every other claim. The flash after sending reads
   "Terkirim (OUT-001), 0 balasan diterima".
8. **English left on triage.** The time-saved assumption ("15 min of manual review per auto-cleared
   claim…") is still English.

**C. Employee side**

9. **The draft does not say why a card is incomplete** until the first save. The banner reads "Lengkapi
   kartu 3, 4." with no reason.
10. **Wrong message for an empty amount.** "Nominal harus bilangan bulat rupiah, misalnya 1.350.000." A
    card made from a boarding pass has no price, and nothing says to add the e-ticket or type the fare.
11. **Anchor targets end up under the sticky header.** After save, the focused card's title and error sit
    under the 65 px header. Item anchors (`#item-…`) on both claim pages have the same problem.
12. **The submit bar total is stale while typing.** It changes only after a save.
13. **The flight receipt gap reads "Struk belum ada"** next to a green boarding-pass chip. For a flight,
    what is missing is the ticket or e-ticket.
14. **Rejected evidence gives no hint.** After finance rejects the meal receipt, the employee sees
    "! Perlu tindakan: Struk belum ada" next to the file they uploaded, with no sign that it was
    rejected.
15. **The portal message list is not linked.** "Pesan dari finance" shows the claim ID as plain text.

**D. Plans and the landing page**

16. **A finished plan still looks unfinished.** After approving both actions:
    - each action shows "Status: Dijalankan";
    - the page still states "Belum ada yang dikirim sampai Anda menyetujui";
    - the decision bar ("0 disetujui, 0 ditolak · Setujui semua · Tolak semua · Kirim keputusan") stays,
      sticky, at the bottom;
    - the flash only says "Keputusan disimpan".
17. **The landing page is a dead end for employees.**
    - The "Finance" and "Karyawan" cards are not links.
    - The quick guide links only to finance pages.
    - "Reset demo" is a prominent danger button.
    - Two footers say the same thing in two languages ("Data sintetis…" and "Synthetic data. Simulation
      only.").

Constraints:

- **The rule engine and the agent are off limits.** No change to `src/reimburse_buddy/` (rules, triage,
  messages, models), `agent/reimburse_agent/graph.py`, `plan_graph.py`, the approval gate, the executor,
  `audit.py` or migrations.
  - `messages.render` may be imported and called, but not edited.
  - Presentation changes in `agent/reimburse_agent/office/page.py` are allowed, as in ADR 0006. They must
    not change which actions run or how they are approved.
- **Masking (ADR 0008) does not weaken.** The employee never sees a sensitive finding, a reviewer
  decision, an evidence-review reason, or approvable or pending amounts.
- **Identity and roles.** Identity comes from the session (ADR 0002). Finance routes keep the finance role
  check (ADR 0001).
- **Tests that must keep passing:**
  - the default triage numbers (10 / 7 / 3 for `2026-W43`);
  - the held-money order of the exception cards (P7);
  - the `id='item-IT-n'` anchors;
  - the masking tests, the Q&A tests and the plan page tests in `agent/tests/test_plan_page*.py`.
- **Pages must work without JavaScript.**

## Decision

Ordered work items. Each ships with tests and keeps every existing test green, including CI with
Tesseract.

### P0 — one claim state, visible evidence

1. **One check for every web view.** Every web view of a claim uses `core.run_check(cid)`, which carries
   the review state.
   - **Q&A:** `_Data.check` in `web/reimburse_web/qa.py` returns `to_jsonable(core.run_check(cid))`.
     Masking stays exactly as it is, because `employee_safe_check` still runs on the result.
   - **Triage:** `admin()` keeps `triage_claims` for batch membership, the cleared/exception split and
     the injection flags. Each exception card's gap chips and rejected items come from
     `run_check(claim_id)`, not from the triage row. The injection notice and `needs_review` stay as
     triage reports them.
   - **Regression test:** add evidence and a decision to a claim, then assert that the triage card, the
     Q&A answer and the claim page agree on its gaps.
2. **New employee claims are visible from triage.**
   - When the selected batch is not `web-submissions` and that batch has submitted claims, `/admin`
     shows a notice above the stats: "N klaim baru dari karyawan menunggu triage" with a link to
     `?batch=web-submissions`.
   - The batch dropdown shows readable names: "2026-W43", "2026-W44 (persetujuan sendiri)", "Pengajuan
     karyawan". The option values stay the same.
   - The default batch and its numbers do not change.
3. **Finance can open uploaded evidence.** Add `GET /admin/claims/{cid}/files/{mid}`.
   - **Access:** the same finance guard as the other `/admin/claims/...` routes. An employee or manager
     session gets the same response as for any other finance route.
   - **Lookup:** the message must be in `store.inbox` and have an attachment whose `linked_claim` (or
     `claim_id`) is `cid` and which has a `stored_as`. Anything else is 404.
   - **Path:** build it only from `stored_as` under `UPLOAD_DIR`, and check that the resolved path stays
     inside it.
   - **Content type:** from an allowlist by extension.
   - **Headers:** `X-Content-Type-Options: nosniff`, `Content-Disposition: inline`,
     `Cache-Control: private, no-store`.
   - **On the claim page:** each evidence chip with a stored file becomes a link (new tab). The pending
     evidence block shows a thumbnail (images) or a PDF icon next to "Terima bukti" / "Tolak bukti".
     Seed messages without a stored file stay plain chips.

### P1 — finance claim page

4. **Indonesian findings.**
   - Item cards render each finding with `messages.render(f.key, f.params, "id")` when `f.key` is set.
     Otherwise they fall back to `f.message`, and the English text stays in the `title` attribute.
   - "Diputuskan oleh reviewer: <choice>" uses the Indonesian action label ("Setujui sebatas plafon"),
     not the code.
   - Provenance tags use Indonesian field names (Kategori, Tanggal, Nominal, Merchant, Malam, No.
     penerbangan) and source names (OCR, Dideklarasikan sendiri).
   - The raw audit page keeps the raw text.
5. **Item status that matches the gap.** When every open gap of an item is a decision gap (`DECISION_GAPS`
   in `app.py`) and no document gap is open, the card says "Perlu keputusan" instead of "Perlu bukti".
   Amounts do not change.
6. **A compact sticky summary.**
   - The sticky part of the claim summary is a single line: claim ID, status, "Disetujui X dari Y",
     "tertunda Z", and the primary actions.
   - The rest (purpose, trip, evidence list, decision prompt) scrolls with the page.
   - All anchor targets on the page get `scroll-margin-top` equal to the header plus the sticky line, so
     a focused item is never covered.
7. **"Minta dokumen" where the gap is.**
   - Each item card with an open document gap gets a "Minta dokumen ke karyawan" disclosure. It holds
     the existing message form, prefilled from the gap: subject "Klaim <id>: dokumen kurang", body
     "Mohon kirim <dokumen> untuk <item> tanggal <tanggal>." Finance edits it before sending.
   - It posts to the existing `/admin/claims/{cid}/message`. The manual-investigation disclosure keeps its
     form, but without the hero-claim placeholder.
   - After sending, the flash reads "Pesan terkirim ke <nama karyawan>."
   - Nothing leaves the app: the outbox is simulated.
8. **Indonesian time-saved assumption on triage.** Render the assumption from a label with the minutes
   per claim as a parameter, not from the triage dict's English string.

### P2 — employee side

9. **Reasons before the first save.**
   - The draft page shows each card's current problems (from `_card_errors`) under the card title, as
     muted hints, also before any save.
   - After a save or submit they are shown as errors, as today.
   - The banner lists the cards together with their first reason, e.g. "Kartu 3: nominal belum diisi".
10. **Clear amount messages.**
    - An empty amount says "Isi nominal." A malformed one keeps the current message.
    - A flight card whose only file is a boarding pass also shows "Boarding pass tidak memuat harga.
      Tambahkan e-ticket atau isi harga tiket."
11. **Anchors clear the header everywhere.** Give `[id^="card-"]`, `[id^="item-"]` and `#draft-cards` a
    `scroll-margin-top` from a CSS variable for the header height. This applies to both claim pages and
    the draft.
12. **A live bar total.** A small script recomputes "Total diajukan" and the incomplete count in the
    submit bar as amounts change. The server value is still rendered first and still validated on
    submit.
13. **Category-aware document names.** In the employee item state, `receipt_missing` reads "Tiket
    pesawat belum ada" for flights and "Struk belum ada" otherwise. Finance pages keep the engine label.
14. **A rejected file says so, without the reason.** When the employee uploaded a file for an item and
    finance rejected that evidence, the item reads "! Perlu tindakan: Berkas sebelumnya tidak diterima
    finance. Unggah ulang."
    - No review reason is shown, because reasons such as `duplicate_file` are sensitive.
    - The file chip shows the neutral tone and "(tidak diterima)".
    - Masked items stay masked.
15. **Linked messages.** On the portal, the claim ID in "Pesan dari finance" links to the employee claim
    page.

### P3 — plans and landing page

16. **A finished plan reads as finished.** When no action of the plan waits for a decision:
    - the "Belum ada yang dikirim…" sentence becomes "Rencana selesai: N aksi dijalankan, M ditolak.";
    - the decision bar and the "Setujui semua / Tolak semua" buttons are not rendered;
    - the flash after submitting decisions states the counts.

    Pending actions keep today's controls. Presentation only: `plans.py` and `office/page.py` decide
    what to render from the existing plan state, and the gate and executor do not change.
17. **A landing page that leads somewhere.**
    - The "Finance" and "Karyawan" cards link to `/admin` and `/employee`.
    - The quick guide gets "Kirim klaim sebagai karyawan" (`/employee/new`) next to the finance links.
    - "Reset demo" moves into a closed `<details>` "Alat demo".
    - Only the Indonesian footer remains.

## Consequences

- Finance, the employee and the Q&A box describe the same claim the same way. The demo story holds
  from submission to decision.
- Finance can look at a receipt before accepting it. This adds one read-only route for finance. Its
  checks (role, claim link, stored name only, allowlist, headers) are the mitigation, and tests must cover
  them.
- Q&A answers change for claims with accepted evidence or reviewer decisions; they become correct. Masking
  still hides reviewer decisions from the employee.
- Not included, because each needs its own decision:
  - storing claims in Postgres;
  - translating plan assumptions generated by `plan_graph` (e.g. "overlaps the Friday prayer window");
  - refusing a "send the minutes" action when no document text was given (plan-graph logic);
  - showing the employee that a hotel was approved up to the cap. Reviewer decisions are masked by design
    (ADR 0008); that would be a masking change.
  - running the agent investigation end to end, which needs a model key.

## Alternatives considered

- **Fix triage inside `triage_claims`.** Cleaner, but it changes `src/reimburse_buddy/`, which the MCP
  server (version A) also uses, and the review state only exists in the web layer. Recomputing in the web
  layer keeps version A unchanged.
- **Show all batches in one triage list.** Simpler for finance, but it changes the demo's headline
  numbers (10 / 7 / 3) that the pitch and tests rely on. A notice with a link keeps them.
- **Serve evidence through the employee draft route.** That route is owner-only and dies on submit. A
  finance route with the finance guard is the right boundary.

## Acceptance

- On the scenario in Context, the triage card, the Q&A answer and the employee claim page list the same
  open documents for `CLM-2026-W001`.
- `/admin` (default batch) shows the notice linking to the new claim. The numbers stay 10 / 7 / 3.
- Finance opens MSG-W004's image from the pending block. An employee session cannot open
  `/admin/claims/{cid}/files/{mid}`. An unknown message or a message from another claim is 404.
- The finance claim page at 1280×900 has no English finding text outside `title` attributes. The sticky
  part is at most about 120 px tall.
- After a draft save and after an item decision, the focused element's top is below the sticky header
  (`getBoundingClientRect().top >= header height`).
- An executed plan shows no decision bar.
- `uv run pytest -q tests` and `cd agent && uv run pytest -q` are green, and CI is green on the PR.

## Notes for the implementer

- **Files in scope:**
  - `web/reimburse_web/app.py` (admin, claim page, landing page, message flash);
  - `web/reimburse_web/qa.py` (`_Data.check` only);
  - `upload_flow.py` (draft hints, item state wording, rejected evidence);
  - `plans.py`, `labels.py`, `ui.py` (only to add helpers if needed), `static/app.css`;
  - `agent/reimburse_agent/office/page.py` (presentation only);
  - the tests.
- **Do not touch:**
  - `src/reimburse_buddy/`;
  - `agent/reimburse_agent/graph.py`, `plan_graph.py` and the qa engine and tools;
  - `audit.py`, migrations;
  - the approval gate and executor;
  - `pending_items`, `evidence_review_reasons`, `_forward`, `forward_files`.
- **Tesseract:** CI has it; local runs may not. New tests must not depend on it.
- **Secrets:** never read or print `.env.local`, DSNs or keys.
- **Commits:** plain commit messages with no AI attribution lines. Do not push; the owner reviews and
  opens the PR.
