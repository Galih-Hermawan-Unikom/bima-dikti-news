# Kemdiktisaintek Tahap 3 — Telegram eksperimen dan durable delivery

Catatan Tahap 4A: keterangan belum commit/push di dokumen ini mendeskripsikan akhir Tahap 3. Tahap 4A mengotorisasi checkpoint dan push **branch fitur saja**, setelah audit serta regression ulang; hasil publikasi dilaporkan terpisah. Live preflight berkredensial, initialization remote, Telegram POST, perubahan default branch dan produksi tetap tidak dilakukan.

Status: implementasi dan simulasi offline; **belum ada live preflight, live Telegram POST, initialization/penulisan remote, push, PR, merge, atau deployment**. Source, cache, journal dan workflow produksi BIMA/YouTube tidak diubah.

Checkpoint Tahap 2 dibuat setelah audit sebelas staged files dan regression ulang: `e58cedf` pada `feat/kemdiktisaintek-monitor-stage2`. Hasil checkpoint: **1.015 passed, 26 subtests passed**, 41.04 detik. Tahap 3 berada pada `feat/kemdiktisaintek-telegram-stage3` dalam worktree pengembangan yang sama. Checkout asli proyek dipertahankan.

## Audit dan keputusan reuse

| Komponen existing | Temuan aktual / keputusan |
| --- | --- |
| `youtube_experiment_preflight.py` | Reusable, tidak spesifik video. Memverifikasi hash allowlist, getChat channel, getMe bot, getChatMember posting permission dan perbedaan dengan channel `@dikti_news`. Dipanggil dengan mapping eksplisit tiga credential eksperimen dan injected GET. Adapter menambahkan kesesuaian getMe.id dengan bot ID pada token. Tidak dipanggil live. |
| `youtube_experiment_pipeline.py` | Mengunci fence, inventory 60, channel YouTube, policy tabs, state youtube-state, app_config dan runner. Tidak digunakan sebagai runner artikel. Pola fail-closed, flags exact true dan pemeriksaan state dipertahankan. |
| `youtube_delivery.py` | Reservation/claim/checkpoint/ambiguity merupakan pola yang sesuai, tetapi store/ticket dan validator memakai video_id, tab evidence, YouTube policy dan format payload khusus. Tidak dipakai sebagai object delivery artikel. |
| `youtube_state_backend.py` | Contents GET/PUT dan expected blob SHA dapat dijadikan pola. Namun load mengharuskan schema_version=1, default validator YouTube dan dependensi/environment khusus. Tidak direfactor untuk source baru; adapter kecil terisolasi memakai schema v2 dan scope tetap. |
| `notifier.py` | Constructor default dapat membaca konfigurasi produksi; source selain bima/youtube dipetakan ke BIMA. Reserved sender mengharuskan DeliveryTicket/video ID; generic formatter memiliki kontrak BIMA dan pengiriman dokumen. Tidak diinstansiasi. Formatter teks artikel dan satu POST adapter ditambahkan; mekanisme preflight umum tetap reuse, tanpa refactor produksi. |
| `production_preflight.py` | Sengaja membaca token/tujuan produksi. Tidak diimpor atau dipanggil. Pemisahan kanal produksi dilakukan melalui helper eksperimen dengan token eksperimen, bukan membaca credential produksi. |
| `production_runtime.py` | Memakai dikti-production-state, validators BIMA/YouTube, activation/inventory dan claim-before-POST. Hanya dibaca sebagai kontrak keselamatan; tidak digunakan. |
| Workflow/tests YouTube | youtube-observation-only sekarang dapat mengirim eksperimen dan bukan collector murni. Tests preflight, pipeline, persistent delivery, runtime, notifier dan state backend menjadi acuan serta tetap dijalankan seluruhnya dalam regression. Tidak ada dispatch workflow existing. |

## Arsitektur dan berkas

