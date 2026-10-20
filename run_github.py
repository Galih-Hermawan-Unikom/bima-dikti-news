import argparse

from app_config import load_config
from monitor_runner import run_once


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-dir", default=".")
    args = parser.parse_args(argv)
    config = load_config(load_env=not args.dry_run, include_secrets=not args.dry_run)
    result = run_once(config, dry_run=args.dry_run, state_dir=args.state_dir)
    for source in result.sources:
        print(f"[{source.source}] {source.status}: {len(source.items)} new; {source.error}")
    for error in result.errors:
        print(f"::error::{error}")
    print(f"[DELIVERY] pending items: {result.pending}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
