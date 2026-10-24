# Kemdiktisaintek Tahap 2 — monitor eksperimen tanpa pengiriman

Implementasi melanjutkan parser Tahap 1, bukan menggantinya. Checkpoint Tahap 1: `ba42d4e` pada branch `feat/kemdiktisaintek-parser-stage1`. Tahap 2 dikembangkan pada branch turunannya, `feat/kemdiktisaintek-monitor-stage2`, dalam worktree pengembangan yang sama. Tidak ada push, PR, merge, deployment, penulisan state GitHub, penggunaan token, atau Telegram.

## Checkpoint dan cakupan Git

Checkpoint memuat 23 berkas terpilih: parser, 83 tes, README, contoh hasil JSON, checksum, `.gitattributes`, dan 16 fixture HTML/API yang diperlukan tes. Gambar, base64 isi duplikat, i18n, shared scripts dan sidebar artikel terkait dikeluarkan dari fixture HTML; fixture terbesar sekitar 14.5 KB. Fixture berasal dari capture nyata Tahap 1, bukan respons sintetis yang menyamar sebagai capture. Fixture ringkas masih melewatkan seluruh 83 tes parser.

Capture mentah dipertahankan lokal di `audit_raw_local/` dan berkas capture lama; tidak masuk Git. `.env`, token, local state, log/XML tes dan manifest berisi path workstation juga tidak masuk Git. Direktori `eksperimen/` tetap di-ignore secara global; hanya berkas relevan di-stage dengan path eksplisit dan `-f`. Tidak memakai `git add .`. Berkas eksperimen yang sudah dilacak tetap dapat dimodifikasi normal setelah checkpoint.

## Struktur kode

| Berkas | Tanggung jawab |
| --- | --- |
| `kemdiktisaintek_parser.py` | Parser/HTTP Tahap 1, tetap dipakai untuk API daftar, detail HTML, URL, tanggal, UUID dan hash konten. Tidak diimplementasikan ulang. |
| `kemdiktisaintek_policy.py` | Classifier transparan serta cross-source dedup murni tanpa I/O. Policy `kemdiktisaintek-experiment-v1`. |
| `kemdiktisaintek_state.py` | Schema, validasi fingerprint/riwayat, local atomic CAS, allowlist branch/path dan emulator Contents API dengan blob SHA. |
| `kemdiktisaintek_monitor.py` | Capture beberapa halaman tanpa filter, baseline, admission artikel baru, pembaruan observasi dan bukti revisi; checkpoint sebelum laporan sukses. |
| `kemdiktisaintek_cli.py` | Mode `collect`, `initialize`, `poll`; state lokal harus dipilih eksplisit. Tidak ada mode kirim atau state remote. |
| `simulate_stage2.py` | Lima simulasi deterministik offline dengan state temporer dan klasifikasi sampel nyata Tahap 1. |
| `test_kemdiktisaintek_monitor.py` | Tes offline state/policy/capture/CAS/isolation/CLI/workflow/simulasi. |
| `kemdiktisaintek-collect.workflow.yml` | Draft workflow manual collect-only, berada di folder eksperimen sehingga belum dieksekusi GitHub Actions. |
| `stage2_simulation.json` | Hasil simulasi yang dapat ditinjau, bukan state aktif atau bukti produksi. |
| `stage2_bima_comparison.json` | Perbandingan read-only lima sampel dengan snapshot historis BIMA lokal berikut hash referensinya. |

Tidak ada import `notifier`, `app_config`, `monitor_runner`, `production_runtime`, atau backend YouTube dalam komponen baru. Tidak ada pembacaan `.env`, token atau konfigurasi produksi. Tidak ada pemanggilan Telegram, bahkan untuk preflight. Outbox sengaja harus tetap kosong menurut validator Tahap 2.

## Schema state

Target untuk penggunaan berikutnya dikunci pada branch `kemdiktisaintek-experiment-state`, path `.bot-state/kemdiktisaintek_experiment.json`. Branch tersebut **tidak dibuat atau ditulis** pada Tahap 2.

