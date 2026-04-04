import sys
import os
import json
from scraper import BimaScraper
from notifier import Notifier

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


def main():
    load_env_file()

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    notif_method = os.environ.get("NOTIFICATION_METHOD", "telegram")

    if bot_token and chat_id:
        config = {
            "notification_method": notif_method,
            "telegram_bot_token": bot_token,
            "telegram_chat_id": chat_id,
            "check_interval_minutes": 30,
        }
        with open("config.json", "w") as f:
            json.dump(config, f, indent=2)

    print("=" * 60)
    print("BIMA Pengumuman Checker - GitHub Actions")
    print("=" * 60)

    try:
        scraper = BimaScraper()
        notifier = Notifier()

        new_announcements = scraper.check_new_announcements()

        if new_announcements:
            print(f"\n[NEW] Found {len(new_announcements)} new announcements!")
            notifier.send_notification(new_announcements)
        else:
            print("[OK] No new announcements")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
