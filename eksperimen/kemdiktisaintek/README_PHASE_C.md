# Fase C — Monitoring dan Delivery Kemdiktisaintek

## Status implementasi

Kode Fase C berada pada branch fitur `feat/kemdiktisaintek-telegram-stage3`. Scheduler belum aktif: konfigurasi `KEMDIKTISAINTEK_SCHEDULER_ENABLED` tidak tersedia, sehingga default-nya OFF. Variable `KEMDIKTISAINTEK_EXPERIMENT_ENABLED` dan `KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND` juga tetap OFF/tidak tersedia.

Fase B tetap dipertahankan sebagai event `TEST` immutable berstatus `DELIVERED`. POLL hanya membuat event `INITIAL` dari keputusan baru yang memenuhi seluruh syarat. POLL tidak mengubah event TEST menjadi event berita dan tidak membuat event revisi.

## Arsitektur scheduler

`.github/workflows/kemdiktisaintek-scheduler.yml` menjalankan cron `11,41 * * * *` pada default branch `master`. Workflow ini dirancang untuk didaftarkan ke master melalui PR satu berkas. Job hanya memiliki `contents:read` dan `actions:write`, tidak memakai environment privileged, dan tidak menerima token Telegram.

Sebelum dispatch, scheduler:

1. Memastikan `GITHUB_REF=refs/heads/master` dan repository tepat.
2. Memerlukan `KEMDIKTISAINTEK_SCHEDULER_ENABLED=true`.
3. Membaca HEAD branch `feat/kemdiktisaintek-telegram-stage3`.
4. Membandingkan HEAD dengan `KEMDIKTISAINTEK_EXPERIMENT_APPROVED_CODE_SHA`.
5. Checkout SHA yang sama dengan `persist-credentials=false`.
6. Mengirim satu input `POLL` ke workflow eksperimen pada branch fitur.

