# Operasi Unified Dikti News Production

Pemulihan satu artikel yang HOLD dilakukan melalui [Controlled Editorial Delivery](README_KEMDIKTISAINTEK_EDITORIAL_DELIVERY.md), bukan dengan mengubah status monitoring atau melakukan backfill otomatis.

Implementasi ini masih menunggu review dan cutover. Pengembangan tidak mengubah Variables, Secrets, baseline, atau state remote, dan tidak melakukan Telegram POST. Master audit: `089e29856a2062a810ae7b5f52348c17f5830cb6`.

## Scheduler, izin, dan isolasi

`.github/workflows/dikti-news-production.yml` adalah satu-satunya file dengan `schedule` pada branch perubahan. Dua cron yang tidak beririsan: `17 */3 * * *` (8 slot/hari, tiga sumber), dan `17 1-23/3,2-23/3 * * *` (16 slot/hari, YouTube + Kemdiktisaintek). Total 24 slot nominal/hari. WIB = UTC+7; giliran BIMA pada 01.17, 04.17, 07.17, 10.17, 13.17, 16.17, 19.17, 22.17 WIB. Sumber lain setiap jam menit 17.

`dikti_news_scheduler.py plan` memakai string cron event, bukan jam aktual runner. Master switch baru `DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED` default `false`. Independent approval memakai `KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA` yang wajib sama dengan GITHUB_SHA dan HEAD master dari API. Approval ini berlaku untuk seluruh paket unified, termasuk BIMA/YouTube. Input dispatch tidak dapat menggantikan approval operator.

Planner hanya `contents:read`, tanpa Telegram Secrets atau `actions:write`. Dispatch manual `observation_only=true` sekarang hanya memvalidasi snapshot state melalui GET ber-path tetap dan pemeriksaan blob SHA. Ketiga output source job false, sehingga job stateful tidak dijalankan. Rehearsal ini dapat berjalan saat unified switch OFF dan source switches OFF; reviewed master SHA tetap wajib. Scope-nya snapshot validation, bukan pembuktian API/capture/delivery. Helper snapshot dapat memakai pure `apply_poll()` untuk simulasi Kemdiktisaintek di memori; tidak membuat admission, migrasi atau checkpoint remote. Adapter juga menolak stateful runtime dengan flag rehearsal sebagai pertahanan tambahan. Tiga source job hanya bergantung pada planner, menggunakan `contents:write`, timeout BIMA/YouTube 20 menit dan Kemdiktisaintek 15 menit. Checkout selalu SHA hasil gate dengan `persist-credentials:false`. Setiap job mengulang SHA gate setelah memperoleh lock `dikti-production-state` sebelum memanggil runtime. Tidak ada workflow induk memegang lock yang dibutuhkan child; tidak ada child dispatch. `queue:max`, `cancel-in-progress:false` mencegah tiga sibling saling menggantikan sebagai satu pending job; GitHub membatasi antrean hingga 100.

State tetap tiga file di branch `dikti-production-state`. Shared job lock melindungi pembaruan commit branch; expected blob SHA/CAS tetap melindungi setiap file dan wajib ACK sebelum POST. Konflik CAS menghentikan operasi; tidak ada retry pengiriman atau fallback lokal. Runtime BIMA/YouTube, scraper, eligibility YouTube, formatter, outbox, serta backend CAS mereka tidak diubah. Adapter mempertahankan guard hash referensi historis sebelum/sesudah runtime dan sanitasi exception tanpa mencetak URL credential. Laporan unified menyatakan interval nominal 10800/3600 detik; field interval pada log internal YouTube legacy tetap diagnostik lama dan tidak menentukan giliran sumber.

Legacy `main.yml`, `youtube-production.yml`, `kemdiktisaintek-production-scheduler.yml`, dan scheduler eksperimen `kemdiktisaintek-scheduler.yml` kehilangan cron secara atomik dalam commit yang menambahkan unified. Workflow lama tetap manual-only, tetapi jalur produksi/legacy dispatch diblokir selama unified switch aktif. `COLLECT` sumber ketiga tetap dapat dipakai tanpa state write. Eksperimen hanya memakai branch/kredensial eksperimen.

## Policy sumber ketiga

Policy delivery baru: `kemdiktisaintek-delivery-v3-all-official`. Semua pengumuman resmi, kategori, dan topik diterima tanpa filter audiens. Classifier lama tetap tersimpan sebagai diagnostik historis; bukan syarat admission/delivery. UUID, canonical URL, sumber resmi, body/detail datetime terpercaya, warning artikel, baseline, identity conflict, fence, overlap, waktu, dan receipt tetap diperiksa. Nomor surat/tenggat hanya dari bukti parser; PDF memakai pengamanan dan operasi durable yang sama.

