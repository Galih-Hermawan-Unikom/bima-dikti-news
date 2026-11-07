# Laporan implementasi dan review lifecycle YouTube

## Ruang kerja dan batas operasi

- Repository: `Galih-Hermawan-Unikom/bima-dikti-news`.
- Workspace awal: `D:\PythonKu\_Eksperimen\BIMA_DIKTI_NEWS_MASTER_CURRENT`, branch
  editorial lama, clean. Worktree lain terdaftar dan tidak dimodifikasi.
- Worktree implementasi: `D:\PythonKu\_Eksperimen\BIMA_DIKTI_NEWS_lifecycle`.
- Branch: `fix/youtube-live-replay-lifecycle`, dari hasil fetch `origin/master`
  **`42e5d5f4f22397e141b44792bfc48932209fb1c8`**.
- Tidak ada Telegram produksi, workflow_dispatch, perubahan Variables/Secrets,
  merge, deploy, reset baseline, atau penulisan branch state produksi.

## Diagnosis dan bukti

`youtube_eligibility._classify` lama menolak LIVE dengan
`LIVE_NOT_ALLOWED_REPLAY_ONLY` dan memotong window menggunakan first_seen untuk
semua kategori. `youtube_delivery.reserve` serta notifier hanya menerima event
`:initial`. Jadi penjadwalan beberapa hari sebelumnya bisa menghabiskan window
sebelum fase LIVE/replay, dan satu event awal tidak bisa merepresentasikan dua fase.

Ada persoalan metadata tambahan: parser tab lama mengakui replay dari
`streamed`/`disiarkan` atau replay badge, sedangkan kartu target berisi
`Streaming 16 jam lalu` / `Streaming 20 jam lalu` dan badge durasi `6.04.24`.
Durasi/tab bukan bukti LIVE ataupun actual end. Mengendurkan pemeriksaan string
saja tidak memperbaiki window, deduplikasi, atau risiko replay historis.

Read-only snapshot pada commit state
`aa2ac774f00a362a22c73ec3abea7746f4f12729` mengonfirmasi:

- `first_seen_at=2026-11-02T09:28:56.716699+00:00`, inventory false.
- Capture 102: `2026-11-07T10:22:44.746830Z`; 103:
  `2026-11-07T14:00:09.685342Z`; kedua capture sehat secara individual.
- Metadata title/URL/streams sesuai target, outbox target kosong.
- Gap kedua capture sekitar 3 jam 37 menit, melebihi batas kontinuitas baru 2 jam.

Anonymous watch GET pada 7 November sekitar 16:50 UTC memperoleh playback OK,
public/non-unlisted, isLiveContent true, isLiveNow false, publisher start
`2026-11-06T11:26:57Z`, end `2026-11-06T17:31:37Z`. Field optional
`videoDetails.isLive` tidak ada. Fixture player menyimpan field yang diperlukan
dan tipe format media; tidak menyimpan signed URL atau raw HTML.

Snapshot lama hanya mempertahankan dua capture terbaru. Tidak ada bukti yang
memadai untuk mengklaim metadata UPCOMING/LIVE pada tanggal pertama kali video
terlihat. Riwayat lintas hari dalam regression test adalah sintetis, diberi ID
kasus nyata agar mekanisme bug dapat diuji tanpa memalsukan histori produksi.
Tidak ada hardcoded send/backfill exception untuk ID ini.

## Audit jalur eksekusi

