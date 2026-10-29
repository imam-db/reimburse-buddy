# 0008. Employee portal and claim page: honest item states, one next step

- Status: Accepted
- Date: 2026-10-29

## Context

After ADR 0007 the employee lands on `/employee/claims/{cid}` (`employee_claim` in
`web/reimburse_web/upload_flow.py`) when they submit a claim, and on `/employee` (`employee_page` in
`web/reimburse_web/app.py`) when they open the app. Both pages went through ADR 0006 (shell, responsive
tables, Indonesian copy), but not through a review of what the employee needs to do next.

A review on the dev server (EMP-001, gate off, 1280 px and 375 px) covered a seeded claim
(`CLM-2026-0101`) and a claim submitted through the new flow (`CLM-2026-W001`, two uploaded files).
`EMPLOYEE_QA` was off, so the Q&A box was not part of it. Findings:

**Claim page**

1. **"Perlu tindakan" on items the employee cannot act on.** Every item with any gap gets
   "! Perlu tindakan: …". That includes "Menunggu verifikasi finance" (an uploaded file waiting for a
   reviewer), "Sedang ditinjau finance" (masked findings) and decision gaps such as a hotel over the cap.
   A freshly submitted claim therefore shows "Perlu tindakan" on every row, although the employee has
   nothing to do.
2. **"Berkas: Tidak ada" next to "Bukti: ✓ Beres".** The files column lists only files uploaded through
   the web and linked to the item. Evidence that reached finance through the mailbox (the engine's
   `evidence_used`) is not shown, so the two columns contradict each other.
3. **"✓ Beres" reads like "approved".** It only means the automated check found no gap. Finance still
   decides.
4. **An upload control on every row.** "Tambah berkas" appears on rows that are complete. When opened,
   it shows the raw browser file input ("Choose File / No file chosen") and a second button with the same
   label as the disclosure.
5. **Green file chips for files that are not verified yet.** Uploaded files waiting for a reviewer are
   shown with the same green tone as accepted evidence.
6. **No summary and no next step.** The page shows a status tag ("Diajukan") and a table. It does not say
   how many items need the employee, what happens next, or where to start.
7. **Raw category codes as descriptions.** When a card had no merchant, the item description is the
   category code (`hotel`, `taxi`), shown under the Indonesian category label.
8. **Technical flash text.** "Klaim diajukan: berkas diteruskan ke reimburse@… dan ditautkan ke itemnya."

**Portal**

9. **No signal of which claim needs the employee.** "Klaim saya" shows only the claim status
   ("Diajukan") for every claim.
10. **Newest claim last.** Claims are listed in store order, so a claim submitted a minute ago is at the
    bottom.
11. **The main action is buried.** "Mulai klaim baru" is the third section. At 375 px its button is
    about 1,300 px down the page.
12. **Internal history dominates.** "Diteruskan ke mailbox reimburse" is a long table of forwarded mail.
    It is the largest section on the page, but it is a technical log. "Pesan dari finance" always takes
    a section, even when it only says there are no messages.
13. **Long cards on mobile.** Each claim becomes five label/value rows at 375 px. Three claims fill more
    than one screen, and the page is about 2,400 px tall.

Constraints:

- Presentation only. The rule engine, `check_claim`, the evidence review flow (`pending_items`,
  `evidence_review_reasons`), forwarding, the audit log and migrations do not change.
- **Masking stays exactly as it is.** An item with a sensitive finding (`is_sensitive_item`) shows only
  "Sedang ditinjau finance". B-only gaps show the same. No new text may reveal a sensitive gap, a
  reviewer decision, or another employee's file (`tests/test_employee_claim_masking.py`).
- **Employees never see approvable or pending amounts.** This follows `employee_safe_check` in
  `agent/reimburse_agent/qa/tools.py`. Only the claimed amounts the employee entered are shown.
- Identity comes from the session (ADR 0002). Another employee's claim is a 404 like a missing one.
- Pages must work without JavaScript.
- Existing assertions that must keep passing:
  - "! Perlu tindakan:" and "Boarding pass belum ada" on the hero claim;
  - "✕ Ditolak" after a reject;
  - "Menunggu verifikasi finance";
  - "Sedang ditinjau finance";
  - `uf.REVIEWING`.

  The "✓ Beres" assertion in `tests/test_web_ui_p5p7.py` changes with item 1 below, in the same commit.

## Decision

Ordered work items. Each ships with tests and must keep every existing test green.

### P1 — honest item states (claim page)

