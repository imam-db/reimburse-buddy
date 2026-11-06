# Perekam video demo

Skrip Playwright yang menjalankan demo (`docs/demo-script.md` dan `docs/demo-tour.md`) dan merekamnya jadi video
1920x1080 lengkap dengan caption di layar dan file subtitle `.srt`.

## Pasang

```bash
uv run --group demo playwright install chromium    # sekali saja, sekitar 150 MB
```

Grup `demo` terpisah dari `dev`, jadi tes dan CI tidak butuh Playwright.

## Jalankan

Server demo harus memakai file env demo (profil demo, tanya-jawab, RapidOCR, model key). Cara paling mudah: biarkan
perekam yang menjalankan dan menghentikan server.

```bash
uv run --group demo python demo/record.py --scene main --manage-server --env-file .env.demo
uv run --group demo python demo/record.py --scene ch1,ch2,ch7 --manage-server --env-file .env.demo
uv run --group demo python demo/record.py --scene all --manage-server --env-file .env.demo
```

Tanpa `--manage-server`, server harus sudah jalan di `--base` (default `http://127.0.0.1:8090`). Adegan yang butuh
restart (`main` langkah 17 dan `ch8`) lalu menulis "Restart the server now, then press Enter".

Opsi lain: `--scene main|ch1..ch8|all` (boleh daftar dipisah koma), `--out recordings`, `--headed` (lihat browser),
`--slow-mo 250` (ms per aksi), `--dry-run` (cetak langkah dan caption tanpa browser dan server).

Sebelum setiap adegan perekam masuk sebagai finance dan menekan Reset demo. Adegan yang butuh klaim lebih dulu
(`ch3` sampai `ch6`, `ch8`) menyiapkannya di konteks terpisah yang tidak direkam.

## Hasil

Di folder `recordings/` (di-ignore git):

- `<adegan>-<YYYYmmdd-HHMM>.webm`: video, satu adegan satu video berkelanjutan;
- `<adegan>-<YYYYmmdd-HHMM>.srt`: subtitle dari perubahan caption (waktu dihitung dari awal rekaman);
- `<adegan>-FAILED-<langkah>.png`: tangkapan layar bila ada langkah yang gagal;
- `server-*.log`: log server bila memakai `--manage-server`.

Akhir proses mencetak tabel ringkasan (adegan, status, detik, video). Kode keluar bukan nol bila ada adegan gagal;
dengan `--scene all`, adegan berikutnya tetap dijalankan.

## Mengedit

- Agent butuh sekitar 2-3 menit sampai gerbang persetujuan. Perekam mencetak
  `[agent] gerbang persetujuan muncul setelah N detik`: potong atau percepat sebanyak itu. Caption
  "Agent sedang menyelidiki… (dipercepat saat editing)" sengaja sudah tampil selama menunggu.
- Jeda baca setelah momen bukti diatur di `PROOF_HOLD` (default 2,5 detik) di `demo/scenes.py`.
- Caption bisa diganti di `demo/scenes.py`; teks yang ditunggu (`expect_text`) dikutip dari dokumen demo dan
  tidak berhubungan dengan caption.
- Muat `.srt` sebagai subtitle di editor, atau abaikan karena caption sudah ikut terekam di video.

## Keamanan

- Menolak berjalan bila host `--base` bukan `127.0.0.1` atau `localhost`.
- Dengan `--manage-server`, menolak bila file env menyetel `APP_ENV` ke `prod` atau `production`. Hanya kunci itu
  yang dibaca; isi file env tidak pernah dicetak.
- Reset demo menghapus data web, jadi jangan arahkan ke server yang datanya penting.
- Adegan `main`, `ch6` memanggil model sungguhan (sekitar satu sen per penyelidikan). Agent selalu ditolak di
  gerbang persetujuan, jadi tidak ada aksi tulis.
- Setiap `ch3` memakai ID permintaan `APR-001`; pada database dengan audit log Postgres yang sama, menjalankan `ch3`
  lagi setelah Reset demo dijawab "Permintaan ini sudah dijawab" (jawaban lama masih ada di audit log).
