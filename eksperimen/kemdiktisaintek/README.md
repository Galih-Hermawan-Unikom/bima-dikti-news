# Pengumuman Kemdiktisaintek — Tahap 1

Catatan checkpoint sebelum Tahap 2: fixture HTML telah diringkas dengan mempertahankan DOM artikel/daftar, pagination, dan metadata JSON. Gambar/base64, i18n, script bersama, serta artikel terkait dikeluarkan; fixture terbesar 14.5 KB. Sebanyak 83 tes parser tetap lulus. Hanya source, dokumentasi, 16 fixture yang dipakai tes, contoh JSON, dan checksum fixture masuk checkpoint Git. Capture mentah, manifest audit lokal, dan output tes lama tidak disertakan. Pernyataan operasional di bawah mendokumentasikan kondisi saat audit Tahap 1; checkpoint lokal kemudian dibuat atas instruksi Tahap 2.

Audit dan implementasi lokal pada 24 Oktober 2026. Branch `feat/kemdiktisaintek-parser-stage1`, berbasis `origin/master` pada commit `8c9fa4833891c31f1609ad2c0aa53d207917c00a`.

Checkout awal tertinggal 534 commit dan memiliki 13 file termodifikasi. Pekerjaan dilakukan dalam worktree baru. Checkout awal tidak di-reset, di-stash, atau dipindah. Semua file tahap ini berada di `eksperimen/kemdiktisaintek/`, yang sudah dicakup `.gitignore` milik master. Tidak ada dependensi baru, perubahan komponen bersama, workflow, cache historis, state, konfigurasi, atau secret.

## Menjalankan dan meninjau

Dari root worktree pengembangan, menggunakan dependensi proyek yang sudah terpasang:

```powershell
# Offline: tidak menghubungi situs atau Telegram.
python -m pytest eksperimen/kemdiktisaintek/test_kemdiktisaintek_parser.py -q

# Regression + prototipe; testpaths default proyek hanya mencakup tests/.
python -m pytest tests eksperimen/kemdiktisaintek/test_kemdiktisaintek_parser.py -q

# Read-only, dibatasi satu halaman + satu detail. JSON ke stdout.
python eksperimen/kemdiktisaintek/kemdiktisaintek_parser.py --method api --pages 1 --details 1

# Pembanding HTML; default metode HTML, satu halaman, tiga detail.
python eksperimen/kemdiktisaintek/kemdiktisaintek_parser.py --method html --pages 1 --details 1
```

Live request hanya dilakukan saat CLI atau client dipanggil secara eksplisit. Import modul dan pytest tidak melakukan request langsung. Maksimum tiga halaman daftar dan sepuluh detail, halaman sepuluh item, tanpa pengambilan arsip otomatis. Lampiran hanya diidentifikasi; tidak diunduh atau diikuti. Skrip `investigate*.py` merupakan alat capture audit eksplisit, bukan bagian jalur parser.

## Audit implementasi master

Temuan berikut dicocokkan dengan kode, bukan disimpulkan dari README:

