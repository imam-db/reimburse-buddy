# 0007. New claim flow: clearer steps, calmer draft cards, file previews

- Status: Proposed
- Date: 2026-10-29

## Context

The employee's new claim flow is `GET /employee/new` (steps 1–2: trip header and file drop) followed by
`GET /employee/drafts/{did}` (step 3: one editable card per file), both in
`web/reimburse_web/upload_flow.py`. ADR 0006 gave the app one stylesheet, the `ui.py` helpers and
Indonesian-first copy, but these two pages still assemble most of their markup by hand.

A review on the dev server (EMP-001, gate off, 1280 px and 800 px) dropped the seven files in
`web/sample_uploads`. Tesseract was not installed on that machine, so every file failed to extract. That
is an environment issue, but it showed the failure path clearly:

1. **Raw English errors reach the employee.** Every card shows "Berkas tidak bisa dibaca: Could not read
   this file (TesseractNotFoundError); fill in the card by hand." The extractor's error string
   (`extract.py`) is printed as is by `_err()`.
2. **No page-level summary of failures.** With 7 of 7 files unreadable, the employee finds out only by
   reading each card.
3. **Cards are dominated by rarely used controls.** Each file has a "pindah ke…" select plus a "Pindah"
   button. Each card has a "gabung ke…" select plus "Gabung", and a "Hapus" button. Together these take
   roughly half of the card's height.
4. **No preview of the file.** The employee is asked to correct the amount and date without seeing the
   receipt. There is no route that serves an uploaded file back to its owner.
5. **No summary before submitting.** "Simpan perubahan", "Tambah item tanpa berkas" and "Kirim klaim"
   have equal weight (Kirim is only bold through an inline style). Total, item count and incomplete
   cards are not shown, and validation errors appear only after the page reloads.
6. **The amount placeholder looks like a value.** "1.350.000" is almost as bright as entered text.
7. **Category-specific fields appear only after a save.** "Nomor penerbangan" and "Jumlah malam" are
   rendered only when the stored category is flight or hotel. Changing the select does nothing until the
   form is posted.
8. **Every card action reloads the page and loses the scroll position.** Only `add` and `delete` return
   to `#draft-cards`; move, merge, split and save go back to the top.
9. **No progress feedback on "Baca berkas".** OCR on several files takes seconds while the page looks
   idle, which invites a double submit.
10. **Step 1 friction.** The end date is not limited by the start date in the browser (the server answers
    400). Destination is prefilled with "Surabaya" and easy to miss. The label reads "→ Tanggal selesai"
    in the stacked layout.
11. **The picked-file list is a comma-separated string.** There is no size and no way to remove one file.
    Oversize or wrong-type files are reported only by the server after upload.
12. **Internal terms and uneven step numbering.** "Ekstraksi: ocr." means nothing to an employee. Steps
    "1." and "2." are box headings, while "3." is hidden inside a note paragraph.

Constraints:

- Presentation only. The rule engine, `check_claim`, the evidence review rules
  (`evidence_review_reasons`), forwarding, the audit log and migrations do not change. Provenance stays
  exactly as it is (`ocr` / `pdf_text` / `vision` / `self_declared`).
- The draft form contract stays: field names `c_<card>_<field>`, `f_<file>_doc_type`, `move_<file>`,
  `merge_<card>`, and `action` values `save`, `add`, `submit`, `delete:<id>`, `split:<id>`,
  `merge:<id>`, `move:<file>`. `tests/test_upload_flow.py`, `test_web_ui_labels.py`,
  `test_web_ui_p5p7.py` and `test_employee_identity.py` post these names.
- Pages must work without JavaScript. JS is progressive enhancement only, and the server still validates
  everything.
- Identity comes from the session (ADR 0002). A draft or file belonging to another employee is a 404,
  identical to a missing one.

## Decision

Ordered work items. Each ships with tests and must keep every existing test green.

### P1 — copy and feedback

1. **Indonesian extraction errors.** The draft page shows one fixed sentence for any extraction error:
   "Berkas tidak bisa dibaca. Isi kartu ini secara manual." It keeps the existing `ui_could_not_read`
   prefix, which a test asserts. The raw error string stays in the draft data and the forwarded
   attachment, unchanged. On the page it appears only in a `title` attribute. No exception class name
   (e.g. `TesseractNotFoundError`) and no English sentence appears in the visible text.
