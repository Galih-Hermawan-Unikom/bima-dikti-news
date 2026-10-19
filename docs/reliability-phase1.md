# Audit Reliabilitas Fase 1

Branch: `fix/reliability-phase1`. Dasar audit: `91145df` (`origin/master`).
Perubahan dilakukan pada worktree terpisah `BIMA_DIKTI_NEWS_reliability`.
Tidak ada deployment, perubahan cron, merge, atau pengiriman Telegram produksi.

## Root Cause dan Perbaikan

| Masalah | Penyebab | Perilaku sesudah perbaikan |
|---|---|---|
| Deteksi dianggap selesai walaupun Telegram gagal | Kedua monitor menyimpan cache sebelum notifier; hasil summary dan dokumen diabaikan runner | Cache merupakan riwayat deteksi. Jurnal `delivery_state.json` dibuat terlebih dahulu dan menyimpan status pengiriman terpisah. Hanya HTTP 200 dan `ok=true` mengonfirmasi pengiriman. |
| Chunk atau lampiran gagal sebagian | Tidak ada checkpoint per chunk/dokumen | Simpan payload ringkasan, daftar chunk/dokumen terkonfirmasi, kebijakan lampiran, jumlah percobaan, serta item pending. Retry hanya bagian yang belum terkonfirmasi. |
| Nomor surat berakhiran tahun+tanggal | `card.textContent` menyatukan teks pada elemen bersebelahan, sedangkan fallback regex menerima rangkaian angka berlebih | Gabungkan text node DOM dengan pemisah. Parser menerima nomor surat yang berakhir pada tahun empat digit. Identitas membaca pola korupsi lama secara kompatibel berdasarkan tanggal kalender valid. |
| Nol kartu BIMA dan RSS 404 terlihat normal | Exception dikonversi menjadi `[]`; satu try/except membungkus semua sumber | Setiap sumber menghasilkan `NEW_ITEMS`, `NO_NEW_ITEMS`, `FAILED`, `DEGRADED`, atau `DISABLED`. Sumber lain tetap diproses. Kegagalan/degradasi menghasilkan exit code 1. Feed Atom valid tanpa entry dapat berarti tidak ada video; dokumen RSS invalid tidak dianggap feed kosong. |
| Video gagal diinspeksi menjadi VOD | Semua fallback inspeksi mengembalikan VOD | Gunakan `UNKNOWN`, periksa ulang, simpan status terakhir yang terkonfirmasi. Jadwal yang lewat tanpa bukti selesai tetap UPCOMING. Transisi UPCOMING -> LIVE tetap mengirim update. |
| Workflow hijau setelah cache gagal dipush | `continue-on-error: true` | Kegagalan commit/push membuat job gagal. Langkah persist berjalan juga setelah error monitor, dan state disimpan sebagai recovery artifact selama 14 hari jika job gagal. |

## Urutan Persistensi dan Retry

1. Baca sumber dan cache; bentuk calon pembaruan di memori.
2. Buat batch jurnal berisi metadata stabil dan ringkasan yang akan dikirim.
3. Tulis jurnal secara atomik sebelum memperbarui cache deteksi.
4. Tulis cache deteksi secara atomik. Jika gagal, jangan mengirim; jurnal tetap tersedia.
5. Kirim bagian yang belum terkonfirmasi, lalu simpan checkpoint setelah setiap konfirmasi.
6. Tandai batch `delivered=true` hanya jika seluruh ringkasan dan lampiran wajib berhasil.
7. Workflow menyimpan dua cache dan jurnal ke repository. Tidak ada penghapusan riwayat.

URL bertanda tangan lampiran tidak dipersist pada jurnal; retry mengambil URL terbaru dari hasil scrape, mencocokkan dokumen berdasarkan nama. Bila URL tidak tersedia, lampiran tetap pending. Ringkasan yang sudah terkirim tidak diulang hanya karena lampiran gagal. Kebijakan lampiran melekat pada batch agar perubahan konfigurasi tidak menganggap lampiran pending selesai.

Jurnal ini file JSON lokal, bukan database atau layanan baru. Tidak ada klaim exactly-once. Jika Telegram menerima pesan tetapi proses crash/timeout sebelum checkpoint tersimpan, retry dapat mengirim duplikat. Jika commit/push gagal, runner berikutnya dapat membaca state remote yang tertinggal; recovery artifact perlu ditinjau dan dipulihkan bersama kedua cache sebelum retry untuk mengurangi duplikasi. Artifact bukan pemulihan otomatis. Proses yang dibatalkan keras sebelum upload artifact juga dapat kehilangan state runner.

Cache lama dianggap riwayat yang sudah pernah diproses, bukan bukti Telegram benar-benar menerima semua notifikasi lama. Fase ini tidak mengirim ulang seluruh riwayat untuk menebak pengiriman masa lalu.

## Migrasi Cache