| Komponen | Perilaku aktual dan batas reuse |
| --- | --- |
| Struktur/dependensi | Modul Python datar di root, workflow `.github/workflows`, tests pytest + fixtures di `tests/fixtures`. `requirements.txt`: playwright 1.49.0, BeautifulSoup 4.12.3, schedule 1.2.1, python-telegram-bot 21.0.1, requests 2.31.0. `requirements-dev.txt` menambah pytest dan PyYAML. Prototipe hanya memakai requests, BeautifulSoup, standard library. |
| `scraper.py` | BIMA memakai browser Playwright, dua layout (`article.ap-card` dan legacy `.card-body`), readiness metadata/dokumen, penolakan mixed layout, validasi nomor surat dan tanggal. Resolusi dokumen menangkap request saat klik; cache hanya menyimpan nama dokumen karena URL bertanda tangan dapat kedaluwarsa. `announcement_identity` menggabungkan nomor surat dan judul; `match_announcement` juga memeriksa tanggal/dokumen dan menolak ambiguitas. Identitas ini spesifik BIMA, jangan diterapkan sebagai identitas sumber baru. |
| YouTube | `youtube_monitor.py` masih berisi RSS discovery; publikasi RSS tidak membuktikan kesiapan video. Jalur produksi aktif memakai `youtube_channel_tabs.py`, `youtube_videos_parser.py`, dan `youtube_tabs_monitor.py`: ekstraksi `ytInitialData`, validasi video/channel/tab, gabungan `/videos` + `/streams`, inventory, policy dan evidence. Keberadaan monitor RSS tidak berarti runtime produksi memakai RSS. |
| `monitor_runner.py` | Memisahkan deteksi dari pengiriman; journal batch, event keys, progress chunks/documents, pemeriksaan identitas BIMA. YouTube punya observation store/outbox tersendiri dan mode eksperimen dengan batas satu kirim. `item_key` memperlakukan sumber selain YouTube sebagai BIMA: berbahaya memasukkan sumber baru tanpa perubahan kontrak yang ditinjau. Jangan memakai runner ini untuk prototipe. |
| `notifier.py` | Dapat memuat konfigurasi/secret jika config tidak diberikan. Merender HTML Telegram, membagi pesan menurut batas UTF-16, mengirim ringkasan dan dokumen serta mencatat receipts. Jalur YouTube memerlukan `DeliveryTicket`, hash payload, attempt pertama, destination allowlist, flags dan format khusus. Bukan utilitas parsing yang aman untuk diimpor tahap ini. |
| `production_runtime.py` | Entry point produksi terpisah untuk BIMA/YouTube, hanya di GitHub Actions. State branch `dikti-production-state`, file `.bot-state/{source}_production.json`. Initialize membentuk inventory tanpa pengiriman. Cache/journal root menjadi referensi historis dan diperiksa integritasnya. BIMA memakai activation fence berbasis tanggal; YouTube memakai policy tabs, window/TTL, startup quarantine. |
| Durable state/SHA/CAS | `youtube_state_backend.GitHubStateBranch`: memastikan branch tersedia, menolak master/main, membaca JSON lewat Contents API dan SHA blob, menulis checkpoint dengan expected SHA. 409/422 menghentikan operasi; tidak ada fallback state basi. CAS yang benar-benar dikirim ke GitHub adalah **SHA blob**, bukan kunci global branch-head. Validator default spesifik YouTube; BIMA mengirim validator sendiri. Nama/path/branch masih harus dikunci oleh wrapper baru agar tidak menyentuh produksi. |
| Outbox | `YouTubeStateStore` mempunyai schema/validasi, observasi, reservation, payload hash, attempt token, sending lease, delivered/ambiguous. Runtime BIMA juga menyimpan reserved lalu sending melalui checkpoint CAS sebelum Telegram POST. Hasil ambigu tidak otomatis diulang. Pola dapat dipakai kembali pada tahap kedua; schema/ticket YouTube tidak boleh dipakai langsung untuk artikel. |
| `reliability.py` | `SourceError`, result dataclasses, `atomic_json` (temp + fsync + replace) tidak memuat secret/network. Dapat digunakan ulang setelah diperlukan, tetapi prototipe tidak membutuhkan state sehingga tidak mengimpornya. |
| Produksi Actions | `main.yml`: BIMA `13 */3 * * *`; `youtube-production.yml`: YouTube `23,53 * * * *`. Keduanya checkout master, `contents: write`, concurrency `dikti-production-state`, preflight tujuan resmi dan dua switch produksi. Hanya dibaca, tidak dieksekusi/diubah. |
| Eksperimen Actions | `youtube-shadow-multitab.yml` manual, contents read, checkout exact SHA, snapshot dua tab dan upload artifact tanpa Telegram. `youtube-experiment-preflight.yml` manual dan hanya preflight. `youtube-observation-only.yml` **walaupun namanya observation-only, kode/workflow kini mencakup pengiriman eksperimen yang digate** melalui `youtube_experiment_pipeline.py`; tidak aman dijalankan hanya berdasarkan namanya. Pipeline mengunci fence/policy/inventory, state `youtube-state`, preflight, kill switch, satu event, dan melarang jalur BIMA. |
| Telegram preflight | `verify_experiment_destination` memakai `YOUTUBE_EXPERIMENT_BOT_TOKEN`, `YOUTUBE_EXPERIMENT_CHAT_ID`, `YOUTUBE_EXPERIMENT_CHAT_ID_SHA256`; memverifikasi hash allowlist, channel ID/type, bot, izin posting, serta perbedaan dengan `@dikti_news` melalui GET. Hasil hanya alias hash. Tidak dipanggil pada tahap ini dan secret tidak dibaca. Nama lama dapat dipetakan dalam wrapper tahap kedua yang memakai tujuan eksperimen sama. |
| Pengujian | Fixtures HTML BIMA modern/legacy, YouTube HTML/XML/initial JSON dan skenario shadow/state. `tests/conftest.py` memblokir jaringan non-loopback, request nyata dan pembacaan `.env`, serta memeriksa hash cache kedua worktree. Tests backend/outbox menggunakan mock HTTP dan tmp_path. Tests prototipe terpisah dengan requests nyata diblokir. |

