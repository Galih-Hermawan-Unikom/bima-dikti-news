import sys
from app_config import load_config
from scraper import BimaScraper
from notifier import Notifier

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