```text
API/HTML parser Tahap 1
  -> capture_window + apply_poll Tahap 2
  -> classifier + fresh/pinned BIMA snapshot
  -> fresh article admission (bukan izin POST)
  -> switch + experiment preflight + current state/eligibility recheck
  -> durable RESERVED checkpoint (blob SHA CAS)
  -> durable SENDING claim checkpoint (blob SHA CAS)
  -> switch/proof/claim/eligibility recheck
  -> satu Telegram sendMessage
  -> DELIVERED receipt checkpoint / AMBIGUOUS / operator reconciliation
```

Berkas baru dalam `eksperimen/kemdiktisaintek/`:

* `kemdiktisaintek_delivery_state.py`: schema v2, explicit migration, immutable admission/outbox lineage.
* `kemdiktisaintek_delivery_policy.py`: eligibility, distinct timing budgets, validated BIMA reference.
* `kemdiktisaintek_remote_state.py`: scoped Contents CAS adapter + offline MemoryRemote.
* `kemdiktisaintek_telegram.py`: explicit experiment credentials, reused preflight adapter, formatter, guarded one-POST gateway.
* `kemdiktisaintek_delivery.py`: observation/admission checkpoint, reservation, claim, receipt, recovery.
* `kemdiktisaintek_stage3_runtime.py`: lima mode manual, default-off dan operator approval gates.
* `stage3_fixtures.py`: transport API/GitHub/Telegram sintetis, deterministic clock; tidak ada jaringan.
* `simulate_stage3.py`, `stage3_simulation.json`: demonstrasi A–H dan preview pesan.
* `test_kemdiktisaintek_delivery.py`: failure-injection tests offline.
* `README_STAGE3.md`: dokumentasi ini.

Perubahan kecil `kemdiktisaintek_monitor.py`: parameter optional `max_poll_gap`, default tetap 30 menit untuk Tahap 2. Tahap 3 mengirim policy gap yang terpisah; kode parser/classifier/dedup tidak diganti. README utama ditautkan ke tahap ini.

Satu berkas baru di luar eksperimen: `.github/workflows/kemdiktisaintek-experiment.yml`, sesuai permintaan workflow khusus. Tidak ada perubahan terhadap workflow produksi atau eksperimen YouTube. Output tes/XML, integrity manifests dan capture lokal tidak di-stage. Tidak ada dependensi baru.

## Schema v2 dan migrasi eksplisit

Schema baru memakai envelope, sehingga state Tahap 2 tetap tervalidasi tanpa perubahan tersembunyi:

```text
schema_version: 2
source: kemdiktisaintek
policy_version: kemdiktisaintek-delivery-v1
generation: naik satu per checkpoint
observation_state: seluruh state schema v1 Tahap 2, termasuk baseline/fence/observations/revisions
migration:
  from_schema: 1, at, source_sha256, legacy_ids[]
admissions:
  event_key: uuid, event_type INITIAL, observed_at, expires_at,
             item_sha256, baseline_sha256, reference_sha256, decision ELIGIBLE
outbox:
  event_key: source_uuid, event_type INITIAL atau TEST,
             status RESERVED/SENDING/DELIVERED/AMBIGUOUS,
             payload, payload_hash, destination identity,
             reserved_at, attempt_id, sending_claimed_at,
             delivered_at, message_id, receipt, error_classification
test_authorizations:
  key: scope ONE_EXPERIMENT_MESSAGE, approved_at, expires_at, destination
```

`migrate_v1` ialah fungsi murni eksplisit. State lama tidak dimutasi; UUID yang sudah ada dimasukkan ke legacy_ids dan tidak pernah di-admit sebagai initial event baru. Admissions/outbox baru kosong. Migrasi remote tersedia hanya lewat `RemoteState.migrate(expected=..., approved=True, at=...)`; tidak dijalankan otomatis oleh load/CLI. Simpan backup state v1 dan SHA sebelum migrasi yang kelak diotorisasi. Migrasi tidak memindahkan file atau menulis state YouTube/BIMA.

Nested state v1 tetap memakai validator/lineage Tahap 2: activation fence, baseline checksum, inventory, first_seen, riwayat URL, observasi dan revisi dipertahankan. Envelope menambah aturan: admission immutable, outbox tidak dihapus, payload/destination/reservation immutable, attempt immutable setelah claim, terminal states tidak dapat di-reset. Outbox baru wajib mulai dari RESERVED. Revision key berbeda dari initial tetapi revision autosend ditolak oleh schema/reservation; revisi hanya NEEDS_REVIEW.

