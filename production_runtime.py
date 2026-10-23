"""Independent production sources, official-channel preflight and durable CAS state.

Historical master files are reference-only. Initialization never sends. A send
claim must be remotely committed before Telegram; ambiguous outcomes never retry.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

from notifier import Notifier
from production_preflight import verify_production
from reliability import atomic_json
from scraper import BimaScraper, announcement_identity, new_announcements, _publication_date
from monitor_runner import stable_item, validate_delivery
from youtube_channel_tabs import CHANNEL_ID
from youtube_delivery import YouTubeStateStore, SendConfirmation
from youtube_eligibility import Policy, Eligibility, Knowledge
from youtube_history import parse_timestamp
from youtube_state_backend import GitHubStateBranch
from youtube_tabs_monitor import ChannelTabsAdapter
from youtube_experiment_preflight import PreflightError


STATE_BRANCH = "dikti-production-state"
POLICY = "tabs-only-v1"


def now():
    return datetime.now(timezone.utc)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def hashes():
    return {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in ("announcements_cache.json", "youtube_cache.json", "delivery_state.json")}


def validate_bima(state):
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or state.get("source") != "bima" or parse_timestamp(state.get("activation_at")) is None
            or type(state.get("initialized")) is not bool or not isinstance(state.get("inventory"), list)
            or not isinstance(state.get("first_seen"), dict) or not isinstance(state.get("outbox"), dict)):
        raise ValueError("Invalid production BIMA state")
    if any(not announcement_identity(item) for item in state["inventory"]):
        raise ValueError("Invalid BIMA inventory identity")
    if any(parse_timestamp(stamp) is None for stamp in state["first_seen"].values()):
        raise ValueError("Invalid BIMA observation timestamp")
    for key, row in state["outbox"].items():
        if (not isinstance(row, dict) or row.get("event_key") != key
                or row.get("status") not in {"reserved", "sending", "delivered", "ambiguous"}
                or row.get("attempts") not in {0, 1} or not isinstance(row.get("items"), list)
                or not isinstance(row.get("receipts"), list)):
            raise ValueError("Invalid BIMA outbox")


def bima_after_fence(item, fence, observed_at):
    """Date-only publications on activation day cannot prove post-fence release."""
    day = _publication_date(item)
    local_day = observed_at.astimezone(ZoneInfo("Asia/Jakarta")).date()
    return (day is not None and fence.astimezone(ZoneInfo("Asia/Jakarta")).date()
            < day.date() <= local_day)


def backend(source, initialize):
    return GitHubStateBranch(os.environ["GITHUB_REPOSITORY"], STATE_BRANCH,
        f".bot-state/{source}_production.json", os.environ["GITHUB_TOKEN"],
        initialize=initialize, validator=validate_bima if source == "bima" else None)


def _deliver_bima_event(state, item, notifier, checkpoint, *, at, audit=None):
    """Shared at-most-once production send: remote reservation and claim precede POST."""
    key = "bima:" + announcement_identity(item) + ":initial"
    row = state["outbox"].get(key)
    if row is None:
        row = {"event_key": key, "status": "reserved", "attempts": 0,
            "items": [stable_item(item)], "chunks": notifier._build_summary_chunks([item]),
            "sent_chunks": [], "sent_documents": [], "receipts": [], "send_files": True}
        if audit is not None:
            row["controlled_backfill"] = deepcopy(audit)
        state["outbox"][key] = row
        checkpoint()
    if row["status"] != "reserved" or row["attempts"] != 0:
        return 0, False
    row.update(status="sending", attempts=1, attempted_at=at.isoformat())
    checkpoint()  # durable remote CAS before the first Telegram POST
    def progress_checkpoint():
        row["receipts"] = deepcopy(notifier.last_delivery_receipts)
        checkpoint()
    try:
        success = notifier.send_notification([item], progress=row, checkpoint=progress_checkpoint)
    except Exception:
        success = False
    row["receipts"] = deepcopy(notifier.last_delivery_receipts)
    expected = len(row["chunks"]) + len(item.get("documents", []))
    confirmed = success is True and len(row["receipts"]) == expected and all(
        SendConfirmation(r.get("http_status"), r.get("ok"), r.get("message_id")).confirmed
        for r in row["receipts"])
    row["status"] = "delivered" if confirmed else "ambiguous"
    checkpoint()
    if not confirmed:
        raise RuntimeError("BIMA outcome ambiguous; automatic replay prohibited")
    return len(row["receipts"]), True


def bima_cycle(state, current, historical, notifier, checkpoint, *, enabled, at):
    validate_bima(state)
    report = {"source": "bima", "visible": len(current), "startup": not state["initialized"],
              "eligible": 0, "hold": 0, "confirmed_messages": 0, "delivered_events": 0,
              "sending": "ON" if enabled else "OFF", "pdf": "ON"}
    if not state["initialized"]:
        state["inventory"] = [stable_item(item) for item in current]
        state["initialized"] = True
        checkpoint()
        report["initial_inventory"] = len(current)
        return report
    for record in state["outbox"].values():
        if record["status"] == "sending":
            record["status"] = "ambiguous"
            record["last_error"] = "UNKNOWN_AFTER_RESTART"
    checkpoint()
    suppressed = historical + state["inventory"] + [row["items"][0]
        for row in state["outbox"].values() if row["status"] != "reserved"]
    candidates = new_announcements(current, suppressed)
    fence = parse_timestamp(state["activation_at"])
    for item in candidates:
        key = "bima:" + announcement_identity(item) + ":initial"
        first_seen = state["first_seen"].setdefault(key, at.isoformat())
        if (not bima_after_fence(item, fence, at)
                or at - parse_timestamp(first_seen) > timedelta(hours=24)):
            report["hold"] += 1
            continue
        report["eligible"] += 1
        if not enabled:
            continue
        messages, delivered = _deliver_bima_event(state, item, notifier, checkpoint, at=at)
        report["confirmed_messages"] += messages
        report["delivered_events"] += int(delivered)
    checkpoint()
    return report


def youtube_cycle(store, adapter, cache, journal, notifier, *, enabled, startup):
    evaluation = adapter.evaluate(cache, journal)
    if evaluation.errors or any(row.get("health") != "HEALTHY" for row in evaluation.health.values()) or set(evaluation.health) != {"videos", "streams"}:
        raise RuntimeError("YouTube source capture unhealthy; no production delivery")
    report = {"source": "youtube", "startup": startup, "sending": "ON" if enabled else "OFF",
        "visible": {tab: evaluation.health[tab].get("detected_count") for tab in ("videos", "streams")},
        "eligible": sum(d.eligibility is Eligibility.ELIGIBLE for d in evaluation.decisions),
        "hold": sum(d.eligibility is Eligibility.HOLD_FOR_REVIEW for d in evaluation.decisions),
        "confirmed_messages": 0, "delivered_events": 0}
    if startup or not enabled:
        return report
    store.recover_expired_sends(now=now())
    decisions = {d.video_id: d for d in evaluation.decisions}
    for item in evaluation.preview_items:
        decision = decisions[item["video_id"]]
        if decision.eligibility is not Eligibility.ELIGIBLE or decision.knowledge is not Knowledge.UNKNOWN:
            continue
        if len(notifier._build_summary_chunks([item])) != 1:
            continue
        record = store.reserve(decision, item, now=now())
        if record["status"] != "reserved":
            continue
        def sender(payload, ticket):
            ok = notifier.send_youtube_production_notification(payload, ticket)
            receipts = notifier.last_delivery_receipts
            if ok is True and len(receipts) == 1:
                r = receipts[0]
                return SendConfirmation(r.get("http_status"), r.get("ok"), r.get("message_id"))
            return None
        status = store.send_once(decision.event_key, sender, enabled=True, now=now())
        if status != "delivered":
            raise RuntimeError("YouTube outcome ambiguous; operator reconciliation required")
        report["confirmed_messages"] += 1
        report["delivered_events"] += 1
        break  # one new event per cycle; no retries or forced backfill
    return report


def run(source, mode):
    if source not in {"bima", "youtube"} or mode not in {"run", "initialize", "preflight", "backfill-1454"}:
        raise ValueError("Invalid production operation")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Production runtime requires GitHub Actions")
    if mode == "backfill-1454" and (source != "bima"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise ValueError("The single-target exception is manual and BIMA-only")
    config = verify_production()
    if mode == "preflight":
        print("PRODUCTION_PREFLIGHT=PASS official_channel=verified bot_can_post=true")
        return 0
    enabled = (os.environ.get("PRODUCTION_AUTO_SEND", "") == "true"
               and os.environ.get("PRODUCTION_SOURCE_ENABLED", "") == "true")
    at = now()
    config.update(telegram_send_files=True, youtube_delivery_enabled=True,
        youtube_delivery_send_enabled=enabled, youtube_production_send_enabled=enabled)
    notifier = Notifier(config)
    cache, journal = read("youtube_cache.json"), read("delivery_state.json")
    validate_delivery(journal)
    if len(read("announcements_cache.json")) != 90 or len(cache) != 129 or len(journal["youtube_rss_cutover"]["baseline_ids"]) != 133:
        raise RuntimeError("Production historical reference integrity mismatch")
    with tempfile.TemporaryDirectory(prefix="dikti-production-") as temporary:
        path = Path(temporary) / "state.json"
        remote = backend(source, mode == "initialize")
        loaded = remote.load(path)
        if source == "bima":
            if loaded:
                state = read(path)
            else:
                state = {"schema_version": 1, "source": "bima", "activation_at": at.isoformat(),
                    "initialized": False, "inventory": [], "first_seen": {}, "outbox": {}}
            def checkpoint():
                validate_bima(state)
                atomic_json(path, state)
                remote.persist(path)
            if not loaded:
                checkpoint()
            if mode == "initialize" and state["initialized"]:
                print("PRODUCTION_INITIALIZATION=ALREADY_EXISTS no_overwrite=true")
                return 0
            current = BimaScraper().fetch_announcements()
            history = read("announcements_cache.json") + [item for batch in journal["batches"]
                for item in batch["items"] if item.get("source") != "youtube"]
            if mode == "backfill-1454":
                if not enabled:
                    raise PreflightError("BIMA_BACKFILL_PRODUCTION_SEND_SWITCH_OFF")
                from bima_controlled_backfill import perform
                report = perform(state, current, read("announcements_cache.json"), journal,
                                 notifier, checkpoint, at=at)
            else:
                report = bima_cycle(state, current, history, notifier, checkpoint,
                    enabled=enabled and mode != "initialize", at=at)
            report["activation_at"] = state["activation_at"]
        else:
            if loaded:
                data = read(path)
                activation = parse_timestamp(data["activation_at"])
                if data["policy_version"] != POLICY:
                    raise RuntimeError("Production policy version mismatch")
            else:
                activation = at
            store = YouTubeStateStore(path, activation_at=activation, policy_version=POLICY)
            store.set_commit_hook(remote.persist)
            if not loaded:
                store.flush()
            if mode == "initialize" and store._state["initialized"]:
                print("PRODUCTION_INITIALIZATION=ALREADY_EXISTS no_overwrite=true")
                return 0
            startup = not store._state["initialized"] or mode == "initialize"
            policy = Policy(CHANNEL_ID, POLICY, scope="production", activation_at=activation,
                window=timedelta(hours=24), ttl=timedelta(hours=4), skew=timedelta(minutes=5),
                capture_interval=timedelta(minutes=10), approved=True, automatic_tabs_verification=True)
            adapter = ChannelTabsAdapter(channel_id=CHANNEL_ID, policy=policy, observation_store=store)
            report = youtube_cycle(store, adapter, cache, journal, notifier,
                                   enabled=enabled, startup=startup)
            report.update(activation_at=store._state["activation_at"], capture_seq=store._state["capture_seq"],
                initial_inventory=sum(row["initial_inventory"] for row in store._state["observations"].values()))
        print("PRODUCTION_RUN=" + json.dumps(report, sort_keys=True))
    return 0


def main():
    before = hashes()
    try:
        return run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "run")
    finally:
        if before != hashes():
            raise RuntimeError("Historical master state changed")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreflightError as exc:
        print("PRODUCTION_FAIL_CLOSED=" + exc.code)
        raise SystemExit(1)
    except Exception as exc:
        print("PRODUCTION_FAIL_CLOSED=" + type(exc).__name__)
        raise SystemExit(1)
