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

## Identitas BIMA Konservatif (20 Oktober 2026)

Tindak lanjut ini berangkat dari commit `4c3b210` di branch `fix/reliability-phase1`, PR draft #1. **Status cutover tetap NO-GO.** Perubahan ini bukan persetujuan migrasi, merge, atau pengaktifan produksi.

### Temuan dan Batas Bukti

Audit baseline menemukan 89 baris, 87 nomor surat kanonis, dan 88 kelompok judul/dokumen. Nomor surat saja bukan identitas unik. Dua baris workshop artikel ilmiah tanggal 11 Juni memiliki judul, tanggal, dan nama dokumen sama; satu nomor bersih dan satu berakhiran hari. Keduanya tetap disimpan, bukan dihapus atau digabungkan.

Dua pengumuman September berbeda: monitoring PBK/PBW tanggal 6 September dan undangan kontrak Kosabangsa tanggal 2 September. Keduanya tersimpan dengan nomor kanonis `1147/DST/C3/DT.06.01/2026`. Audit sumber sebelumnya menemukan nomor monitoring `1173/DST/C3/DT.06.01/2026` pada [salinan surat kementerian](https://dppm.warmadewa.ac.id/public/uploads/pengumuman/pengumuman_260809080956_pemberitahuan-pelaksanaan-monitoring-dan-evaluasi-skema-pemberdayaan-berbasis-kewirausahaan-pbk-dan-skema-pemberdayaan-berbasis-wilayah-pbw-program-pengabdian-kepada-masyakat-tahun-2026.pdf), dengan [konfirmasi ITS](https://www.its.ac.id/drpm/pemberitahuan-pelaksanaan-monitoring-dan-evaluasi-eksternal-program-pengabdian-kepada-masyarakat-skema-pbk-dan-pbw-tahun-2026/). Nomor resmi undangan Kosabangsa belum terverifikasi. Algoritma tidak menebak nomor tersebut dan tidak memuat pemetaan khusus `1147 -> 1173`.

### Desain dan Aturan Pencocokan

Kunci sekarang menggabungkan nomor surat kanonis dan judul. Normalisasi judul/nama dokumen terbatas pada kapitalisasi, whitespace, Unicode NFKC, dan padanan tanda kutip/dash tipografis; bukan kemiripan kata, stemming, atau fuzzy matching. Tanggal divalidasi sebagai tanggal kalender dengan bulan Indonesia/Inggris. URL lampiran, urutan dokumen, dan waktu scraping bukan identitas.

| Bukti antara sumber dan satu baris historis | Keputusan |
|---|---|
| Nomor kanonis dan judul sama | Riwayat yang sama; konflik tanggal sekaligus dokumen dihentikan. |
| Nomor berubah, judul dan tanggal valid sama | Riwayat yang sama jika kedua daftar dokumen yang tersedia tidak bertentangan. |
| Judul berubah, nomor, tanggal valid, dan seluruh nama dokumen sama | Riwayat yang sama, berdasarkan tiga jangkar yang tetap; tidak memakai kemiripan judul. |
| Nomor sama, judul berbeda, tanggal valid berbeda, dan daftar dokumen tidak beririsan | Pengumuman berbeda, termasuk pasangan September. |
| Judul berulang pada tahun berbeda dengan nomor berbeda | Pengumuman baru; tidak membuat alias lintas tahun. |
| Tidak ada nomor | Judul saja tidak cukup untuk menyamakan riwayat yang tanggal/dokumennya bertentangan. |
| Bukti terkait bertentangan, kurang, atau cocok ke beberapa kunci historis | `BimaIdentityError`, health BIMA `FAILED`, exit code gagal, tanpa menebak. |

Pencocokan dilakukan terhadap metadata baris historis tertentu, bukan alias nomor global. Nama satu lampiran generik yang beririsan tidak membentuk alias. Koreksi nomor tidak disimpan ke baris lama; pencocokan dilakukan lagi dari riwayat asli pada run berikutnya. Duplikat historis dengan kunci komposit identik tidak dianggap dua target berbeda, tetapi seluruh baris cache tetap dipertahankan. Duplikat pada hasil sumber tidak membuat dua kandidat baru.

Jika ambigu, tidak ada proposal cache BIMA yang disimpan dan tidak ada batch BIMA baru atau retry BIMA dikirim pada run tersebut. Batch campuran yang sudah tersimpan juga ditunda utuh, tanpa membangun ulang atau memotong chunk. Sumber lain tetap dapat diproses secara independen. Operator perlu membandingkan judul, tanggal, dan dokumen resmi sebelum menentukan tindak lanjut; menghapus cache/jurnal bukan pemulihan yang aman.

Jurnal sebelum perubahan masih dikenali melalui `items` yang disimpannya, termasuk saat cache sebelumnya gagal ditulis dan nomor sumber kemudian dikoreksi. Kunci, item, chunk, dan checkpoint batch lama tidak ditulis ulang menjadi versi baru. Pemetaan item sumber terbaru untuk retry menggunakan pencocokan BIMA yang sama; kode pencocokan nama/pengiriman lampiran dan `notifier.py` tidak diubah.

Contoh: sebelum perbaikan, monitoring dan Kosabangsa sama-sama berkunci `surat:1147/.../2026`. Sekarang judul membedakan keduanya. Jika sumber kemudian mengganti nomor monitoring menjadi `1173/.../2026`, kombinasi judul, tanggal, dan nama dokumennya tetap mengenali baris monitoring lama, bukan Kosabangsa dan bukan berita baru.

### Kompatibilitas Migrasi dan Hasil Regresi

`migrate_cache.py` tidak perlu diubah: ia hanya memperbaiki kerusakan tahun+hari yang terverifikasi tanggal, dengan backup, verifikasi identitas per baris, dan idempotensi. Penerapan pada salinan sementara tetap memperbaiki 80 nomor, mempertahankan 89 baris dan 88 kunci komposit, serta menghasilkan nol perubahan pada penerapan kedua. Migrasi tidak mengoreksi nomor monitoring menjadi `1173`, tidak menebak Kosabangsa, dan tidak melakukan deduplikasi. Identitas baru mengenali cache lama tanpa mensyaratkan migrasi fisik terlebih dahulu.

Simulasi seluruh baseline:

| Skenario | Baris cache | Kelompok komposit | Kandidat historis yang dikirim ulang |
|---|---:|---:|---:|
| Cache asli, sumber asli | 89 | 88 | 0 |
| Cache asli, sumber dengan nomor kanonis | 89 | 88 | 0 |
| Cache asli, nomor monitoring pada sumber menjadi `1173` | 89 | 88 | 0 |
| Cache hasil migrasi sementara, sumber kanonis atau monitoring terkoreksi | 89 | 88 | 0 |
| Baseline ditambah satu pengumuman sintetis yang berbeda | 90 setelah persist sementara | 89 | Hanya 1 item baru, bukan riwayat |

Seluruh regression suite lokal: **110 passed, 0 failed** (69 sebelumnya + 41 regresi identitas). Pengujian mencakup dua pengumuman September, duplikat Juni, akhiran hari, koreksi nomor tanpa alias global, perubahan minor metadata, konflik metadata, fallback tanpa nomor, dokumen invalid, judul historis hilang, kecocokan historis majemuk, baseline 89 sebelum/sesudah migrasi sementara, item baru, jurnal lama pending/delivered, retry setelah cache gagal ditulis, isolasi sumber, dan penundaan batch campuran yang ambigu. Hasil JUnit lokal: `.pytest_cache/reliability-results.xml`. Hasil CI untuk commit tindak lanjut diperiksa melalui checks PR #1 setelah push, bukan diasumsikan dari CI commit dasar.

Seluruh API Telegram pada test adalah mock dengan kredensial dummy. Pengaman akses jaringan/secrets dan hash empat cache tetap aktif dan lulus; SHA256 sama dengan baseline yang dicatat di atas. Tidak ada pembacaan `.env`, Telegram nyata, migrasi/cache/jurnal produksi, workflow/cron, Secrets, kode YouTube, kode pengiriman Telegram, atau deployment yang diubah. Lingkup tindak lanjut hanya `scraper.py`, integrasi identitas BIMA di `monitor_runner.py`, `tests/test_bima_identity.py`, dan laporan ini.

Keterbatasan tetap ada: tanpa ID pengumuman stabil dari sumber, perubahan serentak seluruh jangkar tidak bisa dibuktikan sebagai pengumuman lama. Aturan konservatif sengaja menunda kasus terkait yang ambigu; operator harus menyelesaikan metadata resmi, bukan mengaktifkan fuzzy matching. Nomor Kosabangsa tetap memerlukan verifikasi. Jaminan pengiriman tetap bukan exactly-once; risiko crash/timeout/checkpoint/commit pada bagian sebelumnya tidak berubah.

## Pemisahan RSS dan Inspeksi Cache YouTube (20 Oktober 2026)

Tindak lanjut dari `092930b`, tetap pada branch `fix/reliability-phase1` dan PR draft #1. **Cutover tetap NO-GO.** Tidak ada mekanisme pengecualian backlog, perubahan identitas BIMA, atau perubahan `notifier.py`.

### Diagnosis RSS

Konfigurasi kanal `UCGo_6l_6kp8H8OHcKcSIeDw` memiliki format valid dan cocok dengan `externalId` halaman [kanal resmi](https://www.youtube.com/@kemdiktisaintek), halaman `/channel/<id>`, serta `videoDetails.channelId` video webinar ASN. Kedua halaman kanal memublikasikan link RSS yang persis sama dengan URL yang dibentuk kode: `https://www.youtube.com/feeds/videos.xml?channel_id=UCGo_6l_6kp8H8OHcKcSIeDw`. Tidak ada alasan berbukti untuk mengganti channel ID atau URL.

Audit sebelumnya pada 20 Oktober sekitar 08.55 WIB menemukan HTTP 404. Tiga probe pada 09.35 WIB kini semuanya HTTP 200, `text/xml`, dengan root Atom yang valid. Pengambilan ulang 09.38 WIB juga berhasil dan parser membaca 11 entri. Ini membuktikan kegagalan tidak konsisten sepanjang waktu; penyebab internal YouTube atau jalur jaringan tidak dapat dipastikan dari respons publik saja. Tidak ditemukan kesalahan konfigurasi kanal atau parser yang menyebabkan HTTP 404. Tidak ditambahkan sumber alternatif, retry tersembunyi, perubahan URL, atau library baru.

Akar masalah yang dapat diperbaiki: `check_new_videos()` sebelumnya memanggil `fetch_videos()` sebelum memuat/memeriksa cache. Exception HTTP/timeout/XML menghentikan seluruh pemeriksaan, sehingga ID yang sudah diketahui ikut tidak diperiksa.

### Perubahan Minimal

- Muat dan validasi cache dahulu; cache rusak tetap gagal, bukan dianggap kosong.
- Tangkap kegagalan RSS yang dikenal (`RequestException`/`SourceError`), simpan ringkasan `rss_error` tanpa pesan exception jaringan mentah, lalu jalankan kembali loop inspeksi ID cache yang sudah ada. HTTP status dipertahankan bila tersedia.
- Tanpa cache yang bisa dipakai, RSS gagal tetap melempar error; tidak membuat cache kosong untuk menganggap pemantauan berhasil.
- Runner memprioritaskan health `FAILED` bila `rss_error` terisi, bahkan ketika inspeksi cache sukses dan menghasilkan kandidat. Kegagalan inspeksi video ikut dilaporkan. Exit code tetap 1, bukan `NO_NEW_ITEMS` atau sukses.
- Proposal cache hasil inspeksi yang valid tetap menggunakan urutan persistensi jurnal-sebelum-cache dan pengiriman terkonfirmasi yang sudah ada. Health gagal tidak membuang pembaruan status yang benar-benar terdeteksi, atau memalsukan keberhasilan pengiriman.
- Saat RSS pulih, `rss_error` direset. UNKNOWN tetap menyimpan status terkonfirmasi sebelumnya; jadwal yang lewat tidak dianggap bukti VOD. Seluruh aturan transisi dan kode pengiriman Telegram tidak diubah.

Lingkup perubahan kode produksi hanya 15 baris diff pada `youtube_monitor.py` dan 6 baris pada pelaporan health `monitor_runner.py`. Identitas BIMA, notifier, workflow/cron, konfigurasi kanal, Secrets/Variables, migrasi, dan cache produksi tidak diubah.

### Simulasi Snapshot Produksi 125 Video

Snapshot master `7ef156d1d7de3d36d9f9ff0177673222876d0753`, blob cache `ff52bfe095084a158f0d048242dd2dab29349acd`, SHA256 `2e615790228d8210961e9089a5b52d6b8767d32e9ea31fa9a2949aca193468f6`. Sumber dibaca pada sekitar 09.38 WIB. Snapshot berisi 119 VOD dan 6 UPCOMING; bukan cache lokal utama yang hanya berisi 9 video dan memiliki perubahan milik pengguna.

| ID cache UPCOMING | Hasil inspeksi watch | Usulan status dan status terkonfirmasi terakhir | Kandidat perubahan status |
|---|---|---|---|
| `u7oQyLh3WoE` | HTTP 200, LIVE_STREAM_OFFLINE, isUpcoming=true; jadwal 21 Oktober 09.00 WIB | UPCOMING / UPCOMING | Tidak |
| `EmT9kEWQA-U` | HTTP 200, player ERROR, video tidak tersedia | UNKNOWN / UPCOMING | Tidak |
| `s4FGn5EMd4U` | HTTP 200, player ERROR, video tidak tersedia | UNKNOWN / UPCOMING | Tidak |
| `a93gPqHSkIY` | HTTP 200, player ERROR, video tidak tersedia | UNKNOWN / UPCOMING | Tidak |
| `OSa7fFmQ90k` | HTTP 200, player ERROR, video tidak tersedia | UNKNOWN / UPCOMING | Tidak |
| `ojkn0KmQASY` | HTTP 200, player ERROR, video tidak tersedia | UNKNOWN / UPCOMING | Tidak |

Tidak ada LIVE atau VOD baru yang terverifikasi di antara enam ID tersebut. Penyebab lima video tidak tersedia tidak dipaksakan menjadi privat/dihapus, dan UNKNOWN tidak diubah menjadi VOD.

Replay `run_once(..., dry_run=True)` memakai salinan sementara snapshot, respons RSS/inspeksi yang dicatat di memori, monitor BIMA dummy, dan notifier mock yang melarang pengiriman. RSS 404/timeout diinjeksikan secara terkontrol, bukan diklaim sebagai hasil jaringan saat RSS sedang pulih.

| Kondisi RSS | Inspeksi | Hasil sumber / exit | Kandidat | Baris proposal di memori |
|---|---|---|---|---:|
| Respons aktual HTTP 200, 11 entri | 6 ID cache + 1 ID baru | DEGRADED / 1, karena 5 UNKNOWN | 0 perubahan status lama; 1 video baru `twhITZaDKgA`, UPCOMING, jadwal 20 Oktober 11.00 WIB | 126 |
| HTTP 404 terkontrol | Keenam ID cache tetap diperiksa | FAILED / 1, menyertakan HTTP 404 dan inspeksi unresolved | 0 | 125 |
| Timeout terkontrol | Keenam ID cache tetap diperiksa | FAILED / 1, menyertakan Timeout dan inspeksi unresolved | 0 | 125 |

Cache sementara tetap byte-identik setelah masing-masing preview; tidak ada jurnal dibuat dan tidak ada metode pengiriman dipanggil. Proposal di memori bukan migrasi atau pembaruan cache produksi. Video baru tetap dapat ditemukan ketika RSS pulih; saat RSS gagal, penemuan video baru memang belum tersedia dan harus tetap terlihat sebagai kegagalan.

### Pengujian dan Risiko Tersisa

Seluruh regression suite: **141 passed, 0 failed** (110 sebelumnya + 31 baru). Cakupan baru meliputi RSS 404/503, timeout, XML invalid, feed berhasil tanpa item baru, inspeksi cache pada RSS gagal, kegagalan watch/JSON/player, UPCOMING -> LIVE, UPCOMING -> UNKNOWN -> LIVE, UPCOMING -> VOD, status tidak berubah, pemulihan RSS, cache kosong/rusak, retry pengiriman mock saat RSS tetap gagal, serta seluruh salinan cache 125 video. Test memastikan sumber tetap FAILED walau status cache berhasil diproses dan mock Telegram menerima pembaruan. JUnit lokal diperbarui di `.pytest_cache/reliability-results.xml`.

Run awal menghasilkan 116 passed dan 25 failed karena fixture monitor bersama masih mengganti `fetch_videos()` dengan stub kosong, sehingga test baru melewati jalur RSS yang hendak diuji. Helper test baru dibetulkan untuk memasang kembali metode RSS asli dan hanya memock respons HTTP. Seluruh suite kemudian dijalankan ulang sampai 141 lulus; tidak ada pengujian Telegram nyata untuk menyelesaikan kegagalan tersebut.

Pengaman jaringan/secrets dan pemeriksaan hash empat cache kedua worktree tetap aktif dan lulus. Semua pengiriman dalam test adalah mock dengan kredensial dummy. Tidak ada pembacaan `.env`, migrasi, penulis cache produksi, commit pada master, merge, atau deployment. Hasil CI untuk tindak lanjut ini ditinjau pada checks PR setelah push.

Lima UNKNOWN dapat bertahan dan membuat health DEGRADED ketika RSS sehat atau FAILED ketika RSS bermasalah. Jika kelak terverifikasi LIVE/VOD, transisi tetap menghasilkan kandidat; tidak ada penandaan historis sebagai sudah terkirim. RSS gagal tetap menghalangi penemuan ID baru, tetapi tidak lagi menghalangi inspeksi ID tersimpan. Penyebab intermiten 404 dan rekonsiliasi historis tetap menjadi risiko/tinjauan operator, bukan alasan membungkam error atau notifikasi.

## DOM BIMA dan Publikasi Bernomor Sama (20 Oktober 2026)

Tindak lanjut dari `e4ac89b`, branch `fix/reliability-phase1`, PR #1 tetap draft.
**Cutover produksi tetap NO-GO.** Lingkup kode produksi hanya `scraper.py`;
YouTube, notifier, runner, workflow/cron, konfigurasi dan cache tidak diubah.

### Bukti Dokumen Sebelum Perubahan

Dua PDF asli dibaca langsung melalui tombol dokumen pada
[halaman BIMA](https://bima.kemdiktisaintek.go.id/pengumuman). Keduanya HTTP 200,
valid PDF, dan memiliki perihal/isi berbeda. Tidak ada nomor surat yang ditebak.

| Dokumen / nama PDF | Nomor dan tanggal surat dalam PDF | Isi | Ukuran / halaman |
|---|---|---|---|
| `Undangan Penandatanganan Kontrak Program Aksara Mahasiswa Tahun Anggaran 2026.pdf` | `1385/DST/C3/DT.06.01/2026`, 6 Oktober 2026 | Undangan acara kontrak 12 Oktober, persiapan berkas dan peserta | 265875 byte / 5 |
| `Pengumuman Penerima Pendanaan Program Aksara Mahasiswa Tahun Anggaran 2026.pdf` | `1386/DST/C3/DT.06.01/2026`, 7 Oktober 2026 | Penetapan pendanaan berdasarkan keputusan 498/KPA/C3/KPT/2026, akses hasil seleksi dan mekanisme pencairan | 188699 byte / 2 |

Keduanya mencantumkan satu berkas lampiran. Nama PDF di atas berasal dari tombol
publikasi BIMA; pemeriksaan isi bukan validasi kriptografis tanda tangan PDF.
SHA256 undangan: `40b994827caa53020d424f1af9b3ad8c2ff33e0e7da20c965afd01af949fd6e6`.
SHA256 pendanaan: `4341b7887d0ea1018bf9c2db28afc85549a8bc42913b60dfc96dc7aad0e7c4ed`.

[Publikasi LLDIKTI II](https://lldikti2.kemdiktisaintek.go.id/pengumuman-penerima-pendanaan-program-aksara-mahasiswa-tahun-anggaran-2026/)
menautkan salinan PDF pendanaan yang berhasil diunduh dan byte-identik dengan
PDF BIMA (ukuran dan SHA256 sama).
[DRPM ITS](https://www.its.ac.id/drpm/pemberitahuan-revisi-proposal-dan-rab-program-aksara-mahasiswa-tahun-2026/)
mendukung konteks tindak lanjut program dan penerima, tetapi bukan bukti langsung
nomor undangan. Halaman undangan dan pendanaan DPPM Warmadewa ditemukan dalam
indeks publik; akses langsung keduanya HTTP 403, sehingga isi PDF Warmadewa
tidak diklaim sudah diverifikasi. Bukti kedua PDF BIMA sendiri sudah memadai.

Kartu BIMA menampilkan nomor `1386` dan tanggal publikasi 7 Oktober untuk kedua
judul, sementara PDF undangan bernomor `1385`. Tanggal publikasi tidak harus
sama dengan tanggal surat. Parser mempertahankan metadata publikasi apa adanya;
tidak mengoreksi nomor/tanggal cache atau menambahkan pengecualian Aksara.

### Aturan Identitas Terbatas

Sebelumnya, nomor/tanggal sama dan dokumen berbeda selalu menghasilkan ambiguitas.
Kini dua judul berbeda pada nomor/tanggal sama dapat dipisahkan hanya jika:

- Tanggal keduanya valid dan sama; himpunan nama dokumen terisi serta sepenuhnya terpisah.
- Masing-masing entri memiliki nama PDF/DOC/DOCX yang stem-nya persis sama dengan judulnya setelah normalisasi konservatif yang sudah ada.
- Masing-masing judul memiliki sedikitnya dua kata alfabetis yang tidak ada pada judul pasangannya. Perubahan satu kata/angka, penambahan frasa saja, dan judul yang saling mencakup tidak cukup.

Perbandingan kata adalah pengaman pemisahan berbasis himpunan kata exact, bukan
similarity/fuzzy matching untuk menyatukan entri. Tidak ada alias nomor global,
nomor hardcoded, pencocokan isi secara perkiraan, atau pengecualian per program.
`announcement_identity()` dan `canonical_surat()` tetap sama. Jangkar koreksi
nomor historis, kegagalan saat beberapa riwayat cocok, dan penundaan metadata
tidak lengkap tetap berlaku. Nomor PBK/PBW versus Kosabangsa dan duplikat Juni
tetap tercakup tanpa penghapusan atau penggabungan data.

Batas penting: nama dokumen bukan hash konten. Bukti PDF pasangan Aksara dibaca
sebelum aturan dibuat; saat runtime aturan memakai metadata entri, bukan
mengunduh/membandingkan konten PDF. Perubahan judul dan nama dokumen secara
besar yang memenuhi semua syarat masih dapat menyerupai publikasi baru; tanpa
ID sumber stabil, kepastian identitas mutlak tidak dapat dijanjikan. Perubahan
yang tidak memenuhi bukti tersebut tetap FAILED, bukan dilewatkan diam-diam.

### Parser dan Fixture

DOM aktual memiliki 10 `article.ap-card`, tetapi selector lama `.card-body`
menghasilkan 0. Nomor kini merupakan field `.ap-card__number` tanpa label
`No. Surat:`. Parser membaca `.ap-card__title`, nomor terminal tahun yang valid,
`time.ap-date[datetime]` (ISO valid, konsisten dengan label tanggal bila tersedia),
serta `.ap-file`/`.ap-file__name` secara berurutan. Tanggal dikonversi ke format
Indonesia penuh agar kompatibel dengan identitas historis. Jalur pengambilan
URL dokumen yang sudah ada dipertahankan, hanya locator tombolnya dipetakan
sesuai layout. HTTP error dan alasan validasi SourceError tetap terlihat.

Legacy tetap didukung oleh parser yang sama. Teks judul dan daftar dokumen
dikeluarkan dari pencarian metadata legacy agar tanggal/angka dalam nama PDF
tidak dianggap tanggal publikasi. Layout campuran, halaman kosong/tidak dikenal,
kartu kosong, judul/nomor/tanggal ganda, tanggal invalid/konflik, tahun+tanggal,
serta dokumen tanpa nama menggagalkan seluruh sumber, bukan dianggap tanpa berita.

Fixture `bima.html` lama dipertahankan. `bima_ap_card.html` menambah dua kartu
Aksara sesuai metadata halaman dan satu kartu sintetis berkarakter khusus serta
banyak dokumen. Baseline `bima_history_89.json` dan `youtube_history_125.json`
dibekukan dari `e4ac89b`, nilai/urutan tetap sama dengan cache pada commit itu.
Test historis dan migrasi tidak lagi bergantung pada jumlah cache operasional.
Test pertumbuhan menambah 0/1/12 entri sintetis di salinan sementara, bukan
mengganti angka ekspektasi cache aktif. Pemeriksaan hash cache kedua worktree tetap aktif.

### Dry-Run Master Terbaru

Snapshot master yang dibaca ulang: `7ef156d1d7de3d36d9f9ff0177673222876d0753`.
Cache BIMA 89 baris, SHA256
`4f7e2e0cc58235c5635622e9a5d84710ab62608017ec97cfa5c61068bd1c4d08`;
YouTube 125 baris, SHA256
`2e615790228d8210961e9089a5b52d6b8767d32e9ea31fa9a2949aca193468f6`.
Keduanya hanya ditulis ke direktori sementara. YouTube dinonaktifkan untuk
preview BIMA ini; bukan klaim verifikasi health YouTube terbaru.

`run_once(..., dry_run=True)` dengan scraper aktual menghasilkan:

- 10 kartu valid, seluruh judul/nomor/tanggal/nama dokumen terbaca; seluruh 13 URL dokumen berhasil diambil.
- 9 kartu cocok dengan riwayat; tepat 1 kandidat baru: **Pengumuman Penerima Pendanaan Program Aksara Mahasiswa Tahun Anggaran 2026**, nomor `1386/DST/C3/DT.06.01/2026`, publikasi 7 Oktober 2026, satu PDF.
- Tidak ada kandidat lain di halaman pertama yang diperiksa. Ini bukan klaim semua halaman arsip telah dirayapi.
- Health BIMA `NEW_ITEMS`, error kosong, exit 0, pending 1. Sebelum perbaikan: nol kartu oleh selector lama; dengan metadata baru tetapi aturan lama: FAILED karena benturan Aksara.
- Simulasi seluruh 89 riwayat: 88 identitas unik, 0 replay. Dua baris duplikat Juni tetap dipertahankan.
- State sementara byte-identik setelah preview, tidak ada jurnal dibuat, pemanggilan pengiriman 0. SHA256 empat cache kedua worktree tidak berubah.

### Pengujian dan Persetujuan

Regresi meliputi kedua DOM, seluruh atribut dan indeks tombol, karakter khusus,
metadata ambigu, kedua publikasi Aksara beserta koreksi nomor undangan, guard
edit minor/berkas generik/dokumen bersama, seluruh baseline 89 melalui kedua
DOM sebelum/sesudah migrasi salinan, dan pertumbuhan cache 89/125. Seluruh
regresi identitas September/Juni, retry pengiriman mock dan YouTube tetap dijalankan.

Run pertama: 178 passed / 3 failed (tanggal semu dari nama PDF legacy dan dua
NameError import test saat pemindahan fixture). Parser metadata dan import
diperbaiki, kemudian seluruh suite diulang. Hasil final lokal: **182 passed,
0 failed** (141 sebelumnya + 41 regresi baru), 25,27 detik. Status CI dicatat
pada laporan PR untuk commit tindak lanjut ini; JUnit lokal tersedia di
`.pytest_cache/reliability-results.xml`.

`migrate_cache.py` tidak diubah: uji salinan tetap memperbaiki 80 nomor, menyimpan
backup, menjaga 89 baris/88 identitas, dan apply kedua menghasilkan 0 perubahan.
Tidak ada migrasi produksi, Telegram nyata, cache/jurnal produksi berubah,
merge, deployment atau modifikasi worktree utama. Blocker DOM/identitas Aksara
selesai pada pengujian terisolasi; keputusan cutover tetap NO-GO dan memerlukan
persetujuan operator terpisah serta peninjauan blocker operasional lainnya.

## Penantian Kesiapan Kartu BIMA (20 Oktober 2026)

Tindak lanjut dari `e79fcde`, PR #1 tetap draft, **cutover tetap NO-GO**.
Diagnosis read-only membuktikan `networkidle` dapat selesai saat kartu masih
kosong: pada satu probe, networkidle sekitar 2,86 detik dan respons API baru
tiba sekitar 3,45-3,56 detik. Tiga probe berhasil pada saat ekstraksi, tetapi
ini tidak menjelaskan secara pasti semua kegagalan pengambilan sebelumnya.

Jeda tetap 3000 ms sesudah navigasi kini diganti `_wait_for_cards()` dengan
batas eksplisit 15000 ms. Penantian mengamati kartu yang didukung beserta field
dasarnya, bukan HTTP 200 atau container kosong saja. Untuk ap-card: judul,
nomor, datetime dan nama dokumen; untuk legacy: heading dan nama dokumen.
Seluruh kartu dan seluruh nama dokumen yang ditemukan harus memenuhi kesiapan dasar. Layout campuran
diteruskan ke validator ambiguitas yang sudah ada, bukan dianggap siap valid.

Penantian ini bukan parser baru: validitas tanggal/nomor, field ganda, identitas
konservatif dan penanganan dokumen tetap menggunakan kode yang telah lulus.
Timeout menghasilkan `SourceError` berisi fase, batas ms dan nama layout saja;
tanpa body, URL halaman/API, raw exception, signed URL atau credentials.
Halaman kosong/tidak dikenal tetap FAILED, bukan NO_NEW_ITEMS. Navigasi tetap
memiliki timeout 30 detik; penantian DOM adalah batas terpisah 15 detik, bukan
15 detik untuk keseluruhan proses termasuk pengambilan dokumen.

Regresi baru mencakup kartu modern/legacy langsung siap, rendering lebih dari
3 detik sesudah networkidle, container parsial, kartu terakhir belum siap,
halaman kosong/tidak dikenal, timeout yang aman, metadata ambigu/invalid,
serta propagasi FAILED melalui runner dan cleanup browser tanpa state write.
Seluruh suite dijalankan, bukan hanya test baru. Hasil final lokal:
**201 passed, 0 failed**, 76,51 detik (182 sebelumnya + 19 regresi baru).
Run antara sebelum melengkapi kasus hidrasi metadata menghasilkan 196 passed;
setelah perubahan terakhir seluruh suite dijalankan ulang sampai 201 passed.
Fixture historis 89 BIMA/125 YouTube tetap independen dari pertumbuhan cache
operasional. Status CI untuk commit tindak lanjut dicatat pada komentar PR.

### Baseline Master dan Dry-Run Penguatan DOM

Master terbaru yang dipakai: `f4353d147ccafa0fb65240ec9e8d1e670eb86888`, diverifikasi
ulang sebelum simulasi. Sejak `7ef156d` hanya ada satu commit cache otomatis
`f4353d1`, yang mengubah `youtube_cache.json`, bukan kode atau cache BIMA.

| Cache master | Jumlah | SHA256 |
|---|---:|---|
| BIMA | 89 | `4f7e2e0cc58235c5635622e9a5d84710ab62608017ec97cfa5c61068bd1c4d08` |
| YouTube | 128 | `99e46f88d39947cdf72794aaec33eaa0f3b4cec37867092a52cfac78866dc388` |

Tiga video sudah diproses, bukan kandidat baru: `twhITZaDKgA` (VOD),
`Bb1gSvrljLE` (UPCOMING), dan `IUpiyaiB5e4` (UPCOMING), menurut cache master.
[Log produksi](https://github.com/Galih-Hermawan-Unikom/bima-dikti-news/actions/runs/39392516567)
mencatat tiga item dan satu ringkasan terkirim. [Pesan kanal 270](https://t.me/dikti_news/270)
memuat ketiga ID pada 20 Oktober 2026 pukul 15.20.20 WIB. Status watch terkini
tidak diinspeksi ulang pada fase penguatan BIMA ini. Cache branch yang lebih
lama tidak boleh dipakai untuk menimpa 128 entri master saat cutover.

Simulasi hanya di direktori sementara: tambah satu entri Aksara jika belum ada,
lalu jalankan migrasi salinan dan verifikasi backup. Hasil yang diobservasi,
bukan asumsi jumlah: 89 menjadi 90 baris, 88 menjadi 89 identitas unik,
80 perbaikan nomor, 0 pada migrasi kedua dan 0 append kedua. Seluruh entri lama
tetap berurutan, tanpa penghapusan/perubahan field selain surat yang diperbaiki;
0 replay historis. Cache YouTube 128 baris tetap byte-identik. Tidak ada batch
delivered buatan atau jurnal produksi dibuat. SHA256 BIMA hasil simulasi tetap
`711fca981035df2552be57514ee2f136af5b9a551e2631b7547149ee5170e0c6`.

Tiga run penguatan awal, dengan jeda 20 detik, masing-masing membaca 10 kartu /
13 dokumen, menunggu 1031 / 594 / 718 ms setelah networkidle (awal 0 kartu),
dan menghasilkan NO_NEW_ITEMS, pending 0, exit 0. Sesudah kondisi kesiapan semua
nama dokumen diperketat, satu run mengalami timeout 15000 ms dan tetap FAILED.
Helper audit pada run gagal berhenti karena assertion hasil sukses; ini bukan
pengiriman, kehilangan state atau pengembalian NO_NEW_ITEMS oleh aplikasi.
Penyebab sumber pada kejadian timeout itu tidak direkam lengkap dan tidak ditebak.
Dua probe diagnostik kemudian READY dalam 953 / 875 ms dengan API HTTP 200 dan
seluruh field/nama dokumen terisi.

Dua dry-run lengkap berikutnya pada implementasi final, dengan jeda 20 detik,
masing-masing membaca 10 kartu / 13 dokumen: 0 kartu sebelum penantian, 10
sesudahnya, waktu tunggu 625 / 625 ms, NO_NEW_ITEMS, error kosong, pending 0,
exit 0. Judul/nomor/tanggal/nama seluruh dokumen tetap memakai parser yang sama;
tidak ada kandidat Aksara, identitas ambigu atau pengumuman historis terdeteksi
ulang pada state gabungan. Angka 10 adalah hasil observasi, bukan syarat kode.

Timeout nyata tetap dicatat sebagai risiko kestabilan sumber: penantian terbatas
tidak menjamin sumber selalu tersedia. Tidak ditambahkan retry, fallback atau
perubahan parser untuk memaksakan sukses. Seluruh preview tanpa Telegram dan
tanpa perubahan state sementara, lalu direktori sementara dibersihkan. Hash
cache lokal tetap sama; worktree utama tidak dipakai sebagai sumber cache.
**GO untuk penguatan kode teruji; cutover tetap NO-GO** sampai preflight,
baseline final, backup/rollback, persetujuan penerapan serta health sumber
ditinjau operator.

### Desain Preflight Telegram GitHub: Belum Diimplementasikan

Tidak ada workflow preflight dibuat/diaktifkan/dijalankan pada perubahan ini.
Workflow monitoring bukan sarana preflight karena menjalankan scraper dan
pengiriman. Desain ini memerlukan persetujuan operator terpisah:

1. Workflow terpisah khusus `workflow_dispatch`, tanpa trigger push, pull_request, pull_request_target, schedule, reusable workflow atau workflow_run. GitHub mensyaratkan file workflow manual tersedia pada default branch; file di draft PR saja bukan jalur eksekusi preflight yang sudah tersedia.
2. Review workflow-only pada default branch tepercaya, guard event manual dan `refs/heads/master`, serta hanya dispatch ref tersebut. Jangan checkout atau menjalankan kode dari PR/input ref. Guard bukan perlindungan terhadap penulis repository berbahaya yang dapat mengubah workflow: akses dispatch/penulisan dan review tetap harus dikendalikan. Protected environment hanya dapat dipertimbangkan setelah review terpisah; jangan membuat environment atau memindahkan Secrets tanpa persetujuan.
3. Gunakan GitHub-hosted runner bersih, timeout job pendek, `permissions: {}`, tanpa checkout, artifact, cache atau dependency pihak ketiga. Kode inline yang direview memakai Python standard library. Tidak perlu izin tulis repository atau `GITHUB_TOKEN` untuk Bot API.
4. Inject hanya Secrets `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` ke satu step pemeriksaan, serta Variables metode notifikasi dan pengiriman lampiran dengan fallback dan scope yang sama seperti workflow produksi. Workflow produksi saat ini tidak memakai environment: menambahkan environment pada preflight dapat mengubah precedence Secrets/Variables dan memvalidasi konfigurasi yang berbeda. Jangan menyalin Secrets ke file, input dispatch, output job atau mesin lokal. Jangan membaca `.env`.
5. Allowlist tepat tiga metode GET: `getMe`, `getChat`, `getChatMember`. HTTP harus sukses dan `ok=true`. Identitas wajib bot `@bima_dikti_bot`, target channel publik `DIKTI News` / `@dikti_news`, serta bot owner atau administrator dengan `can_post_messages=true`. Bot ID dan chat ID hanya dipakai internal. Tujuan eksperimen/private chat harus ditolak.
6. Tampilkan hanya nama/username bot, nama/username/tipe kanal, hasil izin posting dan kebijakan efektif lampiran. Kebijakan lampiran harus boolean valid dan disetujui operator; jangan mengasumsikan default false sama dengan konfigurasi produksi. Log produksi terakhir menunjukkan override true.
7. Tangkap error tanpa mencetak exception mentah, request URL, token, ID numerik, JSON respons, environment dump atau debug HTTP/shell tracing. Batasi request ke HTTPS api.telegram.org, tolak redirect, gunakan timeout HTTP dan job eksplisit. Hentikan pemeriksaan dengan exit nonzero saat validasi gagal. Tidak ada sendMessage/sendDocument/getUpdates, scraper, scheduler, persistensi cache atau jurnal.

Prasyarat: persetujuan workflow-only terpisah, review kode yang memegang Secrets,
akses operator untuk dispatch, dan konfirmasi expected identities serta kebijakan
lampiran. Risiko tersisa: workflow manual yang menjalankan kode tidak tepercaya
dapat membocorkan Secrets meski namanya preflight; pembatasan ref/akses dan review
adalah syarat, bukan sekadar label READ-ONLY. Preflight hanya membuktikan identitas,
akses dan izin pada waktu pemeriksaan, bukan keberhasilan pengiriman nyata.
Catat waktu/ref/SHA dan kebijakan efektif yang aman; perubahan Secrets/Variables
sesudah preflight membatalkan bukti konfigurasi dan memerlukan pemeriksaan ulang.

Referensi resmi:
[manual workflow](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[penggunaan Actions yang aman](https://docs.github.com/en/actions/reference/security/secure-use),
[Telegram Bot API](https://core.telegram.org/bots/api).

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
- Inspeksi status masih bergantung pada halaman player YouTube. UNKNOWN dapat bertahan; pengecualian severity historis yang sempit dijelaskan di bawah.
- Riwayat jurnal bertambah seiring waktu; fase ini tidak melakukan pruning.
- Tidak ada uji end-to-end produksi dan tidak ada perubahan credentials. Perlu review/merge/deployment approval terpisah, persetujuan migrasi produksi, serta keputusan operator tentang pemulihan artifact saat persist remote gagal.

## Severity Inspeksi Historis YouTube

WARNING hanya berlaku jika video sudah ada dalam cache, `scheduled_start` cache
valid dan berzona waktu, jadwal sudah lewat minimal tujuh hari terhadap UTC, dan
player secara eksplisit menyatakan video tidak tersedia: HTTP sukses, tanpa
`videoDetails`, `playabilityStatus.status=ERROR`, dan alasan unavailable yang
dikenali dalam bahasa Indonesia/Inggris. Alasan tidak dikenal tetap fail-closed.
Tidak ada daftar pengecualian ID. Jadwal lewat sendiri bukan bukti VOD.

Video tetap UNKNOWN dengan `last_known_status` dan jadwal cache dipertahankan,
termasuk ketika RSS tidak lagi memuat jadwalnya. Video tetap diperiksa ulang pada
run berikutnya; pemulihan UPCOMING -> LIVE/VOD tetap menjadi kandidat satu kali.
Penanda unavailable hanya dipakai saat inspeksi, tidak disimpan sebagai field cache.

Log sumber menampilkan `Historical unavailable inspections: N`. WARNING dapat
berdampingan dengan kandidat baru valid dan tidak menghasilkan exit gagal sendiri.
Video baru UNKNOWN, jadwal mendatang/kurang dari tujuh hari/tidak valid, timeout,
HTTP error, player hilang/rusak, atau alasan lain tetap DEGRADED. RSS/BIMA gagal
tetap FAILED. Error Telegram, checkpoint, persistensi, commit/push tetap kritis;
tidak ada perubahan pada notifier, jurnal, workflow, atau cron.

Pengujian hanya memakai fixture, cache sementara, dan mock API; tidak mengirim
Telegram atau mengubah cache operasional. Perubahan ini memerlukan PR dan
persetujuan merge/aktivasi terpisah. Workflow produksi tetap dinonaktifkan.

Validasi lokal perubahan severity: 255 passed (221 regresi sebelumnya + 34 kasus
baru), termasuk batas tujuh hari, timezone, alasan unavailable eksplisit,
recovery LIVE/VOD tanpa replay, RSS/BIMA gagal, serta error pengiriman/checkpoint/
persistensi. Pemeriksaan integritas cache operasional tetap lulus.