Initial key: `kemdiktisaintek:{uuid}:initial`. Revision key yang disiapkan: `kemdiktisaintek:{uuid}:revision:{revision_hash}`. Test key: `kemdiktisaintek:test:{stable_test_id}`; tidak memakai artikel/UUID historis sebagai berita. Satu state hanya boleh mempunyai satu test event awal, termasuk jika operator mencoba mengganti nonce. Penambahan test berikutnya memerlukan rancangan/review terpisah, bukan menghapus outbox.

## Credentials dan preflight

Hanya tiga key Telegram berikut dibaca oleh adapter, tanpa fallback `.env`, app_config atau `TELEGRAM_*`:

```text
YOUTUBE_EXPERIMENT_BOT_TOKEN
YOUTUBE_EXPERIMENT_CHAT_ID
YOUTUBE_EXPERIMENT_CHAT_ID_SHA256
```

Token dan ID tidak dicetak, tidak disimpan dalam payload/outbox, dan dikecualikan dari repr Credentials. Destination identity pada state memakai SHA256 chat, bot ID terverifikasi, dan fingerprint credential untuk mencegah reserved event berpindah bot. Laporan PREFLIGHT hanya mengeluarkan alias tujuan, bukan token/hash token. Exception request tidak dirender karena URL Telegram mengandung token; error yang disimpan berupa kategori tetap.

Preflight memeriksa identity bot, identity channel, posting rights, SHA256 allowlist, pemisahan dari kanal produksi. Proof terikat credential/destination dan berlaku maksimal lima menit. Runtime memvalidasi remote state/baseline terlebih dahulu. Read-only preflight membuktikan akses baca state; kemampuan checkpoint tulis dibuktikan oleh ACK reservation dan ACK claim sebelum POST, bukan diasumsikan dari permission token.

Kedua flag wajib tepat string `true` sebelum reservation dan sekali lagi sebelum POST:

```text
KEMDIKTISAINTEK_EXPERIMENT_ENABLED
KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND
```

Missing, malformed, `TRUE`, boolean true, atau salah satu OFF memblokir kirim. Perubahan switch setelah reservation/claim juga memblokir POST. Preflight read-only punya approval `READ_ONLY_PREFLIGHT`; tidak dijalankan live selama pengerjaan ini.

## Eligibility dan timing

NEW_CANDIDATE dan admission artikel bukan izin kirim. Reservation memeriksa ulang UUID/detail/hash, publication provenance detail_datetime, publikasi setelah fence, inventory/legacy exclusion, first_seen, classification, capture sehat, identity hold, fresh pinned BIMA reference, admission timestamp/expiry, snapshot hash dan baseline hash. Setelah reservation, claim dan sebelum POST semua bukti diperiksa lagi. KNOWN tidak dipromosikan, initial_decision lama tidak dibaca sebagai izin, dan admission tidak diciptakan ulang hanya karena restart/kode baru.

| Parameter | Nilai Tahap 3 | Fungsi |
| --- | --- | --- |
| Interval rencana | 30 menit | Parameter desain; belum ada cron. |
| Toleransi keterlambatan | 20 menit | Gap aktual antarcapture sampai 50 menit dapat diterima bila overlap sehat. |
| Freshness evidence / admission | 10 menit | Detail/capture/admission harus segar saat reserve dan claim; tidak ikut diperbesar menjadi 50 menit. |
| Preflight proof | 5 menit | Cek bot/tujuan/izin baru sebelum pengiriman. |
| Publication window | 24 jam | Publikasi historis, sebelum/sama fence atau masa depan ditahan. |
| First-seen TTL | 24 jam | Tidak diperpanjang oleh restart/last_seen. |
| BIMA reference freshness | 60 menit | Batas snapshot read-only yang di-review; tidak menerima arsip lama/empty. |
| Overlap | wajib tetap ada | Hilang overlap/duplikat pergeseran/gap >50 menit memerlukan HOLD/reconciliation. |

