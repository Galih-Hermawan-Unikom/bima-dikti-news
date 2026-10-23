"""Persistent tabs observation/outbox tests; all identities and receipts are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import youtube_channel_tabs as tabs
import youtube_eligibility as e
from monitor_runner import run_once
from notifier import Notifier
from reliability import atomic_json
from youtube_delivery import DeliveryTicket, SendConfirmation, YouTubeStateStore
from youtube_review_evidence import SignedReviewVerifier
from youtube_tabs_monitor import ChannelTabsAdapter, history_fingerprint


NOW = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
ACTIVATION = NOW - timedelta(hours=1)
VID = "video000001"
VID2 = "video000002"
KEY = b"synthetic-test-authority-key-only-32bytes"


def store_at(path, *, policy="test-tabs-v1", activation=ACTIVATION, lease=timedelta(minutes=3)):
    return YouTubeStateStore(path, activation_at=activation, policy_version=policy, send_lease=lease)


def observation(video_id, tab="videos", title="Synthetic video"):
    return {"video_id": video_id, "source_tabs": [tab], "title": title,
        "url": "https://www.youtube.com/watch?v=" + video_id}


def decision(video_id=VID, eligible=True):
    return e.Decision(video_id, f"youtube:video:{video_id}:initial", e.Knowledge.UNKNOWN,
        e.Eligibility.ELIGIBLE if eligible else e.Eligibility.HOLD_FOR_REVIEW,
        e.DeliveryState.NONE, ("SYNTHETIC_FIXTURE",), requires_atomic_reservation=eligible)


def delivery_item(video_id=VID):
    return {"source": "youtube", "kind": "video", "video_id": video_id,
        "title": "Synthetic video", "url": "https://www.youtube.com/watch?v=" + video_id,
        "first_seen_at": NOW.isoformat(), "delivery_format": "youtube-tabs-preview-v1"}


def test_startup_inventory_persists_and_never_becomes_new(tmp_path):
    path = tmp_path / "youtube_delivery.json"
    store_at(path).record_capture([observation(VID)], NOW, healthy=True)
    first = store_at(path).observation(VID)
    assert first["first_seen_at"] == NOW.isoformat()
    assert first["initial_inventory"] is True
    for minute in (12, 24):
        store_at(path).record_capture([observation(VID)], NOW + timedelta(minutes=minute), healthy=True)
    restarted = store_at(path).observation(VID)
    assert restarted["first_seen_at"] == NOW.isoformat()
    assert restarted["initial_inventory"] is True
    assert [c["tabs"] for c in restarted["captures"]] == [["videos"], ["videos"]]


def test_post_inventory_first_seen_and_two_captures_survive_restart(tmp_path):
    path = tmp_path / "youtube_delivery.json"
    store_at(path).record_capture([], NOW, healthy=True)
    store_at(path).record_capture([observation(VID)], NOW + timedelta(minutes=12), healthy=True)
    first_seen = store_at(path).observation(VID)["first_seen_at"]
    assert first_seen == (NOW + timedelta(minutes=12)).isoformat()
    assert store_at(path).observation(VID)["initial_inventory"] is False
    store_at(path).record_capture([observation(VID)], NOW + timedelta(minutes=24), healthy=True)
    persisted = store_at(path).observation(VID)
    assert persisted["first_seen_at"] == first_seen
    assert [c["tabs"] for c in persisted["captures"]] == [["videos"], ["videos"]]
    assert persisted["captures"][-1]["seq"] == persisted["captures"][0]["seq"] + 1


def test_unhealthy_gap_breaks_stability_and_quarantines_new_ids(tmp_path):
    store = store_at(tmp_path / "state.json")
    store.record_capture([], NOW, healthy=True)
    store.record_capture([observation(VID)], NOW + timedelta(minutes=10), healthy=True)
    store.record_capture([], NOW + timedelta(minutes=20), healthy=False)
    store.record_capture([observation(VID), observation(VID2, "streams")],
                         NOW + timedelta(minutes=30), healthy=True)
    prior = store.observation(VID)
    late = store.observation(VID2)
    assert prior["first_seen_at"] == (NOW + timedelta(minutes=10)).isoformat()
    assert prior["captures"][-2]["healthy"] is False
    assert late["initial_inventory"] is True
    assert late["first_seen_at"] == (NOW + timedelta(minutes=30)).isoformat()


def test_non_monotonic_capture_fails_without_state_mutation(tmp_path):
    path = tmp_path / "state.json"
    store = store_at(path)
    store.record_capture([], NOW, healthy=True)
    before = store.observation_ids()
    with pytest.raises(ValueError, match="Non-monotonic"):
        store.record_capture([observation(VID)], NOW - timedelta(seconds=1), healthy=True)
    assert store.observation_ids() == before


def test_observation_evaluation_result_is_checkpointed_with_capture_sequence(tmp_path):
    path = tmp_path / "evaluation-state.json"
    store = store_at(path)
    store.record_capture([], NOW, healthy=True)
    result = {"version": 1, "evaluated_at": NOW.isoformat(), "capture_seq": 1,
        "health": {"videos": "HEALTHY", "streams": "HEALTHY"},
        "counts": {"checked": 0, "videos_tab": 0, "streams_tab": 0, "known": 0,
            "unknown": 0, "unverified": 0, "new_candidates": 0, "new_eligible": 0,
            "new_hold": 0, "eligible": 0, "hold": 0, "rejected": 0, "already_delivered": 0},
        "errors": [], "decisions": []}
    store.record_evaluation(result)
    assert store_at(path)._state["last_evaluation"] == result
    with pytest.raises(ValueError, match="latest capture"):
        store.record_evaluation(dict(result, capture_seq=0))
    assert store_at(path)._state["last_evaluation"] == result


def test_reservation_is_atomic_idempotent_and_only_for_eligible_decision(tmp_path):
    store = store_at(tmp_path / "state.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.reserve(decision(), delivery_item(), now=NOW), range(8)))
    assert sum(result["created"] for result in results) == 1
    assert {result["status"] for result in results} == {"reserved"}
    with pytest.raises(ValueError, match="ELIGIBLE"):
        store.reserve(decision(eligible=False), delivery_item(), now=NOW)
    with pytest.raises(ValueError, match="payload conflict"):
        store.reserve(decision(), dict(delivery_item(), title="Changed payload"), now=NOW)


def test_disabled_send_does_not_call_sender_and_delivered_is_never_replayed(tmp_path):
    store = store_at(tmp_path / "state.json")
    store.reserve(decision(), delivery_item(), now=NOW)
    sender = Mock(return_value=SendConfirmation(200, True, 44))
    assert store.send_once(decision().event_key, sender, now=NOW) == "disabled"
    assert sender.call_count == 0
    assert store.send_once(decision().event_key, sender, enabled=True, now=NOW) == "delivered"
    assert store.send_once(decision().event_key, sender, enabled=True, now=NOW) == "delivered"
    assert sender.call_count == 1
    assert store.outbox_record(decision().event_key)["message_id"] == 44


@pytest.mark.parametrize("sender_result", [None, SendConfirmation(503, False, None), SendConfirmation(200, True, None)])
def test_unconfirmed_network_result_becomes_terminal_ambiguous(tmp_path, sender_result):
    store = store_at(tmp_path / "state.json")
    store.reserve(decision(), delivery_item(), now=NOW)
    sender = Mock(return_value=sender_result)
    assert store.send_once(decision().event_key, sender, enabled=True, now=NOW) == "ambiguous"
    assert store.send_once(decision().event_key, sender, enabled=True, now=NOW + timedelta(minutes=5)) == "ambiguous"
    assert sender.call_count == 1


def test_network_exception_is_ambiguous_and_never_retried(tmp_path):
    store = store_at(tmp_path / "state.json")
    store.reserve(decision(), delivery_item(), now=NOW)
    sender = Mock(side_effect=TimeoutError("synthetic timeout"))
    assert store.send_once(decision().event_key, sender, enabled=True, now=NOW) == "ambiguous"
    assert store.outbox_record(decision().event_key)["last_error"] == "TimeoutError"
    assert store.send_once(decision().event_key, sender, enabled=True, now=NOW + timedelta(minutes=5)) == "ambiguous"
    assert sender.call_count == 1


def test_crash_after_claim_recovers_as_ambiguous_without_resend(tmp_path):
    path = tmp_path / "state.json"
    store = store_at(path)
    store.reserve(decision(), delivery_item(), now=NOW)
    sender = Mock(side_effect=SystemExit())
    with pytest.raises(SystemExit):
        store.send_once(decision().event_key, sender, enabled=True, now=NOW)
    assert store_at(path).outbox_record(decision().event_key)["status"] == "sending"
    reopened = store_at(path)
    assert reopened.recover_expired_sends(now=NOW + timedelta(minutes=4)) == 1
    assert reopened.send_once(decision().event_key, sender, enabled=True, now=NOW + timedelta(minutes=4)) == "ambiguous"
    assert sender.call_count == 1


def _page(tab, ids, *, badges=(), relative_text=()):
    return tabs.ChannelTabPage(tab, [{"source": "youtube", "observation_format": tabs.FORMAT,
        "video_id": vid, "title": "Synthetic video", "channel_title": "Fixture",
        "url": "https://www.youtube.com/watch?v=" + vid, "thumbnail": "", "source_tabs": [tab],
        "source_metadata": {"relative_text": list(relative_text), "badges": list(badges), "schedule_timezone": None}}
        for vid in ids], False, empty_verified=not ids)


def _review(adapter, cache, journal, now, first_seen):
    payload = {"schema": "operator-youtube-tabs-review-v1", "video_id": VID,
        "channel_id": tabs.CHANNEL_ID, "scope": adapter.policy.scope,
        "policy_version": adapter.policy.version, "state_fingerprint": history_fingerprint(cache, journal),
        "category": "REGULAR", "readiness": "PUBLIC_RELEASED",
        "source_url": "https://www.youtube.com/watch?v=" + VID, "reviewer": "SYNTHETIC_TEST_ONLY",
        "history_review_reference": "synthetic-fixture", "observed_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(), "ended_at": None,
        "first_seen_at": first_seen.isoformat(), "approved": True}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {"key_id": "test", "payload": payload,
        "signature": hmac.new(KEY, raw, hashlib.sha256).hexdigest()}


def test_adapter_uses_persisted_observation_after_restart_and_pending_journal_holds(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(tabs, "datetime", SimpleNamespace(now=lambda tz: clock[0]))
    path = tmp_path / "state.json"
    cache = []
    journal = {"version": 1, "batches": [], "youtube_rss_cutover": {
        "version": 1, "cutover_at": "2026-10-21T00:00:00Z", "baseline_ids": []}}
    policy = e.Policy(tabs.CHANNEL_ID, "restart-test", activation_at=ACTIVATION,
        window=timedelta(hours=24), ttl=timedelta(minutes=30), skew=timedelta(minutes=5),
        capture_interval=timedelta(minutes=10), approved=True)
    keyring = SignedReviewVerifier({"test": KEY}, frozenset({"www.youtube.com"}))
    reader = Mock()
    present = {"videos": [], "streams": []}
    reader.fetch.side_effect = lambda tab: _page(tab, present[tab])
    store = store_at(path, policy=policy.version)

    def adapter(reviews=None, state_store=None):
        return ChannelTabsAdapter(tabs_reader=reader, policy=policy, reviews=reviews,
            review_verifier=keyring, clock=lambda: clock[0], observation_store=state_store or store_at(path, policy=policy.version))

    assert adapter().evaluate(cache, journal).decisions == ()  # empty startup inventory
    clock[0] += timedelta(minutes=12)
    present["videos"] = [VID]
    assert next(d for d in adapter().evaluate(cache, journal).decisions if d.video_id == VID).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    seen = datetime.fromisoformat(store_at(path, policy=policy.version).observation(VID)["first_seen_at"])
    review = _review(adapter(), cache, journal, clock[0], seen)
    clock[0] += timedelta(minutes=12)
    restarted = adapter({VID: review})
    outcome = restarted.evaluate(cache, journal)
    assert next(d for d in outcome.decisions if d.video_id == VID).eligibility is e.Eligibility.ELIGIBLE
    assert outcome.preview_items[0]["first_seen_at"] == seen.isoformat()

    pending = deepcopy(journal)
    pending["batches"] = [{"event_keys": [f"youtube:video:{VID}:initial"],
        "items": [{"source": "youtube", "video_id": VID}], "chunks": ["x"],
        "sent_chunks": [], "sent_documents": [], "delivered": False,
        "send_files": False, "attempts": 0}]
    clock[0] += timedelta(minutes=12)
    held = adapter({VID: review}).evaluate(cache, pending)
    assert next(d for d in held.decisions if d.video_id == VID).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert not held.preview_items


@pytest.mark.parametrize("case_name", [
    "regular_video", "completed_replay_badge", "completed_replay_text",
    "live_stream", "upcoming_stream", "shorts_marker", "ambiguous_stream",
])
def test_automatic_tabs_verification_is_conservative_and_restart_safe(tmp_path, monkeypatch, case_name):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "youtube_automatic_verification.json")
                         .read_text(encoding="utf-8"))["cases"][case_name]
    clock = [NOW]
    monkeypatch.setattr(tabs, "datetime", SimpleNamespace(now=lambda tz: clock[0]))
    tab, present = fixture["tab"], {"videos": [], "streams": []}
    reader = Mock()
    reader.fetch.side_effect = lambda requested: _page(requested,
        present[requested] if requested == tab else [], badges=fixture["badges"],
        relative_text=fixture["relative_text"])
    policy = e.Policy(tabs.CHANNEL_ID, "auto-tabs-test-v1", activation_at=ACTIVATION,
        window=timedelta(hours=24), ttl=timedelta(hours=4), skew=timedelta(minutes=5),
        capture_interval=timedelta(minutes=10), approved=True, automatic_tabs_verification=True)
    cache, journal = [], {"version": 1, "batches": [], "youtube_rss_cutover": {
        "version": 1, "cutover_at": "2026-10-21T00:00:00Z", "baseline_ids": []}}
    path = tmp_path / (case_name + ".json")
    state = store_at(path, policy=policy.version)
    adapter = lambda store: ChannelTabsAdapter(tabs_reader=reader, policy=policy,
        clock=lambda: clock[0], observation_store=store)
    adapter(state).evaluate(cache, journal)  # Empty startup inventory.
    clock[0] += timedelta(minutes=12)
    present[tab] = [VID]
    first = adapter(store_at(path, policy=policy.version)).evaluate(cache, journal)
    first_decision = next(d for d in first.decisions if d.video_id == VID)
    assert first_decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW
    clock[0] += timedelta(minutes=12)
    # Recreate the adapter/store to prove evidence is durable across a process restart.
    result = adapter(store_at(path, policy=policy.version)).evaluate(cache, journal)
    decision_after_restart = next(d for d in result.decisions if d.video_id == VID)
    assert decision_after_restart.eligibility.value == fixture["expected"]
    assert decision_after_restart.delivery_authorized is False
    assert decision_after_restart.enqueue is False
    if fixture["expected"] == "ELIGIBLE":
        assert decision_after_restart.reason_codes == ("AUTOMATIC_TABS_POLICY_ELIGIBLE_NOT_AUTHORIZED",)
        assert len(result.preview_items) == 1
    else:
        assert result.preview_items == ()


def test_automatic_tabs_end_to_end_mock_outbox_is_idempotent(tmp_path, monkeypatch, monitors):
    clock = [NOW]
    monkeypatch.setattr(tabs, "datetime", SimpleNamespace(now=lambda tz: clock[0]))
    root = tmp_path
    atomic_json(root / "youtube_cache.json", [])
    atomic_json(root / "delivery_state.json", {"version": 1, "batches": [], "youtube_rss_cutover": {
        "version": 1, "cutover_at": "2026-10-21T00:00:00Z", "baseline_ids": []}})
    atomic_json(root / "announcements_cache.json", [])
    policy = e.Policy(tabs.CHANNEL_ID, "auto-outbox-test-v1", activation_at=ACTIVATION,
        window=timedelta(hours=24), ttl=timedelta(hours=4), skew=timedelta(minutes=5),
        capture_interval=timedelta(minutes=10), approved=True, automatic_tabs_verification=True)
    reader = Mock(); present = {"videos": [], "streams": []}
    reader.fetch.side_effect = lambda tab: _page(tab, present[tab], relative_text=("2 days ago",))
    path = root / "youtube_delivery_state.json"
    spy = {"calls": [], "bima": 0}
    class NotifierSpy:
        config = {"telegram_send_files": False}
        def validate_youtube_experiment_destination(self): return True
        def _build_summary_chunks(self, items): return ["mock summary"]
        def send_notification(self, items, progress=None, checkpoint=None): spy["bima"] += 1; return True
        def send_youtube_outbox_notification(self, item, ticket):
            state_now = json.loads(path.read_text(encoding="utf-8"))
            record = state_now["outbox"][ticket.event_key]
            assert record["status"] == "sending"  # reservation/claim checkpoint precedes mock call
            spy["calls"].append(ticket.event_key)
            self.last_delivery_receipts = [{"http_status": 200, "ok": True, "message_id": 8801}]
            return True
    notifier = NotifierSpy()
    config = {"youtube_monitor_enabled": True, "youtube_observation_enabled": True,
        "youtube_delivery_enabled": True, "youtube_delivery_send_enabled": True,
        "youtube_automatic_tabs_verification_enabled": True, "youtube_channel_id": tabs.CHANNEL_ID,
        "youtube_activation_at": ACTIVATION.isoformat(), "youtube_policy_version": policy.version,
        "youtube_policy_approved": True, "youtube_window_seconds": 86400,
        "youtube_ttl_seconds": 14400, "youtube_skew_seconds": 300,
        "youtube_capture_interval_seconds": 600, "notification_method": "telegram",
        "youtube_review_evidence_json": "", "telegram_send_files": False}
    durable_checkpoints = []
    def run():
        store = store_at(path, policy=policy.version)
        store.set_commit_hook(lambda state_path: durable_checkpoints.append(
            json.loads(state_path.read_text(encoding="utf-8"))))
        adapter = ChannelTabsAdapter(tabs_reader=reader, policy=policy, clock=lambda: clock[0],
            observation_store=store)
        return run_once(config, state_dir=root, scraper=monitors[0], notifier=notifier,
                        youtube_adapter=adapter, youtube_send_limit=1, youtube_experiment_only=True)

    run()  # Empty initial inventory.
    clock[0] += timedelta(minutes=12); present["videos"] = [VID]
    run()  # First post-activation appearance.
    clock[0] += timedelta(minutes=12)
    sent = run()
    assert sent.youtube_eligibility is not None, (sent.errors, sent.sources)
    assert sent.youtube_eligibility["outbox"][0]["status"] == "delivered"
    assert spy["calls"] == [f"youtube:video:{VID}:initial"]
    clock[0] += timedelta(minutes=12)
    again = run()
    assert again.youtube_eligibility["outbox"][0]["status"] == "delivered"
    assert spy["calls"] == [f"youtube:video:{VID}:initial"]
    assert any(record["outbox"].get(f"youtube:video:{VID}:initial", {}).get("status") == "reserved"
               for record in durable_checkpoints)
    assert spy["bima"] == 0  # experiment-only remains scoped to YouTube; BIMA is not invoked here.


def test_automatic_tabs_fails_closed_when_either_tab_fails(tmp_path, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(tabs, "datetime", SimpleNamespace(now=lambda tz: clock[0]))
    present = {"videos": [], "streams": []}
    reader = Mock()
    def fetch(tab):
        if tab == "streams" and present["videos"]:
            raise tabs.VideosPageError("NETWORK_ERROR", parsing_errors=0)
        return _page(tab, present[tab], relative_text=("2 days ago",))
    reader.fetch.side_effect = fetch
    policy = e.Policy(tabs.CHANNEL_ID, "auto-tab-failure-v1", activation_at=ACTIVATION,
        window=timedelta(hours=24), ttl=timedelta(hours=4), skew=timedelta(minutes=5),
        capture_interval=timedelta(minutes=10), approved=True, automatic_tabs_verification=True)
    journal = {"version": 1, "batches": [], "youtube_rss_cutover": {
        "version": 1, "cutover_at": "2026-10-21T00:00:00Z", "baseline_ids": []}}
    path = tmp_path / "tab-failure.json"
    store_at(path, policy=policy.version).record_capture([], NOW, healthy=True)
    clock[0] += timedelta(minutes=12); present["videos"] = [VID]
    adapter = ChannelTabsAdapter(tabs_reader=reader, policy=policy, clock=lambda: clock[0],
        observation_store=store_at(path, policy=policy.version))
    adapter.evaluate([], journal)
    clock[0] += timedelta(minutes=12)
    result = adapter.evaluate([], journal)
    assert result.health["streams"]["health"] == "FAILED"
    assert result.preview_items == ()
    decision = next(d for d in result.decisions if d.video_id == VID)
    assert decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert not decision.requires_atomic_reservation


def test_runner_outbox_end_to_end_notifier_mock_and_duplicate_run(tmp_path, monkeypatch, monitors, announcement):
    clock = [NOW]
    monkeypatch.setattr(tabs, "datetime", SimpleNamespace(now=lambda tz: clock[0]))
    db = tmp_path / "youtube_delivery.json"
    cache = []
    journal = {"version": 1, "batches": [], "youtube_rss_cutover": {
        "version": 1, "cutover_at": "2026-10-21T00:00:00Z", "baseline_ids": []}}
    atomic_json(tmp_path / "youtube_cache.json", cache)
    atomic_json(tmp_path / "delivery_state.json", journal)
    atomic_json(tmp_path / "announcements_cache.json", [])
    policy = e.Policy(tabs.CHANNEL_ID, "runner-test", activation_at=ACTIVATION,
        window=timedelta(hours=24), ttl=timedelta(minutes=30), skew=timedelta(minutes=5),
        capture_interval=timedelta(minutes=10), approved=True)
    store = store_at(db, policy=policy.version)
    reader = Mock(); present = {"videos": [], "streams": []}
    reader.fetch.side_effect = lambda tab: _page(tab, present[tab])

    class SpyNotifier:
        config = {"telegram_send_files": False}
        def __init__(self): self.yt_calls, self.bima_calls = [], 0; self.last_delivery_receipts = []
        def validate_youtube_experiment_destination(self): return True
        def _build_summary_chunks(self, items): return ["one synthetic summary"]
        def send_notification(self, items, progress=None, checkpoint=None): self.bima_calls += 1; return True
        def _document_name(self, item): return item.get("name", "")
        def _send_console(self, items): return None
        def send_youtube_outbox_notification(self, item, ticket):
            self.yt_calls.append((deepcopy(item), ticket))
            self.last_delivery_receipts = [{"http_status": 200, "ok": True, "message_id": 9001}]
            return True
    notifier = SpyNotifier()
    config = {"youtube_monitor_enabled": True, "youtube_observation_enabled": True,
        "youtube_delivery_enabled": True, "youtube_delivery_send_enabled": True,
        "youtube_channel_id": tabs.CHANNEL_ID, "notification_method": "telegram",
        "youtube_review_hmac_key": KEY.decode(), "youtube_review_source_hosts": ["www.youtube.com"],
        "youtube_review_key_id": "test",
        "youtube_review_evidence_json": "", "telegram_send_files": False}

    def run(reviews=None, experiment_only=False):
        config["youtube_review_evidence_json"] = json.dumps({"reviews": list((reviews or {}).values())})
        observation_store = store_at(db, policy=policy.version)
        observation_store.set_commit_hook(lambda path: None)  # mock durable checkpoint for Actions environment
        adapter = ChannelTabsAdapter(tabs_reader=reader, policy=policy, clock=lambda: clock[0],
            observation_store=observation_store)
        return run_once(config, dry_run=False, state_dir=tmp_path, scraper=monitors[0],
            notifier=notifier, youtube_adapter=adapter,
            youtube_send_limit=1 if experiment_only else None,
            youtube_experiment_only=experiment_only)

    run()  # healthy empty startup inventory; BIMA uses its own mocked send path
    assert notifier.bima_calls == 1 and notifier.yt_calls == []
    clock[0] += timedelta(minutes=12); present["videos"] = [VID]
    first = run()
    youtube_source = next(source for source in first.sources if source.source == "youtube")
    assert youtube_source.status == "ELIGIBILITY_PREVIEW", first.sources
    assert first.youtube_eligibility.get("outbox", []) == [], first.sources
    first_seen = datetime.fromisoformat(store_at(db, policy=policy.version).observation(VID)["first_seen_at"])
    signed = _review(ChannelTabsAdapter(policy=policy), cache,
        json.loads((tmp_path / "delivery_state.json").read_text(encoding="utf-8")), clock[0], first_seen)
    clock[0] += timedelta(minutes=12)
    delivered = run({VID: signed}, experiment_only=True)
    event = f"youtube:video:{VID}:initial"
    assert delivered.youtube_eligibility["outbox"][0]["status"] == "delivered"
    assert len(notifier.yt_calls) == 1 and notifier.yt_calls[0][1].event_key == event
    clock[0] += timedelta(minutes=1)
    repeated = run({VID: signed})
    assert repeated.youtube_eligibility["decisions"][0]["eligibility"] == "HOLD_FOR_REVIEW"
    assert store_at(db, policy=policy.version).outbox_record(event)["status"] == "delivered"
    assert len(notifier.yt_calls) == 1
    assert notifier.bima_calls == 1  # BIMA id/cache/journal continue independently.


def test_default_feature_flags_are_disabled(monkeypatch):
    from app_config import load_config
    cfg = load_config(config_path="missing-test-config.json", load_env=False, include_secrets=False)
    assert cfg["youtube_observation_enabled"] is False
    assert cfg["youtube_delivery_enabled"] is False
    assert cfg["youtube_delivery_send_enabled"] is False
    assert cfg["youtube_automatic_tabs_verification_enabled"] is False


def test_review_preview_loads_only_review_secrets(monkeypatch):
    from app_config import load_config
    monkeypatch.setenv("YOUTUBE_REVIEW_HMAC_KEY", "synthetic-review-key-material-long-enough")
    monkeypatch.setenv("YOUTUBE_REVIEW_EVIDENCE_JSON", '{"reviews": []}')
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "NEVER_READ_FOR_DRY_RUN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "NEVER_READ_FOR_DRY_RUN")
    cfg = load_config(config_path="missing-test-config.json", load_env=False,
                      include_secrets=False, include_review_secrets=True)
    assert cfg["youtube_review_hmac_key"] == "synthetic-review-key-material-long-enough"
    assert cfg["youtube_review_evidence_json"] == '{"reviews": []}'
    assert cfg["telegram_bot_token"] == ""
    assert cfg["telegram_chat_id"] == ""


def test_notifier_requires_atomic_ticket_and_records_api_receipt(config, monkeypatch):
    import requests
    from dataclasses import replace
    config.update({
        "youtube_delivery_enabled": True,
        "youtube_delivery_send_enabled": True,
        "youtube_experiment_chat_id": "synthetic-experiment-chat",
        "youtube_experiment_chat_id_sha256": hashlib.sha256(b"synthetic-experiment-chat").hexdigest(),
    })
    notifier = Notifier(config)
    item = delivery_item()
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ticket = DeliveryTicket(decision().event_key, VID, "a" * 32, hashlib.sha256(raw).hexdigest())
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "result": {"message_id": 991}}
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)

    assert notifier.send_notification([item]) is False  # generic delivery still rejects tabs previews
    assert post.call_count == 0
    assert notifier.send_youtube_outbox_notification(item, ticket) is True
    assert post.call_count == 1
    assert notifier.last_delivery_receipts == [{"method": "sendMessage", "http_status": 200,
        "ok": True, "message_id": 991}]
    assert notifier.send_youtube_outbox_notification(item, replace(ticket, payload_sha256="0" * 64)) is False
    assert post.call_count == 1
    config["youtube_experiment_chat_id_sha256"] = "0" * 64
    assert notifier.send_youtube_outbox_notification(item, ticket) is False
    assert post.call_count == 1


def test_experiment_allowlist_rejects_production_destination(config, monkeypatch):
    experiment_id = "synthetic-experiment-chat"
    config.update({
        "youtube_experiment_chat_id": experiment_id,
        "youtube_experiment_chat_id_sha256": hashlib.sha256(experiment_id.encode()).hexdigest(),
        "telegram_chat_id": "synthetic-production-chat",
    })
    notifier = Notifier(config)
    assert notifier.validate_youtube_experiment_destination() is True
    config["youtube_experiment_chat_id"] = config["telegram_chat_id"]
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert notifier.validate_youtube_experiment_destination() is False


def test_experiment_preflight_checks_channel_and_post_permission(config, monkeypatch):
    import requests
    experiment_id = "synthetic-experiment-chat"
    config.update({
        "youtube_experiment_chat_id": experiment_id,
        "youtube_experiment_chat_id_sha256": hashlib.sha256(experiment_id.encode()).hexdigest(),
        "telegram_chat_id": "synthetic-production-chat",
        "telegram_bot_token": "TEST_ONLY_TOKEN",
    })
    responses = []
    for body in (
        {"ok": True, "result": {"id": experiment_id, "type": "channel"}},
        {"ok": True, "result": {"id": 7}},
        {"ok": True, "result": {"status": "administrator", "can_post_messages": True}},
    ):
        response = Mock(status_code=200)
        response.json.return_value = body
        responses.append(response)
    get = Mock(side_effect=responses)
    post = Mock(side_effect=AssertionError("preflight must not send"))
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    assert Notifier(config).preflight_youtube_experiment_destination() is True
    assert get.call_count == 3
    assert post.call_count == 0
