import argparse
import json

from app_config import load_config
from monitor_runner import run_once


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--youtube-eligibility", action="store_true",
                        help="Compare hybrid YouTube eligibility; requires --dry-run")
    parser.add_argument("--state-dir", default=".")
    args = parser.parse_args(argv)
    if args.youtube_eligibility and not args.dry_run:
        parser.error("--youtube-eligibility requires --dry-run")
    config = load_config(load_env=not args.dry_run, include_secrets=not args.dry_run)
    if args.youtube_eligibility:
        config["youtube_eligibility_dry_run"] = True
    result = run_once(config, dry_run=args.dry_run, state_dir=args.state_dir)
    if result.youtube_eligibility:
        print("[YOUTUBE ELIGIBILITY] " + json.dumps(result.youtube_eligibility, ensure_ascii=True))
    for source in result.sources:
        print(f"[{source.source}] {source.status}: {len(source.items)} new; {source.error}")
    for error in result.errors:
        print(f"::error::{error}")
    print(f"[DELIVERY] pending items: {result.pending}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