| Parameter | Nilai | Alasan |
|---|---|---|
| Planned interval | 1 jam | 24 slot workflow per hari |
| Toleransi keterlambatan | 11 jam | Batas gap total 12 jam; menoleransi satu/dua cron hilang dan jeda beberapa jam |
| Publication window | 72 jam | Waktu detail sering tengah malam; 24 jam dapat berakhir sebelum bot menangkap berita keesokan hari |
| First-seen TTL | 24 jam | Identitas tidak menjadi baru kembali setelah restart |
| Detail/capture evidence + admission | 10 menit | Bukti pengiriman tidak ikut diperbesar menjadi 72 jam |
| Preflight proof | TTL gateway produksi tetap | Verifikasi ulang tujuan dan hak posting sebelum durable claim/POST |

Riwayat run yang dibaca mencakup jeda sekitar 6 jam, 7 jam, dan 14 jam. Angka 12 jam merupakan batas pemulihan konservatif, bukan klaim GitHub tepat waktu: gap 14 jam tetap HOLD. Capture sukses memperbarui `last_successful_poll_at`; capture yang bebas warning kontinuitas juga memperbarui `last_healthy_poll_at`. Status API HEALTHY sendiri tidak membuktikan overlap/gap sehat.

POLL minimal dua halaman, lanjut hingga halaman penuh berisi 10 UUID dikenal atau akhir daftar. Batas absolut 10 halaman/100 listing; maksimal 100 detail, memprioritaskan seluruh UUID baru, lalu hingga tiga known detail bergilir. Tidak ada category filter, parameter `search` selalu kosong. Total record harus konsisten, setiap halaman lengkap, urutan `created_at` diverifikasi dari sumber (tidak disortir lokal), dan halaman pertama diambil ulang untuk mendeteksi pergeseran. `created_at` hanya bukti traversal; `published_at` dari detail tetap penentu usia berita.

Cap tanpa boundary, urutan tak terbukti, UUID duplikat, overlap hilang, atau gap lebih dari 12 jam menyebabkan HOLD. Recheck yang berubah atau partial page menggagalkan capture tanpa checkpoint observasi sukses. Tidak ada klaim archive complete. Snapshot/bounded prefix tidak membuktikan perubahan arbitrer jauh di arsip; kasus tersebut memerlukan review editorial.

## Migrasi eksplisit dan recovery

State envelope tetap schema v2 dan operation schema tetap v1. Pada POLL pertama dengan kode baru, `migrate_official_policy()` memvalidasi state v2, menambah `official_policy_migration` dengan `from`, `to`, `at`, dan SHA-256 seluruh state sebelumnya, lalu menambah generation satu melalui checkpoint CAS tersendiri. Validator lineage mensyaratkan checkpoint migrasi identik selain policy, marker, dan generation. Seluruh baseline, fence, observations, first_seen, lima revisi eksperimen bila ada, admissions, TEST, outbox, dan receipts dipertahankan. Setelah itu observasi membuat checkpoint berikutnya. Migrasi v1 ke independen v2 yang lebih lama tetap didukung sebelum transisi v3.

Admission lama tetap immutable tetapi tidak menjadi izin baru: eligibility baru hanya menerima admission v3 yang masih segar. Semua HOLD/NEEDS_REVIEW lama tetap ditahan, termasuk setelah gap pulih atau topik kini diterima. KNOWN, baseline, legacy UUID, dan event DELIVERED tidak dapat dipromosikan. Revisi tidak dikirim otomatis. POLL stateful dengan AUTO_SEND=false tetap mencatat admission sah, dan polling berikutnya tidak mempromosikan UUID yang sudah diamati menjadi kandidat baru. Karena itu POLL tersebut tidak boleh dipakai sebagai rehearsal cutover untuk mempertahankan kesempatan kirim. BIMA juga memulai first-seen TTL pada polling stateful tanpa kirim; YouTube menyimpan capture/first-seen yang memengaruhi eligibility berikutnya. Rehearsal snapshot tidak menjalankan ketiga runtime, tidak memulai TTL, dan tidak mengubah bukti tersebut.

Jika checkpoint migrasi sukses tetapi capture/observasi berikutnya gagal, baca kembali state; jangan migrasi ulang, rollback state, atau reinitialize. Jika scan cap terlampaui, gap berlebih, atau detail tidak terpercaya, periksa laporan HOLD/NEEDS_REVIEW dan gunakan keputusan editorial terpisah. Tidak ada backfill umum untuk artikel yang sudah HOLD atau melewati 72 jam.