Toleransi 20 menit menampung keterlambatan kecil terhadap interval rencana 30 menit tanpa memperluas umur artikel atau bukti POST. Ini parameter eksperimen yang perlu ditinjau dari data run nyata, bukan jaminan scheduler. Tests mengukur timestamp capture aktual: gap 31/45 menit dapat lolos, gap 51 menit HOLD. Restart dengan evidence tua tetap ditolak. [GitHub menyatakan scheduled workflows dapat terlambat atau terbuang saat beban tinggi](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule); waktu nominal cron tidak dipakai sebagai bukti capture.

Admission hanya dibuat dari keputusan NEW_CANDIDATE pada capture sekarang, detail harus diperiksa pada capture tersebut dan semua policy checks harus lolos. Masa admission sepuluh menit sengaja konservatif. Setelah poll berikutnya menjadi KNOWN, reservation baru ditolak; existing terminal outbox tetap idempoten. Liveness pengiriman yang tertunda memerlukan review operator, bukan auto-backfill.

## BIMA reference yang dapat dipercaya

Cache historis 90 item Tahap 1/2 tidak otomatis digunakan. Reference wajib envelope berikut, hash canonical-nya dipin di luar file oleh operator/reviewed input:

```text
schema_version: 1, source: bima
source_url: https://bima.kemdiktisaintek.go.id/pengumuman
scope: unfiltered_recent, healthy: true, complete_window: true
captured_at, coverage_from, coverage_until (= captured_at), items[] nonempty
```

Reference harus berumur <=60 menit, tidak dari masa depan, mencakup awal publication window 24 jam dan tervalidasi hash eksternal. Kosong, stale, cakupan tidak lengkap, atau hash tak sesuai tidak pernah menghasilkan NO_MATCH. Observation menggunakan NEEDS_REVIEW; reservation diblokir. Matcher multi-evidence Tahap 2 tetap dipakai, tanpa write ke journal/cache/outbox BIMA.

Refresh yang direkomendasikan: operator/collector read-only terpisah menangkap jendela BIMA terbaru tanpa filter, memverifikasi kelengkapan dan source, membungkus metadata cakupan, lalu mem-pin SHA hasil review. Snapshot dapat diberikan lewat path eksplisit dan SHA input workflow. Nama/flag dalam file bukan bukti origin; hash yang dihitung sendiri dari file tanpa review eksternal tidak memberikan kepercayaan. Pengambilan/penulisan BIMA baru atau workflow refresh otomatis tidak dijalankan/ditambahkan di sini. Snapshot pada commit lama yang sudah melewati freshness harus ditolak, bukan mengubah captured_at.

## Remote CAS dan recovery

Scope adapter dikunci pada repository `Galih-Hermawan-Unikom/bima-dikti-news`, branch `kemdiktisaintek-experiment-state`, path `.bot-state/kemdiktisaintek_experiment.json`. Tidak ada endpoint untuk membuat branch, menulis ref produksi, path arbitrary, atau fallback state lokal. Session HTTP tidak memakai implicit netrc/proxy environment; redirect ditolak. GET/PUT memakai timeout terbatas dan tidak diulang otomatis.

Contents response wajib path/encoding benar, JSON valid <=900 KB dan blob SHA cocok dengan isi. PUT membawa expected blob SHA. 409/422 menghentikan operasi. ACK PUT wajib mengembalikan SHA isi baru yang diharapkan. Ini mengikuti [kontrak GitHub Contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents), dengan validator khusus source dan schema.

INITIALIZE baru boleh dipanggil kelak setelah approval terpisah: branch sudah disiapkan operator, pinned commit SHA, tree kosong, **root/orphan commit tanpa parent**, state file belum ada, serta baseline generation 1 tanpa admissions/outbox. Tree kosong akibat penghapusan state pada commit berikutnya tidak boleh reinitialize. Migrasi v1 existing memakai jalur eksplisit CAS, bukan create. State hilang/rusak menghentikan proses dan memerlukan restore/reconciliation.