Workflow eksperimen memeriksa lagi reviewed SHA terhadap Variable approval dan `GITHUB_SHA), lalu menerapkan environment branch restriction yang hanya mengizinkan branch fitur. Perubahan head atau SHA yang tidak cocok gagal sebelum POLL. Concurrency scheduler dan state memakai grup terpisah, keduanya `cancel-in-progress: false`. Job terkontrol timeout 10 menit.

Nilai `KEMDIKTISAINTEK_SCHEDULER_ENABLED` yang hilang/selain string tepat `true` tidak memicu dispatch. Kedua switch delivery juga harus bernilai tepat `true`; selain itu POLL berjalan sebagai observation-only. Credential Telegram eksperimen hanya diteruskan ke proses ketika kedua switch delivery aktif. Tidak ada fallback ke credential produksi.

## Capture dan deduplikasi BIMA

Adapter `kemdiktisaintek_bima_reference.py` membungkus collector DOM BIMA yang sudah dipakai produksi (`BimaScraper`). Adapter memvalidasi metadata kartu dan tanggal, mengamati status paginasi, serta mengabaikan `scraped_at` naif dari item. Waktu mulai/selesai capture dihasilkan oleh runner UTC. Adapter tidak memanggil cache, journal, BIMA outbox, Notifier, atau Telegram BIMA.

Adapter tidak mengklik atau mengunduh dokumen BIMA; nama dokumen cukup untuk deduplikasi. Snapshot memuat sumber, cakupan, status pagination, urutan tanggal, rentang tanggal yang benar-benar didukung, waktu capture, SHA konten yang dihitung adapter, serta provenance repository/workflow/ref/SHA/run GitHub. Validasi mengikat provenance ke konteks runtime yang sudah lolos reviewed-SHA gate.

- Listing satu halaman yang secara eksplisit menyatakan selesai mencakup tanggal mulai hari pada record tertua.
- Listing dengan halaman berikutnya hanya membuktikan cakupan mulai hari setelah tanggal record tertua, karena record hari tertua mungkin berlanjut pada halaman berikut.
- Urutan tanggal yang tidak menurun, halaman selain halaman pertama, metadata pagination yang tidak diketahui, record kosong, atau tanggal invalid membuat referensi tidak layak untuk deduplikasi.
- Kandidat yang lebih tua dari `coverage_from` menjadi `NEEDS_REVIEW`; referensi hilang, gagal, kosong, atau stale menghasilkan HOLD/NEEDS_REVIEW, bukan NO_MATCH.
- `archive_complete` selalu false. Referensi hanya untuk rentang tanggal yang dibuktikan listing terbaru, bukan seluruh arsip.

Format BIMA produksi saat ini berasal dari kartu DOM `ap-card`/legacy dengan judul, nomor surat, tanggal dan nama dokumen. Collector produksi sendiri tidak mengekspos jaminan cakupan paginasi; adapter baru membaca kontrol pagination dan menahan delivery jika bukti tersebut tidak tersedia.

## Kebijakan waktu dan relevansi

POLL terencana setiap 30 menit dengan toleransi keterlambatan 20 menit (batas gap 50 menit). Capture harus sehat, semua halaman diminta harus sukses, dan overlap harus tetap terlihat. Gap lebih dari 50 menit, kehilangan overlap, atau duplikasi akibat pergeseran paginasi menahan kandidat pada capture itu. Satu capture bersih selanjutnya dapat mengevaluasi ulang kandidat yang masih memenuhi jendela; item lewat jendela tidak dibackfill.

Batas lain tetap terpisah:

- Detail evidence: 10 menit.
- Publication window: 24 jam setelah activation fence.
- First-seen TTL: 24 jam.
- Referensi BIMA: 60 menit.
- Telegram preflight proof: 5 menit.
- Admission: 10 menit dan harus diproses pada rangkaian POLL yang sama.

Classifier memakai judul, isi, tanggal, deadline, nomor surat, kategori dan lampiran. Topik akademik prioritas tinggi dapat menjadi RELEVANT bila disertai bukti konteks tindakan/isi. Topik sedang memerlukan konteks akademik. Berita seremonial dan rekrutmen sekolah nonakademik ditolak; bukti yang tidak cukup menjadi NEEDS_REVIEW. Relevansi tidak melewati activation fence atau syarat deduplikasi.

Deduplikasi BIMA menggunakan surat, judul, hari publikasi, URL dokumen dan nama program. Judul/surat tunggal atau kemiripan menghasilkan NEEDS_REVIEW; duplicate terkonfirmasi ditolak. Referensi kosong tidak pernah diartikan NO_MATCH.

## Pipeline dan durable delivery

`POLL` menjalankan satu siklus:

`CAPTURE → CLASSIFICATION → BIMA DEDUP → ADMISSION → ELIGIBILITY RECHECK → PREFLIGHT → RESERVED → SENDING → TELEGRAM POST → RECEIPT`

Saat kedua switch delivery OFF, siklus berhenti setelah observasi/admission dan tidak membaca credential Telegram. Saat kedua switch ON, hanya kandidat NEW_CANDIDATE yang memperoleh admission pada capture sehat yang sama dapat diteruskan. Eligibility memeriksa ulang UUID, detail, publication basis, activation fence, first seen, TTL, classification, BIMA coverage, dedup, capture freshness, identity holds dan admission hash.

Remote CAS reservation disimpan dahulu. CAS claim `SENDING` harus diakui sebelum POST. Telegram mengonfirmasi `ok=true`, message ID, dan hash tujuan allowlist; receipt lalu disimpan dengan CAS. Satu event hanya memiliki satu attempt. Timeout, crash setelah claim, atau receipt checkpoint gagal tidak mengirim ulang otomatis dan membutuhkan rekonsiliasi. Event `RESERVED` yang tak dapat diproses dalam admission TTL tetap tertahan; operator dapat meninjau, tetapi kode tidak mengubah event menjadi berita baru.

State tetap schema v2 pada branch eksperimen `kemdiktisaintek-experiment-state`, path `.bot-state/kemdiktisaintek_experiment.json`. Activation fence, baseline 20 UUID, checksum, event TEST Fase B, dan lineage CAS dipertahankan. Workflow tidak menjalankan initialization atau menghapus outbox.

## Observability

Setiap POLL mengeluarkan ringkasan tanpa token/chat ID: waktu UTC/WIB, halaman, item/detail, status NEW_CANDIDATE/KNOWN/HOLD/REJECTED/NEEDS_REVIEW, duplicate, admissions, reservation, delivered, ambiguous, kesehatan API, poll gap, coverage BIMA dan error class. Capture Kemdiktisaintek gagal melaporkan `api_health=FAILED` tanpa memajukan observation state; recovery boleh lebih dahulu mengubah attempt `SENDING` lama menjadi `AMBIGUOUS`. Capture Kemdiktisaintek yang sehat dengan BIMA yang gagal tetap dapat menyimpan observasi, tetapi kandidat ditahan.

## Aktivasi eksperimen

Aktivasi tidak dilakukan dalam Fase C. Setelah perubahan ditinjau:

1. Operator meninjau dan merge PR registrasi scheduler yang hanya berisi workflow scheduler. PR tidak mengubah workflow produksi.
2. Operator menetapkan approval SHA tepat ke checkpoint fitur yang ditinjau.
3. Untuk observation-only, operator mengaktifkan `KEMDIKTISAINTEK_SCHEDULER_ENABLED=true`; kedua switch delivery dibiarkan OFF.
4. Operator memeriksa report POLL, terutama `bima_reference_status`, coverage pagination, overlap, dan HOLD/NEEDS_REVIEW.
5. Pengiriman eksperimen memerlukan persetujuan terpisah dan kedua Variable `KEMDIKTISAINTEK_EXPERIMENT_ENABLED=true` serta `KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND=true`. Menonaktifkan salah satu switch atau scheduler mengembalikan operasi ke observation-only/default DENY.

Variable approval harus cocok dengan HEAD branch fitur; SHA lama tidak dapat menjalankan kode baru. Tidak ada workflow dispatch atau pengiriman nyata sebagai bagian dokumentasi ini.

## Rencana integrasi produksi

Integrasi produksi tetap belum dibuat. Rencana aman:

- Implementasikan adapter sender produksi tersendiri dengan preflight produksi dan credential produksi; jangan memakai `TelegramGateway` eksperimen atau memetakan secret eksperimen ke produksi.
- Routing harus eksplisit ke `@dikti_news`, dengan switch `KEMDIKTISAINTEK_PRODUCTION_ENABLED` tersendiri.
- Gunakan state produksi terpisah, misalnya `.bot-state/kemdiktisaintek_production.json` pada `dikti-production-state`, dengan scope branch/path dan CAS sendiri. Initialization/migrasi memerlukan persetujuan terpisah.
- Deduplikasi BIMA harus memakai referensi segar yang dapat dibuktikan, bukan cache master atau snapshot lama. Pertahankan review saat cakupan tidak cukup.
- Review perubahan `production_runtime.py`, workflow produksi dan routing melalui PR terpisah; tidak ada perubahan tersebut dalam Fase C.
- Rollback operasional: matikan switch Kemdiktisaintek, hentikan scheduler, pertahankan state/outbox/receipts, dan rekonsiliasi status ambigu sebelum perubahan code. Jangan hapus event delivered untuk mengulang kiriman.

## Validasi Fase C

- Tes eksperimen parser/monitor/delivery dan Fase C: 317 lulus.
- Regression proyek BIMA dan YouTube: 829 lulus, 26 subtests lulus.
- Seluruh simulasi menggunakan fixtures dan mock; tidak ada Telegram POST.
- Smoke request lokal ke halaman BIMA gagal validasi TLS sertifikat workstation. Tidak ada retry dengan verifikasi TLS dimatikan. Workflow belum dijalankan; bukti pagination BIMA live masih harus diperiksa lewat POLL observation-only setelah aktivasi yang disetujui.