```text
schema_version: 1
source: kemdiktisaintek
initialized: true
policy_version: kemdiktisaintek-experiment-v1
generation: 1, 2, ...
activation_at: timestamp UTC/offset-aware
last_successful_poll_at: timestamp UTC/offset-aware
delivery_authorized: false
baseline:
  observed_at, ids[], coverage, archive_complete: false, sha256
observations:
  <UUID>:
    uuid, url, title, published_at
    first_seen_at, last_seen_at, detail_checked_at
    content_sha256: SHA256 atau null jika belum dihidrasi
    classification: decision, priority, reasons[], evidence, policy_version
    detection_status: NEW_CANDIDATE | KNOWN | HOLD | REJECTED | NEEDS_REVIEW
    delivery_status: BLOCKED_STAGE2
    initial_inventory, initial_decision, reasons[], deduplication
    last_item: snapshot metadata/isi/lampiran
    url_history[]
identity_holds: konflik/fallback beserta first/last seen dan alasan HOLD
revisions[]: revision_id, UUID, event_type, status, observed_at,
             before/after snapshot hash, changes per kelompok, important
outbox: {}
last_capture_coverage: cakupan poll terakhir
bima_reference_sha256: hash canonical snapshot BIMA atau null
```

Baseline, activation fence, first_seen, status inventory awal, riwayat URL dan bukti revisi tidak boleh dihapus/ditulis ulang lewat checkpoint. Generation harus bertambah satu, waktu poll harus maju, semua observasi lama tetap ada. Source/schema/policy tak cocok, waktu tanpa offset, UUID/URL konflik, hash konten tak cocok atau baseline checksum rusak menghentikan load. `content_sha256=null` berarti belum diperiksa detailnya; tidak cukup untuk kandidat baru.

`LocalStateStore` hanya menerima direktori bernama persis `kemdiktisaintek-experiment-state`; nama/path state di dalamnya tetap. Tidak menerima file cache BIMA/YouTube sebagai target. Symlink ditolak. Penulisan memakai lock eksklusif, expected SHA256 file, file temporer, fsync, dan atomic replace. Lock yang tertinggal atau CAS konflik tidak dibersihkan paksa; operator harus merekonsiliasi. Kegagalan save menghentikan proses dan tidak mengeluarkan laporan sukses.

Penanda lokal `.bot-state/kemdiktisaintek_initialized.json` menyimpan activation_at dan baseline hash, ditulis sebelum pembuatan state pertama. Jika file state kemudian hilang atau inisialisasi terputus, penanda melarang initialize ulang; CLI berhenti sebelum request. Penanda hilang/rusak/berbeda juga menggagalkan load. Pemulihan harus mengembalikan pasangan state+penanda dari backup terverifikasi. Jika seluruh direktori beserta penanda dihapus, tidak ada bukti lokal yang dapat membedakannya dari lokasi pertama kali digunakan; mode poll tetap gagal dan tidak menginisialisasi otomatis.

`ContentsCAS` memodelkan GET/PUT GitHub Contents, expected **blob SHA Git 40 karakter**, base64 dan konflik 409/422, dengan validator khusus dan branch/path allowlist. Transport yang diterima hanya `MockContentsTransport` persis. Tidak ada requests client, token atau adapter network; memasukkan transport HTTP nyata ditolak. Ini menyediakan pengujian kontrak CAS tanpa kemampuan menulis GitHub. Adapter remote aktual tetap pekerjaan tahap berikutnya yang memerlukan peninjauan terpisah. Primitive CAS existing dipelajari, namun backend YouTube tidak diimpor karena default validator, branch, path dan environment-nya spesifik YouTube.

## Baseline konservatif

Inisialisasi eksplisit mengambil API daftar tanpa kategori/search, default dua halaman terbaru dengan ukuran sepuluh, maksimal tiga halaman. Semua halaman yang diminta atau halaman terminal yang sah harus sehat. Total harus konsisten sepanjang capture; jumlah row harus sesuai ukuran halaman/total. Detail memiliki budget terpisah maksimal sepuluh. Kegagalan satu detail yang dipilih menggagalkan seluruh capture, bukan menyimpan state parsial.