| Kejadian | Hasil |
| --- | --- |
| Reservation CAS gagal | Tidak ada POST. |
| Claim CAS gagal atau ACK claim hilang | Tidak ada POST; jika SENDING ternyata tersimpan, restart menjadi AMBIGUOUS. |
| Restart pada RESERVED | Boleh melanjutkan hanya setelah seluruh eligibility/proof/TTL diperiksa ulang. |
| Restart pada SENDING | AMBIGUOUS, tanpa resend; mungkin POST belum terjadi atau sudah berhasil. |
| Telegram HTTP error, ok=false, invalid message_id/chat, timeout | AMBIGUOUS; tidak ada automatic retry. |
| Telegram berhasil, receipt CAS gagal | ReconciliationRequired dengan receipt sanitasi; bukan sukses. State mungkin SENDING atau DELIVERED bila ACK checkpoint hilang. |
| Restart pada DELIVERED/AMBIGUOUS | Tidak pernah POST ulang. |

HTTP 200 saja tidak cukup. DELIVERED memerlukan `ok=true`, message_id integer positif, chat ID receipt sama dengan tujuan terverifikasi, dan checkpoint receipt yang terkonfirmasi. Jaminannya **at-most-one attempted POST per event**, bukan exactly-once arrival. Ambiguitas dapat mengorbankan liveness demi mencegah duplikasi. Kehilangan atau rollback state di luar CAS tetap memerlukan operator; jangan menghapus outbox agar dapat mencoba lagi.

## Formatter

Pesan satu teks, tanpa download/upload PDF. Bagian: PENGUMUMAN KEMDIKTISAINTEK, judul, publikasi WIB, kategori sumber, cuplikan faktual body, deadline hanya dari evidence eksplisit parser, dokumen berlabel PDF/Google Drive/dokumen, tautan eksternal terpisah, sumber detail. Nomor surat tidak dibuat jika tidak tersedia. Tanggal kegiatan tidak dijadikan deadline.

HTML di-escape, URL dinormalisasi, teks dipotong deterministik, batas konservatif **3.900 UTF-16 code units termasuk markup/entities**. Maksimal tiga link dokumen yang muat; sumber tetap ada. Mandatory data yang terlalu panjang/invalid menggagalkan reservation. Ringkasan berupa cuplikan teks, bukan generasi spekulatif. Preview yang dapat ditinjau terdapat di `stage3_simulation.json`.

## Mode workflow dan gate operator

Workflow baru manual `workflow_dispatch` saja, default COLLECT, tanpa schedule. Penguatan audit 4A sebelum checkpoint: job review wajib memvalidasi full reviewed_code_sha sama dengan github.sha pada repository/event yang benar, sebelum checkout atau operasi apa pun. Semua checkout memakai reviewed_code_sha, persist-credentials=false, concurrency khusus Kemdiktisaintek dan cancel-in-progress=false. Collect dan PREFLIGHT mempunyai contents read; job state INITIALIZE/POLL/SEND_TEST contents write. PREFLIGHT dipisahkan dari job write. Environment khusus harus diproteksi operator sebelum dipakai. Secrets eksperimen hanya diberikan pada PREFLIGHT atau step SEND_TEST; POLL/INITIALIZE tidak menerima credential Telegram.

| Mode | Kemampuan / gate |
| --- | --- |
| COLLECT | GET situs, tanpa state remote/Telegram. |
| PREFLIGHT | Remote state load + Telegram GET; memerlukan confirmation READ_ONLY_PREFLIGHT. Tidak POST. |
| INITIALIZE | Capture baseline + create state pada reviewed empty orphan branch; confirmation INITIALIZE_REVIEWED_EMPTY_BRANCH + pinned empty_branch_sha. Tidak membuat branch atau POST Telegram. |
| POLL | Remote observation/admission checkpoint; reference BIMA eksplisit atau NEEDS_REVIEW. Tidak mengirim Telegram. |
| SEND_TEST | Hanya pesan operasional berlabel PENGUJIAN, satu event awal. Kedua switches true + confirmation SEND_ONE_EXPERIMENT_TEST + reviewed_code_sha sama dengan full GITHUB_SHA + GitHub Actions manual pada repository yang benar + preflight + durable claim. |