Migrasi produksi belum dijalankan. Perintah berikut hanya untuk operator setelah persetujuan dan penghentian sementara penulis cache:

```powershell
python migrate_cache.py announcements_cache.json
python migrate_cache.py announcements_cache.json --apply
```

Perintah pertama hanya melaporkan kandidat. `--apply` mencocokkan akhiran tahun+hari dengan tanggal publikasi valid (bulan Indonesia/Inggris), membuat backup `.bak` bernama hash, memverifikasi isinya, memastikan input tidak berubah selama persiapan, menulis hasil secara atomik, lalu memverifikasi hasil. Entri ambigu dibiarkan utuh. Isi selain nomor surat dan urutan/jumlah entri tetap dipertahankan. Jalankan kedua kalinya menghasilkan nol perubahan. Identitas sebelum/sesudah diverifikasi identik sehingga migrasi tidak menghasilkan notifikasi lama sebagai item baru. Jika pemeriksaan hasil gagal, hentikan bot dan pulihkan backup yang terverifikasi; jangan menghapus cache.

## Dry-run dan Pengujian

```powershell
python run_github.py --dry-run
python main.py --dry-run
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
```

Dry-run membaca sumber/cache dan menampilkan preview, tetapi tidak memuat `.env`, tidak mengambil token/chat ID dari environment, tidak mengirim Telegram, tidak mengunduh/unggah lampiran melalui notifier, dan tidak menulis kedua cache maupun jurnal. `--state-dir` dapat dipakai pada runner satu kali untuk lokasi state terisolasi. Mode `console` juga preview tanpa acknowledgement Telegram dan tanpa pembaruan cache.

Test memakai HTML BIMA statis, XML RSS statis, data player sintetis, mock Telegram dengan token/chat ID dummy, serta direktori sementara. Akses Requests nyata dan socket eksternal diblokir oleh fixture otomatis; hanya loopback diizinkan untuk IPC Playwright Windows. Browser fixture memblokir semua request halaman dan tidak membuka situs produksi. `.env` diblokir pada test. Hash SHA256 kedua cache diperiksa sebelum/sesudah seluruh suite, baik pada worktree audit maupun workspace asli.

Kasus yang dicakup: tidak ada/ada item baru; sukses, HTTP error, `ok=false`, JSON invalid, timeout; chunk parsial; lampiran gagal/URL hilang; checkpoint dan replace file gagal; commit/push gagal; BIMA nol; RSS 404; batas DOM dan parser surat; migrasi 80/89 entri sintetis serta salinan cache asli; UNKNOWN/UPCOMING/LIVE/VOD; dry-run; retry pada eksekusi berikutnya; cron tetap.

Hasil lokal (19 Oktober 2026): **49 passed, 0 failed**, `pytest 8.4.2`, Python 3.11, Playwright 1.49.0, dependency proyek terpasang melalui `requirements-dev.txt` pada `.venv` worktree audit. `pip check` tidak menemukan dependency rusak. Uji DOM memakai Chromium lokal melalui `BIMA_TEST_BROWSER`; CI memasang browser yang sesuai Playwright. JUnit lokal tersimpan di `.pytest_cache/reliability-results.xml`.

Percobaan awal menemukan pengaman socket yang memblokir IPC loopback Windows, browser Playwright default belum terpasang, serta pembacaan test tanpa encoding UTF-8. Semua diperbaiki pada test dan diverifikasi ulang sampai seluruh suite lulus. Tidak ada uji produksi untuk menyelesaikan kegagalan tersebut.

Bukti integritas cache: SHA256 awal dan akhir identik. Pada worktree audit: BIMA `5ED05613FA1B246F2E3E5BC88E572DDFD05C0EAA4CEA88E52B21E66858378A91`, YouTube `6CDF8BC5F4F0E6D881F874B8F0B331D1A3E6FC0B502834D1783B9E3CA8119A0B`. Pada workspace asli: BIMA `30B83418F2BCA5DF9D03475398947E3129DD0286B2CC27A898F93A4873B0157B`, YouTube `AFE924BABF30296C65DF9B21AB1CF22963B8C537693CE06FFFA2EAA848EFBFA7`. Perubahan lokal README/cache YouTube pada workspace asli tetap utuh. Pembacaan audit cache BIMA mengonfirmasi 89 entri dengan 80 kandidat perbaikan; penerapan hanya diuji pada salinan sementara.

## Tindak Lanjut Review PR #1 (20 Oktober 2026)

Lingkup tambahan hanya `notifier.py`, `tests/test_notifier_review.py`, dan laporan ini. Tidak ada perubahan runner, sumber, workflow/cron, dependency, atau cache produksi pada tindak lanjut ini.

