# UI/UX pass (version B web simulation)

Presentation-only changes. The rule engine, the approval gate and the audit log are unchanged.

## What changed
- **Mobile:** viewport meta on every page (including the plan confirmation screen), wide tables scroll inside
  `.table-wrap`, draft cards are fluid, buttons and action links are at least 44px high.
- **Labels:** `web/reimburse_web/labels.py` holds one `LABELS` dictionary (Indonesian first). `label(code)`
  falls back to a humanised form for unknown codes and never raises. Pages show names instead of employee
  IDs (ID in a tooltip). Employee pages show no message or employee IDs.
- **Pending approvals:** nav item "Menunggu persetujuan" with a count of plans still waiting (no saved
  decisions and at least one action without an outcome). `/admin/plans` shows
  "Tidak ada rencana yang menunggu persetujuan." when empty.
- **Claim page:** "Klaim ini butuh N keputusan Anda" summary with links to the items; override buttons only on
  items whose gap a reviewer decision can settle (`DECISION_GAPS` in `app.py`); document-only gaps
  (receipt / boarding pass) show what is missing instead. "Lihat jejak audit" opens a read-only
  `/admin/claims/<id>/audit` page (raw codes allowed only there).
- **Confirmation screen:** each action starts with a sentence ("Kirim email ke **Rina Wulandari** — …"),
  per-participant local times, a "waktu diusulkan agent" badge for assumptions, a warning on fields influenced
  by flagged content, Reject pre-selected (Approve is never the default) with equal-size targets, and
  "Belum ada yang dikirim sampai Anda menyetujui."
- **Landing page `/`:** the two roles, three guide buttons, Reset demo, and
  "Data sintetis. Tidak ada email atau pesan sungguhan yang dikirim."
- **Accessibility:** status icons (✓ / ! / ✕) next to colours, visible `:focus-visible` outline, a plain
  "Pilih file" button on the drop zone, a label or `aria-label` on every form control.
- **Agent progress:** the investigation page shows the steps so far in plain Indonesian and the elapsed
  seconds while the run is going.
- **Triage:** "Uang tertahan" (the engine's `total_pending`) and "Yang kurang" columns, sorted by held money,
  with "Selidiki dengan agent" on every row.

## Roadmap
- Check once on a real phone before recording (headless 390px screenshots only so far).
- The rest of the English UI text (headings, buttons such as "Search", "Apply status") is not translated yet.
- "Coba tanya sebagai karyawan" opens the employee claim page; the Q&A box needs `EMPLOYEE_QA=1` and a demo
  session, so the demo deploy should turn the flag on.
- Removing override buttons from document-only items means a reviewer can no longer reject such an item
  from the claim page; they put the claim on hold and ask for the document. Revisit if finance wants it back.
