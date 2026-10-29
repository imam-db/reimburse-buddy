# 0006. UI/UX: one design layer, mobile-first pages, Indonesian-first copy

- Status: Accepted
- Date: 2026-10-28

## Context

Version B is server-rendered HTML assembled from f-strings in `web/reimburse_web/` and in
`agent/reimburse_agent/office/page.py`. There is no template directory and no static asset. Five CSS
blocks grew independently:

| Surface | File | Primary blue | Max width | Radius |
|---|---|---|---|---|
| App pages | `app.py` (`CSS`) | `#1a5fb4` | 1100 px | 6–8 px |
| Login | `app.py` (`LOGIN_CSS`) | `#1a5fb4` | 380 px card | 8–12 px |
| Plan confirmation | `office/page.py` (`CSS`) | `#3a7bd5` | 980 px | 6–8 px |
| Employee Q&A | `qa.py` (`CSS`) | `#3a7bd5` | inherits | 8–14 px |
| Nav on plan page | `app.py` (`NAV_CSS`, injected into the plan page's `<head>`) | `#c62828` badge | — | 9 px |

A review of the running app at 1440×900 and 390×844 (dev gate off, `EMPLOYEE_QA=1`) found:

1. **Mobile is an afterthought.** At 390 px the triage exceptions table squeezes gap text to one word per
   line, the nav wraps to three lines, and the claim page table is clipped horizontally (the "Approvable"
   column cannot be reached) while the page grows to roughly 4,000 px. `.table-wrap` avoids a page-level
   scrollbar but does not make the content readable.
2. **The copy is mixed.** Indonesian labels sit next to English headings and actions: "Finance admin:
   triage", "Time saved", "Cleared", "Exceptions", "Rejected items", "All claims", "Investigate",
   "Search", "Send", "Accept evidence", "pending review", "Apply status". Bilingual sentences are
   duplicated inline ("Tanya-jawab reimburse / Ask about reimbursement", "Segera setelah disetujui /
   right after approval"), which roughly doubles the reading length on every screen.
3. **The claim page is a debug console.** It is an 8-column table with four decision buttons repeated in
   every row, raw rule codes in tooltips, and the bilingual decision note dumped into a `<pre>`. There
   is no summary anchor and no sticky action, so a reviewer scrolls past everything to reach the status
   form.
4. **The employee claim page shows a browser file picker in every row** ("Choose File / No file
   chosen"), including rows that need no file, next to a "Berkas" column that is empty for all of them.
5. **Plan confirmation leads with technical metadata.** Each card's primary line contains the run id,
   plan id, `action_id` and a 12-character args hash. Raw ISO timestamps
   ("2026-10-30T06:00:00Z") appear next to localized times, and assumptions are duplicated in two
   languages. The decision controls sit after a long details table, with the submit button at the very
   bottom of the page.
6. **The intake form is visibly broken.** Two textareas are sized with `cols`, so at 1440 px the second
   label runs beside the first box and the form uses a fraction of the available width.
7. **Accessibility gaps.** `page()` emits `<html>` without `lang`; there is no `<main>`, no skip link and
   no `aria-current`; flash messages are a `<p class="tag ok">` with no `role="status"`/`aria-live`;
   tables have no `<caption>` or `scope`; form validation failures and 404s return FastAPI's JSON body
   instead of a page.
8. **No visual system.** One system font at one scale, browser-default buttons, no elevation on content
   cards, proportional digits in money columns, and three different blues/greys/radii across screens.

Constraints:

- The rule engine, the approval gate, the executor, the audit log and the migrations do not change. This
  is presentation only; every amount still comes from `check_claim`.
- Tests assert rendered content and some exact strings. `tests/test_web_ui_p5p7.py` asserts the
  focus-visible rule is present in `web.CSS` **and** `office_page.CSS`; `tests/test_web_ui_viewport_money.py`
  asserts the exact viewport `<meta>` string; the triage tests depend on the row order (held money,
  descending) and on `id='item-IT-n'` row anchors.
- The demo is judged on screen. The current UI works, but it reads like an internal tool rather than a
  finance product.

## Decision

Presentation-only redesign, with no new framework and no build step. Work is ordered; each package ships
with tests and a screenshot pair (1440 px and 390 px).

### P0 — one design layer

1. Move all CSS to `web/reimburse_web/static/app.css`, served by `StaticFiles` at `/static`. Define one
   `:root` token block: ink, muted, line, surface, brand, success, warn, danger, focus; spacing scale;
   radius scale; type scale; elevation. Delete `LOGIN_CSS`, `NAV_CSS` and the Q&A block; they become
   sections of the same file.
2. Give the plan confirmation page the same stylesheet. `office.page.render()` accepts a
   `stylesheet_url` (and `nav_html`) argument so `plans.py` stops injecting CSS and nav into the markup
   with string replacement.
3. Keep a single `page()` shell: `<html lang="id">`, `<head>` with title, viewport, description,
   theme-color and an inline SVG favicon, a skip link, `<header>` with brand, role-aware nav and
   account, `<main id="main">`, and a footer.
4. Nav: `aria-current="page"` on the active item, the pending-approvals badge kept, and a
   `details`/`summary` menu below 720 px. Landings stay role-based (ADR 0001).
5. Add `web/reimburse_web/ui.py` with small HTML helpers: `card`, `page_header`, `stat`, `badge`,
   `table`, `empty_state`, `field`, `button`, `alert`, `stepper`, `details`. Pages call helpers instead
   of repeating f-string markup. Every helper escapes its arguments.
6. Update `test_web_ui_p5p7` to check the focus rule in the served stylesheet rather than in
   `web.CSS`/`office_page.CSS`. Keep the exact viewport meta string that
   `test_web_ui_viewport_money.py` expects.

### P1 — copy, one language on the surface

7. Indonesian is the primary language of every screen. English stays only for domain terms that finance
   already uses (boarding pass, late check-out, e-mail) and on the raw audit page. Replace the headings
   and actions listed in Context. No inline "ID / EN" duplicates: where content is legitimately
   bilingual (agent question, plan assumption), show Indonesian first and put English in a `<details>`
   labelled "English".
8. One label per action across pages, all from `labels.py`: "Setujui", "Tolak", "Setujui sebatas
   plafon", "Minta persetujuan atasan", "Tambah berkas", "Selidiki dengan agent", "Kirim keputusan",
   "Terapkan status".
9. Empty states name the next action ("Belum ada rencana menunggu. Buat permintaan baru.").

### P2 — responsive lists

10. One `table()` helper emits `data-label` on every cell. Below 720 px, CSS turns rows into stacked
    cards (label/value pairs), so primary pages never scroll horizontally. Applies to: employee claims,
    forwarded mailbox, triage cleared/exceptions/all claims, manager requests, audit tables, claim items.
11. Triage: three summary stats (claims, cleared, exceptions) plus held money, then exception cards with
    claim id, employee, held amount, gap chips and one primary action. Keep DOM order sorted by held
    money, descending (P7 test).
12. Employee claim: evidence becomes chips; a single "Tambah berkas" control per item opens a styled
    file field. No raw browser file input in the table.

### P3 — the claim page as a review workspace

13. Sticky summary at the top: claim id, employee, status, claimed / approvable / pending, and
    "N keputusan" chips linking to item anchors. On mobile it stacks and does not cover content.
14. Items become cards: header (category, description, amount), gap chips in plain language, evidence
    chips, then one consistent action row (approve full, capped, reject, ask manager) and only when the
    item has a decision gap. Raw rule codes stay in `title` and on the audit page.
15. "Selidiki dengan agent" becomes a primary action in the summary. Mailbox search, calendar and memos
    move into a `<details>`/tabbed panel below the items.
16. Decision note: Indonesian first, English behind `<details>`, monospace only inside the note, with a
    copy button that asks for clipboard permission only on click.

### P4 — plan confirmation

17. Action card order: sentence first ("Kirim email ke Budi Santoso"), then "Yang terjadi" and "Waktu"
    rows. Run id, plan id, `action_id` and the args hash move into `<details>` "Detail teknis". Show
    localized times in the main row; never show raw ISO.
18. One assumption notice per card in Indonesian, English behind `<details>`. The flagged-content
    warning stays prominent and keeps its id.
19. Keep Reject preselected and Approve never default. Add a sticky submission bar that states how many
    actions are approved and rejected. Keep the disabled-approve explanation.
20. Add explicit "Setujui semua" / "Tolak semua" buttons that set the radios; no silent default.

### P5 — accessibility and feedback

21. `<html lang="id">`, `<main>`, skip link, exactly one `<h1>` per page, `scope` on `<th>`, a
    visually hidden `<caption>` on every table, `aria-current` in the nav.
22. Flash messages use `role="status"`; errors use `role="alert"`. The message survives the redirect
    (it already does) and receives focus (`tabindex="-1"`).
23. Render browser navigations that fail (`400`, `403`, `404`, `413`, `503`) as an HTML page in the same
    shell: one plain Indonesian sentence and a link back. Keep JSON for non-navigation requests.
24. After an action that adds or removes a card, move focus to the new card or the page's status
    region. Honour `prefers-reduced-motion`.
25. Investigation page: progress as an ordered list with the current step and `aria-live="polite"`.
    Keep the 2-second refresh but stop it while the tab is hidden; no layout jump.

### P6 — polish

26. Money right-aligned with `font-variant-numeric: tabular-nums`. Dates as `2 Okt 2026`, with ISO in
    `title`. Zebra striping only where it aids scanning.
27. Three button variants (primary, secondary, quiet-danger). Destructive actions ("Reset demo",
    "Hapus item", "Tolak semua") require a confirmation step. Reset stays hidden in production.
28. Add `prefers-color-scheme: dark` tokens and check both themes on the main pages.
29. Page titles follow "Halaman · Reimburse Buddy"; add `theme-color` and the favicon.

## Consequences

- One stylesheet and one helper layer replace five CSS blocks and repeated markup. Visual changes become
  one edit instead of five.
- Mobile review becomes usable without horizontal scrolling; the demo reads as a product.
- The copy is consistent and shorter, because bilingual duplicates move behind a disclosure.
- The plan page now depends on the web static path. `office.page.render()` gains two presentation-only
  arguments; the plan graph and gate are untouched.
- `test_web_ui_p5p7` must change in the same commit as P0 (it asserts CSS constants). UI tests that
  assert content stay valid; tests that depend on row order or `id='item-IT-n'` must keep those anchors.
- The stylesheet is a new deploy artifact; the web image must include `web/reimburse_web/static/`.
- Screenshot checks remain manual. A future visual-regression job is a separate decision.
- Not included: a template engine, a JS framework, client-side routing, Tailwind or another build step,
  a full i18n framework, and any change to the rule engine, approval gate, executor, audit log or
  migrations.

## Alternatives considered

- **Adopt Jinja2 templates.** Better long-term separation, but it rewrites every page and every markup
  test in one step, for a hackathon demo. The helper layer gets most of the consistency benefit without
  the migration risk. Revisit after the event.
- **Add Tailwind or another CSS build.** Removes hand-written CSS but adds a toolchain and a generated
  artifact. The app serves one small stylesheet; a build step is not justified.
- **Keep the inline CSS and only fix mobile.** Cheapest, but leaves the drift that makes every future
  change five edits.

## Acceptance

- Primary pages at 390 px have no horizontal scroll: `document.documentElement.scrollWidth <=
  window.innerWidth`.
- No English-only heading or action remains on employee/finance pages except domain terms; every new
  string is in `labels.py`.
- Keyboard-only run works with visible focus and announced flash: login → submit a claim → triage →
  item decision → plan approval.
- `uv run pytest -q` and `cd agent && uv run pytest -q` stay green.
- The PR includes screenshots at 1440 and 390 px for `/`, `/admin`, the claim page, `/employee`,
  `/employee/new`, the draft page, `/admin/intake`, a plan question, plan confirmation, `/manager` and an
  investigation page.

## Notes for the implementer

- **Files in scope:** `web/reimburse_web/app.py`, `upload_flow.py`, `intake.py`, `plans.py`, `manager.py`,
  `qa.py`, `labels.py`, new `ui.py`, new `static/app.css`, `agent/reimburse_agent/office/page.py`; the UI
  tests under `tests/` and `agent/tests/test_plan_page*.py`.
- **Do not touch:** `src/reimburse_buddy/rules.py`, `agent/reimburse_agent/graph.py`,
  `plan_graph.py`, `audit.py`, migrations, or any amount/decision logic.
- **Keep stable:** the exact viewport meta string, `id='item-<id>'` row anchors, `id='needs-you'`,
  `id="pending-badge"`, `id='self-approval'`, `id='plan-expired'/'plan-stale'/'plan-refused'`, and the
  triage sort order.
- **Do it in this order:** P0 → P1 → P2 → P3 → P4 → P5 → P6. P0 changes the test that asserts CSS
  constants; do not split that commit.
- **Before handing back:** run both test suites and report the result lines.