1. **Four item states, decided in one function.** Add a function in `upload_flow.py` that maps one item
   (the claim item, its `ItemResult`, `pending_items`, sensitivity) to a state and a reason. The claim
   page and the portal (item 9) both use it. Precedence, first match wins:

   | State | When | Tag text | Tone |
   |---|---|---|---|
   | Ditolak | engine status `rejected` | "✕ Ditolak" | bad |
   | Menunggu finance | the item is sensitive, or has a B-only gap | "◷ Sedang ditinjau finance" | info |
   | Menunggu finance | the item has evidence in `pending_items` | "◷ Menunggu verifikasi finance" | info |
   | Perlu tindakan | the item has a document gap (`receipt_missing`, `boarding_pass_missing`) | "! Perlu tindakan: <gap label>" | warn |
   | Menunggu finance | any other gap (e.g. hotel over the cap) | "◷ Menunggu keputusan finance: <gap label>" | info |
   | Bukti lengkap | no gap | "✓ Bukti lengkap" | ok |

   - Only document gaps are "Perlu tindakan", because uploading a file is the only action the employee
     can take.
   - For a masked item, the reason is never more specific than "Sedang ditinjau finance".
   - The document gap set is the existing `DOCUMENT_GAPS` in `app.py`; reuse it, do not copy it.
   - `static/app.css` has no `info` tag tone yet; add one from the existing tokens.
2. **Claim summary at the top.** Below the page header, add `ui.stats` / `ui.stat` with:
   - the claim status;
   - the claimed total (the sum of item amounts the employee entered, labelled "Total diajukan");
   - the counts per state, e.g. "1 perlu tindakan Anda · 2 menunggu finance · 2 bukti lengkap".

   Below it, one next-step sentence picked by state:
   - any "Perlu tindakan": "Unggah berkas yang diminta. Finance baru bisa memutuskan setelah itu." with
     a link to the first such item (`#item-<id>`);
   - else any "Menunggu finance": "Tidak ada yang perlu Anda lakukan. Finance sedang meninjau klaim ini.";
   - else: "Bukti lengkap. Tinggal menunggu keputusan finance."

   Never show approvable or pending amounts.