| Komponen | Kontrak yang dipertahankan / perubahan |
|---|---|
| `youtube_channel_tabs.py`, `youtube_videos_parser.py` | Discovery dua tab, exact channel/URL, health dan metadata allowlist. Tidak berubah; `/streams` bukan LIVE evidence. |
| `youtube_tabs_monitor.py` | Legacy classification, dua capture, review/history fingerprint dan initial inventory tetap dipakai. Adapter produksi baru menambahkan publisher lifecycle setelah capture tersimpan. |
| `youtube_eligibility.py` | Tambahan typed evidence origin/fence/phase verification. Window fase baru; regular/experiment default tetap memakai first_seen dan initial event. |
| `youtube_lifecycle.py` | Publisher JSON extraction, explicit phases, monotonic completion, continuity, immutable origin/end evidence, additive migration, quarantine, revalidation. |
| `youtube_delivery.py` | JSON schema 1 kompatibel; ekstensi tervalidasi; marker monotonic seen-on-streams; phase reservations dan payload provenance. Lock, atomic replacement, CAS hook, send lease, receipt dan ambiguity tetap. |
| `youtube_state_backend.py` | GET fixed branch/path, validator, blob SHA expected pada Contents PUT, konflik 409/422 menghentikan sebelum send. Tidak berubah. |
| `production_runtime.py` | Lifecycle adapter; fence kosong sebelum migrasi mempertahankan regular/replay legacy dan tetap menolak LIVE. Pemeriksaan sebelum reserve dan sesudah claim. Reserved lifecycle lintas restart perlu operator. Report interval YouTube disesuaikan dengan cron unified satu jam. |
| `dikti_news_scheduler.py` | Pinned master/approved SHA, recheck setelah queue, independent source switches; tidak berubah. |
| `.github/workflows/dikti-news-production.yml` | Hanya satu environment variable tambahan di job YouTube. Cron, BIMA/Kemdikti jobs, lock group, queue, permissions dan credential gates tidak berubah. |
| `.github/workflows/youtube-production.yml` | Recovery manual mendapat variabel fence yang sama. Tidak ditambah schedule/dispatch baru. |
| `youtube_monitor.py`, `youtube_history.py`, `delivery_state.json`, cache/baseline | RSS/cutover hanya referensi historis untuk penekanan. Tidak ada fallback RSS di discovery aktif; jurnal, baseline, cache tidak diedit. |
| `notifier.py` | Validasi ticket/payload per fase; lifecycle ditolak oleh notifier eksperimen; label LIVE/replay berbeda. Destination allowlist, token checks dan format dua sumber lain tetap. |

## State machine, waktu dan deduplikasi

Lihat [diagram dan spesifikasi operasional](youtube-lifecycle.md).
UPCOMING menyimpan observasi saja. LIVE membutuhkan dua capture LIVE yang sehat
dan player publik yang tetap LIVE sebelum reservation dan sesudah durable claim.
Replay membutuhkan dua capture replay, public playback, dan kronologi awal fase
yang dapat dibuktikan. Publisher end dipisah dari first verified replay; origin
tidak bergerak setelah restart/perubahan metadata. Completion tidak boleh mundur
menjadi LIVE, dan end yang bertentangan menghasilkan HOLD.

Minimum capture 10 menit; maksimum gap 2 jam. Replay ditemukan setelah gap/tanpa
kronologi tetap HOLD walau capture selanjutnya sehat. LIVE setelah gap harus
membentuk dua capture baru yang kontinu dan masih LIVE ketika akan dikirim.
Window per fase 24 jam; bukti reservation maksimal 2 menit. Shorts, konflik,
partial/failed capture, perpindahan tab atau hilang sementara tidak memicu send.

Event `:live` dan `:replay` memiliki histori independen. Outbox `:initial` legacy
menekan kedua fase. Reservation lalu claim harus durable sebelum POST; CAS stale
worker berhenti. Receipt tak terkonfirmasi tidak dikirim ulang. Pending/sending/
ambiguous setelah restart tidak memperoleh izin otomatis dari lease expiry.

## Migrasi offline

State lama tidak dinaikkan/direinitialisasi melalui policy_version baru. Ekstensi
`lifecycle.version=1` ditambahkan pada schema luar 1. Fence
`YOUTUBE_LIFECYCLE_ACTIVATION_AT` default kosong dan tidak diaktifkan dalam PR.
Pada cutover, semua ID yang telah ada ditekan secara permanen, termasuk UPCOMING
lama; hanya identitas baru pasca-cutover yang dapat mengikuti lifecycle otomatis.
Ini menghindari auto-backfill yang tidak dapat ditentukan aman dari dua capture.

