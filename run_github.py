import sys
from app_config import load_config
from scraper import BimaScraper
from notifier import Notifier
from youtube_monitor import YouTubeMonitor

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def main():
    config = load_config()

    print("=" * 60)
    print("BIMA Pengumuman Checker - GitHub Actions")
    print("=" * 60)

    try:
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
            print(f"\n[NEW] Found {len(new_items)} new items!")
            notifier.send_notification(new_items)
        else:
            print("[OK] No new items")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
