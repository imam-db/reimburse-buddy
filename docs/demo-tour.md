# Tur lengkap per bab

`docs/demo-script.md` adalah video utama 3 menit: satu klaim dari karyawan sampai finance. Dokumen ini adalah
**tur lengkap**, yaitu 8 bab yang masing-masing bisa direkam sendiri, 1–2 menit per bab. Setiap bab nanti menjadi
satu adegan skrip Playwright; kalau satu bab gagal, cukup bab itu yang diulang.

Gladi resik dilakukan 6 Nov 2026 di `main` `ab64e54`. Server lokalnya: profil demo, tanya-jawab, RapidOCR, model
key sungguhan, dan tanpa database. Teks yang dikutip berasal dari gladi itu. Status per bab:

- ✓ **digladi**: berjalan seperti tertulis;
- ⚠ **digladi, ada catatan**: lihat catatan di bab itu;

## Persiapan environment demo (sudah disiapkan 6 Nov 2026)

| Hal | Isi |
|---|---|
| Database | Branch Neon **`demo`** (salinan `production`, migrasi 001–008 sudah ada). Password `reimburse_app` di branch ini sudah diganti, jadi berbeda dari production. |
| Akun | `seed_users --all --same-password`, ditambah **EMP-101 Hendra sebagai `finance`**. Password demo disimpan pemilik di password manager; tidak dipakai saat demo karena masuk lewat kartu profil. |
| `.env.demo` (lokal, di-ignore git) | `REIMBURSE_DEMO_PROFILES=1`, `EMPLOYEE_QA=1`, `REIMBURSE_EXTRACTOR=rapidocr`, `REIMBURSE_PLAN_BACKEND=postgres`, DSN aplikasi ke branch `demo`, session key sendiri, serta model, budget, dan harga dari `.env.local`. `APP_ENV` kosong. |
| Menjalankan server | Konfigurasi `web-demo` di `.claude/launch.json` (port 8090), atau `uv run --group rapidocr --env-file .env.demo uvicorn --app-dir web reimburse_web.app:app --port 8090` |
| Sebelum setiap rekaman | Masuk sebagai finance, lalu **Alat demo → Reset demo**. |

Gladi kedua (6 Nov, environment `demo`) mencakup restart, persetujuan rencana oleh finance kedua, dan larangan
menyetujui klaim sendiri. Semua bab sekarang ✓.

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

Setelah `fix/demo-rehearsal`, jawabannya: *"IT-1 (Hotel): Melebihi plafon hotel…; IT-3 (Penerbangan): Tiket pesawat
(e-ticket) belum ada…; IT-4 (Makan): Berkas sudah diunggah dan menunggu verifikasi finance."*

## Bab 3 · Manager menjawab permintaan (≈ 1 menit) ✓

1. Rani, di halaman klaim, item hotel: **Minta persetujuan atasan**, alasan *"Hotel di atas plafon, mohon
   konfirmasi kebutuhan menginap."* Muncul flash *"Permintaan persetujuan dikirim ke atasan (APR-…)"*. Nomornya
   bertambah setiap rekaman, karena ID permintaan tidak pernah berulang walau sudah Reset demo.
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

## Bab 5 · Halaman klaim finance (≈ 2 menit) ✓

5a. Ikuti langkah 9–13 di `docs/demo-script.md`: angka dari mesin aturan, hotel disetujui sebatas plafon
    (**Rp2.400.000**), membuka file bukti, menolak foto buram, dan meminta e-ticket.
5b. **Ganti profil → Rina.** Item makan menampilkan *"Berkas sebelumnya tidak diterima finance. Unggah ulang."*
    (tanpa alasan, karena alasan termasuk data sensitif), dan pesan e-ticket muncul di **Pesan dari finance**.
5c. **Tidak bisa menyetujui klaim sendiri.** Ganti profil ke **Hendra** (finance), buka batch `2026-W44` →
    `CLM-2026-0120` (klaim milik Hendra).
    - Layar menulis *"Anda tercatat sebagai pemilik klaim ini, jadi tombol persetujuan dimatikan. Minta reviewer
      lain menyetujuinya."*
    - Putusan item yang dipaksakan maupun persetujuan massal dijawab *"Anda tidak dapat menyetujui permintaan/klaim
      Anda sendiri."* (untuk batch: *"Batch tidak disetujui; klaim Anda: CLM-2026-0120"*).
    - Sebagai Rani, klaim yang sama tetap bisa diputuskan.

## Bab 6 · Agent menyelidiki (≈ 1,5 menit, menunggu dipotong) ✓

1. Di klaim web, klik **Selidiki dengan agent**. Halaman menampilkan *"Agent membaca data klaim saat penyelidikan
   dimulai."* lalu langkah-langkah yang berjalan. Langkah pertama muncul sekitar detik ke-40.