Migrasi **seluruh snapshot produksi** dilakukan di temporary directory offline:
78 observasi, capture_seq 103, dua capture, tiga outbox delivered. Pembandingan
seluruh field legacy sebelum/sesudah identik secara semantik; payload JSON/receipt,
first_seen dan inventory dipertahankan. `rPKcKDj0oa8` masuk suppression. Tidak ada
remote PUT atau Telegram. Tes otomatis juga mempertahankan setiap status legacy:
reserved, sending, ambiguous, delivered. Fence berbeda atau provenance rusak
menyebabkan kegagalan tertutup.

## Pengujian

Suite penuh: `python -X utf8 -m pytest -q`, dengan JUnit tersimpan di temporary
directory. Semua tes menggunakan fixture/mock; autouse fixture menolak network
dan akses secret file. Validasi implementasi awal: **1.072 passed, 26 subtests passed, 0 failed,
0 skipped**, 42,76 detik pada Python 3.11/Windows. `git diff --check` bersih.

| Persyaratan | Bukti tes otomatis |
|---|---|
| 1–3 UPCOMING dan beberapa hari sebelum LIVE/replay | `test_upcoming_for_days_then_phase_has_independent_window` |
| 4 fase independen maksimal sekali | `test_upcoming_live_replay_deliver_once_per_phase_across_restarts` |
| 5–6 first detection LIVE/replay tanpa histori | `test_first_detection_without_upcoming_or_live`, replay tanpa end tetap HOLD |
| 7 selesai sebelum reserve/POST | `test_live_ends_between_evaluation_reservation_and_post` |
| 8 dua capture dan interval | capture positif end-to-end, interval terlalu pendek dan gap 8 jam |
| 9 capture partial/failed | `test_missing_partial_and_changed_tabs_break_chronology` serta suite tabs existing |
| 10 kontradiksi | publisher mutations, conflicting card, changed end, replay-regression tests |
| 11 tab berubah/hilang | persistent `seen_on_streams`, moving-to-videos dan missing-video tests |
| 12 polling gap | gap tests LIVE vs replay, quarantine tidak direset oleh capture berikutnya |
| 13 restart tiap fase | harness membuka ulang store pada setiap tick |
| 14 retry ambigu | ambiguous dan reserved restart tests; existing lease-recovery tests |
| 15 worker bersamaan | stale CAS worker lifecycle; concurrency/outbox/backend suites existing |
| 16 state delivered lama | per-field migration test, empat status legacy, full-state offline rehearsal |
| 17 historis tanpa backfill | suppression/cutover test, original production fixture, history suite existing |
| 18 Shorts | unit card conflict + end-to-end Shorts, original category/parser tests |
| 19 SHA/provenance/credentials | scheduler/preflight suites existing, lifecycle gate mutations dan notifier gate matrix |
| 20 PIMNAS regression | original state/player fixtures dan synthetic multi-day transitions |

Kegagalan selama iterasi telah digunakan untuk memperbaiki kontinuitas setelah
video hilang, workflow hash guard, dan field optional player. Satu intermittent
Windows file replacement/Access Denied muncul ketika tes membuat ratusan checkpoint
beruntun; riwayat multi-hari kini direpresentasikan dengan capture relevan sebelum/
sesudah jeda UPCOMING dan kontinuitas menjelang transisi. Tidak ada retry filesystem
atau pelemahan durability produksi untuk mengatasi kondisi pengujian tersebut.

## Risiko dan keputusan aktivasi

Watch player bukan API stabil, respons regional dapat berbeda, parser dapat HOLD
ketika field berubah/hilang, dan tidak ada fixture publisher LIVE produksi saat
siaran berlangsung yang direkam dalam pekerjaan ini. Assertion playback bukan
download/pemutaran seluruh media. Race kecil antara pemeriksaan terakhir dan POST
tidak dapat dihilangkan lintas layanan; at-most-once dapat menghasilkan nol pesan.
Request/checkpoint tambahan per stream dapat menambah durasi siklus; tidak ada
pagination atau retry tambahan. Histori yang ditekan saat cutover tidak otomatis
dipulihkan, termasuk UPCOMING lama.