2. **Summary banner on the draft page.** Above the cards, `ui.alert` (role `status`) states how many
   files could not be read and how many cards are incomplete, for example "2 dari 7 berkas belum terbaca.
   Lengkapi kartu 3 dan 5." Each card number links to the card anchor (see item 8). There is no banner
   when everything was read. Cards are not reordered: their numbers are used in the move and merge
   selects.
3. **Stepper on both pages.** Use `ui.stepper(["Perjalanan", "Unggah berkas", "Periksa & kirim"],
   current)` at the top of `/employee/new` (current 0 while filling the header; the drop box is step 2
   on the same page) and of the draft page (current 2). Box headings lose their "1." / "2." prefixes,
   and the check note loses its "3.".
4. **Drop internal terms.** Remove "Ekstraksi: {engine}." from the employee page and keep only "Anda bisa
   memeriksa dan memperbaiki setiap isian di langkah berikutnya." The engine name stays available to
   reviewers through provenance; it is not removed from the data.
5. **Submit bar.** A bar sticks to the bottom of the viewport on the draft page. It shows the item count,
   "Total diajukan" (the sum of the card amounts that parse, formatted as Rupiah) and the number of
   incomplete cards. "Kirim klaim" is a `ui.button(variant="primary")` and "Simpan perubahan" is
   secondary. The bar must not cover the last card (pad the page bottom), and below 720 px it stays one
   row. The total is labelled as claimed, not approved: approved amounts still come only from
   `check_claim`.
6. **Busy state on long submits.** On submit of "Baca berkas" and "Kirim klaim", a small script disables
   the button, changes its text to "Membaca N berkas…" / "Mengirim…" and sets `aria-busy="true"` on the
   form. Without JS the buttons behave as today.

### P2 — the draft card

7. **Secondary actions behind a disclosure.** Each card shows its fields, its files and one
   `ui.details("Atur kartu", …)`. That disclosure contains move (per file), merge, split and delete, with
   the same names and values as today. The delete confirmation ("Hapus item ini?") stays. "Tambah item
   tanpa berkas" moves below the card grid as a secondary button.
8. **Stable card anchors and return position.** Each card gets `id="card-<card id>"` and
   `tabindex="-1"`. After any draft POST, the redirect fragment is the card the employee acted on:
   - save: the first card with an error, else none;
   - add: the new card;
   - delete: `#draft-cards`;
   - split: the first resulting card;
   - merge: the target card;
   - move: the source card.

   A short script focuses the element named by the fragment (ADR 0006 item 24). Honour
   `prefers-reduced-motion` for any scroll.
9. **Category fields follow the select.** Render the flight-number and nights fields for every card, with
   `hidden` and `disabled` when they do not apply to the current category. A script toggles both when
   the category select changes. Disabled fields are not submitted, so `_apply_edits` sees no change for
   them. Server-side validation (`_validate`) stays the authority.
10. **Amount input.** Add a visual "Rp" prefix outside the input and change the placeholder to "contoh:
    1.350.000". Placeholders use the muted token from `static/app.css`, so they read clearly as hints.
    Parsing (`parse_amount`) does not change.
11. **Calmer file rows.** Each file row shows the file name, the document-type select and its provenance
    badge on one line where width allows. The error sentence from item 1 goes underneath.

### P3 — steps 1 and 2

12. **Trip dates.** Labels "Tanggal mulai" and "Tanggal selesai", side by side from 480 px up. A script
    keeps `min` on the end date equal to the start date. The server check (`end_date < start_date` →
    400, rendered as an HTML page per ADR 0006 item 23) stays.
13. **Destination and purpose.** Destination is empty, with placeholder "mis. Surabaya". Purpose gets
    placeholder "mis. Kunjungan klien PT Contoh". Both stay `required`.
14. **Picked-file list.** Replace the comma-separated text with a list (`<ul>`): name, size in KB/MB and a
    "Hapus" button per file. The input's `FileList` is rebuilt through `DataTransfer`. A file above 5 MB
    or with an extension outside `.pdf,.png,.jpg,.jpeg` is marked in the list with a plain Indonesian
    reason. "Baca berkas" is disabled while any file is marked, or when more than `MAX_FILES` files are
    picked. The server checks (`_save_and_extract`, `MAX_FILES`) stay unchanged.

### P4 — file preview

