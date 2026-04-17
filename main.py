import sys
import schedule
import time
from app_config import load_config
from scraper import BimaScraper
from notifier import Notifier
from youtube_monitor import YouTubeMonitor

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def check_announcements():
    print("\n[Check] Mengecek pengumuman baru...")

    try:
        config = load_config()
        scraper = BimaScraper()
        notifier = Notifier(config)

        new_announcements = scraper.check_new_announcements()
        new_youtube_items = []

        if config.get("youtube_monitor_enabled", True):
            monitor = YouTubeMonitor(
                channel_id=config.get("youtube_channel_id", ""),
                channel_url=config.get("youtube_channel_url", ""),
            )
            new_youtube_items = monitor.check_new_videos()

        new_items = new_announcements + new_youtube_items

        if new_items:
            print(f"\n[NEW] Ditemukan {len(new_items)} item baru!")
            notifier.send_notification(new_items)
        else:
            print("[OK] Tidak ada item baru")
    except Exception as e:
        print(f"[ERROR] Error saat mengecek: {e}")


def main():
    config = load_config()
    check_interval = config.get("check_interval_minutes", 30)

    print("=" * 60)
    print("BOT NOTIFIKASI PENGUMUMAN BIMA KEMDIKTISAINTEK")
    print("=" * 60)

    check_announcements()

    print(f"\n[Schedule] Pengecekan setiap {check_interval} menit")
    print("[Info] Tekan Ctrl+C untuk berhenti\n")

    schedule.every(check_interval).minutes.do(check_announcements)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n[Exit] Bot dihentikan")
        sys.exit(0)


if __name__ == "__main__":
    main()
