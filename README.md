# Dikti News Bot — Monitoring Informasi Pendidikan Tinggi

Bot Python yang memantau informasi dari tiga sumber resmi Kementerian Pendidikan Tinggi, Sains, dan Teknologi (Kemdiktisaintek) dan mengirim pemberitahuan yang memenuhi kriteria ke kanal Telegram **[@dikti_news](https://t.me/dikti_news)**:

- 🏛️ **[BIMA](https://bima.kemdiktisaintek.go.id/pengumuman)** — pengumuman pendanaan, penelitian, pengabdian, dan program terkait; ringkasan serta lampiran PDF yang tersedia.
- 📺 **[YouTube Kemdiktisaintek](https://www.youtube.com/@kemdiktisaintek)** — video reguler dan rekaman siaran langsung (replay) yang telah terverifikasi.
- 📢 **[Pengumuman situs Kemdiktisaintek](https://kemdiktisaintek.go.id/announcement)** — pengumuman resmi, tenggat jika ada, dan dokumen PDF yang memenuhi persyaratan.

Monitoring berjalan melalui GitHub Actions tanpa memerlukan komputer lokal yang terus menyala. Tiap sumber mempunyai kebijakan kelayakan, baseline, dan state operasional tersendiri untuk mencegah pengiriman ulang konten lama. Pengiriman Telegram bukan arsip lengkap semua konten sumber.

**Implementasi pada branch ini:** Unified Production Scheduler siap direview, belum diaktifkan. Merge dan cutover memerlukan persetujuan operator. Nilai Variables dan state remote tidak diubah oleh pengembangan ini. Prosedur cutover/rollback tersedia di [panduan operasi](README_KEMDIKTISAINTEK_PRODUCTION.md).

## Unified Production Scheduler

Satu workflow [dikti-news-production.yml](.github/workflows/dikti-news-production.yml) mempunyai dua cron UTC yang saling lepas, total **24 pemicu nominal per hari**. Keputusan menjalankan BIMA memakai `github.event.schedule`, sehingga runner yang terlambat tidak mengubah giliran sumber.

| Sumber | Interval | UTC | WIB |
|---|---|---|---|
| BIMA | 3 jam | 00.17, 03.17, 06.17, 09.17, 12.17, 15.17, 18.17, 21.17 | 01.17, 04.17, 07.17, 10.17, 13.17, 16.17, 19.17, 22.17 |
| YouTube | 1 jam | Menit 17 setiap jam | Menit 17 setiap jam |
| Pengumuman Kemdiktisaintek | 1 jam | Menit 17 setiap jam | Menit 17 setiap jam |

Cron `17 */3 * * *` menjalankan tiga sumber; `17 1-23/3,2-23/3 * * *` menjalankan YouTube dan Kemdiktisaintek. Keduanya tidak beririsan. Sebelumnya tiga workflow produksi menghasilkan 104 pemicu/hari (8 + 48 + 48), di luar 48 pemicu scheduler eksperimen.

Job `bima`, `youtube`, dan `kemdiktisaintek` independen: kegagalan satu job tidak melewati dependency ke sumber lain. Untuk melindungi commit pada branch state bersama, setiap job memegang concurrency group `dikti-production-state`, `cancel-in-progress: false`, `queue: max`. Job antre dan memproses secara bergantian; tidak ada lock pada workflow induk atau dispatch child workflow. Setiap job memeriksa ulang approved SHA terhadap HEAD master setelah antre, sebelum menjalankan runtime. SHA/CAS tetap wajib di semua checkpoint.

Workflow lama `main.yml`, `youtube-production.yml`, dan kedua scheduler Kemdiktisaintek menjadi manual-only dalam perubahan yang sama. Jalur recovery produksi lama diblokir saat unified switch aktif. Scheduler eksperimen menjadi manual-only juga; tujuan dan kredensial eksperimen tetap terpisah. Perubahan YAML belum berlaku di GitHub sampai PR digabung.

GitHub cron dapat terlambat atau terlewat. `queue: max` menampung maksimal 100 job/run pending, bukan jaminan waktu mulai atau eksekusi setiap slot. [Dokumentasi concurrency GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency) menjelaskan batas antrean ini.

## Arsitektur

```mermaid
flowchart TD
    GH["Satu workflow · 24 slot/hari"] --> G["Master + approved SHA + unified switch"]
    G --> B["Job bima · setiap 3 jam"]
    G --> Y["Job youtube · setiap jam"]
    G --> K["Job kemdiktisaintek · setiap jam"]
    B --> RB["production_runtime.py bima"]
    Y --> RY["production_runtime.py youtube"]
    K --> RK["kemdiktisaintek_prod_runtime.py POLL"]
    RB --> SB["bima_production.json"]
    RY --> SY["youtube_production.json"]
    RK --> SK["kemdiktisaintek_production.json"]
    L["Shared job lock · dikti-production-state · SHA/CAS"] -.-> SB
    L -.-> SY
    L -.-> SK
    SB --> T["Preflight + durable claim + Telegram @dikti_news + receipt"]
    SY --> T
    SK --> T
```

Ketiga runtime, identitas event, baseline, activation fence, dan outbox tetap terpisah. Tidak ada deduplikasi lintas sumber. Reservasi dan claim wajib tersimpan secara durable sebelum POST. AMBIGUOUS dan hasil parsial memerlukan rekonsiliasi operator.

## Aturan deteksi dan kelayakan

### BIMA

- Pengumuman dibandingkan dengan cache, jurnal historis, initial inventory, dan outbox produksi menggunakan identitas pengumuman kanonis.
- Capture startup menjadi **initial inventory** dan tidak dikirim. Activation fence disimpan saat inisialisasi menggunakan waktu UTC aktual.
- Karena sumber BIMA menyediakan tanggal tanpa jam, tanggal publikasi harus **sesudah tanggal aktivasi dalam WIB** dan tidak boleh melampaui tanggal observasi. Pengumuman pada hari aktivasi atau lebih lama tidak otomatis memenuhi syarat.
- Kandidat yang pertama kali terlihat lebih dari 24 jam lalu ditahan. Tidak ada backfill otomatis.
- Ringkasan memakai HTML Telegram; dokumen dikirim dengan nama metadata yang aman, bukan nama tempfile.

**PDF BIMA tetap didukung.** Adapter unified menetapkan `TELEGRAM_SEND_FILES: 'true'`, dan runtime produksi secara eksplisit memakai `telegram_send_files=True`. Nilai `telegram_send_files: false` dalam `config.json` tidak mematikan lampiran pada jalur produksi ini. Jumlah lampiran mengikuti dokumen yang tersedia pada pengumuman.

### YouTube tabs-only

Kebijakan produksi `tabs-only-v1` memakai `first_seen_at` persisten. Nilai ini adalah waktu observasi pertama, **bukan `published_at`** dan bukan bukti bahwa video baru diterbitkan.

Video hanya dapat dikirim jika seluruh syarat terpenuhi:

1. Identitas belum dikenal cache/baseline/jurnal, bukan initial inventory, dan pertama kali teramati setelah activation fence produksi.
2. Kedua tab `/videos` dan `/streams` sehat, dengan dua capture yang stabil dan berjarak minimal **10 menit**.
3. Identitas kanal, provenance observasi, kategori, dan readiness tervalidasi. Keanggotaan tab dapat menjadi bukti kategori sesuai kebijakan, tetapi tidak menggantikan pemeriksaan kondisi lain.
4. `/videos`: video reguler yang telah publik dan terverifikasi. `/streams`: hanya **completed replay** dengan bukti penyelesaian yang tervalidasi pada observasi stabil.
5. Bukti masih segar, tidak bertentangan, dan event belum pernah dikirim atau memiliki status pengiriman ambigu.

Shorts ditolak. LIVE, upcoming, kategori UNKNOWN, replay yang belum terbukti siap, capture parsial/gagal, atau bukti tidak cukup tetap **HOLD** dan tidak dikirim. UNKNOWN adalah status pengetahuan identitas, bukan izin pengiriman; hanya keputusan **ELIGIBLE** yang dapat masuk jalur pengiriman.

| Parameter produksi | Nilai |
|---|---|
| Window sejak `first_seen_at` | 24 jam |
| Evidence TTL | 4 jam |
| Clock skew | 5 menit |
| Jarak minimum capture | 10 menit |

Pembaca tab dibatasi 30 item per tab; pagination belum ditangani. Video yang tidak terlihat pada satu capture tidak dianggap dihapus. Perubahan metadata tidak menjadikan `video_id` lama sebagai video baru. Runtime YouTube mengirim maksimal satu event baru per siklus.


### Pengumuman situs Kemdiktisaintek

Sumber ini memantau [halaman pengumuman resmi](https://kemdiktisaintek.go.id/announcement) secara independen dari BIMA, menggunakan UUID pengumuman sebagai identitas. Pengumuman baru tidak otomatis dikirim hanya karena muncul dalam hasil pengambilan data:

- Kandidat harus melewati pemeriksaan kesehatan sumber, waktu publikasi/observasi, detail terpercaya, baseline, dan activation fence. Semua kategori/topik resmi diterima; classifier lama hanya menjadi diagnostik dan tidak menentukan admission maupun delivery.
- Artikel yang sudah ada saat inisialisasi tidak dikirim ulang. Kandidat revisi memerlukan pemeriksaan; tidak diperlakukan sebagai berita baru secara otomatis.
- Ringkasan memuat judul, tanggal, nomor surat dan tenggat **jika tersedia**, tautan sumber, serta daftar dokumen. PDF resmi yang memenuhi validasi dapat dikirim lewat `sendDocument`.
- Outbox dan receipt terpisah dari BIMA/YouTube. `AMBIGUOUS` atau pengiriman parsial perlu rekonsiliasi operator, tanpa retry otomatis.
- Inventaris baseline hanya meliputi jendela pengamatan terbaru (`archive_complete=false`), **bukan klaim telah mengarsipkan seluruh pengumuman historis**.

Policy `kemdiktisaintek-delivery-v3-all-official` bermigrasi secara eksplisit dari policy independen v2 melalui checkpoint tersendiri; baseline, fence, first-seen, admission lama, revisi, dan receipt tetap dipertahankan. **HOLD/NEEDS_REVIEW lama tidak dipromosikan** oleh perubahan policy, pemulihan gap, atau restart.

| Waktu/bukti | Batas dan makna |
|---|---|
| Interval rencana | 1 jam; bukan waktu aktual polling |
| Gap antar-capture sukses | Maksimal 12 jam (1 + toleransi 11 jam); gap lebih besar tetap HOLD |
| Publikasi resmi | Harus setelah activation fence, tidak di masa depan, dan berumur maksimal 72 jam |
| First-seen | Maksimal 24 jam; dipertahankan saat restart |
| Bukti detail dan admission | Maksimal 10 menit; diperiksa ulang sebelum reservation dan claim |
| Preflight Telegram | Tetap mengikuti TTL proof produksi yang ketat |

Jendela publikasi 72 jam menangani tanggal detail yang diberi waktu tengah malam dan keterlambatan beberapa cron. Ini tidak membuka backfill: UUID yang sudah diamati, baseline, revisi, serta admission kedaluwarsa tetap tidak menjadi berita baru. Batas gap 12 jam adalah ambang kontinuitas maksimum, bukan jaminan scheduler. Gap nyata sekitar 14 jam pada audit tetap akan ditahan. `last_successful_poll_at` mencatat capture sukses; `last_healthy_poll_at` hanya diperbarui jika kontinuitas sehat.

POLL memakai API tanpa kategori dan `search=` kosong. Scan minimal dua halaman, dilanjutkan sampai halaman penuh dengan 10 UUID dikenal atau akhir daftar, maksimum **10 halaman/100 item**. Semua UUID baru dalam capture diprioritaskan untuk detail (maksimal 100), ditambah hingga tiga detail dikenal yang berotasi. Urutan `created_at` sumber harus menurun, total stabil, dan halaman pertama dicek ulang; tanggal ini hanya bukti traversal, bukan tanggal publikasi. Kehilangan overlap, pergeseran pagination, urutan tak terbukti, atau batas scan tanpa boundary menahan admission. Cakupan ini terbatas; bukan klaim seluruh arsip sudah diperiksa.

## State, deduplikasi, dan pemulihan

| Lokasi | Peran |
|---|---|
| `master/announcements_cache.json` | Referensi historis BIMA: 90 entri. |
| `master/youtube_cache.json` | Referensi historis YouTube: 129 entri. |
| `master/delivery_state.json` | Jurnal historis dan baseline 133 ID YouTube. Identitas Shorts historis tetap disimpan untuk penekanan replay, tanpa memonitor Shorts. |
| Branch `dikti-production-state`, `.bot-state/bima_production.json` | Activation fence, initial inventory, first-seen, outbox, progres dokumen, dan receipt produksi BIMA. |
| Branch `dikti-production-state`, `.bot-state/youtube_production.json` | Activation fence, initial inventory, capture/observasi persisten, event key, outbox, dan receipt produksi YouTube. |
| Branch `dikti-production-state`, `.bot-state/kemdiktisaintek_production.json` | Baseline UUID, activation fence, observasi, admission, durable outbox, dan receipt sumber pengumuman Kemdiktisaintek. |
| Branch `youtube-state`, `.bot-state/youtube_delivery_state.json` | State eksperimen, terpisah dari produksi; tidak menjadi fallback produksi. |

File historis di `master` hanya dibaca oleh runtime produksi; jumlah referensinya diperiksa, dan hash ketiganya dibandingkan sebelum/sesudah eksekusi. State operasional baru disimpan pada branch state, bukan ditulis kembali ke cache historis.

Saat inisialisasi, video/pengumuman yang sudah terlihat dimasukkan ke initial inventory tanpa pengiriman. Fence dan inventory tetap digunakan setelah restart; inisialisasi terhadap state yang sudah diinisialisasi tidak menimpanya. Jangan menghapus state, mengubah fence, atau mereset inventory untuk memaksa pengiriman.

Setiap event memakai kunci idempoten: BIMA berdasarkan identitas pengumuman kanonis, YouTube berdasarkan `video_id` dan event awal, serta Kemdiktisaintek berdasarkan UUID sumber. Sebelum Telegram dipanggil, reservasi dan status percobaan pengiriman harus berhasil tersimpan di remote melalui **GitHub Contents API dengan SHA/CAS**. Konflik SHA atau kegagalan checkpoint menghentikan proses; tidak ada fallback ke salinan lokal yang basi.

Status outbox:

| Status | Makna dan tindakan |
|---|---|
| `reserved` | Reservasi tersimpan; belum ada percobaan pengiriman yang diklaim. |
| `sending` | Percobaan sudah diklaim secara durable sebelum API Telegram. |
| `delivered` | Respons terkonfirmasi: HTTP 200, `ok=true`, dan `message_id` valid; receipt disimpan. |
| `ambiguous` | Hasil tidak dapat dipastikan, termasuk pemulihan percobaan yang terputus. Wajib rekonsiliasi operator; tidak dikirim ulang otomatis. |

Jalur produksi membatasi satu percobaan per event. Untuk BIMA, satu percobaan mencakup ringkasan dan lampirannya; progres yang berhasil disimpan per checkpoint. Jika sebagian terkirim, jangan mengulang seluruh pengumuman. Perlindungan ini menghindari resend otomatis saat hasil jaringan tidak pasti, tetapi bukan jaminan bahwa setiap gangguan selalu menghasilkan pesan terkirim.

### Controlled backfill

Backfill bukan bagian dari siklus terjadwal. Pengecualian yang tersedia dibatasi pada **satu identitas BIMA** melalui mode manual `backfill-1454`, dengan pemeriksaan judul, nomor surat, tanggal, sumber resmi, PDF, ukuran/hash, jurnal, dan outbox. Mode ini memakai kunci event serta reservasi CAS produksi yang sama. Event yang sudah delivered menghasilkan `SKIPPED`; status pending/ambigu memerlukan rekonsiliasi.

Contoh yang telah selesai:

- Surat `1454/DST/C3/DT.06.01/2026`, tanggal **22 Oktober 2026**.
- **Pemberitahuan Jadwal Pelaporan dan Monitoring dan Evaluasi Internal Program Pengabdian kepada Masyarakat Tahun Anggaran 2026**.
- Ringkasan Telegram [#272](https://t.me/dikti_news/272) dan PDF [#273](https://t.me/dikti_news/273), keduanya HTTP 200 dan `ok=true`; outbox berstatus `delivered`.
- PDF utama berukuran **285.439 byte**. Receipt dan bukti pengecualian tersimpan pada outbox produksi.

Keberhasilan tersebut tidak membuka backfill umum. Pengecualian identitas lain memerlukan keputusan operator dan pemeriksaan tersendiri, bukan penghapusan histori atau perubahan activation fence.

## Konfigurasi GitHub Actions produksi

Atur melalui **Settings → Secrets and variables → Actions**. Jangan menaruh token atau chat ID aktual dalam kode, log, issue, atau repository.

### Secrets

| Nama | Kegunaan |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token bot produksi yang memiliki hak posting pada kanal resmi. |
| `TELEGRAM_CHAT_ID` | Tujuan produksi: ID numerik kanal resmi yang terverifikasi atau `@dikti_news`. |
| `GITHUB_TOKEN` | Disediakan otomatis oleh GitHub Actions; workflow meminta `contents: write` untuk checkpoint branch state. Tidak perlu menyalin token ini ke repository. |

### Variables dan kill switch

| Nama | Nilai untuk produksi aktif | Fungsi |
|---|---|---|
| `PRODUCTION_TELEGRAM_CHAT_ID_SHA256` | SHA-256 ID numerik kanal resmi | Allowlist wajib. Hash dihitung dari string ID numerik UTF-8 tanpa spasi/newline, **bukan** dari `@dikti_news`. |
| `BIMA_PRODUCTION_ENABLED` | `true` | Mengizinkan job BIMA terjadwal. |
| `BIMA_PRODUCTION_AUTO_SEND` | `true` | Mengizinkan pengiriman BIMA. Ubah menjadi `false` untuk menghentikan pengiriman tanpa menghapus state. |
| `YOUTUBE_PRODUCTION_ENABLED` | `true` | Mengizinkan job YouTube terjadwal. |
| `YOUTUBE_PRODUCTION_AUTO_SEND` | `true` | Mengizinkan pengiriman YouTube. Ubah menjadi `false` untuk menghentikan pengiriman tanpa menghapus state. |
| `KEMDIKTISAINTEK_PRODUCTION_ENABLED` | `true` | Mengizinkan operasi sumber Kemdiktisaintek. |
| `DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED` | `true` hanya setelah cutover disetujui | Master switch unified; default OFF. |
| `KEMDIKTISAINTEK_PRODUCTION_SCHEDULER_ENABLED` | Legacy | Hanya jalur scheduler lama manual; tidak mengendalikan unified. |
| `KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND` | `true` | Mengizinkan pengiriman kandidat Kemdiktisaintek yang lolos kriteria; set `false` untuk menghentikan pengiriman. |
| `KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA` | SHA penuh HEAD `master` yang ditinjau | Wajib cocok dengan HEAD master dan SHA workflow unified; semua sumber ditahan jika tidak cocok. Commit baru memerlukan rollover terkendali. |

Saklar boolean produksi menggunakan string persis `true`; default adalah **OFF** jika tidak diatur. Pengiriman setiap sumber mensyaratkan saklar source dan auto-send aktif. Unified memerlukan approved SHA yang cocok dengan `master` dan unified switch. Dispatch manual unified default `observation_only=true` hanya membaca dan memvalidasi snapshot tiga file state pada job planner berizin `contents:read`, tanpa Secrets Telegram. Ketiga job produksi dilewati. Rehearsal boleh berjalan saat unified switch OFF, tetapi approved master SHA tetap wajib. Ini bukan capture sumber atau checkpoint produksi; tidak memulai first-seen, admission, migrasi, atau reservation. Untuk menjalankan runtime stateful diperlukan `observation_only=false` dan unified switch aktif. **Jangan menghapus baseline atau mereset state** untuk memaksa pengiriman. Setelah merge apa pun ke `master`, periksa rollover approved SHA sebelum scheduler berikutnya.

Jalur pengiriman produksi melakukan preflight read-only `getMe`, `getChat`, dan `getChatMember` sebelum POST Telegram: identitas kanal harus cocok dengan kanal resmi serta hash allowlist, dan bot harus memiliki hak posting. Tujuan pengiriman dipin ke ID numerik yang telah diverifikasi. Kredensial eksperimen tidak dipakai sebagai fallback. Jika verifikasi gagal, proses berhenti sebelum pengiriman.

Branch `dikti-production-state` harus tersedia dan dapat ditulis oleh workflow. Mode `initialize` membuat state yang belum ada dengan UTC aktual serta initial inventory tanpa pengiriman; bukan alat reset. Mode `preflight` hanya memeriksa tujuan dan izin tanpa mengirim Telegram atau menginisialisasi state. Mode `run` menjalankan siklus normal menggunakan state yang sudah tersedia.

## Lokal, eksperimen, dan produksi

| Lingkungan | Penggunaan |
|---|---|
| Lokal | Pengembangan, regression tests, serta dry-run dengan direktori state terisolasi. `config.json` dan `.env` lokal tidak mengatur runtime produksi Actions. |
| Eksperimen | Workflow [YouTube Observation Only](.github/workflows/youtube-observation-only.yml) hanya melalui `workflow_dispatch` (manual), tanpa `schedule`; branch `youtube-state` dan kredensial/allowlist eksperimen tersendiri. Auto-send dikendalikan `YOUTUBE_EXPERIMENT_AUTO_SEND`; tetap OFF. |
| Produksi | Satu workflow dengan tiga job dan runtime masing-masing; semuanya memakai kredensial kanal resmi dan branch `dikti-production-state`. |

Eksperimen memakai secrets `YOUTUBE_EXPERIMENT_BOT_TOKEN`, `YOUTUBE_EXPERIMENT_CHAT_ID`, dan `YOUTUBE_EXPERIMENT_CHAT_ID_SHA256`. Jangan menyalin state atau kredensial eksperimen ke produksi.

Untuk pengembangan lokal, gunakan Python 3.11 dan virtual environment:

```bash
git clone https://github.com/Galih-Hermawan-Unikom/bima-dikti-news.git
cd bima-dikti-news
python -m venv .venv
# Aktifkan virtual environment sesuai shell yang digunakan.
pip install -r requirements-dev.txt
playwright install chromium
python -m pytest
```

Untuk dry-run, salin ketiga file referensi historis ke direktori terisolasi di luar worktree Git, lalu jalankan:

```bash
python run_github.py --dry-run --youtube-eligibility --state-dir <direktori-state-terisolasi>
```

Perintah dry-run mengevaluasi sumber publik tanpa pengiriman Telegram. Satu capture belum cukup untuk membuktikan stabilitas. Gunakan fixture dan notifier mock untuk pengujian delivery; jangan memakai kredensial produksi dalam eksperimen lokal. `production_runtime.py` sendiri mensyaratkan lingkungan GitHub Actions.


## Struktur proyek

```text
.github/workflows/
  dikti-news-production.yml               # Satu scheduler, tiga job produksi
  main.yml                               # Recovery BIMA manual-only
  youtube-production.yml                 # Recovery YouTube manual-only
  kemdiktisaintek-production-scheduler.yml  # Scheduler legacy manual-only
  kemdiktisaintek-production.yml         # POLL/INITIALIZE/COLLECT bergate SHA
  telegram-preflight.yml                 # Preflight kanal produksi
  tests.yml                              # Regression suite
  ...                                    # Workflow eksperimen/manual tambahan
dikti_news_scheduler.py                   # SHA gate, pemilihan siklus, adapter sumber
production_runtime.py                    # Runtime BIMA/YouTube
production_preflight.py                  # Pemeriksaan tujuan Telegram
scraper.py / notifier.py                 # Data BIMA dan formatter Telegram
youtube_*.py                             # Pemantauan, eligibility, outbox YouTube
kemdiktisaintek_prod_*.py                # Parser, policy, state, outbox, scheduler sumber ketiga
announcements_cache.json                 # Referensi historis BIMA
youtube_cache.json                       # Referensi historis YouTube
delivery_state.json                      # Jurnal dan baseline historis
docs/                                    # Desain, audit, dan pengujian terdahulu
tests/                                   # Unit/integration tests dan fixtures
.env.example / requirements*.txt         # Konfigurasi contoh dan dependensi
```

Modul Python tetap berada di root agar import dan workflow yang sudah beroperasi tidak berubah. Sebagian workflow observasi/eksperimen menunjuk branch eksperimen dengan script yang tidak terdapat pada `master`; itu bukan jalur produksi. State operasional `.bot-state/` berada pada branch terpisah, **bukan** di root `master`.

## Membaca hasil dan troubleshooting

Siklus produksi normal menulis satu baris `PRODUCTION_RUN=` diikuti JSON. Interpretasikan field bersama status job dan outbox, bukan hanya jumlah kandidat:

| Field/log | Arti |
|---|---|
| `source` | Sumber yang dijalankan: `bima` atau `youtube`; jalur Kemdiktisaintek menggunakan ringkasan `DIKTI_NEWS_PRODUCTION` pada unified dan `KEMDIKTISAINTEK_PRODUCTION_RUN` pada jalur manual. |
| `startup: true` | Capture initial inventory; tidak ada pengiriman startup. |
| `visible` | Jumlah item terlihat; YouTube memisahkan jumlah per tab. Bukan jumlah video baru. |
| `eligible` / `hold` | Hasil evaluasi pada siklus itu; bukan jumlah pesan terkirim. |
| `sending: ON` | Saklar mengizinkan pengiriman; bukan bukti API Telegram telah dipanggil. |
| `confirmed_messages` | Jumlah pesan Telegram terkonfirmasi; pada BIMA mencakup ringkasan dan dokumen. |
| `delivered_events` | Jumlah event yang selesai dikirim, berbeda dari jumlah pesan/lampiran. |
| `activation_at` / `initial_inventory` | Fence tersimpan dan ukuran inventory jika disertakan pada laporan. |
| `capture_seq` | Urutan capture YouTube yang tersimpan. |
| `PRODUCTION_PREFLIGHT=PASS` | Pemeriksaan tujuan/izin berhasil; mode preflight tidak mengirim. |
| `KEMDIKTISAINTEK_PRODUCTION_RUN=` | JSON polling sumber ketiga: `api_health`, `capture_warnings`, `status_counts`, `poll_gap_seconds`, `delivery_enabled`, `delivered_this_poll`. |
| `PRODUCTION_FAIL_CLOSED=...` | Pengamanan menghentikan siklus. Periksa kode kegagalan dan state sebelum bertindak. |

**Job sukses dengan 0 pengiriman adalah hasil normal** saat belum ada konten baru yang memenuhi kebijakan. Untuk Kemdiktisaintek, `delivery_enabled=true` berarti saklar pengiriman aktif, sedangkan `delivery_authorized=false` dengan `NEW_CANDIDATE=0` berarti tidak ada kandidat layak pada polling tersebut. Status **ARMED** belum membuktikan pengiriman produksi pertama. Untuk controlled backfill, baca field `backfill`, `reason`, dan receipt/message ID yang tersedia.

| Gejala | Pemeriksaan aman |
|---|---|
| Job terjadwal dilewati | Periksa status workflow, branch `master`, dan variable `*_PRODUCTION_ENABLED`. |
| Unified tidak menjalankan job sumber | Cocokkan `KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA` dengan HEAD `master`; periksa scheduler/source switch dan workflow run. Jangan mematikan SHA gate. |
| Kemdiktisaintek `UNHEALTHY_POLL_GAP` | Periksa jarak antar-run dan API health; lihat kemungkinan cron terlambat/terlewat. Jangan backfill atau mengirim arsip otomatis. |
| Run terlambat atau antrean penuh | Periksa antrean dan log ketiga pipeline produksi pada concurrency group `dikti-production-state`. Cron menggunakan UTC dan tidak menjamin waktu mulai; `queue: max` membatasi antrean hingga 100; antrean penuh tetap memerlukan penanganan operator. |
| Eksperimen tidak berjalan otomatis | Sesuai konfigurasi: `youtube-observation-only.yml` tidak memiliki `schedule` dan hanya tersedia melalui `workflow_dispatch` manual. Auto-send eksperimen tetap OFF. |
| Job berjalan tetapi tidak mengirim | Periksa auto-send, inventory/histori, fence, hasil eligibility, dan outbox; jangan menghapus histori. |
| Allowlist atau izin Telegram gagal | Periksa secrets, hash ID numerik resmi, identitas kanal, serta hak admin bot. Jangan mencetak nilai rahasia. |
| YouTube HOLD | Periksa kesehatan kedua tab, jarak/stabilitas capture, kesegaran bukti, kategori, serta replay readiness. Jangan mempromosikan UNKNOWN secara manual tanpa bukti. |
| Konflik CAS/state tidak tersedia | Periksa izin `contents: write`, branch/path state, dan checkpoint terakhir. Jangan memaksa overwrite atau reset branch. |
| Outbox `ambiguous` atau pengiriman parsial | Matikan auto-send sumber terkait bila perlu; cocokkan receipt, message ID, dan riwayat kanal. Tidak ada resend otomatis. |
| PDF BIMA tidak selesai | Periksa ketersediaan/unduhan dokumen dan receipt `sendDocument`. Flag `config.json` bukan saklar PDF produksi; jangan mengulang ringkasan yang sudah terkirim. |
| Chromium tidak tersedia | Pastikan langkah `playwright install --with-deps chromium` pada workflow BIMA berhasil; untuk lokal gunakan `playwright install chromium`. |
| Timeout sumber publik | Periksa log job dan kesehatan sumber; timeout workflow 20 menit. Jangan mengatasi timeout dengan reset cache/outbox. |

## Dokumentasi lanjutan

- [Kebijakan classifier tabs-only](docs/youtube-eligibility-offline.md)
- [Adapter channel-tabs dan verifikasi otomatis](docs/youtube-channel-tabs.md)
- [Observasi persisten dan durable outbox](docs/youtube-persistent-outbox.md)
- [Preflight Telegram eksperimen](docs/telegram-preflight.md)
- [Riwayat audit reliabilitas Fase 1](docs/reliability-phase1.md)
- [Operasi unified, cutover dan rollback](README_KEMDIKTISAINTEK_PRODUCTION.md)

Perubahan dokumentasi ke `master` juga mengubah SHA commit. Ketika Kemdiktisaintek aktif, operator perlu menyelaraskan approved SHA secara terkendali setelah merge.

Dokumen fase terdahulu menjelaskan desain dan riwayat implementasi. Untuk operasi produksi saat ini, rujuk README ini beserta workflow produksi setiap sumber; BIMA/YouTube menggunakan `production_runtime.py`, sedangkan Kemdiktisaintek menggunakan `kemdiktisaintek_prod_runtime.py`.

## Pengembang

**Galih Hermawan**  
Program Studi Teknik Informatika  
Universitas Komputer Indonesia  

- Website: [https://galih.eu](https://galih.eu)
- Email: [galih.hermawan@gmail.com](mailto:galih.hermawan@gmail.com)
- Threads: [https://www.threads.com/@galihhermawan](https://www.threads.com/@galihhermawan)

## Lisensi

MIT
