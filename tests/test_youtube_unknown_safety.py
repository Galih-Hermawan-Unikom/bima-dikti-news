"""No real network: detection, verification and delivery are distinct states."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
import requests

from monitor_runner import event_key, run_once, stable_item
from reliability import SourceError, atomic_json, read_json
from youtube_monitor import YouTubeMonitor


def video(video_id="safevideo01", status="UNKNOWN"):
    return {"source": "youtube", "title": "Synthetic video", "video_id": video_id,
            "status": status, "documents": [], "scheduled_start": "", "last_known_status": ""}


def batch(items, notifier, *, sent=None, delivered=False):
    return {"event_keys": [event_key(i) for i in items], "items": [stable_item(i) for i in items],
            "chunks": notifier._build_summary_chunks(items), "sent_chunks": sent or [],
            "sent_documents": [], "delivered": delivered, "send_files": False, "attempts": 1}


@pytest.fixture
def setup_unknown(tmp_path, announcement, monitors, monkeypatch):
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    atomic_json(tmp_path / "youtube_cache.json", [])
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [deepcopy(video())])
    analyze = Mock(return_value={"status": "UNKNOWN"})
    monkeypatch.setattr(monitors[1], "_analyze_video_type", analyze)
    return analyze


def execute(tmp_path, config, monitors, notifier, **kwargs):
    return run_once(config, state_dir=tmp_path, scraper=monitors[0], youtube=monitors[1], notifier=notifier, **kwargs)


def test_unknown_detected_and_rechecked_without_delivery(tmp_path, config, monitors, notifier,
                                                        telegram, setup_unknown, monkeypatch):
    for n in range(3):
        result = execute(tmp_path, config, monitors, notifier)
        assert result.exit_code == 1 and result.awaiting_verification == 1 and result.pending == 0
        assert result.sources[1].status == "DEGRADED"
        assert read_json(tmp_path / "delivery_state.json", {})["batches"] == []
        saved = read_json(tmp_path / "youtube_cache.json", [])
        assert len(saved) == 1 and saved[0]["status"] == "UNKNOWN"
        assert setup_unknown.call_count == n + 1
        monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [])
    telegram.assert_not_called()


@pytest.mark.parametrize("target", ["VOD", "LIVE", "UPCOMING"])
def test_unknown_recovers_initial_once_outside_rss(tmp_path, config, monitors, notifier, telegram,
                                                setup_unknown, monkeypatch, target):
    execute(tmp_path, config, monitors, notifier)
    telegram.assert_not_called()
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [])
    setup_unknown.return_value = {"status": target}
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0 and result.awaiting_verification == 0
    state = read_json(tmp_path / "delivery_state.json", {})
    assert len(state["batches"]) == 1 and state["batches"][0]["delivered"]
    assert state["batches"][0]["event_keys"] == ["youtube:video:safevideo01:initial"]
    assert not state["batches"][0]["items"][0].get("is_status_update")
    for _ in range(2):
        assert execute(tmp_path, config, monitors, notifier).pending == 0
    assert telegram.call_count == 1


@pytest.mark.parametrize("target", ["VOD", "LIVE", "UPCOMING"])
def test_incident_initial_delivered_never_replays(tmp_path, config, monitors, notifier, telegram,
                                               setup_unknown, monkeypatch, target):
    incident = video("K94OBen4CiU")
    atomic_json(tmp_path / "youtube_cache.json", [incident])
    historical = {"version": 1, "batches": [batch([incident], notifier, sent=[1], delivered=True)]}
    atomic_json(tmp_path / "delivery_state.json", historical)
    journal_before = read_json(tmp_path / "delivery_state.json", {})
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [])
    setup_unknown.return_value = {"status": target}
    for _ in range(3):
        result = execute(tmp_path, config, monitors, notifier)
        assert result.exit_code == 0 and result.pending == 0
        assert read_json(tmp_path / "delivery_state.json", {}) == journal_before
    assert read_json(tmp_path / "youtube_cache.json", [])[0]["status"] == target
    telegram.assert_not_called()


def test_unknown_delivered_does_not_create_later_false_update(tmp_path, config, monitors, notifier,
                                                           telegram, setup_unknown, monkeypatch):
    incident = video("K94OBen4CiU")
    atomic_json(tmp_path / "youtube_cache.json", [incident])
    original = {"version": 1, "batches": [batch([incident], notifier, sent=[1], delivered=True)]}
    atomic_json(tmp_path / "delivery_state.json", original)
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [])
    for target in ["UPCOMING", "LIVE", "VOD"]:
        setup_unknown.return_value = {"status": target}
        assert execute(tmp_path, config, monitors, notifier).exit_code == 0
        assert read_json(tmp_path / "delivery_state.json", {}) == original
    telegram.assert_not_called()


@pytest.mark.parametrize("target", ["LIVE", "VOD"])
def test_delivered_upcoming_preserves_status_event(tmp_path, config, monitors, notifier, telegram,
                                                 setup_unknown, monkeypatch, target):
    prior = video(status="UPCOMING")
    prior["last_known_status"] = "UPCOMING"
    atomic_json(tmp_path / "youtube_cache.json", [prior])
    initial = batch([prior], notifier, sent=[1], delivered=True)
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [initial]})
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [])
    execute(tmp_path, config, monitors, notifier)  # UPCOMING -> UNKNOWN does not send.
    telegram.assert_not_called()
    setup_unknown.return_value = {"status": target}
    execute(tmp_path, config, monitors, notifier)
    state = read_json(tmp_path / "delivery_state.json", {})
    assert state["batches"][0] == initial
    assert state["batches"][1]["event_keys"] == [f"youtube:video:safevideo01:{target}"]
    assert state["batches"][1]["delivered"]
    execute(tmp_path, config, monitors, notifier)
    assert telegram.call_count == 1


def test_mixed_new_videos_deliver_only_valid(tmp_path, config, monitors, notifier, telegram,
                                           setup_unknown, monkeypatch):
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [video(), video("validvideo1")])
    setup_unknown.side_effect = lambda key: {"status": "VOD" if key == "validvideo1" else "UNKNOWN"}
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.awaiting_verification == 1
    items = read_json(tmp_path / "delivery_state.json", {})["batches"][0]["items"]
    assert [i["video_id"] for i in items] == ["validvideo1"]
    assert telegram.call_count == 1


def test_bima_delivery_not_blocked_by_unknown(tmp_path, config, monitors, notifier, telegram,
                                           announcement, setup_unknown, monkeypatch):
    fresh = dict(announcement, title="New synthetic BIMA notice", surat="1000/2026")
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [fresh])
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1
    saved = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert saved["delivered"] and saved["items"][0]["title"] == fresh["title"]
    assert telegram.call_count == 1


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("sent", [[], [1]])
def test_pending_unknown_immutable_even_after_verification(tmp_path, config, monitors, notifier,
                                                         telegram, setup_unknown, monkeypatch, mixed, sent):
    items = [video()] + ([video("validvideo1", "VOD")] if mixed else [])
    original = batch(items, notifier, sent=sent)
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [original]})
    for status in ["UNKNOWN", "VOD"]:
        setup_unknown.return_value = {"status": status}
        result = execute(tmp_path, config, monitors, notifier)
        assert result.exit_code == 1 and result.pending == len(items)
        assert "operator reconciliation" in " ".join(result.errors)
        assert read_json(tmp_path / "delivery_state.json", {})["batches"] == [original]
    telegram.assert_not_called()


def test_blocked_batch_does_not_block_independent_valid_delivery(tmp_path, config, monitors, notifier,
                                                              telegram, setup_unknown, monkeypatch):
    original = batch([video()], notifier)
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [original]})
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [video(), video("validvideo1")])
    setup_unknown.side_effect = lambda key: {"status": "VOD" if key == "validvideo1" else "UNKNOWN"}
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1
    batches = read_json(tmp_path / "delivery_state.json", {})["batches"]
    assert batches[0] == original and batches[1]["delivered"]
    assert telegram.call_count == 1


@pytest.mark.parametrize("direct", [False, True])
def test_notifier_cannot_bypass_guard(notifier, telegram, direct):
    items = [video(), video("validvideo1", "VOD")]
    checkpoint = Mock()
    original = batch(items, notifier)
    before = deepcopy(original)
    if direct:
        result = notifier._send_summary_message("TEST", "TEST", [video("validvideo1", "VOD")], original, checkpoint)
    else:
        result = notifier.send_notification(items, original, checkpoint)
    assert result is False and original == before
    telegram.assert_not_called()
    checkpoint.assert_not_called()


def test_empty_items_cannot_acknowledge_unknown_progress(notifier, telegram):
    original = batch([video()], notifier)
    before = deepcopy(original)
    assert notifier.send_notification([], original) is False
    assert original == before
    telegram.assert_not_called()


def test_pending_valid_batch_blocked_if_current_inspection_unknown(tmp_path, config, monitors, notifier,
                                                                 telegram, setup_unknown):
    original = batch([video(status="VOD")], notifier)
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [original]})
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1
    assert read_json(tmp_path / "delivery_state.json", {})["batches"] == [original]
    telegram.assert_not_called()


def test_recovered_initial_retries_failed_telegram(tmp_path, config, monitors, notifier, telegram, setup_unknown):
    execute(tmp_path, config, monitors, notifier)
    setup_unknown.return_value = {"status": "VOD"}
    telegram.return_value.json.return_value = {"ok": False}
    failed = execute(tmp_path, config, monitors, notifier)
    assert failed.exit_code == 1 and failed.pending == 1
    assert not read_json(tmp_path / "delivery_state.json", {})["batches"][0]["delivered"]
    telegram.return_value.json.return_value = {"ok": True}
    assert execute(tmp_path, config, monitors, notifier).exit_code == 0
    assert execute(tmp_path, config, monitors, notifier).pending == 0
    assert telegram.call_count == 2


def test_persistence_failure_blocks_recovered_send(tmp_path, config, monitors, notifier, telegram,
                                                 setup_unknown, monkeypatch):
    execute(tmp_path, config, monitors, notifier)
    setup_unknown.return_value = {"status": "VOD"}
    monkeypatch.setattr("monitor_runner.atomic_json", Mock(side_effect=OSError("synthetic")))
    assert execute(tmp_path, config, monitors, notifier).exit_code == 1
    telegram.assert_not_called()


def test_recovered_checkpoint_failure_not_acknowledged(tmp_path, config, monitors, notifier,
                                                     telegram, setup_unknown, monkeypatch):
    execute(tmp_path, config, monitors, notifier)
    setup_unknown.return_value = {"status": "VOD"}
    import monitor_runner
    original = monitor_runner.atomic_json
    def fail_confirmation(path, state):
        if state["batches"] and state["batches"][0]["sent_chunks"]:
            raise OSError("synthetic checkpoint failure")
        original(path, state)
    monkeypatch.setattr(monitor_runner, "atomic_json", fail_confirmation)
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.pending == 1
    persisted = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert not persisted["delivered"] and persisted["sent_chunks"] == []
    assert telegram.call_count == 1  # Remote success without durable ack remains ambiguous.


def test_rss_failed_recovery_remains_failed_health(tmp_path, config, monitors, notifier, telegram,
                                                setup_unknown, monkeypatch):
    execute(tmp_path, config, monitors, notifier)
    setup_unknown.return_value = {"status": "VOD"}
    monkeypatch.setattr(monitors[1], "fetch_videos", Mock(side_effect=SourceError("RSS failed")))
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.sources[1].status == "FAILED"
    assert telegram.call_count == 1


def test_unknown_dry_run_never_changes_state(tmp_path, config, monitors, notifier, telegram, setup_unknown):
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = execute(tmp_path, config, monitors, notifier, dry_run=True)
    assert result.awaiting_verification == 1 and result.pending == 0
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    telegram.assert_not_called()


@pytest.mark.parametrize("failure,category", [
    ("http", "HTTP_ERROR"), ("timeout", "TIMEOUT"), ("network", "NETWORK_ERROR"),
    ("missing", "PLAYER_RESPONSE_MISSING"), ("invalid", "PLAYER_JSON_INVALID"),
    ("denied", "PLAYABILITY_DENIED"), ("no_details", "VIDEO_DETAILS_MISSING"),
    ("insufficient", "STATUS_METADATA_INSUFFICIENT"), ("other", "OTHER")])
def test_diagnostics_bounded_and_not_persisted(tmp_path, monkeypatch, capsys, failure, category):
    monitor = YouTubeMonitor(cache_file=str(tmp_path / "youtube.json"))
    sensitive = "SECRET_COOKIE_TOKEN_SIGNED_URL"
    response = requests.Response()
    response.status_code = 503 if failure == "http" else 200
    response.url = "https://example.invalid/" + sensitive
    payload = {"videoDetails": {"videoId": "safevideo01"}}
    if failure == "denied":
        payload = {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": sensitive}}
    elif failure == "no_details":
        payload = {"playabilityStatus": {"status": "OK"}}
    elif failure == "other":
        payload = []
    response._content = ("ytInitialPlayerResponse = " + json.dumps(payload) + ";").encode()
    if failure == "missing":
        response._content = sensitive.encode()
    elif failure == "invalid":
        response._content = ("ytInitialPlayerResponse = " + sensitive).encode()
    request = Mock(return_value=response)
    if failure in {"timeout", "network"}:
        request.side_effect = (requests.Timeout if failure == "timeout" else requests.ConnectionError)(sensitive)
    monkeypatch.setattr(monitor.session, "get", request)
    monkeypatch.setattr(monitor, "fetch_videos", lambda: [video()])
    detected = monitor.check_new_videos()
    assert detected[0]["status"] == "UNKNOWN"
    assert monitor.inspection_diagnostics == {"safevideo01": category}
    output = capsys.readouterr().out
    assert category in output and sensitive not in output and "https://" not in output
    assert "category" not in json.dumps(monitor.proposed_cache)
    assert not (tmp_path / "youtube.json").exists()