Baseline menyimpan UUID, timestamp observasi/activation fence, halaman yang diminta/ditangkap, total yang dilaporkan dan detail budget. `archive_complete` selalu false; tidak ada klaim menginventarisasi seluruh arsip. UUID tanpa detail tetap tercatat sebagai inventory KNOWN, dengan hash null. Fallback URL disimpan di identity_holds, bukan dibentuk menjadi UUID palsu.

Baseline existing tidak dapat ditimpa. Mode poll memuat dan memvalidasi state **sebelum request**; file hilang/rusak menghentikan proses. Tidak ada pembuatan ulang baseline otomatis, fallback cache lokal lain, atau fallback state produksi. `initialize` merupakan satu-satunya operasi create; operator tidak boleh menggunakannya untuk menyamarkan state lama yang hilang. Pemulihan backup tetap membutuhkan keputusan operator.

## Capture dan deteksi baru

Setiap poll membaca kembali jendela dua halaman terbaru secara overlap. Total berubah di tengah capture atau halaman kurang row merupakan kegagalan. UUID/URL konflik antarrow menggagalkan capture. Duplikat identik antarpages dideduplikasi dan dicatat sebagai indikasi pergeseran; kandidat baru pada capture itu ditahan. Tidak ada asumsi bahwa item yang absen dihapus: last_seen tetap timestamp sebelumnya dan observasi tetap tersimpan.

Detail kandidat baru diprioritaskan, sambil menyisihkan hingga tiga slot untuk artikel dikenal yang paling lama/tidak pernah diperiksa detailnya. Ini menghasilkan pemeriksaan detail bergilir dan membatasi beban situs. Artikel baru yang belum mendapat slot detail tetap HOLD. Rotasi melanjutkan timestamp detail_checked_at persisten setelah restart.

Kandidat baru harus memenuhi semua kondisi berikut:

1. UUID valid dan URL kanonis tanpa konflik/identity hold.
2. Teks detail serta content fingerprint tersedia.
3. Timestamp publikasi detail (`publication_basis=detail_datetime`) valid, offset-aware, setelah activation fence dan tidak di masa depan. Timestamp daftar saja tidak dianggap terpercaya.
4. Publikasi masih dalam jendela 24 jam dan first_seen belum melewati TTL 24 jam.
5. Poll sehat, overlap dengan inventory sebelumnya masih ada, tidak ada indikasi duplikat pergeseran, serta gap dari poll sukses sebelumnya tidak melebihi 30 menit.
6. Classifier RELEVANT dan dedup BIMA NO_MATCH memakai reference eksplisit.

Ketiadaan reference BIMA berarti NEEDS_REVIEW, bukan melewati dedup. Artikel relevan sebelum/sama fence tetap HOLD. Classifier NOT_RELEVANT yang memenuhi bukti waktu/detail menjadi REJECTED. Ketidakpastian relevansi/duplikasi menjadi NEEDS_REVIEW. Kandidat tidak menghasilkan outbox. Poll berikutnya yang identik menjadi KNOWN dan initial_decision tetap menyimpan hasil admission pertama. KNOWN berarti sudah diobservasi/diproses, **bukan** sudah dikirim.

Gap/overlap hilang/pergeseran pagination menghasilkan HOLD yang tetap menunggu rekonsiliasi, tidak otomatis dipromosikan pada poll berikutnya. Fallback UUID-ke-URL menahan event dan observasi UUID lama; UUID yang kembali juga tetap NEEDS_REVIEW. UUID sama dengan slug baru menjadi revision candidate; UUID berbeda yang memakai URL historis milik UUID lama menjadi identity hold.

## Deteksi revisi

Perbandingan snapshot memisahkan metadata (judul/kategori/nomor surat), isi, lampiran, tanggal publikasi/update detail, URL/slug, deadline, serta link eksternal. Fingerprint Tahap 1 dipakai dan divalidasi ulang. Pemeriksaan daftar saja dapat mendeteksi perubahan judul/URL; isi/lampiran diperiksa ketika artikel mendapat rotasi detail. Timestamp `updated_at` daftar tidak disalahartikan sebagai waktu modifikasi artikel.