Engine initial-announcement delivery diimplementasikan dan diuji offline; workflow live Tahap 3 sengaja hanya mengekspos POST untuk SEND_TEST operasional. POLL tidak otomatis mengirim initial event walaupun flags ON. Tidak ada revision autosend atau auto-backfill. Mengaktifkan pengiriman berita rutin merupakan perubahan scope/operator approval berikutnya.

[Workflow dispatch memerlukan workflow terdaftar pada default branch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch). Feature branch lokal ini belum dipush; jangan menganggap UI Actions bisa langsung menjalankannya, dan `--ref` saja bukan pengganti pendaftaran default branch. Publikasi workflow/default-branch change memerlukan persetujuan terpisah. Tidak ada merge dilakukan untuk mengaktifkannya.

## Pengujian dan simulasi

```powershell
# Offline: seluruh jaringan nyata diblokir fixtures pengujian.
python -m pytest tests eksperimen/kemdiktisaintek/test_kemdiktisaintek_parser.py eksperimen/kemdiktisaintek/test_kemdiktisaintek_monitor.py eksperimen/kemdiktisaintek/test_kemdiktisaintek_delivery.py -q

# Offline: source API/HTML fixture -> parser -> monitor -> policy -> CAS mock -> Telegram mock.
python eksperimen/kemdiktisaintek/simulate_stage3.py
```

Simulasi memakai **RemoteState adapter yang sama** dengan mock GitHub HTTP, bukan melewati checkpoint hanya dengan object in-memory. Data, UUID, tanggal, token dan response seluruhnya sintetis dan ditandai sebagai fixture. Tidak ada kredensial nyata yang dibaca atau HTTP live yang dipanggil.

| Kasus | Hasil |
| --- | --- |
| A. Baru relevan | DELIVERED sekali, receipt tervalidasi, SENDING tersimpan sebelum mock POST. |
| B. Artikel sama poll berikutnya | Tidak ada POST tambahan. |
| C. Historis | HOLD, nol POST. |
| D. Nonakademik | REJECTED, nol POST. |
| E. Revisi | NEEDS_REVIEW, nol POST. |
| F. Telegram timeout | AMBIGUOUS, satu attempt, nol retry. |
| G. Claim CAS conflict | BLOCKED, nol POST. |
| H. Destination salah | BLOCKED saat preflight, nol POST. |

Hasil final: **1.105 passed, 26 subtests passed**, 99.66 detik, return code 0. Rincian: **829 regression BIMA/YouTube + 83 parser + 103 monitor + 90 delivery Tahap 3**. Ekspor Git index ke checkout temporer yang bersih juga melewatkan **276 tes eksperimen**, 18.67 detik; ini membuktikan source, workflow dan fixtures yang diperlukan tersedia tanpa artefak lokal yang di-ignore.

Hash **114 file terlindungi** (99 pada worktree pengembangan dan 15 pada checkout asli) tidak berubah. Satu-satunya perubahan di luar folder eksperimen adalah workflow Kemdiktisaintek baru. Sebanyak **14 berkas Tahap 3 di-stage secara eksplisit**, belum di-commit; satu commit yang dibuat pada tahap ini hanyalah checkpoint Tahap 2 `e58cedf`.

Output full regression berada pada `stage3_test_results.log`, XML dan `stage3_test_summary.json`; bukti ekspor `stage3_index_review.json` dan pemeriksaan hash `stage3_integrity_after.json` tetap lokal, tidak di-commit. Tests mencakup preflight mismatch/missing credential/rights, allowlist, exact switches, UTF-16/escaping, reference empty/stale, current admission, migration/restart, remote scope/blob corruption/redirect/explicit init, CAS di setiap fase, lost ACK, receipt persistence failure, terminal ambiguity, workflow dan simulasi A–H. Tidak ada koneksi kode pengujian ke GitHub/Telegram nyata.

## Langkah operator sebelum satu live SEND_TEST

