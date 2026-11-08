"""Offline publisher fixtures and production lifecycle/durability regression tests."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from unittest.mock import Mock

import pytest

import production_runtime as prod
import youtube_eligibility as e
import youtube_tabs_monitor as monitor
from youtube_channel_tabs import CHANNEL_ID
from youtube_delivery import DeliveryTicket, SendConfirmation, YouTubeStateStore
from notifier import Notifier
from youtube_lifecycle import (LifecycleTabsAdapter, PublicPlayerReader,
                               card_consistent, migrate, player_evidence)
from youtube_state_backend import DurableStateConflict

AT = datetime(2026, 11, 8, tzinfo=timezone.utc)
VID = "rPKcKDj0oa8"
JOURNAL = {"version": 1, "batches": [], "youtube_rss_cutover": {
    "version": 1, "cutover_at": "2026-10-20T00:00:00Z", "baseline_ids": []}}


def player(phase, at, *, end=True):
    live = {"isLiveNow": phase == "LIVE", "startTimestamp": (at - timedelta(hours=6)).isoformat()}
    if phase == "COMPLETED_REPLAY" and end:
        live["endTimestamp"] = (at - timedelta(minutes=5)).isoformat()
    return {"videoDetails": {"videoId": VID, "channelId": CHANNEL_ID, "isPrivate": False,
                "isLiveContent": True, "isLive": phase == "LIVE", "isUpcoming": phase == "UPCOMING"},
            "microformat": {"playerMicroformatRenderer": {"isUnlisted": False, "liveBroadcastDetails": live}},
            "playabilityStatus": {"status": "LIVE_STREAM_OFFLINE" if phase == "UPCOMING" else "OK"},
            "streamingData": {"formats": [{"url": "https://fixture.invalid/never-persist-this"}]}}


class Harness:
    def __init__(self, tmp_path, monkeypatch, *, fence=AT):
        self.path = tmp_path / "state.json"
        self.at, self.phase, self.visible, self.healthy = AT, "UPCOMING", False, True
        self.fence, self.end = fence, True
        self.replay_end = None
        self.policy = e.Policy(CHANNEL_ID, "tabs-only-v1", scope="production", activation_at=AT - timedelta(days=10),
            window=timedelta(hours=24), ttl=timedelta(hours=4), skew=timedelta(minutes=5),
            capture_interval=timedelta(minutes=10), approved=True, automatic_tabs_verification=True)
        self.reader = Mock()
        self.reader.fetch.side_effect = self.publisher
        self.notifier = Mock()
        self.notifier._build_summary_chunks.return_value = ["mock message"]
        self.notifier.send_youtube_production_notification.side_effect = self.send
        self.posts, self.checkpoints = [], []
        monkeypatch.setattr(monitor, "read_channel_tabs", lambda *a: self.snapshot())
        monkeypatch.setattr(prod, "now", lambda: self.at)
        self.open()

    def publisher(self, vid):
        result = player(self.phase, self.at, end=self.end)
        if self.phase == "COMPLETED_REPLAY" and self.end:
            self.replay_end = self.replay_end or (self.at - timedelta(minutes=5)).isoformat()
            result["microformat"]["playerMicroformatRenderer"]["liveBroadcastDetails"]["endTimestamp"] = self.replay_end
        return result

    def open(self):
        self.store = YouTubeStateStore(self.path, activation_at=self.policy.activation_at, policy_version=self.policy.version)
        self.store.set_commit_hook(lambda p: self.checkpoints.append(json.loads(p.read_text())))
        self.adapter = LifecycleTabsAdapter(policy=self.policy, observation_store=self.store,
            lifecycle_fence=self.fence, player_reader=self.reader, clock=lambda: self.at)

    def snapshot(self):
        return {"observed_at_utc": self.at.isoformat(), "pages": {t: {"health": "HEALTHY" if self.healthy else "FAILED"}
                for t in ("videos", "streams")}, "observations": [{"video_id": VID,
            "source_tabs": ["streams"], "title": "Penutupan PIMNAS ke-39 (synthetic chronology)",
            "url": f"https://www.youtube.com/watch?v={VID}", "title_conflict": False,
            "metadata_by_tab": {"streams": {"badges": [], "relative_text": [], "schedule_timezone": None}}}] if self.visible else []}

    def send(self, payload, ticket):
        assert self.checkpoints[-1]["outbox"][ticket.event_key]["status"] == "sending"
        assert self.checkpoints[-2]["outbox"][ticket.event_key]["status"] == "reserved"
        self.posts.append(ticket.event_key)
        self.notifier.last_delivery_receipts = [{"http_status": 200, "ok": True, "message_id": 100 + len(self.posts)}]
        return True

    def tick(self, phase=None, *, hours=1, send=True, restart=True):
        self.at += timedelta(hours=hours)
        if phase:
            self.phase = phase
            self.visible = True
        if restart:
            self.open()
        return prod.youtube_cycle(self.store, self.adapter, [], JOURNAL, self.notifier,
                                  enabled=send, startup=False)


@pytest.fixture
def h(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.tick(hours=0)  # empty startup, migrate before first observation
    return h


@pytest.mark.parametrize("phase", ["LIVE", "COMPLETED_REPLAY"])
def test_upcoming_for_days_then_phase_has_independent_window(h, phase):
    for hours in (1, 96, 1):
        h.tick("UPCOMING", hours=hours)
    first_seen = h.store.observation(VID)["first_seen_at"]
    assert h.posts == [] and h.store._state["outbox"] == {}
    h.tick(phase)
    h.tick(phase)
    assert h.posts == [f"youtube:video:{VID}:{'live' if phase == 'LIVE' else 'replay'}"]
    assert h.store.observation(VID)["first_seen_at"] == first_seen


def test_upcoming_live_replay_deliver_once_per_phase_across_restarts(h):
    for hours in (1, 72, 1):
        h.tick("UPCOMING", hours=hours)
    for phase in ("LIVE", "LIVE", "LIVE", "COMPLETED_REPLAY", "COMPLETED_REPLAY", "COMPLETED_REPLAY"):
        h.tick(phase)
    assert h.posts == [f"youtube:video:{VID}:live", f"youtube:video:{VID}:replay"]
    assert all(r["attempts"] == 1 and r["status"] == "delivered" for r in h.store._state["outbox"].values())


def test_replay_without_end_uses_observed_transition_and_keeps_actual_end_unknown(h):
    h.tick("UPCOMING")
    h.end = False
    h.tick("COMPLETED_REPLAY")
    first = h.at.isoformat()
    h.tick("COMPLETED_REPLAY")
    row = h.store._state["lifecycle"]["records"][VID]
    assert row["first_verified_replay_at"] == first
    assert row["origins"]["COMPLETED_REPLAY"] == first
    assert row["latest"]["actual_end_at"] is None
    assert h.posts == [f"youtube:video:{VID}:replay"]


@pytest.mark.parametrize("phase", ["LIVE", "COMPLETED_REPLAY"])
def test_first_detection_without_upcoming_or_live(h, phase):
    h.tick(phase)
    h.tick(phase)
    assert len(h.posts) == 1


def test_first_replay_without_end_or_prior_phase_stays_held(h):
    h.end = False
    for _ in range(3):
        h.tick("COMPLETED_REPLAY")
    assert h.posts == []


@pytest.mark.parametrize("phase", ["LIVE", "COMPLETED_REPLAY"])
def test_gap_never_treats_discovery_as_recent_transition(h, phase):
    h.tick("UPCOMING")
    h.tick(phase, hours=8)
    assert h.posts == []
    h.tick(phase)
    assert len(h.posts) == (1 if phase == "LIVE" else 0)


def test_missing_partial_and_changed_tabs_break_chronology(h):
    h.tick("UPCOMING")
    h.healthy = False
    with pytest.raises(RuntimeError, match="unhealthy"):
        h.tick("LIVE")
    h.healthy = True
    h.tick("LIVE")
    assert h.posts == []
    h.visible = False
    h.tick()
    h.visible = True
    h.tick("COMPLETED_REPLAY")
    h.tick("COMPLETED_REPLAY")
    assert h.posts == []


@pytest.mark.parametrize("hours", [0.05, 0.15])
def test_capture_interval_too_short(h, hours):
    h.tick("LIVE")
    h.tick("LIVE", hours=hours)
    assert h.posts == []


@pytest.mark.parametrize("when", ["before_reserve", "before_post"])
def test_live_ends_between_evaluation_reservation_and_post(h, when):
    h.tick("LIVE")
    h.reader.fetch.side_effect = [player("LIVE", h.at + timedelta(hours=1)),
        player("LIVE" if when == "before_post" else "COMPLETED_REPLAY", h.at + timedelta(hours=1)),
        player("COMPLETED_REPLAY", h.at + timedelta(hours=1))]
    if when == "before_post":
        with pytest.raises(RuntimeError, match="ambiguous"):
            h.tick("LIVE")
        assert h.store.outbox_record(f"youtube:video:{VID}:live")["status"] == "ambiguous"
    else:
        h.tick("LIVE")
        assert h.store._state["outbox"] == {}
    assert h.posts == []


def test_ambiguous_send_is_not_retried_after_restart(h):
    h.tick("LIVE")
    h.notifier.send_youtube_production_notification.side_effect = TimeoutError
    with pytest.raises(RuntimeError, match="ambiguous"):
        h.tick("LIVE")
    h.tick("LIVE")
    assert h.notifier.send_youtube_production_notification.call_count == 1


def test_reserved_before_restart_requires_reconciliation(h):
    h.tick("LIVE", send=False)
    h.tick("LIVE", send=False)
    # Use next healthy capture to produce an actual decision/payload.
    h.at += timedelta(hours=1)
    result = h.adapter.evaluate([], JOURNAL)
    h.store.reserve(result.decisions[0], result.preview_items[0], now=h.at)
    h.tick("LIVE")
    assert h.posts == []


def test_two_workers_stale_cas_stops_second_before_post(h, tmp_path):
    h.tick("LIVE", send=False)
    h.at += timedelta(hours=1)
    result = h.adapter.evaluate([], JOURNAL)
    duplicate = tmp_path / "second.json"
    duplicate.write_bytes(h.path.read_bytes())
    second = YouTubeStateStore(duplicate, activation_at=h.policy.activation_at, policy_version=h.policy.version)
    second.set_commit_hook(Mock(side_effect=DurableStateConflict("stale blob SHA")))
    item, decision = result.preview_items[0], result.decisions[0]
    h.store.reserve(decision, item, now=h.at)
    with pytest.raises(DurableStateConflict):
        second.reserve(decision, item, now=h.at)
    assert second._state["outbox"] == {}
    assert h.store.send_once(decision.event_key, lambda *args: SendConfirmation(200, True, 99), enabled=True, now=h.at) == "delivered"


def test_cutover_suppresses_existing_video_and_preserves_historical_fields(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, fence=None)
    h.tick("UPCOMING", hours=0)
    before = deepcopy(h.store._state)
    h.fence = h.at
    for _ in range(3):
        h.tick("LIVE")
    assert h.posts == []
    assert h.store.observation(VID)["first_seen_at"] == before["observations"][VID]["first_seen_at"]
    assert VID in h.store._state["lifecycle"]["suppressed_ids"]
    with pytest.raises(ValueError, match="mismatch"):
        migrate(h.store, AT + timedelta(hours=1), h.at)


def test_migrate_delivered_initial_state_preserves_every_legacy_field(tmp_path):
    path = tmp_path / "legacy.json"
    store = YouTubeStateStore(path, activation_at=AT-timedelta(days=10), policy_version="tabs-only-v1")
    store.record_capture([], AT-timedelta(days=8), healthy=True)
    store.record_capture([{"video_id": VID, "source_tabs": ["streams"], "title": "Legacy",
        "url": f"https://www.youtube.com/watch?v={VID}"}], AT-timedelta(days=7), healthy=True)
    decision = e.Decision(VID, f"youtube:video:{VID}:initial", e.Knowledge.UNKNOWN, e.Eligibility.ELIGIBLE,
        e.DeliveryState.NONE, (), requires_atomic_reservation=True)
    store.reserve(decision, {"source": "youtube", "video_id": VID, "title": "Legacy",
        "url": f"https://www.youtube.com/watch?v={VID}", "delivery_format": "youtube-tabs-preview-v1"}, now=AT-timedelta(days=7))
    store.send_once(decision.event_key, lambda *a: SendConfirmation(200, True, 123), enabled=True, now=AT-timedelta(days=7))
    before = deepcopy(store._state)
    migrate(store, AT, AT)
    assert {k: v for k, v in store._state.items() if k != "lifecycle"} == before
    migrate(store, AT, AT+timedelta(hours=1))
    assert store.outbox_record(decision.event_key)["status"] == "delivered"


@pytest.mark.parametrize("mutation", [
    lambda p: p["videoDetails"].update(channelId="UC"+"x"*22),
    lambda p: p["videoDetails"].update(videoId="video000002"),
    lambda p: p["videoDetails"].update(isPrivate=True),
    lambda p: p["microformat"]["playerMicroformatRenderer"].update(isUnlisted=True),
    lambda p: p["playabilityStatus"].update(status="LOGIN_REQUIRED"),
    lambda p: p.update(streamingData={}),
    lambda p: p["microformat"]["playerMicroformatRenderer"]["liveBroadcastDetails"].update(endTimestamp=AT.isoformat()),
    lambda p: p["videoDetails"].update(isUpcoming=True),
    lambda p: p["videoDetails"].update(isLive=False),
])
def test_contradictory_or_unplayable_live_is_unknown(mutation):
    p = player("LIVE", AT)
    mutation(p)
    assert player_evidence(p, VID, AT)["phase"] == "UNKNOWN"


def test_tab_and_title_are_never_live_evidence(h):
    h.reader.fetch.return_value = {}
    h.reader.fetch.side_effect = None
    h.tick("LIVE")
    h.tick("LIVE")
    assert h.posts == []


@pytest.mark.parametrize("badges,phase", [(["LIVE", "replay"], "LIVE"), (["upcoming"], "LIVE"),
    (["LIVE"], "COMPLETED_REPLAY"), (["Shorts"], "COMPLETED_REPLAY")])
def test_conflicting_card_or_shorts_never_admitted(badges, phase):
    assert not card_consistent({"metadata_by_tab": {"streams": {"badges": badges}}}, phase)


def test_no_activation_fence_never_enables_live(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, fence=None)
    h.tick(hours=0)
    h.tick("LIVE")
    h.tick("LIVE")
    h.tick("COMPLETED_REPLAY")
    h.tick("COMPLETED_REPLAY")
    assert h.posts == [] and "lifecycle" not in h.store._state


@pytest.mark.parametrize("bad", ["provenance", "policy", "baseline", "clock"])
def test_lifecycle_preserves_eligibility_gates(h, bad):
    h.tick("LIVE")
    if bad == "provenance":
        h.reader.fetch.side_effect = lambda v: {}
    elif bad == "policy":
        h.policy = replace(h.policy, approved=False)
    elif bad == "baseline":
        h.adapter._history = Mock(side_effect=ValueError)
        result = h.adapter.evaluate([], {})
        assert result.errors
        return
    else:
        # Durable source timestamps cannot move backwards across a restart.
        with pytest.raises(RuntimeError):
            h.tick("LIVE", hours=-1)
        return
    h.tick("LIVE")
    assert h.posts == []


def test_phase_reservation_rejects_stale_proof(h):
    h.tick("LIVE", send=False)
    h.at += timedelta(hours=1)
    result = h.adapter.evaluate([], JOURNAL)
    with pytest.raises(ValueError, match="evidence"):
        h.store.reserve(result.decisions[0], result.preview_items[0], now=h.at+timedelta(minutes=3))
    assert h.store._state["outbox"] == {}


def test_stream_moving_to_videos_never_becomes_regular_initial(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch)
    h.tick(hours=0)
    h.tick("LIVE")
    original = h.snapshot
    def moved():
        result = original()
        item = result["observations"][0]
        item["source_tabs"] = ["videos"]
        item["metadata_by_tab"] = {"videos": {"badges": [], "relative_text": [], "schedule_timezone": None}}
        return result
    h.snapshot = moved
    h.tick()
    h.tick()
    h.tick()
    assert h.posts == [] and h.store._state["outbox"] == {}
    assert h.store.observation(VID)["seen_on_streams"] is True


def test_shorts_badge_blocks_stream_end_to_end(h):
    original = h.snapshot
    def short():
        result = original()
        result["observations"][0]["metadata_by_tab"]["streams"]["badges"] = ["Shorts"]
        return result
    h.snapshot = short
    h.tick("LIVE")
    h.tick("LIVE")
    assert h.posts == []


@pytest.mark.parametrize("status,text", [(302, ""), (200, "consent page"),
    (200, "var ytInitialPlayerResponse = {}; var ytInitialPlayerResponse = {};"),
    (200, "var ytInitialPlayerResponse = broken")])
def test_player_structure_failure_closes_without_fallback(monkeypatch, status, text):
    response = Mock(status_code=status, content=b"fixture", text=text)
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError):
        PublicPlayerReader().fetch(VID)
    response.close.assert_called_once()


def test_public_reader_fixed_url_extracts_and_closes(monkeypatch):
    response = Mock(status_code=200, content=b"fixture")
    response.text = "<script>var ytInitialPlayerResponse = " + json.dumps(player("LIVE", AT)) + ";</script>"
    get = Mock(return_value=response)
    monkeypatch.setattr("requests.get", get)
    assert PublicPlayerReader().fetch(VID)["videoDetails"]["videoId"] == VID
    assert get.call_args.kwargs["allow_redirects"] is False
    response.close.assert_called_once()


def test_real_case_fixture_is_audited_evidence_not_transition_proof(fixtures, tmp_path):
    fixture = json.loads((fixtures / "youtube_pimnas_regression.json").read_text())
    assert fixture["observations"][VID]["first_seen_at"] == "2026-11-02T09:28:56.716699+00:00"
    path = tmp_path / "production-excerpt.json"
    path.write_text(json.dumps(fixture))
    store = YouTubeStateStore(path, activation_at=datetime.fromisoformat(fixture["activation_at"]), policy_version=fixture["policy_version"])
    before = deepcopy(store._state)
    migrate(store, AT, AT)
    assert store._state["outbox"] == before["outbox"] == {}
    assert store._state["captures"] == before["captures"]
    assert VID in store._state["lifecycle"]["suppressed_ids"]
    assert (datetime.fromisoformat(fixture["last_capture_at"]) - datetime.fromisoformat(fixture["observations"][VID]["first_seen_at"])).days >= 5


def test_real_replay_player_omits_is_live_field_and_preserves_publisher_end(fixtures):
    p = json.loads((fixtures / "youtube_pimnas_player.json").read_text())
    assert "isLive" not in p["videoDetails"]
    result = player_evidence(p, VID, AT)
    assert result["phase"] == "COMPLETED_REPLAY"
    assert result["actual_end_at"] == "2026-11-06T17:31:37+00:00"


def test_live_uses_explicit_publisher_is_live_now_when_optional_is_live_absent():
    p = player("LIVE", AT)
    del p["videoDetails"]["isLive"]
    assert player_evidence(p, VID, AT)["phase"] == "LIVE"


def test_replay_cannot_regress_into_late_live(h):
    h.tick("COMPLETED_REPLAY")
    h.tick("COMPLETED_REPLAY", send=False)
    h.tick("LIVE")
    h.tick("LIVE")
    assert h.posts == []
    assert h.store._state["lifecycle"]["records"][VID]["latest"]["phase"] == "UNKNOWN"


def test_changed_publisher_end_never_rebases_replay_window(h):
    h.tick("COMPLETED_REPLAY", send=False)
    original = deepcopy(h.store._state["lifecycle"]["records"][VID])
    h.replay_end = (h.at-timedelta(days=2)).isoformat()
    h.tick("COMPLETED_REPLAY")
    row = h.store._state["lifecycle"]["records"][VID]
    assert row["origins"] == original["origins"]
    assert row["actual_end_at"] == original["actual_end_at"]
    assert row["latest"]["phase"] == "UNKNOWN"
    assert h.posts == []


@pytest.mark.parametrize("damage", ["digest", "attempts", "fence", "identity", "extension"])
def test_corrupt_lifecycle_outbox_fails_closed(h, damage):
    h.tick("LIVE")
    h.tick("LIVE")
    state = deepcopy(h.store._state)
    record = state["outbox"][f"youtube:video:{VID}:live"]
    if damage == "digest":
        record["payload_sha256"] = "0"*64
    elif damage == "attempts":
        record["attempts"] = 2
    elif damage == "fence":
        record["item"]["lifecycle_activation_at"] = (AT-timedelta(days=1)).isoformat()
    elif damage == "identity":
        record["video_id"] = "video000002"
    else:
        del state["lifecycle"]
    with pytest.raises(ValueError, match="provenance"):
        h.store._validate_state(state)


@pytest.mark.parametrize("phase,label", [("live", "Sedang LIVE"), ("replay", "Rekaman Tersedia")])
@pytest.mark.parametrize("gate", ["pass", "credential", "allowlist", "payload", "experiment"])
def test_lifecycle_notifier_labels_and_security_gates(config, monkeypatch, phase, label, gate):
    destination = "-100123456789"
    config.update(youtube_delivery_enabled=True, youtube_delivery_send_enabled=True,
        youtube_production_send_enabled=True, telegram_chat_id=destination,
        production_chat_id_sha256=hashlib.sha256(destination.encode()).hexdigest())
    item = {"source": "youtube", "kind": "video", "video_id": VID, "title": "<PIMNAS>",
        "url": f"https://www.youtube.com/watch?v={VID}", "lifecycle_phase": phase,
        "lifecycle_activation_at": AT.isoformat(), "delivery_format": "youtube-tabs-preview-v1"}
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ticket = DeliveryTicket(f"youtube:video:{VID}:{phase}", VID, "a"*32, hashlib.sha256(raw.encode()).hexdigest())
    if gate == "credential":
        config["telegram_bot_token"] = ""
    elif gate == "allowlist":
        config["production_chat_id_sha256"] = "0"*64
    elif gate == "payload":
        item["lifecycle_phase"] = "replay" if phase == "live" else "live"
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "result": {"message_id": 44}}
    post = Mock(return_value=response)
    monkeypatch.setattr("requests.post", post)
    notifier = Notifier(config)
    send = notifier.send_youtube_outbox_notification if gate == "experiment" else notifier.send_youtube_production_notification
    assert send(item, ticket) is (gate == "pass")
    assert post.call_count == (1 if gate == "pass" else 0)
    if gate == "pass":
        assert label in post.call_args.kwargs["json"]["text"]
        assert "&lt;PIMNAS&gt;" in post.call_args.kwargs["json"]["text"]


@pytest.mark.parametrize("status", ["reserved", "sending", "ambiguous", "delivered"])
def test_migration_never_requeues_any_legacy_outbox_status(tmp_path, status):
    store = YouTubeStateStore(tmp_path / "legacy.json", activation_at=AT-timedelta(days=10), policy_version="tabs-only-v1")
    store._state["outbox"][f"youtube:video:{VID}:initial"] = {
        "event_key": f"youtube:video:{VID}:initial", "video_id": VID, "status": status,
        "item": {}, "attempts": 0 if status == "reserved" else 1}
    store.flush()
    before = deepcopy(store._state)
    migrate(store, AT, AT)
    assert {k: v for k, v in store._state.items() if k != "lifecycle"} == before


@pytest.mark.parametrize("tab,badges,eligible", [
    ("videos", [], True), ("streams", ["replay"], True),
    ("streams", ["LIVE"], False), ("streams", ["upcoming"], False),
    ("videos", ["Shorts"], False)])
def test_flag_off_matches_legacy_adapter_and_delivery(tmp_path, monkeypatch, tab, badges, eligible):
    h = Harness(tmp_path, monkeypatch, fence=None)
    h.tick(hours=0)
    original = h.snapshot
    def snapshot():
        result = original()
        if result["observations"]:
            item = result["observations"][0]
            item["source_tabs"] = [tab]
            item["metadata_by_tab"] = {tab: {"badges": badges, "relative_text": [], "schedule_timezone": None}}
        return result
    h.snapshot = snapshot
    h.visible = True
    for _ in range(2):
        h.at += timedelta(hours=1)
        reference_path = tmp_path / "legacy-reference.json"
        reference_path.write_bytes(h.path.read_bytes())
        reference_store = YouTubeStateStore(reference_path, activation_at=h.policy.activation_at, policy_version=h.policy.version)
        reference = monitor.ChannelTabsAdapter(policy=h.policy, observation_store=reference_store, clock=lambda: h.at)
        expected = reference.evaluate([], JOURNAL)
        h.open()
        actual = h.adapter.evaluate([], JOURNAL)
        assert actual == expected
    assert (actual.decisions[0].eligibility is e.Eligibility.ELIGIBLE) is eligible
    h.tick()
    h.tick()  # restart and duplicate candidate must not create another send
    assert h.posts == ([f"youtube:video:{VID}:initial"] if eligible else [])
    assert "lifecycle" not in h.store._state
    h.reader.fetch.assert_not_called()


@pytest.mark.parametrize("reason", ["expired", "inventory", "history"])
def test_flag_off_keeps_legacy_no_backfill_fences(tmp_path, monkeypatch, reason):
    h = Harness(tmp_path, monkeypatch, fence=None)
    h.tick(hours=0)
    original = h.snapshot
    def replay():
        result = original()
        if result["observations"]:
            result["observations"][0]["metadata_by_tab"]["streams"]["badges"] = ["replay"]
        return result
    h.snapshot = replay
    h.tick("COMPLETED_REPLAY", send=False)
    if reason == "expired":
        h.at += timedelta(days=2)
    elif reason == "inventory":
        h.store._commit(lambda s: s["observations"][VID].update(initial_inventory=True))
    else:
        monkeypatch.setitem(JOURNAL["youtube_rss_cutover"], "baseline_ids", [VID])
    h.tick()
    assert h.posts == []
    h.reader.fetch.assert_not_called()


def test_flag_off_after_activation_never_falls_back_to_legacy_initial(h):
    h.tick("LIVE")
    h.tick("LIVE")
    h.fence = None
    h.tick("COMPLETED_REPLAY")
    h.tick("COMPLETED_REPLAY")
    assert h.posts == [f"youtube:video:{VID}:live"]
    assert f"youtube:video:{VID}:initial" not in h.store._state["outbox"]


@pytest.mark.parametrize("formats", ["changed", {}, [None], [{}], ["changed"]])
def test_malformed_player_media_format_is_unknown(formats):
    p = player("LIVE", AT)
    p["streamingData"]["formats"] = formats
    assert player_evidence(p, VID, AT)["phase"] == "UNKNOWN"


def test_missing_streams_and_permanent_replay_holds_do_not_recheckpoint(h):
    h.end = False
    h.tick("COMPLETED_REPLAY")  # no prior phase/end, permanently quarantined
    row = deepcopy(h.store._state["lifecycle"]["records"][VID])
    calls = h.reader.fetch.call_count
    for visible in (True, False, False, True):
        h.visible = visible
        checkpoints = len(h.checkpoints)
        h.tick()
        assert len(h.checkpoints) - checkpoints == 1  # source capture only
    assert h.reader.fetch.call_count == calls
    assert h.store._state["lifecycle"]["records"][VID] == row
    assert h.posts == []


def test_many_cutover_streams_add_no_player_requests_or_per_video_checkpoints(h):
    ids = [f"hist{i:07d}" for i in range(100)]
    def seed(state):
        for vid in ids:
            state["observations"][vid] = {"first_seen_at": AT.isoformat(), "initial_inventory": False}
        state["lifecycle"]["suppressed_ids"] = ids
        state["lifecycle"]["stream_ids"] = ids
    h.store._commit(seed)
    observations = deepcopy(h.store._state["observations"])
    checkpoints = len(h.checkpoints)
    h.tick()
    assert len(h.checkpoints) - checkpoints == 1
    h.reader.fetch.assert_not_called()
    assert h.store._state["lifecycle"]["records"] == {}
    assert h.store._state["observations"] == observations