Reuse aman sekarang: dependensi parsing/HTTP dan pola validasi saja. Reuse bersyarat tahap kedua: primitive atomic JSON, backend CAS dengan validator/allowlist baru, prinsip outbox dan preflight tujuan eksperimen. Tidak ada import dari bot/runner/notifier/backend/config di parser baru.

## Temuan situs dan bukti respons nyata

Sumber [daftar resmi](https://kemdiktisaintek.go.id/announcement), diperiksa lewat GET dengan timeout connect/read 10/30 detik, jeda sekitar satu detik, tanpa POST atau crawling rekursif. Capture mentah ada di `fixtures/`, checksum di `fixture_checksums.json`; manifest bertimestamp untuk batch lanjutan di `*_capture_manifest.json`.

Batch pertama sempat berhenti ketika terminal Windows gagal mencetak emoji setelah semua delapan respons disimpan. Manifest batch pertama tidak selesai; jangan menganggap manifest lanjutan mencakup provenance semua fixture awal. Raw fixture dan checksum tetap tersedia. Tidak ada klaim bahwa checksum saja mengautentikasi origin. Capture berikutnya serta dua smoke test berhasil. Angka di bawah berasal dari respons langsung lokal, bukan hasil cache indeks mesin pencari.

* Daftar HTTP 200, server-rendered Next.js Pages Router: konten sudah ada pada respons HTML, tidak perlu browser. Script `#__NEXT_DATA__` memuat `props.pageProps.announcements.data` dan `total`. Ada `buildId` dan script chunks; bukan WordPress feed walaupun ada rewrite URL warisan `/wp-content/uploads/` dalam bundle.
* Daftar total 928, sepuluh item per halaman, 93 halaman. `?page=2&size=10&search=` benar-benar memuat item berbeda, Next menuju halaman 3. Prev halaman 1 menunjuk page=0: parser mengabaikan Prev, tidak merayap ke sana.
* Filter yang benar-benar ditautkan dan diuji: `category=umum` (883), `pendidikan-tinggi` (23), `sains-dan-teknologi` (22), `riset-dan-pengembangan` (0). Kategori riset kosong tidak berarti seluruh situs tidak punya informasi riset.
* `search=beasiswa` bekerja pada server; API melaporkan 114 hasil. JavaScript juga mempunyai filter teks client-side pada item yang tampil. Hasil pencarian tidak boleh dipakai sebagai satu-satunya inventory monitoring.
* Card DOM: `a[href*="/announcement/article/"]`, judul, metadata pada parent card, kategori `p.text-blue-400`, tanggal teks. Terdapat dua tautan artikel per card, sehingga deduplikasi wajib. Detail menggunakan `/announcement/article/{slug}`, `h1` dan `article.rich-content`; tombol PDF berada **di luar** article, dalam container detail yang sama. Sidebar/related articles tidak dibaca sebagai isi utama.
* Metadata JSON daftar: UUID, slug, title, category, created_at, updated_at, tags. Detail `pageProps.data`: UUID/title/slug/category, `datetime`, `created_at`, `updated_at`, `content` berupa base64 HTML, `source` berupa URL lampiran dan tags. Isi diambil dari DOM yang sudah dirender agar tidak perlu decoding rich-text base64 beserta gambar besar.
* Tampilan `20 Oktober 2026 | 05.00 WIB` menggunakan zona UTC+7. Bundle halaman secara eksplisit memformat `updated_at` daftar. Pada detail, `datetime` ialah waktu publikasi/tampilan dan `updated_at` ialah timestamp terpisah. Contoh LNLM: publikasi 16 September pukul 14.00 WIB, created_at 17 September. Karena itu prototipe menyimpan `publication_basis`, created_at, dan updated_at detail terpisah; updated_at daftar tidak disebut waktu perubahan artikel.
* PDF `/api/file/humas-production/announcements/{tahun}/{bulan}/files/{uuid}/{nama}.pdf`, folder `drive.google.com`, serta shortlink pendaftaran `bit.ly` ditemukan. Semuanya diekstrak tanpa mengikuti link, mengunduh PDF atau menjelajahi Drive. Status `verification=link_only`, tipe berdasar URL, bukan hasil pemeriksaan MIME/download.
* Nomor surat tidak tersedia sebagai field khusus pada lima artikel sampel; nilainya `null`. Parser mengenali label eksplisit dalam isi HTML jika ada dan diuji memakai fixture sintetis. Tidak menebak nomor surat dari nama PDF. Tenggat eksplisit LNLM: 25 September 2026 pukul 23.59 WIB; disimpan sebagai kutipan bukti, bukan keputusan kelayakan otomatis. Rentang tanggal seperti 20–28 Oktober belum dinormalisasi sebagai deadline.
* `robots.txt` langsung HTTP 404 dengan halaman HTML error. Ini berarti tidak ada aturan robots yang diterbitkan di path itu pada saat audit, bukan jaminan izin permanen. Client memeriksanya per sesi; rules disallow dipatuhi, delay/rate dihormati; 403/429/error/robots tak dikenali menghentikan proses. `/feed` juga HTTP 404, tidak ada link RSS/Atom pada daftar. Tidak dilakukan enumerasi path API/feed secara luas, jadi tidak mengklaim RSS mustahil tersedia.
* Tidak ada login/CAPTCHA/429 yang teramati dalam sampel; tidak menyimpulkan akses tak terbatas. Redirect ditolak, timeout/connection error dan 500/502/503/504 maksimal dua percobaan, 429 tidak diulang otomatis. Respons dibatasi pemeriksaan 5 MB setelah diterima. Streaming hard cap dan total wall-clock timeout belum diimplementasikan.

## API yang terverifikasi dan rekomendasi

[API daftar publik](https://kemdiktisaintek.go.id/api/announcement?page=1&size=10&search=) ditemukan dari JavaScript halaman, lalu diuji langsung: HTTP 200 `application/json`, schema `{data: [...], total: 928}`. Parameter `page`, `size=10`, `search` dan `category` diverifikasi dengan empat fixture API. API search dan category masing-masing menghasilkan total 114 dan 23. Item daftar API persis sama dengan JSON tertanam HTML pada capture tersebut.

Rekomendasi tahap kedua: **API daftar + HTML detail**, dengan schema validation, batas halaman, serta metode HTML eksplisit untuk diagnosis/fallback operator. API daftar hanya sekitar 3.6 KB, HTML daftar sekitar puluhan KB, dan tidak memerlukan build ID. API ini endpoint internal publik yang dipakai situs, bukan API dengan kontrak/versioning terdokumentasi; perubahan schema tetap mungkin.

Endpoint Next.js `/_next/data/{buildId}/id/announcement.json?...` juga HTTP 200 dan memuat pageProps sama, tetapi bergantung pada build ID deployment, membawa i18n tambahan, dan tidak menawarkan manfaat dibanding API daftar. Tidak direkomendasikan sebagai jalur utama. Endpoint API detail tidak ditebak/dipanggil; HTML detail sudah memberi semua metadata yang diperlukan.

Prototipe mendukung `--method api` dan `--method html` secara eksplisit. Tidak ada fallback otomatis ketika API rusak yang dapat menyamarkan status gagal. Kedua jalur memakai parser detail yang sama, UUID stabil, URL tervalidasi dan hash isi.

## Struktur hasil

`sample_output.json` berisi lima artikel lengkap dari tiga kategori dan dua halaman. `live_smoke.json` dan `live_api_smoke.json` ialah dua observasi read-only terpisah, masing-masing satu halaman dan satu detail. Envelope berisi schema_version, observed_at UTC, robots_status, pages, items, bounded=true, archive_complete=false, delivery_authorized=false.

Field artikel: source, id, identity, identity_basis, slug, title, category, url, created_at, published_at, publication_basis, updated_at, warnings. Detail menambah body_text, letter_number, deadline_evidence, attachments, external_links, method, content_sha256. UUID menjadi identitas utama, URL kanonis fallback dengan warning jika embedded JSON tidak tersedia; pergantian basis identitas wajib direkonsiliasi sebelum tahap state.

Contoh ringkas nyata, field lain tersedia pada berkas JSON:

```json
{
  "source": "kemdiktisaintek",
  "id": "90eb5a79-6fb3-4f39-b2ac-6855119d3579",
  "identity": "kemdiktisaintek:90eb5a79-6fb3-4f39-b2ac-6855119d3579",
  "title": "Pendaftaran Lindau Nobel Laureate Meetings (LNLM) 2027",
  "category": "Umum",
  "published_at": "2026-09-16T14:00:00+07:00",
  "publication_basis": "detail_datetime",
  "url": "https://kemdiktisaintek.go.id/announcement/article/pendaftaran-lindau-nobel-laureate-meetings-lnlm-2027",
  "letter_number": null,
  "deadline_evidence": ["Batas pendaftaran: 25 September 2026, pukul 23.59 WIB"]
}
```

## Pengujian dan batas hasil

Regression proyek awal: **829 passed**, 106.82 detik. Prototipe awal **69 passed**, kemudian diperluas dengan fixtures API dan dua detail tambahan menjadi **83 tes**. Hasil final gabungan: **912 passed, 26 subtests passed**, 235.44 detik, return code 0. Log, metadata dan XML tersedia dalam `test_results.log`, `test_summary.json`, `test_results.xml`.

Cakupan offline: daftar/detail nyata, tiga kategori dan kategori kosong, pencarian, paginasi, tanggal WIB/ISO/date-only/invalid, normalisasi/rejection URL, PDF/Word/Drive/embed, tanpa lampiran, elemen opsional hilang, elemen wajib hilang, body kosong, respons kosong, perubahan schema dan JSON rusak, UUID/URL conflict, dedup URL dalam/antarhalaman, error HTTP/timeout/retry terbatas, robots disallow, batas collection dan fingerprint stabil terhadap markup. Tidak bergantung pada internet. Versi lingkungan aktual saat tes: Python 3.11.9, pytest 9.0.3; constraint dev repo menyebut pytest >=8,<9. Tidak mengubah instalasi global atau dependency lock; pengujian CI dengan versi sesuai constraint dapat dilakukan pada tahap peninjauan berikutnya.

Smoke situs HTML dan API masing-masing berhasil: 10 daftar, satu detail berisi teks dan lampiran, total 928, robots 404, delivery_authorized=false. Ini membuktikan parsing saat audit, bukan operasi bot produksi atau pengiriman Telegram.

## Risiko teknis yang perlu dibawa ke tahap berikutnya

1. API tanpa versi, perubahan DOM/schema, slug berganti, dan fallback UUID-ke-URL perlu quarantine/reconciliation, bukan membuat event baru secara otomatis.
2. Publikasi, creation, modification tidak identik; backdated/imported articles dapat muncul belakangan. Waktu 00.00 mungkin waktu yang ditetapkan editor. Jangan menganggapnya bukti first-seen atau waktu kirim.
3. Offset pagination dapat bergeser saat artikel baru masuk; dedup UUID membantu duplikat tetapi tidak menjamin snapshot lengkap. Snapshot terbatas tidak membuktikan artikel dihapus. Baseline hanya halaman terbaru tidak boleh mengizinkan arsip lebih tua yang kemudian muncul sebagai baru.
4. Kategori tidak konsisten penulisan (`Sains-dan-teknologi` vs `Sains Dan-teknologi`, `Pendidikan-tinggi` vs `Pendidikan Tinggi`). Simpan nilai sumber; normalisasi label untuk classifier terpisah. Kategori Umum memuat LNLM, beasiswa, jurnal dan reviewer; jangan dikecualikan.
5. Deadline dan nomor surat sering hanya ada pada PDF. Tahap ini tidak mengekstrak PDF/OCR, tidak memvalidasi tautan mati, atau mengembangkan shortlink. Null/daftar kosong berarti tidak terdeteksi pada HTML, bukan bukti informasi tidak ada.
6. Artikel dapat memiliki base64 images besar (satu respons sekitar 2 MB), PDF/link sementara dan rich-text aneh. Parser menormalkan teks, tidak mengeksekusi HTML/script. Kebijakan streaming size cap/cache conditional ETag perlu dirancang sebelum polling rutin.
7. Fingerprint mencakup teks/judul/kategori/lampiran/link; perubahan label kategori, query URL, atau urutan link bisa menghasilkan perubahan tanpa makna. Hash sama tidak mendeteksi isi PDF yang diganti pada URL sama. Perubahan harus ditinjau, bukan langsung dikirim ulang.
8. State backend dan sender existing mempunyai nama serta schema YouTube/BIMA. Reuse tanpa wrapper khusus dapat menulis branch salah atau salah mengklasifikasikan source. Tidak menghubungkan modul baru ke komponen tersebut pada tahap ini.

## Rekomendasi tahap kedua — belum diimplementasikan

1. **State terpisah.** Branch misalnya `kemdiktisaintek-experiment-state`, path `.bot-state/kemdiktisaintek_experiment.json`, validator khusus, source tetap, schema version, activation fence, baseline marker/hash, observations, outbox. Wrapper menolak master/main, `dikti-production-state`, `youtube-state`, dan semua path cache produksi. Gunakan expected blob SHA CAS, concurrency khusus dan konflik menghentikan kirim.
2. **Baseline awal.** Jalankan collect-only sehat tanpa kirim, simpan UUID snapshot terbatas + waktu UTC + metadata cakupan, lalu tinjau baseline. Tidak perlu mengunduh 928 arsip. Artikel tidak dikenal tetapi publikasi sebelum/sama fence, timestamp hilang/konflik, atau datang dari halaman lebih tua menjadi HOLD; jangan menganggap UUID yang absen dari baseline terbatas otomatis baru. Startup/restart dengan state hilang harus berhenti, tidak membaseline ulang diam-diam.
3. **Deteksi baru.** Poll API tanpa filter kategori/search, jendela halaman kecil dengan overlap, UUID dedup, detail untuk kandidat baru, first_seen terpisah, publication fence dan TTL eksplisit. Hanya capture sehat lengkap memperbarui state; kegagalan satu halaman/detail tidak menghasilkan NO_NEW_ITEMS yang palsu. Gap/overflow pagination memerlukan review/catch-up terkontrol.
4. **Deteksi perubahan.** Simpan fingerprint metadata/isi/lampiran, updated_at detail sebagai petunjuk. Re-fetch beberapa artikel terbaru secara bergilir dengan budget, bedakan initial event dari revision event, tampilkan diff, debounce, review untuk koreksi deadline/dokumen. Tidak mengirim ulang sekadar timestamp atau markup berubah.
5. **Klasifikasi relevansi.** Gunakan judul+isi+deadline+nomor surat+link dan kategori sebagai satu fitur. Prioritas penelitian/pengabdian, hibah/BIMA, dosen/reviewer, beasiswa, jabatan akademik, jurnal/akreditasi. Umum tetap dinilai. Hasil `relevant / not_relevant / needs_review` dengan alasan dan versi kebijakan; awalnya rules + operator review, bukan auto-send atas keyword tunggal.
6. **Pesan Telegram.** Tanda sumber `Pengumuman Kemdiktisaintek`, judul, kategori, tanggal WIB, ringkasan pendek berbasis isi, tenggat hanya jika bukti jelas, link artikel dan daftar lampiran. Escape HTML, batasi panjang UTF-16, jangan gabungkan lampiran Drive folder sebagai PDF. Gunakan sumber asli, jangan menyebut tanggal program sebagai deadline tanpa konteks. Download/sendDocument baru diputuskan terpisah.
7. **Telegram eksperimen yang sudah tersedia.** Gunakan bot/channel eksperimen existing melalui mapping eksplisit secret `YOUTUBE_EXPERIMENT_*` ke konfigurasi runner artikel baru, tanpa fallback `TELEGRAM_*` produksi. Tidak mengubah secret/Variables pada tahap ini. Jangan memakai `Notifier()` default yang memuat config produksi. Jika membuat helper preflight generik, pertahankan kontrak lama dan tambah regression tests semua caller.
8. **Workflow eksperimen.** Workflow baru manual `workflow_dispatch`, checkout exact reviewed SHA, credential persistence off, contents read untuk collect-only, artifact JSON/checksum/run identity. Jalur state write/pengiriman dipisah dan baru diaktifkan setelah persetujuan tahap kedua; concurrency sendiri. Karena eksperimen di-ignore, penambahan terpilih ke Git harus dilakukan eksplisit saat siap ditinjau, bukan `git add .` atau menghapus ignore global.
9. **Preflight/kill switch.** Periksa hash allowlist destination, getMe/getChat/getChatMember, izin posting, ketidaksamaan ID dengan channel produksi, branch/path state exact, activation/policy/baseline hash. Flag enable sumber DAN enable kirim harus exact `true`, default false; failed preflight melarang planning/sending. Klaim sending dan payload hash disimpan CAS sebelum POST. Receipt wajib message_id+ok+HTTP200; timeout/ambigu tidak diulang otomatis. Awali maksimal satu event, batas pesan/attachment, dan audit tanpa secret.
10. **Dedup dengan BIMA.** UUID tetap identitas internal sumber. Tambahkan registry cross-source terpisah yang mereferensikan BIMA read-only: nomor surat ternormalisasi + judul/tanggal/dokumen, URL dokumen stabil, dan alasan confidence. Nomor surat saja tidak cukup (kode BIMA sendiri menolak asumsi itu). Exact match dapat menggabungkan atribusi; fuzzy match hanya HOLD/review. Jangan memasukkan item ke cache/journal BIMA, jangan memanggil `item_key` existing untuk sumber baru, dan jangan menghapus riwayat BIMA.

Belum ada workflow baru, state eksperimen persisten, baseline aktif, klasifikasi kirim, preflight live Telegram, sender, deployment, commit/push/PR atau merge. Semua itu tetap di luar tahap ini.
