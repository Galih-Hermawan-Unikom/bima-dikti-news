# Bot Notifikasi Pengumuman BIMA Kemdiktisaintek

Bot otomatis untuk mengecek dan mengirim notifikasi ketika ada pengumuman baru di [BIMA Kemdiktisaintek](https://bima.kemdiktisaintek.go.id/pengumuman).

## Fitur

- Scraping otomatis halaman pengumuman BIMA menggunakan Playwright + Chromium
- Notifikasi via Telegram dan Console
- Pengecekan berkala (default setiap 30 menit)
- Cache untuk mendeteksi pengumuman baru
- Bisa dijalankan lokal atau hosting gratis di GitHub Actions

## Cara Kerja

```
[GitHub Actions / Lokal] 
        │
        ▼
[Playwright buka halaman BIMA]
        │
        ▼
[Ekstrak daftar pengumuman]
        │
        ▼
[Bandingkan dengan cache]
        │
        ▼
[Ada baru?] ──Ya──► [Kirim notifikasi Telegram]
        │
       Tidak
        │
        ▼
[Tunggu jadwal berikutnya]
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
TELEGRAM_BOT_TOKEN=isi_token_bot
TELEGRAM_CHAT_ID=isi_chat_id
CHECK_INTERVAL_MINUTES=30
```

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
2. Klik **New repository secret**, tambahkan:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token dari BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID tujuan notifikasi, bisa chat pribadi atau channel |

**Langkah 5:** Workflow Otomatis Jalan

- Pengecekan otomatis setiap **30 menit**
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
├── config.json                # Konfigurasi non-secret
├── .env                       # Secret lokal (tidak di-commit)
├── requirements.txt           # Python dependencies
├── announcements_cache.json   # Cache pengumuman (auto-generated)
└── README.md
```

## Konfigurasi

### config.json

```json
{
  "notification_method": "telegram",
  "check_interval_minutes": 30
}
```

| Key | Nilai | Keterangan |
|---|---|---|
| `notification_method` | `console`, `telegram`, `both` | Cara notifikasi |
| `check_interval_minutes` | Angka (menit) | Frekuensi pengecekan |

### .env (Lokal)

```env
NOTIFICATION_METHOD=telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CHECK_INTERVAL_MINUTES=30
```

| Key | Keterangan |
|---|---|
| `NOTIFICATION_METHOD` | Override metode notifikasi dari `config.json` |
| `TELEGRAM_BOT_TOKEN` | Secret token bot Telegram |
| `TELEGRAM_CHAT_ID` | Secret chat ID Telegram |
| `CHECK_INTERVAL_MINUTES` | Override interval dari `config.json` |

### GitHub Secrets

| Secret | Wajib | Keterangan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Ya (untuk Telegram) | Token bot dari [@BotFather](https://t.me/botfather) |
| `TELEGRAM_CHAT_ID` | Ya (untuk Telegram) | Chat ID tujuan notifikasi, bisa chat pribadi atau channel |

### GitHub Variables (Opsional)

| Variable | Default | Keterangan |
|---|---|---|
| `NOTIFICATION_METHOD` | `telegram` | `telegram`, `console`, atau `both` |
| `CHECK_INTERVAL_MINUTES` | `30` | Override interval konfigurasi |

## Jadwal Pengecekan

**Default saat ini: setiap 30 menit**

Edit `.github/workflows/main.yml` bagian `cron`:

```yaml
schedule:
  - cron: '*/30 * * * *'   # Setiap 30 menit (default)
  - cron: '0 * * * *'      # Setiap 1 jam
  - cron: '0 8 * * *'      # Sekali sehari jam 8 pagi
  - cron: '0 8,12,16 * * *' # Jam 8, 12, 16 setiap hari
```

> **Catatan:** Free tier GitHub Actions mendapat **2.000 menit/bulan**.
> Estimasi pemakaian:
>
> | Jadwal | Pemakaian/Bulan |
> |---|---|
> | Setiap 30 menit | ~1.440 menit |
> | Setiap 1 jam | ~720 menit |
> | Sekali sehari | ~30 menit |
>
> Jika kuota habis, workflow berhenti sampai bulan berikutnya (tidak kena biaya).

## Contoh Notifikasi Telegram

```
📢 PENGUMUMAN BARU BIMA KEMDIKTISAINTEK

📅 04/04/2026 22:42 WIB
🔢 Jumlah: 2 pengumuman baru

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

━━━━━━━━━━━━━━━━━━━━
Bot Notifikasi BIMA
```

> Semua waktu notifikasi menggunakan **WIB (UTC+7)**.

## Troubleshooting

| Masalah | Solusi |
|---|---|
| `Executable doesn't exist` | Jalankan `playwright install --with-deps chromium` |
| Notifikasi Telegram tidak terkirim | Periksa `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_CHAT_ID` di Secrets |
| Workflow tidak jalan | Pastikan file ada di `.github/workflows/` dan branch default repository sesuai workflow |
| Timeout di GitHub Actions | Website BIMA lambat, timeout sudah diset 15 menit |
| Cache tidak ter-commit | Pastikan `announcements_cache.json` tidak ada di `.gitignore` |

## Lisensi

MIT
