import sys
import schedule
import time
from app_config import load_config
from scraper import BimaScraper
from notifier import Notifier

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def check_announcements():
    print("\n[Check] Mengecek pengumuman baru...")

    try:
        scraper = BimaScraper()
        notifier = Notifier(load_config())

        new_announcements = scraper.check_new_announcements()

        if new_announcements:
            print(f"\n[NEW] Ditemukan {len(new_announcements)} pengumuman baru!")
            notifier.send_notification(new_announcements)
        else:
            print("[OK] Tidak ada pengumuman baru")
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