Revisi menyimpan hash snapshot sebelum/sesudah dan nilai field yang berubah. Event bertipe REVISION_CANDIDATE dengan status NEEDS_REVIEW; perubahan deadline/lampiran/tanggal/URL ditandai important. Tidak ada reservation, pengiriman, penghapusan cache, atau kenaikan hak kirim karena hash berubah. Bukti revisi yang sama dideduplikasi. NEEDS_REVIEW tetap terbuka pada poll identik sampai mekanisme review tahap berikutnya ditentukan. Hidrasi isi pertama pada record list-only tidak otomatis dianggap revisi konten lama yang tidak pernah diamati.

## Classifier dan contoh nyata

Rule transparan menggabungkan frasa topik, audiens akademik, kata tindakan dan bukti isi/lampiran/deadline. Kategori, tanggal, nomor surat, dan keberadaan lampiran disertakan dalam evidence keputusan. Kategori tidak pernah cukup sendiri; tanggal tidak mengubah relevansi menjadi kelayakan kirim. Nomor surat/tanggal dicatat untuk penelusuran dan dedup, bukan dipakai sebagai keyword yang membuktikan relevansi.

Prioritas tinggi: riset/hibah/pengabdian, beasiswa dosen/doktor, reviewer akademik, akreditasi/jabatan/regulasi perguruan tinggi, program akademik dengan deadline eksplisit. Prioritas menengah: seminar ilmiah, kompetisi mahasiswa, pelatihan/webinar/pengembangan kompetensi dengan audiens akademik. Rekrutmen tata usaha sekolah dan laporan seremonial tanpa tindakan/deadline diklasifikasikan rendah. Satu kata seperti reviewer atau kategori riset saja menghasilkan NEEDS_REVIEW.

| Sampel nyata dari Tahap 1 | Keputusan | Alasan |
| --- | --- | --- |
| Seleksi Kepala Subbagian Tata Usaha SMA Unggul Garuda Baru | NOT_RELEVANT, low | Rekrutmen administrasi sekolah, bukan mayoritas audiens perguruan tinggi. |
| AKPT Tahap III 2026 Dibuka di ITB | NOT_RELEVANT, low | Laporan pelaksanaan/seremonial; tidak ada deadline tindakan. |
| Pendaftaran LNLM 2027, kategori Umum | RELEVANT, high | Audiens dosen/ilmuwan, pendaftaran akademik, deadline eksplisit. |
| Penetapan Reviewer Beasiswa PDDI | RELEVANT, high | Reviewer akademik + beasiswa dan bukti isi. |
| Pelaksanaan Pendidikan dan BDR di Perguruan Tinggi Terdampak | NEEDS_REVIEW | Aturan kata belum cukup mengenali seluruh variasi regulasi; tidak ditebak otomatis. |

Seluruh sampel tersebut adalah materi historis relatif terhadap fence simulasi. RELEVANT tidak membuatnya kandidat berita baru. Detail evidence dan policy version ada pada `stage2_simulation.json`.

## Deduplikasi lintas sumber

Kode BIMA memadukan surat+judul dan memeriksa tanggal/dokumen; nomor surat bukan identitas unik. Tahap 2 menggunakan UUID Kemdiktisaintek sendiri, tidak memakai `item_key` atau matcher BIMA sebagai identitas sumber baru.

Matcher read-only menyimpan evidence nomor surat ternormalisasi, judul, hari publikasi WIB, URL dokumen dan nama program bila tersedia eksplisit. DUPLICATE memerlukan judul sama + URL dokumen sama, atau judul sama + nomor surat sama + tanggal sama. Surat saja, judul saja, dokumen saja, atau kemiripan judul dengan konteks tanggal/program menghasilkan NEEDS_REVIEW. Tidak ada fuzzy auto-suppression. Nama program opsional belum diekstraksi otomatis oleh parser.

