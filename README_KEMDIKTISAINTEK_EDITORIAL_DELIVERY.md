# Controlled Editorial Delivery Kemdiktisaintek

Fasilitas ini memulihkan satu artikel yang sudah tertahan tanpa mengubah keputusan monitoring otomatis. Jalur ini hanya mengizinkan dua UUID yang ditinjau, satu UUID per dispatch, dan tidak mengubah `HOLD`, baseline, activation fence, first-seen, observations, admissions, atau outbox otomatis.

## Sasaran yang ditinjau

| UUID | Artikel | Prioritas |
|---|---|---|
| `2407f6f4-1e06-452b-a053-864426e9a183` | Tawaran Mengikuti Program Bridging Course Tahun 2026 | Tinggi |
| `b7abee5c-39d8-48fa-a2ac-93fe07ec7ed2` | Kemdiktisaintek Perkuat Kesiapan Dosen dan Tendik untuk Studi Doktor di Luar Negeri | Normal |

UUID dan URL kanonis dicocokkan ke allowlist, API resmi, detail artikel, dan state produksi. Bridging wajib memiliki bukti tenggat eksplisit. Artikel doktor tidak diberi tenggat yang tidak ditemukan.

## Alur operator

Workflow `.github/workflows/kemdiktisaintek-editorial-delivery.yml` hanya memiliki `workflow_dispatch`; tidak ada cron.

1. **PREVIEW** (default) membaca API/detail, state produksi, dan ledger editorial. Output memuat teks formatter produksi, PDF yang ditemukan, hash isi/payload, dan status sebelumnya. Tidak ada remote write atau Telegram credential/POST.
2. **AUTHORIZE** memerlukan UUID pilihan, konfirmasi persis `AUTHORIZE <UUID>`, actor owner yang diizinkan, master ref, reviewed SHA = `GITHUB_SHA` = repository approved SHA, serta source switch produksi ON. Tahap ini melakukan preflight read-only dan mencatat authorization bertanda waktu yang kedaluwarsa dalam 60 menit. Belum ada Telegram POST.
3. **SEND** memerlukan dispatch terpisah, konfirmasi persis `SEND <UUID>`, actor dan SHA yang sama dengan authorization aktif, dan semua binding masih cocok. Artikel dan lampiran diambil ulang; content/payload/destination berubah berarti ditolak.

Gunakan master SHA penuh yang sudah direview pada input `reviewed_code_sha`. Jangan gunakan approval input sebagai pengganti `KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA`. Persetujuan terikat pada UUID, URL, content SHA, payload hash, destination alias identity, actor, master SHA, waktu, dan TTL. Authorization satu UUID tidak berlaku untuk UUID lain.

## Ledger, duplicate protection, dan receipt

Ledger append-only berada pada branch `dikti-production-state`, file `.bot-state/kemdiktisaintek_editorial.json`, terpisah dari state observasi Kemdiktisaintek dan state BIMA/YouTube. Writer memakai workflow concurrency `dikti-production-state`, exact repository/branch/path allowlist, blob SHA Contents API, expected-SHA CAS, schema, generation, dan lineage validator. File ledger baru baru dibuat pada AUTHORIZE yang eksplisit; PREVIEW tidak menginisialisasi ledger.

Event stabil: `kemdiktisaintek:editorial:<UUID>:initial`. Sebelum authorization dan setiap operasi pengiriman, runtime memuat ulang state otomatis utama. Baseline UUID, automatic admission/outbox, UUID yang tidak lagi HOLD, dan content hash yang berbeda menghentikan proses. Ledger editorial sendiri tidak dianggap bukti tunggal bahwa item belum dikirim.

Operasi `SUMMARY` dan setiap PDF mempunyai status dan receipt masing-masing. Urutan: ledger `RESERVED` → CAS `SENDING` claim dan read-back ACK → cek preflight/destination → satu POST → receipt Telegram valid → CAS receipt. Jika operasi sebelumnya gagal/ambigu, operasi berikutnya ditandai `BLOCKED`; pesan ringkasan yang sudah delivered tidak diulang.

`SENDING` atau `AMBIGUOUS` berarti rekonsiliasi operator wajib, bukan retry. Telegram timeout menjadi `AMBIGUOUS`. Jika POST berhasil tetapi receipt checkpoint gagal, runtime menampilkan `RECONCILIATION_REQUIRED`; jangan mengulang dispatch SEND. `PARTIAL` dengan PDF gagal juga tidak mengulang summary. Simpan bukti Telegram receipt secara aman dan rekonsiliasi manual sebelum tindakan lanjutan.

## Pemulihan scheduler yang terlewat

Jangan mempromosikan `HOLD` menjadi `NEW_CANDIDATE`, mengubah timestamp, membuka backfill umum, atau menulis ulang state observasi. Periksa state terkini dan sumber resmi, lakukan PREVIEW satu UUID, minta otorisasi editorial eksplisit, lalu gunakan AUTHORIZE dan SEND terpisah. Artikel historis ditampilkan sebagai “INFORMASI RESMI”, bukan seolah baru diterbitkan hari ini. Fasilitas ini tidak menjamin artikel masih layak secara editorial; operator harus memutuskan sebelum AUTHORIZE.

## Master SHA rollover

PR yang mengubah master membuat approved SHA scheduler lama tidak cocok. Setelah merge yang ditinjau, operator harus membaca master HEAD penuh melalui GitHub API, meninjau diff hasil merge, memperbarui `KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA` ke SHA tersebut, lalu read-back. Unified scheduler akan tetap fail-closed sampai rollover cocok. Jangan mengubah Variables/Secrets sebagai bagian dari PR dan jangan mengirim saat rollout kode.

## Batas keselamatan

- Tidak ada pengiriman massal atau daftar HOLD.
- Tidak ada jalur fallback ke credential eksperimen.
- Workflow editorial memakai credential produksi hanya untuk AUTHORIZE/SEND; PREVIEW tidak memuat token Telegram.
- Tidak ada perubahan pada unified scheduler atau policy automatic delivery.
- Semua tes menggunakan fixture, mock CAS, dan mock Telegram; workflow live tidak dijalankan oleh pengembangan ini.
