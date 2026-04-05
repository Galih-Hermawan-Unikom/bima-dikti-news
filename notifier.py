import sys
import requests
from datetime import datetime, timezone, timedelta
from app_config import load_config

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

WIB = timezone(timedelta(hours=7))


def now_wib():
    return datetime.now(WIB)


class Notifier:
    def __init__(self, config=None, config_path="config.json"):
        self.config = config or load_config(config_path=config_path)

    def send_notification(self, announcements):
        if not announcements:
            return

        method = self.config.get("notification_method", "console")

        if method == "telegram":
            self._send_telegram(announcements)
        elif method == "console":
            self._send_console(announcements)
        elif method == "both":
            self._send_console(announcements)
            self._send_telegram(announcements)

    def _format_message(self, announcements):
        message = "📢 *PENGUMUMAN BARU BIMA KEMDIKTISAINTEK*\n\n"
        message += f"📅 {now_wib().strftime('%d/%m/%Y %H:%M WIB')}\n"
        message += f"🔢 Jumlah: {len(announcements)} pengumuman baru\n\n"

        for i, ann in enumerate(announcements, 1):
            message += f"*{i}. {ann['title']}*\n"
            if ann.get("surat"):
                message += f"   📜 {ann['surat']}\n"
            if ann.get("date"):
                message += f"   📆 {ann['date']}\n"
            if ann.get("documents"):
                for doc in ann["documents"]:
                    message += f"   📎 {doc}\n"
            message += f"   🔗 {ann.get('url', 'https://bima.kemdiktisaintek.go.id/pengumuman')}\n"
            message += "\n"

        message += "━━━━━━━━━━━━━━━━━━━━\n"
        message += "_Bot Notifikasi BIMA_"

        return message

    def _send_console(self, announcements):
        print("\n" + "=" * 60)
        print("PENGUMUMAN BARU BIMA KEMDIKTISAINTEK")
        print("=" * 60)
        print(f"  {now_wib().strftime('%d/%m/%Y %H:%M WIB')}")
        print(f"  Jumlah: {len(announcements)} pengumuman baru\n")

        for i, ann in enumerate(announcements, 1):
            print(f"  {i}. {ann['title']}")
            if ann.get("surat"):
                print(f"     {ann['surat']}")
            if ann.get("date"):
                print(f"     {ann['date']}")
            if ann.get("documents"):
                for doc in ann["documents"]:
                    print(f"     -> {doc}")
            print()

        print("=" * 60)

    def _send_telegram(self, announcements):
        bot_token = self.config.get("telegram_bot_token", "")
        chat_id = self.config.get("telegram_chat_id", "")

        if not bot_token or not chat_id:
            print("  Telegram bot_token atau chat_id belum dikonfigurasi")
            return

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

        header = f"📢 *PENGUMUMAN BARU BIMA KEMDIKTISAINTEK*\n\n📅 {now_wib().strftime('%d/%m/%Y %H:%M WIB')}\n🔢 Jumlah: {len(announcements)} pengumuman baru\n\n"

        chunks = []
        current = header
        for i, ann in enumerate(announcements, 1):
            item = f"*{i}. {ann['title']}*\n"
            if ann.get("surat"):
                item += f"   📜 {ann['surat']}\n"
            if ann.get("date"):
                item += f"   📆 {ann['date']}\n"
            if ann.get("documents"):
                for doc in ann["documents"]:
                    item += f"   📎 {doc}\n"
            item += f"   🔗 {ann.get('url', 'https://bima.kemdiktisaintek.go.id/pengumuman')}\n\n"

            if len(current) + len(item) > 4000:
                chunks.append(current)
                current = item
            else:
                current += item

        if current:
            chunks.append(current)

        for idx, chunk in enumerate(chunks):
            if idx == len(chunks) - 1:
                chunk += "━━━━━━━━━━━━━━━━━━━━\n_Bot Notifikasi BIMA_"

            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": False,
            }

            try:
                response = requests.post(url, json=payload, timeout=10)
                if response.status_code == 200:
                    print(f"  Notifikasi Telegram terkirim ({idx + 1}/{len(chunks)})")
                else:
                    print(f"  Gagal kirim Telegram: {response.text}")
            except Exception as e:
                print(f"  Error kirim Telegram: {e}")