Reference BIMA diberikan sebagai list snapshot atau `--bima-history` eksplisit. Tidak ada save ke BIMA. URL dokumen bertanda tangan dan cache BIMA yang hanya berisi nama dokumen dapat membuat bukti URL tidak tersedia; jangan dianggap NO_MATCH sebagai bukti pasti tidak duplikat secara semantik. `stage2_bima_comparison.json` mencatat hasil perbandingan dengan 90 referensi historis lokal Tahap 1, bukan inventaris BIMA produksi terbaru.

## Menjalankan tanpa Telegram

Dari root worktree pengembangan:

```powershell
# Offline, membuat state hanya dalam direktori temporer yang dibersihkan otomatis.
python eksperimen/kemdiktisaintek/simulate_stage2.py

# Semua tes eksperimen, offline.
python -m pytest eksperimen/kemdiktisaintek/test_kemdiktisaintek_parser.py eksperimen/kemdiktisaintek/test_kemdiktisaintek_monitor.py -q

# Seluruh regression proyek + eksperimen.
python -m pytest tests eksperimen/kemdiktisaintek/test_kemdiktisaintek_parser.py eksperimen/kemdiktisaintek/test_kemdiktisaintek_monitor.py -q

# Read-only live collection jika sengaja diminta operator; tidak menulis state.
python eksperimen/kemdiktisaintek/kemdiktisaintek_cli.py collect --pages 2 --details 3
```

API lokal untuk persistent state sudah diuji memakai TemporaryDirectory dan backend mock. Jika kelak baseline lokal nyata diotorisasi, CLI `initialize --state-dir <direktori>/kemdiktisaintek-experiment-state` dan `poll --state-dir ... --bima-history <snapshot.json>` tersedia. Pada pengerjaan Tahap 2 ini **tidak dijalankan live initialization/poll**; seluruh perubahan state menggunakan fixture/mocks/temporary storage. Tidak ada CLI opsi untuk Telegram, token atau penulisan remote.

## Simulasi wajib

`simulate_stage2.py` memakai UUID/timestamp sintetis yang ditandai jelas, diturunkan dari bentuk data Tahap 1. Reference BIMA simulasi ialah list kosong eksplisit, bukan klaim tidak ada duplikat di produksi.

| Skenario | Hasil |
| --- | --- |
| Artikel historis di luar baseline | HOLD; sebelum activation fence/jendela baru. |
| Artikel baru dan relevan | NEW_CANDIDATE; seluruh admission check terpenuhi. |
| Artikel baru, rekrutmen nonakademik | REJECTED; classification NOT_RELEVANT. |
| Artikel baseline direvisi isi/deadline/lampirannya | REVISION_CANDIDATE / NEEDS_REVIEW; bukti perubahan tersimpan. |
| Kandidat yang sudah diproses pada poll sebelumnya | KNOWN; first_seen dan initial_decision dipertahankan. |

Seluruh skenario: outbox kosong, delivery_authorized=false, tidak ada Telegram.

## Draft Actions dan keselamatan

Draft YAML berada di folder eksperimen, belum di `.github/workflows`, sehingga tidak bisa dijalankan GitHub. Jika kelak dipindahkan setelah peninjauan: workflow_dispatch saja, contents read, checkout exact github.sha, persist-credentials=false, collect dua halaman/tiga detail, timeout lima menit, artifact run/attempt terpisah. Tidak memakai cron, secret Telegram, env produksi, atau argumen state. Pengujiannya memeriksa trigger, permission, mode collect, checkout dan ketiadaan secret/remote state. Tidak ada perubahan workflow produksi.

## Pengujian dan bukti

Hasil final: **1.015 passed, 26 subtests passed**, 58.86 detik, return code 0. Rinciannya **829 regression tests BIMA/YouTube + 83 tes parser Tahap 1 + 103 tes Tahap 2**. Ekspor bersih seluruh Git index ke checkout temporer juga berhasil menjalankan **186 tes eksperimen**, 6.71 detik; ini membuktikan source/fixtures yang diperlukan sudah terlacak tanpa bergantung pada capture/output yang di-ignore.