Audit 4A sebelum checkpoint: **1.112 tes dan 26 subtests lulus** (109.66 detik), termasuk tujuh tes tambahan untuk SHA gate dan pemisahan permission PREFLIGHT. GitHub metadata pada pemeriksaan 4A menyatakan tiga secret eksperimen repository tersedia; nilainya tidak dibaca. Branch state khusus dan environment `kemdiktisaintek-experiment` belum ada, kedua switch Kemdiktisaintek belum disetel sehingga default OFF, dan workflow belum ada pada default branch `master`. Karena state belum ada, live preflight tidak dijalankan. Semua status remote ini merupakan snapshot audit dan wajib diperiksa ulang sebelum operasi yang kelak diotorisasi.

1. Review diff, hasil offline, preview pesan dan SHA implementasi. Perubahan Tahap 3 belum dipush/PR/merge. Publikasi workflow, default-branch registration dan pengujian remote memerlukan otorisasi terpisah.
2. Siapkan GitHub environment `kemdiktisaintek-experiment` dengan reviewer dan pembatasan branch/SHA. Verifikasi tiga secret eksperimen existing tersedia; jangan mengganti nilainya atau memetakan credential produksi. Kedua switches tetap OFF.
3. Secara terpisah otorisasi pembuatan state branch khusus yang kosong/orphan, lalu INITIALIZE dengan pinned SHA; atau migrasi eksplisit v1 yang telah dibackup/ditinjau. Adapter tidak membuat branch. Tinjau baseline/fence/checksum/state v2 yang tersimpan. Tidak ada pengiriman pada langkah ini.
4. Berikan persetujuan eksplisit untuk **live PREFLIGHT GET**. Jalankan mode PREFLIGHT dengan confirmation READ_ONLY_PREFLIGHT, lalu tinjau alias, identitas/izin dan pemisahan produksi. Langkah ini belum dijalankan selama Tahap 3.
5. Setelah hasil implementasi **dan** preflight ditinjau, operator memberikan persetujuan eksplisit untuk maksimal satu pesan PENGUJIAN. Barulah kedua switches diset tepat true, masukkan reviewed_code_sha dan test_id stabil, confirmation SEND_ONE_EXPERIMENT_TEST; runtime mengulang preflight sebelum reserve/claim.
6. Periksa receipt message_id dan remote DELIVERED; konfirmasi manusia bahwa pesan muncul hanya di channel eksperimen. Kembalikan auto-send OFF. Jika AMBIGUOUS atau receipt checkpoint gagal, hentikan dan rekonsiliasi; jangan retry dengan test_id baru, reset event, atau hapus state.

Prosedur ini siap ditinjau, **bukan izin live**. Tidak ada pesan nyata pada pekerjaan ini, dan tidak ada alasan untuk memakai artikel historis sebagai event test berita baru.

## Risiko tersisa

* At-most-one attempt dapat menghasilkan pesan tidak terkirim ketika claim sudah tersimpan lalu proses mati sebelum POST; perlu rekonsiliasi, bukan retry otomatis.
* Validasi snapshot BIMA bergantung pada review provenance/cakupan dan SHA eksternal. Field self-declared bukan autentikasi. Pengumuman BIMA setelah snapshot tetap dapat menimbulkan gap dedup; jangan menafsirkan NO_MATCH sebagai kepastian semantik global.
* Evidence/admission sepuluh menit dan reference pin immutable sengaja menahan pengiriman yang tertunda/berganti snapshot. Re-admission/operator review belum mempunyai UI/CLI umum.
* Policy gap 50 menit perlu evaluasi dari run nyata; overlap hilang tetap HOLD. Tidak ada cron aktif atau bukti SLA scheduler.
* Rich-text/PDF classifier dan dedup memiliki keterbatasan Tahap 2. Hash konten tidak mendeteksi PDF yang diganti pada URL tetap. Revisi tidak dikirim.
* State satu blob <=900 KB; belum ada pruning. Restore/rollback manual yang menghapus outbox dapat merusak jaminan dedup sehingga wajib review backup/receipts.
* Integrasi remote API dan Telegram hanya diuji dengan mock. Belum ada bukti live permission/token/branch/environment/default-branch dispatch/receipt; itu merupakan langkah operator terpisah.