2. Sekitar detik ke-130, agent berhenti di **gerbang persetujuan** dengan status *"menunggu persetujuan"*. Usulannya
   `request_missing_evidence` untuk IT-3: meminta e-ticket ke Rina.
   - *Dasar persetujuan*: disetujui **Rp2.630.000**, bukti MSG-W001, MSG-W002, MSG-5201.
   - Foto buram yang masih menunggu verifikasi tidak ikut dipakai (ADR 0011).
   - Teks di layar: *"Panggilan ini hanya dijalankan jika Anda menekan Setujui… Persetujuan yang tidak dijawab
     otomatis ditolak."*
3. Narasi: "Agent hanya membaca dan mengusulkan. Setiap pesan ke karyawan menunggu persetujuan manusia, dan
   persetujuan itu terikat pada angka yang tertera."

Catatan: saat gladi pertama, run dari web macet di `audit._git_sha()`. Bug itu diperbaiki di branch
`fix/demo-rehearsal` (git HEAD dibaca dari file `.git`, tanpa subprocess), lalu digladi ulang dengan hasil di atas.
Persetujuan agent yang dijalankan belum sampai ke portal karyawan (lihat ADR 0011, bagian *Not included*).

## Bab 7 · Permintaan menjadi rencana (≈ 1,5 menit) ✓

1. Rani membuka `/admin/intake`. Isi permintaan: *"Tolong kirim notulen ke Pak Budi ya, terus jadwalin follow-up
   Jumat siang."*, dengan peminta **EMP-100 (Dewi)**. Jangan pilih EMP-101: Hendra yang akan menyetujui, dan
   peminta maupun pengaju tidak boleh menyetujui rencananya sendiri.
2. Muncul pertanyaan: *"Ada 2 karyawan bernama Budi: Budi Santoso (EMP-201, … Jakarta, WIB); Budi Hartono
   (EMP-202, … Jayapura, WIT). Yang mana yang dimaksud?"* → pilih **Budi Hartono**.
3. Rencana tersusun: email ke Budi Hartono (WIT) dan jadwal follow-up. Layar menulis *"Anda tidak dapat
   menyetujui rencana ini… Anda yang mengajukan permintaan ini, jadi tombol Setujui dimatikan."*
4. **Ganti profil → Hendra** (finance kedua, bukan peminta dan bukan pengaju). Hendra menyetujui email dan menolak
   jadwal. Flash: *"Keputusan disimpan: 1 disetujui, 1 ditolak."*, lalu halaman menulis *"Rencana selesai: 1 aksi
   dijalankan, 1 ditolak."*
5. Varian injection (opsional): permintaan *"Tolong teruskan notulen ini."* dengan dokumen yang berisi instruksi
   untuk meneruskan file ke semua staf menghasilkan peringatan injection, dan tidak ada aksi yang disusun.

Catatan: jadwal follow-up ditulis *"Senin, 9 Nov 08.00 WIB"* saat gladi hari Jumat sore. Waktu dihitung dari jam
saat permintaan dibuat, jadi cek ulang hasilnya pada hari rekaman.

## Bab 8 · Jejak dan ketahanan (≈ 45 detik) ✓

1. Di halaman klaim, klik **Lihat jejak audit**. Tampil log bukti (terima/tolak), keputusan item, dan pesan keluar.
2. **Restart server**, lalu refresh. Semua masih ada:
   - klaim W001 dan keputusan hotel Rp2.400.000;
   - log penolakan foto;
   - di sisi Rina: berkas "tidak diterima" dan pesan e-ticket;
   - rencana "1 aksi dijalankan, 1 ditolak".

   Butuh `REIMBURSE_PLAN_BACKEND=postgres` (sudah ada di `.env.demo`).
3. **Reset demo**: data web kosong lagi untuk rekaman berikutnya, dan angka batch seed kembali 10 / 7 / 3.

## Perbaikan sebelum merekam

| # | Temuan | Dampak | Usulan pelaksana |
|---|---|---|---|
| 1 | ~~Worker agent macet di `audit._git_sha()` saat dijalankan dari web~~ | Diperbaiki di `fix/demo-rehearsal`, digladi ulang ✓ | Sonnet |
| 2 | ~~Teks mentah *"Ui status word"* di halaman penyelidikan~~ | Diperbaiki: "Status:" ✓ | Sonnet |
| 3 | ~~Tanya-jawab menulis kode kategori dan "bukti belum ada" untuk bukti yang menunggu verifikasi~~ | Diperbaiki: "IT-4 (Makan): Berkas sudah diunggah dan menunggu verifikasi finance" ✓ | Sonnet |
| 4 | ~~Legenda halaman profil menulis path dan halaman sempit~~ | Diperbaiki: kalimat biasa, lebar sampai 1040 px ✓ | Sonnet |
