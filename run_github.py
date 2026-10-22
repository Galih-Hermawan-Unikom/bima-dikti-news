import argparse
import json
import os
from pathlib import Path

from app_config import load_config
from monitor_runner import run_once


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--youtube-eligibility", action="store_true",
                        help="Report channel-tabs eligibility; requires --dry-run")
    parser.add_argument("--state-dir", default=".")
    parser.add_argument("--config")
    parser.add_argument("--youtube-experiment-preflight", action="store_true",
                        help="Read-only allowlist, channel identity, and bot rights check")
    parser.add_argument("--youtube-review-preview", action="store_true",
                        help="Verify signed review evidence during a no-send dry-run")
    parser.add_argument("--initialize-youtube-state", action="store_true",
                        help="Create an empty durable state DB on the configured state branch")
    parser.add_argument("--youtube-experiment-smoke-once", action="store_true",
                        help="Send at most one eligible item after explicit confirmation")
    parser.add_argument("--confirm-experiment-destination", action="store_true",
                        help="Required explicit confirmation for the experiment-only smoke send")
    args = parser.parse_args(argv)
    if args.youtube_eligibility and not args.dry_run:
        parser.error("--youtube-eligibility requires --dry-run")
    if args.youtube_review_preview and not args.dry_run:
        parser.error("--youtube-review-preview requires --dry-run")
    if args.initialize_youtube_state and (args.dry_run or args.youtube_experiment_preflight
            or args.youtube_experiment_smoke_once or args.youtube_review_preview):
        parser.error("State initialization is a separate no-send command")
    if args.youtube_experiment_preflight and (args.dry_run or args.youtube_experiment_smoke_once):
        parser.error("Experiment preflight is a separate non-sending command")
    if args.youtube_experiment_smoke_once and (args.dry_run or not args.confirm_experiment_destination):
        parser.error("Experiment smoke requires live mode and --confirm-experiment-destination")
    if args.confirm_experiment_destination and not args.youtube_experiment_smoke_once:
        parser.error("Confirmation is only valid with --youtube-experiment-smoke-once")
    config_args = {
        "load_env": not args.dry_run or args.youtube_review_preview,
        "include_secrets": not args.dry_run,
    }
    if args.config:
        config_args["config_path"] = args.config
    if args.youtube_review_preview:
        config_args["include_review_secrets"] = True
    if args.initialize_youtube_state:
        config_args["load_env"] = False
        config_args["include_secrets"] = False
        config_args["include_state_secrets"] = True
    config = load_config(**config_args)
    if args.initialize_youtube_state:
        if (config.get("youtube_state_initialize") is not True
                or config.get("youtube_observation_enabled") is True
                or config.get("youtube_delivery_enabled") is True
                or config.get("youtube_delivery_send_enabled") is True
                or config.get("youtube_policy_approved") is True):
            parser.error("Initialization requires its explicit flag, delivery disabled, and policy approval false")
        from monitor_runner import _youtube_policy
        from youtube_delivery import YouTubeStateStore
        from youtube_state_backend import GitHubStateBranch
        channel = config.get("youtube_channel_id") or ""
        policy = _youtube_policy(config, channel)
        root = Path(args.state_dir)
        if policy.activation_at is None:
            parser.error("An activation timestamp is required to initialize the durable state")
        root.mkdir(parents=True, exist_ok=True)
        backend = GitHubStateBranch(os.environ.get("GITHUB_REPOSITORY", ""),
            config.get("youtube_state_branch", "youtube-state"),
            ".bot-state/youtube_delivery_state.json", config.get("youtube_state_token", ""),
            initialize=True)
        db_path = root / "youtube_delivery_state.json"
        if backend.load(db_path):
            print("[YOUTUBE STATE] already initialized; no state overwritten")
            return 2
        store = YouTubeStateStore(db_path, activation_at=policy.activation_at,
                                  policy_version=policy.version)
        store.set_commit_hook(backend.persist)
        store.flush()
        print("[YOUTUBE STATE] empty durable state initialized; no sources or Telegram contacted")
        return 0
    if args.youtube_experiment_preflight or args.youtube_experiment_smoke_once:
        if args.youtube_experiment_smoke_once and (
                config.get("notification_method") != "telegram"
                or not all(config.get(key) is True for key in (
                    "youtube_observation_enabled", "youtube_delivery_enabled", "youtube_delivery_send_enabled"))):
            parser.error("Smoke requires Telegram mode and all three explicitly enabled YouTube flags")
        from notifier import Notifier
        notifier = Notifier(config)
        if not notifier.preflight_youtube_experiment_destination():
            print("[TELEGRAM EXPERIMENT PREFLIGHT] FAILED; no message sent")
            return 2
        print("[TELEGRAM EXPERIMENT PREFLIGHT] passed; no message sent")
        if args.youtube_experiment_preflight:
            return 0
        result = run_once(config, dry_run=False, state_dir=args.state_dir,
                          notifier=notifier, youtube_send_limit=1, youtube_experiment_only=True)
    else:
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
