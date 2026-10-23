"""One-cycle, read-only-source YouTube observation with durable JSON CAS checkpoints."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from app_config import load_config
from youtube_channel_tabs import CHANNEL_ID
from youtube_history import parse_timestamp
from youtube_eligibility import Policy
from youtube_delivery import YouTubeStateStore
from youtube_state_backend import GitHubStateBranch
from youtube_tabs_monitor import ChannelTabsAdapter


ACTIVATION_FENCE = "2026-10-22T23:17:48Z"
POLICY_VERSION = "tabs-only-v1"
STATE_PATH = ".bot-state/youtube_delivery_state.json"
EXPECTED_INVENTORY_SIZE = 60


def _load_json(path):
    with Path(path).open("r", encoding="utf-8") as stream:
        return json.load(stream)


def _sha256(paths):
    return {str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths}


def _require_observation_only(config):
    required_true = ("youtube_observation_enabled", "youtube_automatic_tabs_verification_enabled",
                     "youtube_policy_approved")
    required_false = ("youtube_delivery_enabled", "youtube_delivery_send_enabled", "youtube_state_initialize")
    if any(config.get(key) is not True for key in required_true):
        raise RuntimeError("Observation-only feature configuration is incomplete")
    if any(config.get(key) is not False for key in required_false):
        raise RuntimeError("Delivery, sending, and initialization must remain disabled")
    if config.get("youtube_activation_at") != ACTIVATION_FENCE:
        raise RuntimeError("Activation fence does not match the approved value")
    if config.get("youtube_policy_version") != POLICY_VERSION:
        raise RuntimeError("YouTube policy version does not match the approved value")
    if config.get("youtube_state_branch") != "youtube-state":
        raise RuntimeError("Unexpected YouTube durable-state branch")
    if config.get("youtube_channel_id") != CHANNEL_ID:
        raise RuntimeError("Unexpected YouTube channel identity")
    expected = {"youtube_window_seconds": 86400, "youtube_ttl_seconds": 14400,
                "youtube_skew_seconds": 300, "youtube_capture_interval_seconds": 600}
    if any(config.get(key) != value for key, value in expected.items()):
        raise RuntimeError("YouTube policy timing differs from the approved values")


def _summary(evaluation, capture_seq):
    evidence = {item["video_id"]: item for item in evaluation.evidence}
    decisions, counts = [], {key: 0 for key in (
        "checked", "videos_tab", "streams_tab", "known", "unknown", "unverified", "new_candidates",
        "new_eligible", "new_hold", "eligible", "hold", "rejected", "already_delivered")}
    counts["checked"] = len(evaluation.decisions)
    counts["videos_tab"] = sum("videos" in item.get("tabs", []) for item in evaluation.evidence)
    counts["streams_tab"] = sum("streams" in item.get("tabs", []) for item in evaluation.evidence)
    for decision in evaluation.decisions:
        item = evidence.get(decision.video_id, {})
        knowledge = decision.knowledge.value if decision.knowledge is not None else None
        eligibility = decision.eligibility.value
        counts["known" if knowledge == "KNOWN" else "unknown" if knowledge == "UNKNOWN" else "unverified"] += 1
        if knowledge == "UNKNOWN" and item and not item.get("initial_inventory", True):
            counts["new_candidates"] += 1
            if eligibility == "ELIGIBLE":
                counts["new_eligible"] += 1
            elif eligibility == "HOLD_FOR_REVIEW":
                counts["new_hold"] += 1
        counts[{"ELIGIBLE": "eligible", "HOLD_FOR_REVIEW": "hold", "REJECTED": "rejected",
                "ALREADY_DELIVERED": "already_delivered"}[eligibility]] += 1
        decisions.append({
            "video_id": decision.video_id,
            "knowledge": knowledge,
            "eligibility": eligibility,
            "delivery_state": decision.delivery_state.value,
            "reason_codes": list(decision.reason_codes),
            "first_seen_at": item.get("first_seen_at", ACTIVATION_FENCE),
            "initial_inventory": bool(item.get("initial_inventory", True)),
            "tabs": list(item.get("tabs", [])),
            "category": item.get("category", "UNKNOWN"),
            "readiness": item.get("readiness", "UNKNOWN"),
        })
    return {
        "version": 1,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "capture_seq": capture_seq,
        "health": {tab: evaluation.health.get(tab, {}).get("health", "FAILED")
                   for tab in ("videos", "streams")},
        "counts": counts,
        "errors": list(evaluation.errors),
        "decisions": decisions,
    }


def run():
    config = load_config(load_env=False, include_secrets=False)
    _require_observation_only(config)
    if os.environ.get("GITHUB_ACTIONS", "").lower() != "true":
        raise RuntimeError("Observation workflow must run in GitHub Actions")
    if not os.environ.get("GITHUB_TOKEN") or not os.environ.get("GITHUB_REPOSITORY"):
        raise RuntimeError("GitHub durable-state credentials are unavailable")
    tracked_files = ("announcements_cache.json", "youtube_cache.json", "delivery_state.json")
    before = _sha256(tracked_files)
    cache = _load_json("youtube_cache.json")
    journal = _load_json("delivery_state.json")
    if not isinstance(cache, list) or len(cache) != 129:
        raise RuntimeError("YouTube historical cache failed integrity validation")
    if not isinstance(journal, dict) or len(journal.get("youtube_rss_cutover", {}).get("baseline_ids", [])) != 133:
        raise RuntimeError("Historical delivery journal or baseline failed integrity validation")

    activation = parse_timestamp(ACTIVATION_FENCE)
    policy = Policy(CHANNEL_ID, POLICY_VERSION, activation_at=activation,
        window=timedelta(seconds=86400), ttl=timedelta(seconds=14400),
        skew=timedelta(seconds=300), capture_interval=timedelta(seconds=600),
        approved=True, automatic_tabs_verification=True)

    with tempfile.TemporaryDirectory(prefix="youtube-observe-") as temporary:
        state_file = Path(temporary) / "youtube_delivery_state.json"
        backend = GitHubStateBranch(os.environ["GITHUB_REPOSITORY"], "youtube-state", STATE_PATH,
            os.environ["GITHUB_TOKEN"], initialize=False)
        if backend.load(state_file) is not True:
            raise RuntimeError("Existing durable YouTube state was not loaded")
        durable = _load_json(state_file)
        if (durable.get("activation_at") != activation.isoformat()
                or durable.get("policy_version") != POLICY_VERSION
                or durable.get("initialized") is not True
                or durable.get("needs_inventory") is not False
                or sum(item.get("initial_inventory") is True
                       for item in durable.get("observations", {}).values()) != EXPECTED_INVENTORY_SIZE):
            raise RuntimeError("Existing 60-video inventory or approved policy failed validation")

        store = YouTubeStateStore(state_file, activation_at=activation, policy_version=POLICY_VERSION,
                                  commit_hook=backend.persist)
        adapter = ChannelTabsAdapter(channel_id=CHANNEL_ID, policy=policy,
                                     observation_store=store)
        result = adapter.evaluate(cache, journal)
        if set(result.health) != {"videos", "streams"}:
            raise RuntimeError("Evaluation stopped before a complete tab capture")
        report = _summary(result, store._state["capture_seq"])
        outbox_count = len(store._state["outbox"])
        after = _sha256(tracked_files)
        if before != after:
            raise RuntimeError("Historical BIMA/YouTube caches or delivery journal changed")
        if outbox_count != 0:
            raise RuntimeError("Observation-only run found an unexpected delivery outbox record")
        report["outbox_count"] = outbox_count
        report["historical_state_unchanged"] = True
        store.record_evaluation(report)

    print("YOUTUBE_OBSERVATION_ONLY=" + json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception as exc:
        print("YouTube observation-only failed closed: " + type(exc).__name__)
        raise SystemExit(1)
