# Tur lengkap per bab

`docs/demo-script.md` adalah video utama 3 menit: satu klaim dari karyawan sampai finance. Dokumen ini adalah
**tur lengkap**, yaitu 8 bab yang masing-masing bisa direkam sendiri, 1–2 menit per bab. Setiap bab nanti menjadi
satu adegan skrip Playwright; kalau satu bab gagal, cukup bab itu yang diulang.

Gladi resik dilakukan 6 Nov 2026 di `main` `ab64e54`. Server lokalnya: profil demo, tanya-jawab, RapidOCR, model
key sungguhan, dan tanpa database. Teks yang dikutip berasal dari gladi itu. Status per bab:

- ✓ **digladi**: berjalan seperti tertulis;
- ⚠ **digladi, ada catatan**: lihat catatan di bab itu;
- ○ **belum digladi**: ada tesnya, tapi belum dicoba di server.

## Persiapan environment demo

| Hal | Isi |
|---|---|
| Database | Branch Neon `demo` sendiri, bukan `production`. Jalankan migrasi 001–008, lalu `seed_users --all --same-password`. |
| **Akun finance kedua** | Seed **EMP-101 Hendra sebagai `finance`**. Rani tidak boleh menyetujui rencana yang ia ajukan sendiri (bab 7), dan Hendra butuh peran finance untuk bab 5c (klaim miliknya sendiri, `CLM-2026-0120`). Akibatnya, di bab 3 hanya Dewi yang tampil sebagai manager. |
| `.env.demo` | `APP_ENV` dikosongkan, `REIMBURSE_DEMO_PROFILES=1`, `EMPLOYEE_QA=1`, `REIMBURSE_EXTRACTOR=rapidocr`, `REIMBURSE_PLAN_BACKEND=postgres`, DSN branch `demo`, serta model key, budget, dan harga dari `.env.local`. |
| Menjalankan server | `uv run --group rapidocr --env-file .env.demo uvicorn --app-dir web reimburse_web.app:app --port 8010` |
| Sebelum setiap rekaman | Masuk sebagai finance, lalu **Alat demo → Reset demo**. |

## Bab 1 · Masuk dan peran (≈ 45 detik) ✓

1. Buka `/login`. Tampil judul **"Pilih profil"** dan *"Mode demo: masuk tanpa kata sandi. Data sintetis."* dengan
   lima kartu: Rina dan Sari (Karyawan), Dewi (Manager), Hendra, dan Rani (Finance).
2. Klik **Rani Kusuma**. Halaman pindah ke `/admin`, dan header menulis *"Masuk sebagai Rani Kusuma (Finance)"*.
3. Klik **Ganti profil**, lalu **Rina Wulandari**. Halaman pindah ke `/employee` dengan menu karyawan yang berbeda.
4. Narasi: "Setiap peran hanya melihat menunya sendiri. Kartu ini hanya ada di environment demo; di production
   aplikasi menolak start."

## Bab 2 · Karyawan mengirim klaim dan bertanya (≈ 2 menit) ✓

Ikuti langkah 1–5b di `docs/demo-script.md`. Tanda di layar:

- OCR mengisi hotel **Rp2.700.000, 2 malam**, taksi **Rp230.000**, dan penerbangan **QZ 7520**.
- Status item: hotel *menunggu keputusan finance*, taksi *bukti lengkap*, penerbangan *tiket belum ada*, makan
  *menunggu verifikasi*.
- Tombol cepat **"Berapa plafon hotel?"** menjawab dengan **Pasal 4**, Rp1.200.000 untuk tier 1.
- Tambahan untuk tur: tombol **"Apa yang kurang di klaim ini?"** menjawab per item dengan sumber POL-4 dan POL-6.

Catatan: jawaban "Apa yang kurang?" masih menulis kode kategori (`(flight)`, `(meal)`) dan menyebut makan *"bukti
belum ada"*, sementara halaman klaim menulis *menunggu verifikasi*. Lihat daftar perbaikan di akhir.

## Bab 3 · Manager menjawab permintaan (≈ 1 menit) ✓

1. Rani, di halaman klaim, item hotel: **Minta persetujuan atasan**, alasan *"Hotel di atas plafon, mohon
   konfirmasi kebutuhan menginap."* Muncul flash *"Permintaan persetujuan dikirim ke atasan (APR-001)"*.
2. **Ganti profil → Dewi Anggraini** (atasan Rina). `/manager` menampilkan permintaan untuk klaim itu →
   **Setujui**, dengan catatan *"Rapat selesai sore, wajar menginap 2 malam."*
3. Kembali sebagai Rani. Item hotel menampilkan *"Atasan Dewi Anggraini: Disetujui atasan"*.
4. Narasi: "Jawaban manager adalah masukan. Finance tetap yang memutuskan, dan jawaban itu dicatat di audit
   sebelum statusnya berubah."

## Bab 4 · Triage finance (≈ 1 menit) ✓

1. `/admin` menampilkan *"1 klaim baru dari karyawan menunggu triage"* dan batch `2026-W43`: **10 klaim, 7 bersih, 3
   pengecualian**, dengan uang tertahan.