`RESERVED → SENDING → DELIVERED` dan receipt per summary/PDF tetap berlaku. Timeout/receipt write tidak pasti berarti AMBIGUOUS atau klaim SENDING yang memerlukan rekonsiliasi; jangan retry. PARTIAL tidak boleh mengulang summary yang sudah delivered. Switch berubah tidak menghentikan environment run lama; selalu audit/drain run aktif.

## Kasus Bridging Course: bukti read-only

Pada 27 Oktober 2026, API total 930 dan memuat **Tawaran Mengikuti Program Bridging Course Tahun 2026**, UUID `2407f6f4-1e06-452b-a053-864426e9a183`, publikasi `2026-10-27T00:00:00+07:00`. Detail menyatakan **paling lambat 06 November 2026** dan memuat satu PDF resmi `Tawaran_Pendaftaran_Program_BRIDGING_2026.pdf` serta tautan Google Docs format izin. Tidak ada nomor surat yang berhasil diekstrak dari teks HTML; tidak diada-adakan.

Sumber: https://kemdiktisaintek.go.id/announcement/article/tawaran-mengikuti-program-bridging-course-tahun-2026

Snapshot produksi dibaca pada blob `c9fe9c5ecef6ded35b269ad772256f6f81404b69`, generation 14, baseline 20, fence `2026-10-25T08:58:01.786965+00:00`, last capture `2026-10-27T05:33:48.844753+00:00`. UUID Bridging belum ada di observations/admissions/outbox; belum ada bukti pengiriman untuk UUID ini pada snapshot tersebut. Artikel baru terlihat pada API dengan created_at 16.00 WIB, sesudah capture terakhir 12.33 WIB, sehingga belum diamati pada snapshot itu, bukan terbukti terlewat parser. Produksi tetap dapat bergerak secara independen sesudah audit. Artikel Perkuat Kesiapan Dosen dan Tendik yang sudah dikenal tetap HOLD; snapshot menyimpan alasan HISTORICAL_OUTSIDE_NEW_WINDOW dan UNHEALTHY_POLL_GAP. Window baru tidak mempromosikannya.

Rehearsal read-only langsung pada 2026-10-27 10:21:08 UTC / 17:21:08 WIB menggunakan snapshot tersebut: 2 halaman, 20 listing, 4 detail (1 baru + 3 known), total API 930, known boundary tercapai, urutan valid, recheck head stabil, dan warnings kosong. Transisi policy/monitor hanya dihitung di memori; Bridging menjadi NEW_CANDIDATE. Baseline dan fence identik; remote PUT dan Telegram POST sama-sama 0. Ini bukan checkpoint produksi atau bukti pengiriman.

Fixture nyata `tests/fixtures/kemdiktisaintek_bridging_2026.json` mencatat waktu pengambilan UTC dan data parser. Simulasi offline: UUID baru + gap 4 jam + detail valid → NEW_CANDIDATE; UUID sudah HOLD → tetap HOLD; publikasi berumur 73 jam → HOLD. Simulasi bukan izin pengiriman. Jika saat cutover UUID telah HOLD/KNOWN atau di luar window, gunakan preview/editorial berlabel dengan persetujuan terpisah; jangan menghapus histori.

## Cutover setelah PR disetujui — belum dijalankan

1. Catat master/reviewed merge SHA, Variables, state blob/generation, baseline/fence, serta receipt setiap sumber. Periksa AMBIGUOUS/SENDING/PARTIAL. Jadwalkan jeda singkat dan otorisasi operator.
2. Matikan scheduler legacy Kemdiktisaintek dan eksperimen; matikan source switches BIMA/YouTube/Kemdiktisaintek serta AUTO_SEND saat drain. Ini prosedur operator terpisah, tidak dikerjakan oleh PR. Tunggu semua run aktif/pending selesai; jangan membatalkan job setelah POST dan jangan mengasumsikan perubahan Variable membatalkan run lama.
3. Merge satu commit yang sekaligus menghapus empat cron lama dan menambahkan unified. Verifikasi hanya `dikti-news-production.yml` mempunyai schedule. Master switch unified harus tetap unset/false. Jangan mendaftarkan scheduler kedua.
4. Baca HEAD master dari API dan review diff hasil squash. Rollover `KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA` ke SHA penuh master baru, lalu read-back. Pastikan SHA scheduler/run/checkout/master sama. Dengan unified switch tetap false, jalankan satu dispatch `observation_only=true`: hanya snapshot validation pada job contents:read, ketiga job produksi skipped. Pastikan baseline/fence/receipt dan blob SHA tidak berubah. API dapat diperiksa melalui COLLECT read-only terpisah atau pure snapshot simulation; jangan gunakan POLL stateful sebagai rehearsal. Gate tidak boleh dilewati ketika mismatched.
5. Setelah rehearsal read-only lulus, tentukan mode aktivasi secara eksplisit. Bila operator hanya mengizinkan monitoring, AUTO_SEND=false dapat dipakai, tetapi artikel yang diamati akan menjadi histori dan tidak dijanjikan dikirim saat delivery kelak diaktifkan. Bila operator mengizinkan delivery sejak polling produksi pertama, set source dan AUTO_SEND yang disetujui selagi unified masih false; set unified switch true sebagai langkah terakhir. Jangan menjalankan polling stateful tanpa kirim di antara rehearsal dan aktivasi delivery. Ini mencegah kandidat terkonsumsi saat cutover; migrasi policy dan checkpoint pertama baru terjadi di run produksi yang diotorisasi.
6. Periksa run produksi pertama: capture/poll gap/overlap, migrasi eksplisit CAS, baseline/fence, receipt dan status operasi. HOLD lama tetap HOLD dan bukan kandidat backfill. Buktikan cron lewat event=schedule, jangan memakai dispatch sebagai bukti cron. Tidak perlu INITIALIZE atau SEND_TEST. Batas admission, waktu, CAS dan larangan retry tidak dilonggarkan untuk mengejar kandidat saat cutover.

