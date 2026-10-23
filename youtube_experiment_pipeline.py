"""Tabs-only observation and outbox planning, isolated from BIMA and production Telegram."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile

from app_config import load_config
from monitor_runner import run_once
from notifier import Notifier
from youtube_channel_tabs import CHANNEL_ID
from youtube_history import parse_timestamp
from youtube_eligibility import Policy
from youtube_delivery import YouTubeStateStore
from youtube_state_backend import GitHubStateBranch
from youtube_tabs_monitor import ChannelTabsAdapter
from youtube_experiment_preflight import PreflightError, verify_experiment_destination


ACTIVATION_FENCE = "2026-10-22T23:17:48Z"
POLICY_VERSION = "tabs-only-v1"
STATE_PATH = ".bot-state/youtube_delivery_state.json"
EXPECTED_INVENTORY_SIZE = 60


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _hashes():
    names = ("announcements_cache.json", "youtube_cache.json", "delivery_state.json")
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in names}


def _require_config(config):
    if (config.get("youtube_observation_enabled") is not True
            or config.get("youtube_automatic_tabs_verification_enabled") is not True
            or config.get("youtube_delivery_enabled") is not True
            or config.get("youtube_state_initialize") is not False
            or config.get("youtube_policy_approved") is not True
            or config.get("youtube_delivery_send_enabled") is not False
            or config.get("youtube_activation_at") != ACTIVATION_FENCE
            or config.get("youtube_policy_version") != POLICY_VERSION
            or config.get("youtube_state_branch") != "youtube-state"
            or config.get("youtube_channel_id") != CHANNEL_ID):
        raise RuntimeError("Experiment pipeline policy or send-off configuration is invalid")
    expected = {"youtube_window_seconds": 86400, "youtube_ttl_seconds": 14400,
                "youtube_skew_seconds": 300, "youtube_capture_interval_seconds": 600}
    if any(config.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Experiment pipeline timing differs from the approved policy")


class _NoBima:
    data_file = ""
    current_items = []

    @staticmethod
    def check_new_announcements():
        raise RuntimeError("BIMA path must never run in the YouTube experiment pipeline")


def run():
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise RuntimeError("Experiment pipeline is restricted to GitHub Actions")
    if not os.environ.get("GITHUB_TOKEN") or not os.environ.get("GITHUB_REPOSITORY"):
        raise RuntimeError("Durable state credentials are unavailable")
    config = load_config(load_env=False, include_secrets=True)
    _require_config(config)

    before = _hashes()
    cache = _json("youtube_cache.json")
    journal = _json("delivery_state.json")
    announcements = _json("announcements_cache.json")
    if (not isinstance(announcements, list) or len(announcements) != 90
            or not isinstance(cache, list) or len(cache) != 129
            or len(journal.get("youtube_rss_cutover", {}).get("baseline_ids", [])) != 133):
        raise RuntimeError("Historical BIMA/YouTube state failed integrity validation")

    # A failed Telegram preflight turns planning off for this run, while public
    # observation and verification continue. Sending is pinned OFF in all cases.
    try:
        destination = verify_experiment_destination()
        planning_enabled = True
        preflight = "PASS"
    except PreflightError as exc:
        destination = None
        planning_enabled = False
        preflight = "BLOCKED:" + exc.code

    activation = parse_timestamp(ACTIVATION_FENCE)
    policy = Policy(CHANNEL_ID, POLICY_VERSION, activation_at=activation,
        window=timedelta(seconds=86400), ttl=timedelta(seconds=14400),
        skew=timedelta(seconds=300), capture_interval=timedelta(seconds=600),
        approved=True, automatic_tabs_verification=True)
    with tempfile.TemporaryDirectory(prefix="youtube-experiment-state-") as temporary:
        state_file = Path(temporary) / "youtube_delivery_state.json"
        backend = GitHubStateBranch(os.environ["GITHUB_REPOSITORY"], "youtube-state", STATE_PATH,
                                    os.environ["GITHUB_TOKEN"], initialize=False)
        if backend.load(state_file) is not True:
            raise RuntimeError("Existing durable YouTube state was not loaded")
        remote_state = _json(state_file)
        if (remote_state.get("activation_at") != activation.isoformat()
                or remote_state.get("policy_version") != POLICY_VERSION
                or remote_state.get("initialized") is not True
                or remote_state.get("needs_inventory") is not False
                or sum(row.get("initial_inventory") is True
                       for row in remote_state.get("observations", {}).values()) != EXPECTED_INVENTORY_SIZE):
            raise RuntimeError("Activation fence, policy, or initial inventory failed validation")
        store = YouTubeStateStore(state_file, activation_at=activation, policy_version=POLICY_VERSION)
        store.set_commit_hook(backend.persist)
        adapter = ChannelTabsAdapter(channel_id=CHANNEL_ID, policy=policy,
                                     observation_store=store)
        config["youtube_delivery_enabled"] = planning_enabled
        config["youtube_delivery_send_enabled"] = False
        config["notification_method"] = "telegram"
        result = run_once(config, dry_run=False, state_dir=".", scraper=_NoBima(),
            notifier=Notifier(config), youtube_adapter=adapter,
            youtube_send_limit=1, youtube_experiment_only=True)
        if result.errors:
            raise RuntimeError("Experiment pipeline stopped with safe runner errors")

    if before != _hashes():
        raise RuntimeError("Historical cache, baseline, or journal changed")
    youtube = next((source for source in result.sources if source.source == "youtube"), None)
    report = result.youtube_eligibility or {}
    counts = {"eligible": 0, "hold": 0, "rejected": 0, "already_delivered": 0}
    for decision in report.get("decisions", []):
        status = decision.get("eligibility")
        key = {"ELIGIBLE": "eligible", "HOLD_FOR_REVIEW": "hold", "REJECTED": "rejected",
               "ALREADY_DELIVERED": "already_delivered"}.get(status)
        if key:
            counts[key] += 1
    outbox = report.get("outbox", [])
    print("YOUTUBE_EXPERIMENT_PIPELINE=" + json.dumps({
        "preflight": preflight,
        "source_status": youtube.status if youtube else "FAILED",
        "health": {name: value.get("health") for name, value in report.get("health", {}).items()},
        "counts": counts,
        "planned_reservations": sum(item.get("status") == "reserved" for item in outbox),
        "outbox_statuses": [item.get("status") for item in outbox],
        "sending": "OFF",
        "bima_invoked": False,
        "historical_state_unchanged": True,
        "experiment_destination": destination.get("destination_alias") if destination else None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        print("YouTube experiment pipeline failed closed: " + type(exc).__name__)
        raise SystemExit(1)
