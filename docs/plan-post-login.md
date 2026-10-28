# Rencana: halaman setelah login

Keputusan desainnya ada di `docs/adr/0001`–`0004`. Setiap tahap dimulai hanya setelah ADR yang
dirujuk berstatus `Accepted`. Tahap dikerjakan berurutan (file yang disentuh tumpang tindih), masing-masing
di worktree sendiri; Claude me-review diff dan menjalankan ulang semua tes sebelum melapor. Push, PR dan
merge diputuskan Imam.

| Tahap | Isi | ADR | Oleh |
|---|---|---|---|
| 0 | `seed_users.py --all` / `--dry-run` / `--same-password`, peran diturunkan dari `employees.json`; tes | 0004 | Haiku |
| 1 | Identitas karyawan dari sesi (`current_employee`), cek pemilik klaim/draft/upload, hapus `emp` dari query dan form; tes akses silang | 0002 | Sonnet |
| 2 | Satu portal `/employee`; `/employee/claims` dialihkan; link "← Klaim saya" dirapikan | 0002 | Sonnet |
| 3+4 | Digabung, karena nav harus mengikuti hak akses. Nav per peran (karyawan / manager / finance), nama + peran + Keluar, catatan data sintetis di footer. Hak akses per peran di `auth.may_open` / `home_for`. `/manager` baca-saja (daftar permintaan, tanpa tombol). Reset hanya finance dan tidak di production. Skenario self-approval di README ditulis ulang. | 0001 | Sonnet |
| 5 | Teks halaman karyawan ke `labels.py` (Indonesia dulu); tes nav dan pengalihan | 0001, 0002 | Haiku |
| 6 | Halaman `/manager`: jawab permintaan persetujuan tim sendiri, dicatat ke audit, tampil di halaman klaim finance | 0003 | Claude |

## Aturan di setiap brief

- Tanpa baris atribusi AI di commit.
- Jangan sentuh: `src/reimburse_buddy/rules.py`, gerbang `plan_graph`, `agent/reimburse_agent/graph.py`,
  `agent/reimburse_agent/audit.py`, `agent/migrations/`.
- Jangan membaca atau mencetak `.env.local`, DSN, atau password.
- Tidak push, tidak membuat PR.

## Selesai jika

- Setiap peran mendarat di halamannya sendiri dan hanya melihat menu miliknya.
- Tidak ada route karyawan yang menerima ID karyawan dari query atau form; akses silang ditolak dengan 404.
- Manager bisa menjawab permintaan persetujuan timnya; finance melihat jawabannya dan tetap memutuskan.
- `uv run pytest` dan `uv --directory agent run pytest` hijau.