- `_send_summary_message()` sebelumnya berhenti pada exception, tetapi melanjutkan chunk berikutnya setelah HTTP error atau `ok=false`. Sekarang seluruh kegagalan menghentikan urutan batch. Checkpoint yang sudah terkonfirmasi tetap disimpan; eksekusi berikutnya memakai payload tersimpan dan melanjutkan dari chunk pertama yang belum terkonfirmasi. Payload tidak dibangun ulang saat retry.
- `_build_summary_chunks()` sebelumnya hanya memindahkan satu item ke chunk lain, sehingga item tunggal dengan banyak lampiran tetap dapat melampaui batas. Sekarang splitter memprioritaskan baris utuh, kemudian kata utuh, dan terakhir karakter bila satu kata sangat panjang. Tag bold/italic ditutup dan dibuka kembali pada batas chunk. Entitas HTML dan code point Unicode tidak dipotong; teks tidak dipangkas dan urutannya dipertahankan. Pesan pendek tetap menggunakan format sebelumnya.
- Setiap chunk dibatasi konservatif sampai 4.000 unit UTF-16 pada string HTML lengkap, termasuk tag dan entitas. Ini lebih ketat dari batas pesan [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage), yaitu 4.096 karakter setelah parsing entitas. Emoji non-BMP turut dihitung sebagai dua unit. Pengiriman juga menolak chunk kosong/terlalu panjang yang mungkin sudah tersimpan sebelum perbaikan; antrean tersebut tetap pending dan tidak ditulis ulang otomatis.

Hasil seluruh regression suite pada lingkungan `.venv` yang sama: **69 passed, 0 failed** (49 test sebelumnya + 20 test review), bukan hanya test baru. Test tambahan mencakup HTTP error dan `ok=false` pada chunk pertama/tengah dari tiga chunk; urutan retry; 180/250 lampiran; judul, nomor surat, nama dokumen, URL, dan nama kanal sangat panjang; entitas HTML dan emoji; batas 3.999/4.000/4.001/4.096/4.097; validitas HTML setiap payload; serta guard untuk payload tersimpan yang tidak valid. JUnit diperbarui di `.pytest_cache/reliability-results.xml`.

Seluruh Telegram tetap mock dengan kredensial dummy, fixture pengaman jaringan/secrets dan pemeriksaan hash cache tetap aktif. Tidak ada bot produksi dijalankan, migrasi produksi, merge, atau deployment. Diff tindak lanjut diperiksa terpisah dari perubahan fase 1 agar kedua temuan review menjadi satu-satunya perubahan kode.

## File Diubah

| File | Alasan |
|---|---|
| `scraper.py` | Batas DOM, identitas kompatibel cache lama, cache ketat/atomik, deteksi tanpa persist langsung, error sumber eksplisit. |
| `youtube_monitor.py` | Kegagalan RSS eksplisit, UNKNOWN dan status terkonfirmasi, pemeriksaan ulang stream, proposal cache tanpa persist langsung. |
| `notifier.py` | Return keberhasilan, checkpoint ringkasan/lampiran, HTTP + ok validation, log error tanpa URL credential, file unduhan sementara unik dan cleanup. |
| `monitor_runner.py` | Koordinasi dua sumber, jurnal retry, pemisahan deteksi/pengiriman, preview tanpa side effect. |
| `reliability.py` | Hasil terstruktur dan persist JSON atomik; cache rusak tidak dianggap kosong. |
| `run_github.py`, `main.py` | Runner bersama dan dry-run; exit status GitHub Actions. |
| `app_config.py` | Opsi tidak membaca secrets untuk dry-run. |
| `migrate_cache.py` | Migrasi eksplisit, backup, verifikasi, idempotensi. |
| `.github/workflows/main.yml` | Serialisasi run, production job khusus master, persist saat monitor gagal, error commit terlihat dan recovery artifact; cron tetap. |
| `.github/workflows/tests.yml`, `requirements-dev.txt`, `pytest.ini`, `tests/` | CI tanpa secrets, dependency pengujian, fixture dan test regresi. |
| `README.md`, `docs/reliability-phase1.md` | Prosedur operasional, batas jaminan, dan hasil audit. |

## Risiko dan Persetujuan

- Jurnal dan cache harus dipertahankan bersama. Jangan menghapus jurnal untuk mengatasi error.
- Konkurensi workflow diserialisasi; menjalankan bot lokal bersamaan dengan workflow pada state yang sama belum didukung. Gunakan satu penulis.
- Lampiran pengumuman yang keluar dari halaman BIMA sebelum retry bisa tetap pending karena signed URL perlu diambil kembali. Operator perlu meninjau pemulihan dokumen.
- Inspeksi status masih bergantung pada halaman player YouTube. UNKNOWN dapat bertahan saat akses dibatasi dan akan membuat health DEGRADED.
- Riwayat jurnal bertambah seiring waktu; fase ini tidak melakukan pruning.
- Tidak ada uji end-to-end produksi dan tidak ada perubahan credentials. Perlu review/merge/deployment approval terpisah, persetujuan migrasi produksi, serta keputusan operator tentang pemulihan artifact saat persist remote gagal.