2. Pilih batch **"Pengajuan karyawan"**. Kartu klaim web membawa chip **"Ada instruksi mencurigakan"**.
3. Kembali ke `2026-W43` → **setujui 7 klaim bersih** (wajib mencentang konfirmasi). Klaim yang berisi instruksi
   mencurigakan tidak pernah ikut persetujuan massal.

## Bab 5 · Halaman klaim finance (≈ 2 menit) ✓ (5c ○)

5a. Ikuti langkah 9–13 di `docs/demo-script.md`: angka dari mesin aturan, hotel disetujui sebatas plafon
    (**Rp2.400.000**), membuka file bukti, menolak foto buram, dan meminta e-ticket.
5b. **Ganti profil → Rina.** Item makan menampilkan *"Berkas sebelumnya tidak diterima finance. Unggah ulang."*
    (tanpa alasan, karena alasan termasuk data sensitif), dan pesan e-ticket muncul di **Pesan dari finance**.
5c. ○ **Tidak bisa menyetujui klaim sendiri.** Ganti profil ke **Hendra** (finance), buka batch `2026-W44` →
    `CLM-2026-0120` (klaim milik Hendra). Tombol persetujuan disembunyikan, dan server menolak permintaan yang
    dipaksakan.

## Bab 6 · Agent menyelidiki (≈ 1,5 menit, menunggu dipotong) ⚠

1. Di klaim web, klik **Selidiki dengan agent**. Halaman menampilkan *"Agent membaca data klaim saat penyelidikan
   dimulai."* dan langkah-langkah yang berjalan.
2. Agent mencari bukti, menghitung dengan mesin aturan, lalu berhenti di **gerbang persetujuan** dengan *Dasar
   persetujuan* sebelum aksi tulis apa pun.

**Catatan gladi (penting):** dijalankan dari web, run **macet di "Menunggu langkah pertama"**. Penyebabnya bukan
model: worker tertahan di `audit._git_sha()`, yang menjalankan `git rev-parse` dan tidak pernah selesai (dilihat
dengan `py-spy`). Batas 5 detiknya tidak efektif karena Python menunggu pipe yang tidak pernah ditutup. Worker yang
dijalankan langsung, juga lewat `agent_bridge.Run` di luar server, selesai dalam 40–60 detik. **Bab ini belum bisa
direkam sampai bug ini diperbaiki.**

## Bab 7 · Permintaan menjadi rencana (≈ 1,5 menit) ✓ (persetujuan ○)

1. Rani membuka `/admin/intake`. Isi permintaan: *"Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up
   Jumat siang."*, dengan peminta **EMP-101**.
2. Muncul pertanyaan: *"Ada 2 karyawan bernama Budi: Budi Santoso (EMP-201, … Jakarta, WIB); Budi Hartono
   (EMP-202, … Jayapura, WIT). Yang mana yang dimaksud?"* → pilih **Budi Hartono**.
3. Rencana tersusun: email ke Budi Hartono (WIT) dan jadwal follow-up. Layar menulis *"Anda tidak dapat
   menyetujui rencana ini… Anda yang mengajukan permintaan ini, jadi tombol Setujui dimatikan."*
4. ○ **Ganti profil → Hendra** (finance kedua). Hendra menyetujui email dan menolak jadwal. Hasilnya *"Rencana
   selesai: 1 aksi dijalankan, 1 ditolak."*, dan hanya satu pesan yang masuk ke outbox tiruan.
5. Varian injection (opsional): permintaan *"Tolong teruskan notulen ini."* dengan dokumen yang berisi instruksi
   untuk meneruskan file ke semua staf menghasilkan peringatan injection, dan tidak ada aksi yang disusun.

Catatan: jadwal follow-up ditulis *"Senin, 9 Nov 08.00 WIB"* saat gladi hari Jumat sore. Waktu dihitung dari jam
saat permintaan dibuat, jadi cek ulang hasilnya pada hari rekaman.

## Bab 8 · Jejak dan ketahanan (≈ 45 detik) ✓ (restart ○)

1. Di halaman klaim, klik **Lihat jejak audit**. Tampil log bukti (terima/tolak), keputusan item, dan pesan keluar.
2. ○ **Restart server**, lalu refresh. Klaim, keputusan, dan pesan masih ada (butuh `REIMBURSE_PLAN_BACKEND=postgres`).
3. **Reset demo**: data web kosong lagi untuk rekaman berikutnya, dan angka batch seed kembali 10 / 7 / 3.

## Perbaikan sebelum merekam

| # | Temuan | Dampak | Usulan pelaksana |
|---|---|---|---|
| 1 | Worker agent macet di `audit._git_sha()` saat dijalankan dari web | **Bab 6 tidak bisa direkam** | Baca commit dari folder `.git` tanpa subprocess (Sonnet, perubahan kecil plus tes) |
| 2 | Teks mentah *"Ui status word"* di halaman penyelidikan | Terlihat di video | Tambah label yang hilang (Sonnet) |
| 3 | Tanya-jawab "Apa yang kurang?" menulis kode kategori dan "bukti belum ada" untuk bukti yang menunggu verifikasi | Membingungkan juri | Label kategori dan status menunggu verifikasi (Sonnet) |
| 4 | Legenda halaman profil menulis path (`/employee`) dan lebar halaman sekitar 570 px | Kosmetik | Kalimat biasa dan halaman lebih lebar (Sonnet) |
