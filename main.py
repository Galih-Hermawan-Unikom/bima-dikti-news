import sys
import time
from app_config import load_config
from monitor_runner import run_once

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def check_announcements():
    print("\n[Check] Mengecek pengumuman baru...")

    try:
        config = load_config()
        result = run_once(config)
        for source in result.sources:
            print(f"[{source.source}] {source.status}: {source.error}")
        for error in result.errors:
            print(f"[ERROR] {error}")
    except Exception as e:
        print(f"[ERROR] Error saat mengecek: {e}")


def main():
    import schedule

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
    if "--dry-run" in sys.argv or "--youtube-eligibility" in sys.argv:
        from run_github import main as run_check
        raise SystemExit(run_check())
    main()
