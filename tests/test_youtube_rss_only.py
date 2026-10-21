"""RSS cutover, historical compatibility and delivery safety. All network is mocked."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
import requests

from monitor_runner import event_key, run_once
from reliability import SourceError, atomic_json, read_json
from youtube_monitor import YouTubeMonitor, plan_rss_cutover, validate_cutover


def run(root, config, monitors, notifier, **kwargs):
    return run_once(config, state_dir=root, scraper=monitors[0], youtube=monitors[1],
                    notifier=notifier, **kwargs)


@pytest.fixture
def setup(tmp_path, monitors, announcement, rss_video, approved_cutover, monkeypatch):
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [deepcopy(rss_video)])


def test_new_video_once(tmp_path, config, monitors, notifier, telegram, setup):
    first = run(tmp_path, config, monitors, notifier)
    assert first.exit_code == 0 and first.sources[1].status == "NEW_ITEMS"
    assert telegram.call_count == 1
    state = read_json(tmp_path / "delivery_state.json", {})
    assert state["batches"][0]["delivered"]
    assert state["batches"][0]["event_keys"] == ["youtube:video:fixture1234:initial"]
    for _ in range(3):
        result = run(tmp_path, config, monitors, notifier)
        assert result.exit_code == 0 and result.sources[1].status == "NO_NEW_ITEMS"
    assert telegram.call_count == 1


@pytest.mark.parametrize("status", ["UNKNOWN", "UPCOMING", "LIVE", "VOD"])
def test_historical_status_never_replayed(tmp_path, config, monitors, notifier, telegram,
                                        setup, rss_video, status):
    old = dict(rss_video, status=status, last_known_status="UPCOMING", arbitrary_history="retain")
    atomic_json(tmp_path / "youtube_cache.json", [old, dict(old)])
    before = (tmp_path / "youtube_cache.json").read_bytes()
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert read_json(tmp_path / "youtube_cache.json", []) == [old, old]
    assert (tmp_path / "youtube_cache.json").read_bytes() == before
    telegram.assert_not_called()


def test_message_271_journal_only_blocks_replay(tmp_path, config, monitors, notifier, telegram,
                                              setup, approved_cutover, rss_video, monkeypatch):
    item = dict(rss_video, video_id="K94OBen4CiU", status="UNKNOWN")
    item.pop("delivery_format")
    batch = {"event_keys": ["youtube:video:K94OBen4CiU:initial"], "items": [item],
             "chunks": ["historical payload"], "sent_chunks": [1], "sent_documents": [],
             "send_files": False, "attempts": 1, "delivered": True}
    approved_cutover["batches"] = [batch]
    atomic_json(tmp_path / "delivery_state.json", approved_cutover)
    monkeypatch.setattr(monitors[1], "fetch_videos",
                        lambda: [dict(rss_video, video_id="K94OBen4CiU")])
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert read_json(tmp_path / "delivery_state.json", {}) == approved_cutover
    telegram.assert_not_called()


def test_outside_rss_retains_125_history(tmp_path, config, monitors, notifier, telegram,
                                       fixtures, announcement):
    history = json.loads((fixtures / "youtube_history_125.json").read_text(encoding="utf-8"))
    assert len(history) == 125
    atomic_json(tmp_path / "youtube_cache.json", history)
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert read_json(tmp_path / "youtube_cache.json", []) == history
    telegram.assert_not_called()


@pytest.mark.parametrize("published", ["2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z", "", "bad"])
def test_old_unknown_requires_operator(tmp_path, config, monitors, notifier, telegram,
                                     setup, rss_video, monkeypatch, published):
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [dict(rss_video, published=published)])
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.sources[1].status == "FAILED"
    assert result.awaiting_verification == 1
    assert read_json(tmp_path / "youtube_cache.json", []) == []
    assert not read_json(tmp_path / "delivery_state.json", {})["batches"]
    telegram.assert_not_called()


def test_no_marker_fail_closed(tmp_path, config, monitors, notifier, telegram, setup):
    (tmp_path / "delivery_state.json").unlink()
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and "reconciliation" in result.sources[1].error
    assert not read_json(tmp_path / "delivery_state.json", {})["batches"]
    telegram.assert_not_called()


def test_plan_pure_and_no_false_delivery(rss_video):
    cache = [dict(rss_video, video_id="known-id", status="UNKNOWN")]
    journal = {"version": 1, "batches": []}
    before = deepcopy((cache, journal))
    proposal = plan_rss_cutover(cache, journal, [dict(rss_video, video_id="known-id"), rss_video],
                               "2026-04-01T00:00:00Z")
    assert proposal["known_current_ids"] == ["known-id"]
    assert proposal["proposed_marker"]["baseline_ids"] == ["known-id"]
    assert proposal["requires_operator_review"][0]["video_id"] == rss_video["video_id"]
    assert (cache, journal) == before


@pytest.mark.parametrize("value", [{}, {"version": 2}, {"version": 1, "cutover_at": "bad"},
    {"version": 1, "cutover_at": "2026-04-01", "baseline_ids": []},
    {"version": 1, "cutover_at": "2026-04-01T00:00:00Z", "baseline_ids": ["x", "x"]}])
def test_invalid_marker_rejected(value):
    with pytest.raises(ValueError):
        validate_cutover(value)


def test_known_baseline_without_cache(tmp_path, config, monitors, notifier, telegram,
                                     setup, approved_cutover, rss_video):
    approved_cutover["youtube_rss_cutover"]["baseline_ids"] = [rss_video["video_id"]]
    atomic_json(tmp_path / "delivery_state.json", approved_cutover)
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    telegram.assert_not_called()


def test_combined_bima_youtube(tmp_path, config, monitors, notifier, telegram, setup):
    (tmp_path / "announcements_cache.json").unlink()
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert telegram.call_count == 1
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert len(batch["items"]) == 2 and batch["delivered"]


def test_held_video_does_not_block_valid_or_bima(tmp_path, config, monitors, notifier,
                                               telegram, setup, rss_video, monkeypatch):
    (tmp_path / "announcements_cache.json").unlink()
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [
        dict(rss_video, video_id="oldfixture1", published="2020-01-01T00:00:00Z"), rss_video])
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and telegram.call_count == 1
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert len(batch["items"]) == 2 and batch["delivered"]
    assert all(i.get("video_id") != "oldfixture1" for i in batch["items"])


@pytest.mark.parametrize("failure", ["ok_false", "http", "timeout"])
def test_rss_delivery_retry(tmp_path, config, monitors, notifier, telegram, setup, failure):
    if failure == "timeout":
        telegram.side_effect = requests.Timeout()
    else:
        telegram.return_value.status_code = 503 if failure == "http" else 200
        telegram.return_value.json.return_value = {"ok": False}
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    state = read_json(tmp_path / "delivery_state.json", {})
    assert not state["batches"][0]["delivered"]
    telegram.side_effect = None
    telegram.return_value = Mock(status_code=200, json=lambda: {"ok": True})
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert telegram.call_count == 2


@pytest.mark.parametrize("status", ["UNKNOWN", "VOD", "LIVE", "UPCOMING"])
def test_pending_legacy_fail_closed(tmp_path, config, monitors, notifier, telegram,
                                   setup, approved_cutover, rss_video, status):
    legacy = dict(rss_video, status=status)
    batch = {"event_keys": [event_key(legacy)], "items": [legacy],
             "chunks": ["unchanged legacy"], "sent_chunks": [], "sent_documents": [],
             "send_files": False, "attempts": 0, "delivered": False}
    approved_cutover["batches"] = [batch]
    atomic_json(tmp_path / "delivery_state.json", approved_cutover)
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    assert read_json(tmp_path / "delivery_state.json", {}) == approved_cutover
    telegram.assert_not_called()


def test_pending_mixed_legacy_preserves_confirmed_chunks(tmp_path, config, monitors, notifier,
                                                       telegram, setup, approved_cutover,
                                                       rss_video, announcement):
    batch = {"event_keys": [event_key(rss_video), event_key(announcement)],
             "items": [dict(rss_video, status="UNKNOWN"), announcement],
             "chunks": ["confirmed", "not sent"], "sent_chunks": [1], "sent_documents": [],
             "send_files": False, "attempts": 1, "delivered": False}
    approved_cutover["batches"] = [batch]
    atomic_json(tmp_path / "delivery_state.json", approved_cutover)
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    assert read_json(tmp_path / "delivery_state.json", {}) == approved_cutover
    telegram.assert_not_called()


def test_dry_run_byte_identical(tmp_path, config, monitors, notifier, telegram, setup):
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert run(tmp_path, config, monitors, notifier, dry_run=True).exit_code == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    telegram.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "404", "xml", "root"])
def test_feed_failure_bima_continues(tmp_path, config, monitors, notifier, telegram,
                                    monkeypatch, failure):
    youtube = YouTubeMonitor()
    response = Mock(content=b"<bad" if failure == "xml" else b"<html/>")
    if failure == "404":
        response.raise_for_status.side_effect = requests.HTTPError()
    request = Mock(return_value=response)
    if failure == "timeout":
        request.side_effect = requests.Timeout()
    monkeypatch.setattr(youtube.session, "get", request)
    result = run_once(config, state_dir=tmp_path, scraper=monitors[0], youtube=youtube, notifier=notifier)
    assert result.exit_code == 1 and result.sources[1].status == "FAILED"
    assert telegram.call_count == 1


def test_only_rss_http_no_login_or_player(fixtures, monkeypatch):
    monitor = YouTubeMonitor()
    request = Mock(return_value=Mock(content=(fixtures / "youtube.xml").read_bytes()))
    monkeypatch.setattr(monitor.session, "get", request)
    videos = monitor.fetch_videos()
    request.assert_called_once_with(
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCGo_6l_6kp8H8OHcKcSIeDw", timeout=15)
    assert monitor.session.auth is None and not monitor.session.cookies
    assert "status" not in videos[0]
    assert not hasattr(monitor, "_analyze_video_type")


def test_neutral_format(notifier, rss_video):
    message = "".join(notifier._build_summary_chunks([rss_video]))
    assert "VIDEO BARU" in message and "YOUTUBE KEMDIKTISAINTEK" in message
    assert rss_video["title"] in message and rss_video["channel_title"] in message
    assert "Publikasi (RSS)" in message
    for forbidden in ("[UNKNOWN]", "[VOD]", "[LIVE]", "[UPCOMING]", "Jadwal"):
        assert forbidden not in message


@pytest.mark.parametrize("status", ["UNKNOWN", "LIVE", "UPCOMING", "VOD"])
def test_direct_legacy_delivery_blocked(notifier, rss_video, telegram, status):
    assert not notifier.send_notification([dict(rss_video, status=status)])
    telegram.assert_not_called()


@pytest.mark.parametrize("stage", ["journal", "cache", "checkpoint"])
def test_rss_persistence_errors_critical(tmp_path, config, monitors, notifier, telegram,
                                         setup, monkeypatch, stage):
    import monitor_runner
    original = monitor_runner.atomic_json
    def write(path, state):
        if stage == "journal" or (stage == "checkpoint" and state["batches"]
                                  and state["batches"][0]["sent_chunks"]):
            raise OSError("synthetic disk failure")
        original(path, state)
    if stage == "cache":
        monkeypatch.setattr(monitors[1], "save_cache", Mock(side_effect=OSError()))
    else:
        monkeypatch.setattr(monitor_runner, "atomic_json", write)
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    journal = read_json(tmp_path / "delivery_state.json", {"batches": []})
    assert not any(b["delivered"] for b in journal["batches"])
    assert telegram.call_count == (1 if stage == "checkpoint" else 0)


def test_rss_partial_chunk_retry_no_duplicate(tmp_path, config, monitors, notifier, telegram,
                                             setup, monkeypatch):
    monkeypatch.setattr(notifier, "_build_summary_chunks", lambda items: ["first", "second"])
    ok = Mock(status_code=200, json=lambda: {"ok": True})
    telegram.side_effect = [ok, Mock(status_code=200, json=lambda: {"ok": False})]
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    assert read_json(tmp_path / "delivery_state.json", {})["batches"][0]["sent_chunks"] == [1]
    telegram.reset_mock()
    telegram.side_effect = [ok]
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert telegram.call_count == 1 and telegram.call_args.kwargs["json"]["text"] == "second"


@pytest.mark.parametrize("field,value", [("videoId", "short"), ("title", ""),
    ("published", "bad"), ("published", "2026-04-16T00:00:00")])
def test_feed_invalid_metadata_fails(fixtures, monkeypatch, field, value):
    import xml.etree.ElementTree as ET
    root = ET.fromstring((fixtures / "youtube.xml").read_bytes())
    namespace = "http://www.youtube.com/xml/schemas/2015" if field == "videoId" else "http://www.w3.org/2005/Atom"
    root.find("{http://www.w3.org/2005/Atom}entry").find("{" + namespace + "}" + field).text = value
    monitor = YouTubeMonitor()
    monkeypatch.setattr(monitor.session, "get", Mock(return_value=Mock(content=ET.tostring(root))))
    with pytest.raises(SourceError):
        monitor.fetch_videos()


def test_duplicate_rss_ids_fail(fixtures, monkeypatch):
    import xml.etree.ElementTree as ET
    root = ET.fromstring((fixtures / "youtube.xml").read_bytes())
    root.append(deepcopy(root.find("{http://www.w3.org/2005/Atom}entry")))
    monitor = YouTubeMonitor()
    monkeypatch.setattr(monitor.session, "get", Mock(return_value=Mock(content=ET.tostring(root))))
    with pytest.raises(SourceError, match="Duplicate"):
        monitor.fetch_videos()


def test_empty_feed_healthy(tmp_path, config, monitors, notifier, telegram, setup, monkeypatch):
    monitor = YouTubeMonitor()
    monkeypatch.setattr(monitor.session, "get", Mock(return_value=Mock(
        content=b'<feed xmlns="http://www.w3.org/2005/Atom"/>')))
    result = run_once(config, state_dir=tmp_path, scraper=monitors[0], youtube=monitor, notifier=notifier)
    assert result.exit_code == 0 and result.sources[1].status == "NO_NEW_ITEMS"
    telegram.assert_not_called()


def test_pending_retry_outside_feed(tmp_path, config, monitors, notifier, telegram, setup, monkeypatch):
    telegram.return_value.json.return_value = {"ok": False}
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [])
    telegram.return_value.json.return_value = {"ok": True}
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert telegram.call_count == 2


def test_published_time_is_not_delivery_evidence(tmp_path, config, monitors, notifier, telegram, setup):
    telegram.return_value.json.return_value = {"ok": False}
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    state = read_json(tmp_path / "delivery_state.json", {})
    assert state["batches"][0]["attempts"] == 1 and not state["batches"][0]["delivered"]


def test_invalid_channel_never_requests(monkeypatch):
    monitor = YouTubeMonitor(channel_id="invalid")
    request = Mock()
    monkeypatch.setattr(monitor.session, "get", request)
    with pytest.raises(SourceError):
        monitor.fetch_videos()
    request.assert_not_called()


def test_legacy_status_metadata_changes_no_updates(tmp_path, config, monitors, notifier,
                                                   telegram, setup, rss_video, monkeypatch):
    historical = dict(rss_video, status="UPCOMING", scheduled_start="2020-01-01T00:00:00Z")
    atomic_json(tmp_path / "youtube_cache.json", [historical])
    for title in ["Changed LIVE title", "Changed VOD title"]:
        monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [dict(rss_video, title=title)])
        assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert read_json(tmp_path / "youtube_cache.json", []) == [historical]
    telegram.assert_not_called()


def test_unknown_historical_feed_not_sent_en_masse(tmp_path, config, monitors, notifier,
                                                  telegram, setup, rss_video, monkeypatch):
    old = [dict(rss_video, video_id=f"oldfeed{i:04d}",
                published="2020-01-01T00:00:00Z") for i in range(15)]
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: deepcopy(old))
    for _ in range(2):
        result = run(tmp_path, config, monitors, notifier)
        assert result.exit_code == 1 and result.awaiting_verification == 15
        assert not read_json(tmp_path / "delivery_state.json", {})["batches"]
    assert read_json(tmp_path / "youtube_cache.json", []) == []
    telegram.assert_not_called()


def test_marker_corruption_fails_without_send(tmp_path, config, monitors, notifier, telegram, setup):
    state = read_json(tmp_path / "delivery_state.json", {})
    state["youtube_rss_cutover"]["cutover_at"] = "invalid"
    atomic_json(tmp_path / "delivery_state.json", state)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    telegram.assert_not_called()