15. **Owner-only file route.** Add `GET /employee/drafts/{did}/files/{fid}`.
    - Ownership: resolve the draft with `get_draft(did, emp)`, so another employee's draft is the same
      404 as a missing one. Find the file by `fid` among that draft's cards; an unknown `fid` is 404.
    - Path: build it only from the stored name (`core.UPLOAD_DIR / f["stored"]`). Never use a path taken
      from the request. Check that the resolved path is inside `UPLOAD_DIR`.
    - Content type: from an allowlist by extension (`.png`, `.jpg`, `.jpeg` → image types; `.pdf` →
      `application/pdf`).
    - Headers: `X-Content-Type-Options: nosniff`, `Content-Disposition: inline`,
      `Cache-Control: private, no-store`.
    - The route lives behind the same login gate as every other employee route.
    - Once the claim is submitted the draft is gone, so the link stops working. That is intended:
      previews after submission are out of scope.
16. **Thumbnails in the card.** Image files show `<img loading="lazy" alt="Pratinjau <name>">`, capped at
    about 120 px high, inside a link that opens the full file in a new tab. PDFs show a document icon
    and the same link. An unreadable file still gets a preview, because that is when the employee needs
    it most.

## Consequences

- An employee whose files could not be read sees one clear sentence and a list of cards to fill, instead
  of seven English error messages.
- Cards are shorter. Moving, merging and splitting take one extra click, which is acceptable because
  they are rare.
- Correcting a card no longer means scrolling back from the top after each save.
- One new read-only route serves uploaded files to their owner during the draft. It widens the attack
  surface slightly; the checks in item 15 are the mitigation, and tests must cover them.
- The page gains a few small inline scripts (busy state, focus, category toggle, file list, date min).
  The pages still work without them.
- Not included: fetch-based partial updates (no page reload), drag-and-drop between cards, persisting
  drafts across restarts (`DRAFTS` stays in memory), previews on the submitted-claim page, changes to
  extraction or confidence thresholds, the hard-coded trip origin "Jakarta" in `_submit`, and
  installing Tesseract (a deployment concern).

## Alternatives considered

- **Reorder cards so incomplete ones come first.** Puts the work in front, but card numbers change
  between reloads, and the move/merge selects refer to them. A banner with links gives the same focus
  without the confusion.
- **A client-side editor (fetch plus JSON).** Removes the reloads entirely, but it duplicates
  validation in JS and breaks the no-JS path and the existing form tests. Revisit after the event.
- **Serve uploads through `StaticFiles`.** Simpler, but it has no ownership check and would expose
  every employee's receipts by URL.

## Acceptance

- With Tesseract missing, the draft page for the seven sample files contains "Berkas tidak bisa dibaca"
  and does not contain "Could not read", "TesseractNotFoundError" or "fill in the card by hand" in
  visible text.
- `/employee/new` and the draft page show the stepper; "Ekstraksi:" is gone from `/employee/new`.
- Another employee's `GET /employee/drafts/{did}/files/{fid}` gets 404 with the same body as an unknown
  draft. An unknown `fid` gets 404. A valid request has `X-Content-Type-Options: nosniff`.
- At 390 px the draft page has no horizontal scroll, and the submit bar does not cover the last card.
- `uv run pytest -q` stays green. Report how many tests were skipped because Tesseract is missing.
- Screenshots at 1280 and 390 px of `/employee/new` (with files picked) and the draft page (one readable
  file and one unreadable file).

## Notes for the implementer

- **Files in scope:** `web/reimburse_web/upload_flow.py`, `labels.py`, `ui.py` (only to add helpers if
  needed), `static/app.css`; tests under `tests/` (`test_upload_flow.py`, `test_web_ui_*.py`, and a new
  test file for the file route and copy).
- **Do not touch:** `extract.py` error strings (map them on display instead), `src/reimburse_buddy/`
  (rules, gaps, amounts), `evidence_review_reasons`, `_forward`, `forward_files`, `agent/`, `audit.py`
  or migrations.
- **Tesseract:** `tests/test_upload_flow.py` is skipped when `tesseract` is not on `PATH`. New tests for
  this ADR must not depend on Tesseract. Monkeypatch `get_extractor()` with a stub extractor that returns
  fixed fields or a fixed error.
- **Secrets:** never read or print `.env.local`, DSNs or keys.
- **Commits:** plain commit messages with no AI attribution lines. Do not push; the owner reviews and
  opens the PR.