**NO-GO untuk aktivasi produksi** sampai operator memvalidasi fixture LIVE
representatif, meninjau CI/PR, dan menyetujui fence. PR dibuat untuk review tanpa
merge/deployment. Implementasi default-off dapat dinilai terpisah dari persetujuan
aktivasi. Tidak ada klaim Telegram production success dari tes offline ini.

## Review lanjutan: kompatibilitas feature OFF (8 November 2026)

Blocker merge karena perilaku OFF telah diperbaiki: sebelum state pernah
bermigrasi, fence kosong langsung memakai keputusan dan preview adapter legacy.
Regular dan replay yang lolos policy lama tetap memakai `:initial`; LIVE tidak
diaktifkan. Tidak ada request player, migrasi, atau checkpoint lifecycle tambahan.
Window first-seen, TTL, inventory, history, Shorts, reservation, CAS dan outbox
legacy tetap berlaku. Replay kedaluwarsa, termasuk kasus PIMNAS, tidak dibuka.
Jika state sudah memiliki ekstensi lifecycle, OFF tetap menahan stream; rollback
tidak boleh menciptakan initial event setelah histori `:live` / `:replay`.

Audit tiga risiko menghasilkan perbaikan terbatas berikut:

- Metadata: format media wajib berupa list berisi object nonempty; perubahan
  tipe seperti string, dict, null entry atau object kosong menghasilkan UNKNOWN.
  Tidak ada fallback dari player rusak menjadi bukti LIVE.
- Biaya/state: stream hilang, startup inventory, replay yang permanen tidak
  admitted, serta histori cutover tidak membutuhkan GET/per-video checkpoint
  berulang. Tes 100 stream historis membatasi pekerjaan ke satu checkpoint source
  capture dan mempertahankan seluruh observasi. Record menyimpan latest evidence
  dan origin tetap, bukan daftar tak terbatas setiap poll. Limit state 900.000 byte
  tetap fail-closed; histori/outbox tidak dipangkas.
- Actions terlambat: record yang melewati capture hilang tidak diperlakukan
  seperti first detection baru ketika replay muncul kembali. Origin tidak
  digeser; continuity, freshness, CAS, dan aturan tanpa retry ambigu dipertahankan.
  Dengan OFF, temporal policy legacy berlaku persis seperti sebelum fitur ini.

Migrasi offline diulang terhadap snapshot state
`07b61cbe125210882e162a7e83720936d212125e`: **47.238 byte, 78 observasi,
capture_seq 107, tiga outbox delivered**. Seluruh field legacy, termasuk outbox,
receipt, inventory dan first-seen, identik sebelum/sesudah ekstensi ditambahkan.
PIMNAS masuk suppression; tidak ada remote PUT atau Telegram.

Keputusan review: **GO untuk merge dengan lifecycle tetap OFF setelah CI PASS**.
Status NO-GO di bagian sebelumnya berlaku untuk aktivasi lifecycle, bukan merge
kode dengan OFF yang kompatibel. Risiko parser watch, biaya banyak stream baru
yang belum dikenal, fixture LIVE produksi yang belum tersedia, dan race terakhir
YouTube/Telegram tetap harus ditinjau sebelum aktivasi terpisah. Tidak ada Variables,
Secrets, baseline atau state produksi yang diubah dalam review ini.

Validasi lanjutan: **1.087 passed, 26 subtests passed, 0 failed, 0 skipped**
(Python 3.11/Windows, 190,18 detik); `git diff --check` bersih. Termasuk parity
keputusan/preview dengan adapter legacy pada OFF, mock delivery `:initial` satu
kali lintas restart, inventory/history/window fences, rollback pasca-aktivasi,
format player rusak dan biaya checkpoint 100 stream historis. CI pada head PR
menjadi syarat terakhir sebelum merge; tidak ada merge atau aktivasi dalam tugas ini.