Output lengkap lokal berada di `stage2_test_results.log`, XML `stage2_test_results.xml`, metadata `stage2_test_summary.json`, dan bukti ekspor `stage2_index_review.json`; tidak di-commit. `stage2_integrity_after.json` memverifikasi hash 99 file terlindungi di worktree pengembangan dan 15 file terlacak di checkout asli, total **114 file identik tanpa perubahan**; tidak di-commit karena berisi path workstation. Diff Tahap 2 hanya sebelas berkas pada `eksperimen/kemdiktisaintek/`, sudah di-stage eksplisit dan belum di-commit. Satu-satunya commit yang dibuat ialah checkpoint Tahap 1.

Cakupan tes: baseline/reinitialize/restart/missing/corrupt/hash-corrupt state, historical/backdated/no-date/future publication, first_seen, publication provenance, UUID fallback/URL conflict/duplicate IDs, overlap/pagination shift/gap/partial API/total berubah/detail timeout, absent article, revisi semua kelompok dan dedup revision, classifier semua topik prioritas dan lima sampel nyata, dedup multi-evidence BIMA, local/blob CAS conflict/lock/disk failure, lineage immutable, branch/path isolation, penolakan transport nyata, draft workflow, larangan network/Telegram/import sender, serta lima simulasi.

## Risiko tersisa dan kesiapan Tahap 3

* API tanpa kontrak versi dan offset pagination masih dapat berubah. Jendela terbatas + overlap tidak membuktikan kelengkapan arsip. Burst tanpa overlap atau gap membutuhkan rekonsiliasi; tidak ada backfill otomatis.
* Rules classifier konservatif belum mencakup semua bentuk regulasi, report kegiatan, program dan bahasa. Evidence membantu review, tetapi belum ada ground-truth evaluation besar.
* Nomor surat/deadline di PDF tidak diekstraksi; lampiran hanya link. Isi PDF yang berubah pada URL tetap tidak terdeteksi. URL/urutan/label lampiran yang berubah dapat menghasilkan revision candidate tanpa perubahan substantif.
* Budget detail membuat sebagian metadata/body belum dipantau setiap poll. Detail yang gagal menggagalkan checkpoint seluruh poll; ini konservatif tetapi mengurangi liveness.
* Identity/gap/revision HOLD bersifat sticky; belum ada CLI untuk mengubah keputusan review. Ini disengaja agar tahap ini tidak memberi izin kirim tersembunyi.
* State JSON satu blob dibatasi 900 KB; belum ada pruning/archival otomatis. Melebihi batas menghentikan save. Lock crash harus direkonsiliasi. Atomic replace/fsync file tidak sama dengan jaminan durability direktori setelah power loss di semua OS.
* Transport Contents API nyata belum disediakan. Mock CAS membuktikan kontrak lokal, bukan integrasi/persistensi GitHub produksi. Tidak ada branch state remote yang disentuh.
* Reference BIMA lokal historis belum mencakup produksi terbaru. Stage 3 perlu reference yang ditinjau/di-hash, strategi refresh read-only dan penanganan unknown semantic duplicates.
* Test run menggunakan environment Python/pytest yang tersedia; tidak mengganti instalasi global. Dependensi baru tidak ditambahkan ke proyek.

**Siap untuk peninjauan Tahap 3**, bukan siap mengirim. Tahap 3 perlu preflight identitas/izin channel eksperimen existing, hash allowlist dan pembuktian terpisah dari produksi; kill switch default-off; format Telegram dan escaping; outbox/ticket/payload hash; CAS claim sebelum POST; receipts/ambiguous-reconciliation tanpa retry otomatis; validasi adapter remote khusus bila diotorisasi. Review ulang baseline/fence dan evidence kandidat wajib dilakukan saat aktivasi. Tidak boleh meneruskan initial_decision NEW_CANDIDATE historis sebagai izin kirim tanpa pemeriksaan ulang.
