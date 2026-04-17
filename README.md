# Bot Monitoring BIMA + YouTube Kemdiktisaintek

Bot otomatis untuk mengecek dan mengirim notifikasi ketika ada update baru dari:
- halaman [BIMA Kemdiktisaintek](https://bima.kemdiktisaintek.go.id/pengumuman)
- kanal YouTube Kemdiktisaintek melalui RSS feed

Implementasi publik proyek ini dapat dilihat di channel Telegram **Dikti News**:
[https://t.me/dikti_news](https://t.me/dikti_news)

## Fitur

- Scraping otomatis halaman pengumuman BIMA menggunakan Playwright + Chromium
- Deteksi dokumen lampiran beserta URL unduhan file
- Monitoring kanal YouTube Kemdiktisaintek via RSS feed
- Notifikasi via Telegram dan Console
- Opsi kirim ringkasan saja atau ringkasan + file ke Telegram
- Notifikasi gabungan BIMA + YouTube dalam satu ringkasan
- Pengecekan berkala yang bisa disesuaikan: 30 menit saat masa kritis, 3 jam saat masa normal
- Cache metadata terpisah untuk BIMA dan YouTube
- Bisa dijalankan lokal atau hosting gratis di GitHub Actions

## Cara Kerja

```
[GitHub Actions / Lokal] 
        │
        ▼
[Playwright buka halaman BIMA]
        │
        ▼
[Ekstrak daftar pengumuman + dokumen]
        │
        ├──────────────► [Ambil RSS YouTube Kemdiktisaintek]
        │
        ▼
[Bandingkan cache:
 announcements_cache.json
 youtube_cache.json]
        │
        ▼
[Ada baru?] ──Ya──► [Kirim ringkasan Telegram]
        │           [Unggah lampiran BIMA jika aktif]
        │
       Tidak
        │
        ▼
[Tidak ada aksi]
```

## Instalasi

### 1. Clone Repository

```bash
git clone https://github.com/USERNAME/bima-dikti-news.git
cd bima-dikti-news
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
playwright install --with-deps chromium
```

## Penggunaan

### Mode Lokal

Jalankan sekali:
```bash
python main.py
```

Bot akan mengecek pengumuman dan berjalan terus-menerus sesuai interval di `config.json`.
Nilai ini bisa dioverride lewat `CHECK_INTERVAL_MINUTES` di `.env`.

Untuk notifikasi Telegram lokal, isi `.env`:

```env
NOTIFICATION_METHOD=telegram
TELEGRAM_SEND_FILES=true
TELEGRAM_BOT_TOKEN=isi_token_bot
TELEGRAM_CHAT_ID=isi_chat_id
CHECK_INTERVAL_MINUTES=30
YOUTUBE_MONITOR_ENABLED=true
YOUTUBE_CHANNEL_URL=https://www.youtube.com/@kemdiktisaintek
YOUTUBE_CHANNEL_ID=UCGo_6l_6kp8H8OHcKcSIeDw
```

Rekomendasi penggunaan:
- Lokal / eksperimen: isi `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` dengan bot pribadi
- GitHub Actions / channel resmi: isi GitHub Secrets dengan token dan chat ID channel resmi
- Monitor YouTube bisa dimatikan dengan `YOUTUBE_MONITOR_ENABLED=false`
- Kirim lampiran Telegram aktif jika `TELEGRAM_SEND_FILES=true`
- Jika tidak ada item baru, bot tidak mengirim pesan dan tidak mengunggah file apa pun

### Mode GitHub Actions (Hosting Gratis)

**Langkah 1:** Push repository ke GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin <default-branch>
```

**Langkah 2:** Buat Bot Telegram

1. Buka [@BotFather](https://t.me/botfather) di Telegram
2. Kirim `/newbot`
3. Ikuti instruksi sampai mendapat **token** (contoh: `123456:ABC-DEF...`)

**Langkah 3:** Dapatkan Chat ID Tujuan

Bot bisa mengirim notifikasi ke:
- chat pribadi Telegram
- channel Telegram

**Untuk chat pribadi:**

1. Buka [@userinfobot](https://t.me/userinfobot)
2. Kirim pesan `/start`
3. Catat **Id** Anda (contoh: `987654321`)

**Untuk channel Telegram:**

1. Buat channel Telegram
2. Tambahkan bot sebagai **admin channel**
3. Kirim minimal satu pesan ke channel
4. Buka:

```text
https://api.telegram.org/bot<TOKEN_BOT_ANDA>/getUpdates
```

5. Cari nilai `chat.id` pada object `channel_post` atau `my_chat_member`
6. Gunakan nilai tersebut sebagai `TELEGRAM_CHAT_ID` channel

> Chat ID channel biasanya bernilai negatif, misalnya `-100xxxxxxxxxx`.

**Langkah 4:** Setup Secrets di GitHub

1. Buka repository → **Settings** → **Secrets and variables** → **Actions**
2. Di tab **Secrets**, tambahkan:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token dari BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID tujuan notifikasi, bisa chat pribadi atau channel |

3. Di tab **Variables**, tambahkan:

| Name | Contoh Value |
|---|---|
| `NOTIFICATION_METHOD` | `telegram` |
| `TELEGRAM_SEND_FILES` | `true` |
| `CHECK_INTERVAL_MINUTES` | `30` |
| `YOUTUBE_MONITOR_ENABLED` | `true` |
| `YOUTUBE_CHANNEL_URL` | `https://www.youtube.com/@kemdiktisaintek` |
| `YOUTUBE_CHANNEL_ID` | `UCGo_6l_6kp8H8OHcKcSIeDw` |

> Gunakan **Secrets** untuk data sensitif seperti token dan chat ID.
> Gunakan **Variables** untuk pengaturan workflow seperti mode notifikasi, monitoring YouTube, dan kirim lampiran.

**Langkah 5:** Workflow Otomatis Jalan

- Pengecekan otomatis saat ini setiap **3 jam**
- Bisa trigger manual: tab **Actions** → **BIMA Pengumuman Checker** → **Run workflow**

## Struktur File

```
bima-dikti-news/
├── .github/workflows/
│   └── main.yml               # GitHub Actions workflow
├── main.py                    # Entry point (lokal + scheduler)
├── scraper.py                 # Web scraper (cross-platform)
├── notifier.py                # Notifikasi Console + Telegram
├── run_github.py              # Entry point GitHub Actions
├── youtube_monitor.py         # Monitor RSS YouTube + cache video
├── config.json                # Konfigurasi non-secret
├── .env                       # Secret lokal (tidak di-commit)
├── requirements.txt           # Python dependencies
├── announcements_cache.json   # Cache metadata pengumuman (auto-generated)
├── youtube_cache.json         # Cache metadata video YouTube (auto-generated)
└── README.md
```

## Konfigurasi

### config.json

```json
{
  "notification_method": "telegram",
  "check_interval_minutes": 30,
  "youtube_monitor_enabled": true,
  "youtube_channel_url": "https://www.youtube.com/@kemdiktisaintek",
  "youtube_channel_id": "UCGo_6l_6kp8H8OHcKcSIeDw",
  "telegram_send_files": false
}
```

| Key | Nilai | Keterangan |
|---|---|---|
| `notification_method` | `console`, `telegram`, `both` | Cara notifikasi |
| `check_interval_minutes` | Angka (menit) | Frekuensi pengecekan |
| `youtube_monitor_enabled` | `true`, `false` | Aktifkan monitoring YouTube |
| `youtube_channel_url` | URL channel | URL kanal YouTube target |
| `youtube_channel_id` | Channel ID | ID kanal YouTube untuk RSS |
| `telegram_send_files` | `true`, `false` | Kirim lampiran dokumen ke Telegram |

### .env (Lokal)

```env
NOTIFICATION_METHOD=telegram
TELEGRAM_SEND_FILES=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CHECK_INTERVAL_MINUTES=30
YOUTUBE_MONITOR_ENABLED=true
YOUTUBE_CHANNEL_URL=https://www.youtube.com/@kemdiktisaintek
YOUTUBE_CHANNEL_ID=UCGo_6l_6kp8H8OHcKcSIeDw
```

| Key | Keterangan |
|---|---|
| `NOTIFICATION_METHOD` | Override metode notifikasi dari `config.json` |
| `TELEGRAM_SEND_FILES` | Override kirim lampiran Telegram |
| `TELEGRAM_BOT_TOKEN` | Secret token bot Telegram |
| `TELEGRAM_CHAT_ID` | Secret chat ID Telegram |
| `CHECK_INTERVAL_MINUTES` | Override interval dari `config.json` |
| `YOUTUBE_MONITOR_ENABLED` | Override aktivasi monitor YouTube |
| `YOUTUBE_CHANNEL_URL` | Override URL channel YouTube |
| `YOUTUBE_CHANNEL_ID` | Override channel ID YouTube |

### GitHub Secrets

| Secret | Wajib | Keterangan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Ya (untuk Telegram) | Token bot dari [@BotFather](https://t.me/botfather) |
| `TELEGRAM_CHAT_ID` | Ya (untuk Telegram) | Chat ID tujuan notifikasi, bisa chat pribadi atau channel |

### GitHub Variables (Opsional)

| Variable | Default | Keterangan |
|---|---|---|
| `NOTIFICATION_METHOD` | `telegram` | `telegram`, `console`, atau `both` |
| `TELEGRAM_SEND_FILES` | `false` | `true` untuk kirim lampiran Telegram di workflow |
| `CHECK_INTERVAL_MINUTES` | `30` | Override interval konfigurasi |
| `YOUTUBE_MONITOR_ENABLED` | `true` | `false` untuk menonaktifkan monitor YouTube |
| `YOUTUBE_CHANNEL_URL` | `https://www.youtube.com/@kemdiktisaintek` | URL kanal YouTube target |
| `YOUTUBE_CHANNEL_ID` | `UCGo_6l_6kp8H8OHcKcSIeDw` | Channel ID untuk RSS YouTube |

## Jadwal Pengecekan

**Default saat ini: setiap 3 jam**

Rekomendasi penggunaan:
- Masa kritis, misalnya saat menunggu pengumuman hibah: setiap `30 menit`
- Masa normal: setiap `3 jam`

Edit `.github/workflows/main.yml` bagian `cron`:

```yaml
schedule:
  - cron: '0 */3 * * *'    # Setiap 3 jam (default saat ini)
  - cron: '*/30 * * * *'   # Setiap 30 menit (masa kritis)
  - cron: '0 * * * *'      # Setiap 1 jam
  - cron: '0 */6 * * *'    # Setiap 6 jam
  - cron: '0 8 * * *'      # Sekali sehari jam 8 pagi
  - cron: '0 8,12,16 * * *' # Jam 8, 12, 16 setiap hari
```

> **Catatan:** Free tier GitHub Actions mendapat **2.000 menit/bulan**.
> Estimasi pemakaian:
>
> | Jadwal | Pemakaian/Bulan |
> |---|---|
> | Setiap 30 menit | ~1.440 menit |
> | Setiap 3 jam | ~240 menit |
> | Setiap 1 jam | ~720 menit |
> | Sekali sehari | ~30 menit |
>
> Jika kuota habis, workflow berhenti sampai bulan berikutnya (tidak kena biaya).

## Contoh Notifikasi Telegram

```
📢 UPDATE BARU BIMA + YOUTUBE KEMDIKTISAINTEK

📅 17/04/2026 18:57 WIB
🔢 Jumlah: 3 item baru
🏛️ BIMA: 2
📺 YouTube: 1

1. Pengumuman Penerima Pendanaan Penelitian Program PHC - NUSANTARA 2026
   📜 203/DST/C2/PP.01.11/2026
   📆 01 April 2026
   📎 Surat Pengumuman PHC-Nusantara 2026.pdf
   🔗 https://bima.kemdiktisaintek.go.id/pengumuman

2. Pengumuman Pendanaan Program Penelitian Multitahun Lanjutan TA 2026
   📜 195/DST/C3/DT.05.00/2026
   📆 06 March 2026
   📎 Pengumuman Pendanaan Program Penelitian Multitahun Lanjutan TA 2026.pdf
   🔗 https://bima.kemdiktisaintek.go.id/pengumuman

3. ▶️ Sosialisasi Peraturan tentang Petunjuk Teknis PEKERTI AA
   📺 Kementerian Pendidikan Tinggi, Sains dan Teknologi
   📆 2026-04-16T04:05:14+00:00
   📝 Sosialisasi Peraturan tentang Petunjuk Teknis PEKERTI AA (Pimpinan PT dan Kepala LLDIKTI)
   🔗 https://www.youtube.com/watch?v=pSgfm5BB-aI

━━━━━━━━━━━━━━━━━━━━
Bot Notifikasi BIMA
```

> Semua waktu notifikasi menggunakan **WIB (UTC+7)**.
> Jika `TELEGRAM_SEND_FILES=true`, setelah pesan utama bot akan mengunggah dokumen lampiran satu per satu untuk item **BIMA saja**.
> Item YouTube hanya mengirim ringkasan teks dan tautan video, tanpa lampiran.
> Jika tidak ada item baru, bot hanya menulis log `[OK] No new items` tanpa mengirim pesan ke Telegram.
> File cache yang ter-commit hanya menyimpan metadata stabil dan nama dokumen, bukan signed URL unduhan.

## Troubleshooting

| Masalah | Solusi |
|---|---|
| `Executable doesn't exist` | Jalankan `playwright install --with-deps chromium` |
| Notifikasi Telegram tidak terkirim | Periksa `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` di Secrets |
| File lampiran tidak ikut terkirim | Pastikan `TELEGRAM_SEND_FILES=true` dan bot punya izin kirim dokumen ke chat/channel tujuan |
| Video YouTube tidak muncul | Periksa `YOUTUBE_MONITOR_ENABLED`, `YOUTUBE_CHANNEL_URL`, dan `YOUTUBE_CHANNEL_ID` |
| Workflow tidak jalan | Pastikan file ada di `.github/workflows/` dan branch default repository sesuai workflow |
| Timeout di GitHub Actions | Website BIMA lambat, timeout sudah diset 15 menit |
| Cache ter-reset atau rusak | Hapus `announcements_cache.json` atau `youtube_cache.json`, lalu jalankan ulang agar cache metadata dibuat ulang |

## Pengembang

**Galih Hermawan**  
Program Studi Teknik Informatika  
Universitas Komputer Indonesia  

- Website: [https://galih.eu](https://galih.eu)
- Email: [galih.hermawan@gmail.com](mailto:galih.hermawan@gmail.com)
- Threads: [https://www.threads.com/@galihhermawan](https://www.threads.com/@galihhermawan)

## Lisensi

MIT