## Rollback

Matikan `DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED` dan AUTO_SEND sumber yang bermasalah; drain run aktif/pending dan pertahankan seluruh state/receipt. Workflow lama manual-only dapat dipakai untuk observation/recovery ketika unified OFF. Jangan menyalakan scheduler lama bersamaan dengan unified.

Setelah state bermigrasi ke v3, **jangan wholesale revert ke validator v2**: kode lama akan menolak state baru. Rollback orchestration harus mempertahankan validator/migrasi v3 dan perlindungan HOLD; pemulihan cron lama memerlukan PR/review terpisah. Jangan mengembalikan state ke snapshot sebelum delivery, karena receipt terbaru bisa hilang. Rollover approved SHA wajib mengikuti reviewed master hasil rollback. Restore baseline/fence bukan langkah rollback rutin.

## Validasi

`python -m pytest -q` menjalankan regression BIMA, YouTube, Kemdiktisaintek, formatter/PDF, CAS, timeout, dan mock Telegram. `tests/test_unified_dikti_news.py` mencakup rehearsal dua slot cron, sumber gagal secara independen, snapshot master berubah setelah antre, semua topik, usia publikasi, gap, HOLD lama, scan adaptif, partial capture, pagination shift, dua artikel/restart/revisi, dan migrasi receipt TEST. Network publik dan Telegram asli diblokir oleh fixtures tes.

YAML diperiksa dengan PyYAML dan actionlint v1.7.12. actionlint versi ini belum mengenali properti concurrency `queue`; satu diagnostik sintaks `queue` dikecualikan secara spesifik, sementara tests memastikan `queue:max` dan `cancel-in-progress:false` sesuai dokumentasi GitHub terkini. Tidak ada pengecualian security/expression lain.

```powershell
python -m pytest -q
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12 -shellcheck= -pyflakes= -ignore 'unexpected key "queue" for "concurrency" section' .github/workflows/dikti-news-production.yml
```

Referensi: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule dan https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency

## Final review cutover safety (27 Oktober 2026)

Prosedur lama berisiko mengonsumsi kandidat: Kemdiktisaintek NEW_CANDIDATE/admission dari POLL tanpa kirim menjadi KNOWN di poll berikutnya; BIMA memulai first-seen TTL; YouTube menambah capture dan first-seen persisten. `observation_only=true` sekarang merupakan snapshot rehearsal read-only, bukan POLL dengan AUTO_SEND dipaksa OFF. Tidak ada remote write capability pada helper, dan job planner hanya contents:read. Tes membuktikan snapshot tetap identik, lalu kandidat baru masih dapat dikirim tepat sekali menggunakan mock pada pemrosesan sah berikutnya. Rehearsal tidak memberi jaminan mempertahankan window waktu selamanya; batas 72 jam, first-seen, freshness, overlap dan fence tetap berlaku.

Read-back terbaru sebelum review ini: blob `ae59dfa5ff04febcb6b5fac8884f26da504d209b`, generation 15, policy independen v2. Bridging Course UUID `2407f6f4-1e06-452b-a053-864426e9a183` sudah HOLD (`UNHEALTHY_POLL_GAP`), first-seen `2026-10-27T10:58:08.441862+00:00`, tanpa admission/outbox. Baseline masih 20 dan fence tetap. Perubahan dari snapshot generation 14 terjadi pada produksi lama secara independen, sebelum tindakan review ini; tidak dimanipulasi. UUID tersebut tidak boleh otomatis dikirim oleh cutover. Jika masih diperlukan, siapkan keputusan editorial terpisah dengan bukti/otorisasi khusus, bukan reset state.