3. **Files that match the evidence.** The files cell shows, without duplicates:
   - files uploaded through the web for the item (today's list);
   - the attachment names of the item's `evidence_used` messages, but only for messages whose
     `owner_id` is the signed-in employee, and never for a masked item.

   Chip tone: `ok` for evidence the engine used, `warn` with "menunggu verifikasi" for files in
   `pending_items`, neutral otherwise. If a masked item has uploaded files, show their names in a
   neutral tone only.
4. **Item descriptions.** When an item's description equals its category code, do not repeat it under
   the category label.

### P2 — one clear upload action (claim page)

5. **Upload where it is needed.**
   - Items in "Perlu tindakan" show a visible upload control in the row: a button-styled `<label>`
     ("Pilih berkas") wrapping a visually hidden `<input type=file>`, the chosen file name, and a
     primary "Unggah" button.
   - Every other item keeps a quiet `<details>` with the summary "Tambah berkas" and the same control
     inside, with a secondary "Unggah" button.
   - The raw browser input is never visible.
   - The form target, field name (`file`), accepted types and the server checks do not change.
6. **Row anchors.** Each item row (or card on mobile) has `id="item-<item id>"` so the summary link and
   the post-upload redirect can land on it. After "Tambah berkas", redirect to
   `/employee/claims/{cid}?msg=File+added#item-<id>`.
7. **Plain flash text.**
   - After submit: "Klaim diajukan. Berkas Anda sudah diteruskan ke finance."
   - After an upload: "Berkas ditambahkan ke <item>."

   The mailbox address is not shown. Keep the `?msg=` mechanism and `flash_text`.

### P3 — the portal

8. **Main action first.** `/employee` starts with `ui.page_header` and "Mulai klaim baru" as its
   primary action, above the claim list. Remove the separate "Klaim baru" box. Keep the hint sentence
   as the header subtitle.
9. **Which claim needs me.** Each claim in "Klaim saya" shows its status tag and, when at least one
   item is "Perlu tindakan" (item 1), a warn badge "N perlu tindakan". Only document gaps count, so a
   masked item never shows up as "perlu tindakan". Claims with an action come first, then the rest by
   `submitted_at` descending (ties by ID descending).
10. **Compact claims on mobile.** Below 720 px, each claim is one compact block:
    - first line: purpose as the link, with the status and action badge;
    - second line: destination and dates;
    - third line: claimed amount and ID in muted text.

    Desktop keeps the table. No horizontal scroll at 375 px.
11. **Messages only when there are messages.** "Pesan dari finance" renders as a section only when the
    employee has messages, and then it comes right after "Klaim saya". With no messages, it is omitted.
12. **History out of the way.** "Diteruskan ke mailbox reimburse" moves to the bottom, inside a closed
    `ui.details("Riwayat berkas yang diteruskan (N)", …)`. Same table and same data inside.
13. **Q&A box position.** When `EMPLOYEE_QA` is on, the portal box comes after "Klaim saya" (and
    messages, if any). Its behaviour is unchanged.

## Consequences

- An employee can see at a glance whether anything is waiting on them. Items waiting on finance no
  longer ask them to act.
- The files column agrees with the evidence status for evidence that came through the mailbox.
- The portal leads with the main action and the claims, not a mail log. It is markedly shorter on
  mobile.
- The item-state function becomes the single place where employee-facing item status is decided. A
  future change to wording or masking happens there.
- The portal calls `run_check` once per claim to count actions. This is fine for the demo volumes. A
  large history would need caching, which is not part of this record.
- Not included:
  - storing claims or drafts in Postgres (they stay in memory, as do mailbox messages);
  - previews of submitted files (ADR 0007 limited previews to drafts);
  - editing or withdrawing a submitted claim;
  - notifications;
  - finance pages;
  - changes inside the Q&A box.

## Alternatives considered

- **Keep "Perlu tindakan" for every gap and add a tooltip.** No markup change, but the main message
  stays wrong, and tooltips do not work on touch.
- **Show the engine's approvable amount on the claim page.** More informative, but it breaks the rule
  that employees never see approvable or pending amounts, and it reads as a promise before finance
  decides.
- **Drop the forwarded-mail history.** Simpler, but employees use it to confirm a file arrived. A
  closed disclosure keeps it without the noise.

## Acceptance

- A claim submitted through the new flow, with both files waiting for verification, shows no "Perlu
  tindakan" and shows "Tidak ada yang perlu Anda lakukan…".
- The hero claim (`HERO` in `tests/test_web_ui_p5p7.py`) shows "! Perlu tindakan: Boarding pass belum ada" with a visible upload control on that
  row only. Its next-step sentence links to `#item-IT-1` (or the actual item ID).
- A masked claim (masking tests) still shows only "Sedang ditinjau finance" for the masked item, with no
  new text revealing the finding, under both `EMPLOYEE_QA=0` and `1`.
- No approvable or pending amount appears on either page. A test checks that the claim page does not
  contain the engine's approvable total when it differs from the claimed total.
- `/employee`:
  - "Mulai klaim baru" comes before "Klaim saya";
  - the newest claim is first among claims without an action;
  - with no messages there is no "Pesan dari finance" section;
  - the mailbox history is inside a closed `<details>`.
- At 375 px neither page scrolls horizontally, and the portal is clearly shorter than before. Report the
  before and after `scrollHeight`.
- `uv run pytest -q` stays green. Screenshots at 1280 and 375 px of `/employee`, the hero claim and a
  freshly submitted claim.

## Notes for the implementer

- **Base:** `main` after PR #47 (ADR 0007) is merged. It is.
- **Files in scope:**
  - `web/reimburse_web/upload_flow.py` (`employee_claim`, `add_file`, the new item-state function);
  - `web/reimburse_web/app.py` (`employee_page`, `flash_text`; reuse `DOCUMENT_GAPS`);
  - `labels.py`, `ui.py` (only to add helpers if needed), `static/app.css`;
  - tests under `tests/`: a new `test_employee_pages.py`, plus the assertion change in
    `test_web_ui_p5p7.py`.
- **Do not touch:**
  - `src/reimburse_buddy/` (rules, gaps, models);
  - `agent/` (including `qa/tools.py`; import `is_sensitive_item` and `UNDER_REVIEW`, do not edit them);
  - `pending_items`, `evidence_review_reasons`, `_forward`, `forward_files`;
  - `audit.py`, migrations, finance pages.
- **Tesseract:** new tests must not depend on it. Build claims and evidence directly in the store, or
  stub `get_extractor()` as in `tests/test_upload_flow_ux.py`.
- **Secrets:** never read or print `.env.local`, DSNs or keys.
- **Commits:** plain commit messages with no AI attribution lines. Do not push; the owner reviews and
  opens the PR.
