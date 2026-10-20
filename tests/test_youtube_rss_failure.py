"""RSS discovery failures must not stop inspection of cached video IDs."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from monitor_runner import run_once
from reliability import SourceError, atomic_json, read_json
from youtube_monitor import YouTubeMonitor


@pytest.fixture
def video():
    return {"source": "youtube", "title": "Synthetic stream", "video_id": "fixture1234",
            "url": "https://example.invalid/video", "status": "UPCOMING", "documents": [],
            "scheduled_start": "2020-01-01T00:00:00Z", "actual_end": ""}


def player(status):
    details = {"videoId": "fixture1234", "isLiveContent": True}
    live = {"startTimestamp": "2020-01-01T00:00:00Z"}
    if status == "UPCOMING":
        details["isUpcoming"] = True
    elif status == "LIVE":
        live["isLiveNow"] = True
    elif status == "VOD":
        live["endTimestamp"] = "2020-01-01T02:00:00Z"
    elif status == "UNKNOWN":
        return {"playabilityStatus": {"status": "ERROR"}}
    return {"videoDetails": details, "microformat": {"playerMicroformatRenderer": {"liveBroadcastDetails": live}}}


def install_responses(monkeypatch, monitor, fixtures, mode, status="UPCOMING"):
    # The shared monitors fixture stubs fetch_videos; exercise the real RSS path.
    monkeypatch.setattr(monitor, "fetch_videos", YouTubeMonitor.fetch_videos.__get__(monitor))
    def get(url, **kwargs):
        if "/feeds/" in url:
            if mode == "timeout":
                raise requests.Timeout("private request detail must not be logged")
            if isinstance(mode, int):
                response = requests.Response()
                response.status_code = mode
                response.url = "https://example.invalid/rss"
                return response
            content = b"<broken" if mode == "invalid_xml" else (fixtures / "youtube.xml").read_bytes()
            return Mock(content=content, status_code=200)
        if status == "timeout":
            raise requests.Timeout()
        if status == "http_error":
            response = requests.Response()
            response.status_code = 503
            return response
        text = "ytInitialPlayerResponse = " + json.dumps(player(status)) + ";"
        if status == "no_player":
            text = "unavailable"
        elif status == "invalid_json":
            text = "ytInitialPlayerResponse = invalid;"
        return Mock(text=text, status_code=200)
    request = Mock(side_effect=get)
    monkeypatch.setattr(monitor.session, "get", request)
    return request


def execute(tmp_path, config, monitors, notifier, announcement, video):
    if not (tmp_path / "announcements_cache.json").exists():
        atomic_json(tmp_path / "announcements_cache.json", [announcement])
    if not (tmp_path / "youtube_cache.json").exists():
        atomic_json(tmp_path / "youtube_cache.json", [video])
    return run_once(config, state_dir=tmp_path, scraper=monitors[0], youtube=monitors[1], notifier=notifier)


@pytest.mark.parametrize("mode", [404, 503, "timeout", "invalid_xml"])
@pytest.mark.parametrize("status", ["LIVE", "VOD"])
def test_rss_failure_still_delivers_cached_transition_with_failed_health(tmp_path, config, monitors, notifier, telegram, announcement, video, fixtures, monkeypatch, mode, status):
    request = install_responses(monkeypatch, monitors[1], fixtures, mode, status)
    result = execute(tmp_path, config, monitors, notifier, announcement, video)
    source = result.sources[1]
    assert result.exit_code == 1 and source.status == "FAILED"
    assert "RSS failed" in source.error
    assert "private request detail" not in source.error
    if isinstance(mode, int):
        assert f"HTTP {mode}" in source.error
    assert [i["video_id"] for i in source.items] == [video["video_id"]]
    assert source.items[0]["status"] == status and source.items[0]["is_status_update"]
    assert any("watch?v=" in call.args[0] for call in request.call_args_list)
    assert read_json(tmp_path / "youtube_cache.json", [])[0]["status"] == status
    assert read_json(tmp_path / "delivery_state.json", {})["batches"][0]["delivered"]
    assert telegram.call_count == 1
    again = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert again.exit_code == 1 and again.sources[1].status == "FAILED"
    assert again.sources[1].items == [] and telegram.call_count == 1


@pytest.mark.parametrize("mode", ["success", 404, "timeout"])
@pytest.mark.parametrize("status", ["UPCOMING", "LIVE", "VOD"])
def test_unchanged_status_does_not_replay(tmp_path, config, monitors, notifier, telegram, announcement, video, fixtures, monkeypatch, mode, status):
    video = dict(video, status=status)
    request = install_responses(monkeypatch, monitors[1], fixtures, mode, status)
    result = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert result.sources[1].status == ("NO_NEW_ITEMS" if mode == "success" else "FAILED")
    assert result.exit_code == (0 if mode == "success" else 1)
    assert result.sources[1].items == []
    assert read_json(tmp_path / "youtube_cache.json", [])[0]["status"] == status
    assert sum("watch?v=" in c.args[0] for c in request.call_args_list) == (0 if status == "VOD" else 1)
    telegram.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "http_error", "no_player", "invalid_json", "UNKNOWN"])
def test_failed_inspection_stays_unknown_and_retains_upcoming(tmp_path, config, monitors, notifier, telegram, announcement, video, fixtures, monkeypatch, failure):
    install_responses(monkeypatch, monitors[1], fixtures, 404, failure)
    result = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert result.exit_code == 1 and result.sources[1].status == "FAILED"
    assert "HTTP 404" in result.sources[1].error and "inspection unresolved" in result.sources[1].error
    saved = read_json(tmp_path / "youtube_cache.json", [])[0]
    assert saved["status"] == "UNKNOWN" and saved["last_known_status"] == "UPCOMING"
    assert saved["scheduled_start"] == video["scheduled_start"]
    telegram.assert_not_called()


@pytest.mark.parametrize("first,second", [(404, 404), (404, "success"), ("success", 404)])
def test_unknown_recovers_to_live_without_duplicate(tmp_path, config, monitors, notifier, telegram, announcement, video, fixtures, monkeypatch, first, second):
    install_responses(monkeypatch, monitors[1], fixtures, first, "UNKNOWN")
    initial = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert initial.sources[1].status == ("DEGRADED" if first == "success" else "FAILED")
    assert read_json(tmp_path / "youtube_cache.json", [])[0]["last_known_status"] == "UPCOMING"
    telegram.assert_not_called()
    install_responses(monkeypatch, monitors[1], fixtures, second, "LIVE")
    recovered = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert recovered.sources[1].status == ("NEW_ITEMS" if second == "success" else "FAILED")
    assert recovered.sources[1].items[0]["status"] == "LIVE"
    assert telegram.call_count == 1
    assert execute(tmp_path, config, monitors, notifier, announcement, video).sources[1].items == []
    assert telegram.call_count == 1


def test_rss_error_clears_after_recovery(tmp_path, config, monitors, notifier, telegram, announcement, video, fixtures, monkeypatch):
    install_responses(monkeypatch, monitors[1], fixtures, 404)
    assert execute(tmp_path, config, monitors, notifier, announcement, video).sources[1].status == "FAILED"
    install_responses(monkeypatch, monitors[1], fixtures, "success")
    result = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert result.exit_code == 0 and result.sources[1].status == "NO_NEW_ITEMS"
    assert monitors[1].rss_error == "" and result.sources[1].error == ""
    telegram.assert_not_called()


@pytest.mark.parametrize("mode", [404, "timeout"])
def test_no_cache_and_failed_rss_remains_an_error(tmp_path, fixtures, monkeypatch, mode):
    monitor = YouTubeMonitor(cache_file=str(tmp_path / "missing.json"))
    install_responses(monkeypatch, monitor, fixtures, mode)
    with pytest.raises(SourceError, match="RSS failed"):
        monitor.check_new_videos()
    assert monitor.proposed_cache is None and not Path(monitor.cache_file).exists()


def test_corrupt_cache_is_not_overwritten_or_treated_as_empty(tmp_path, fixtures, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_bytes(b"not-json")
    monitor = YouTubeMonitor(cache_file=str(path))
    request = install_responses(monkeypatch, monitor, fixtures, 404)
    with pytest.raises(ValueError):
        monitor.check_new_videos()
    assert monitor.proposed_cache is None and path.read_bytes() == b"not-json"
    request.assert_not_called()


def test_pending_delivery_retries_despite_failed_rss(tmp_path, config, monitors, notifier, telegram, announcement, video, fixtures, monkeypatch):
    install_responses(monkeypatch, monitors[1], fixtures, 404, "LIVE")
    telegram.return_value.json.return_value = {"ok": False}
    result = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert result.exit_code == 1 and result.pending == 1
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert not batch["delivered"]
    telegram.return_value.json.return_value = {"ok": True}
    retry = execute(tmp_path, config, monitors, notifier, announcement, video)
    assert retry.exit_code == 1 and retry.sources[1].status == "FAILED" and retry.pending == 0
    batches = read_json(tmp_path / "delivery_state.json", {})["batches"]
    assert len(batches) == 1 and batches[0]["delivered"] and batches[0]["chunks"] == batch["chunks"]
    assert telegram.call_count == 2


def test_actual_125_row_cache_inspected_on_rss_failure(tmp_path, config, monitors, notifier, telegram, announcement, fixtures, monkeypatch):
    source = fixtures / "youtube_history_125.json"
    raw = source.read_bytes()
    history = json.loads(raw)
    assert len(history) == 125
    upcoming = [i["video_id"] for i in history if i["status"] == "UPCOMING"]
    assert len(upcoming) == 6
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    (tmp_path / "youtube_cache.json").write_bytes(raw)
    install_responses(monkeypatch, monitors[1], fixtures, 404)
    inspect = Mock(side_effect=lambda vid: {"status": "UPCOMING" if vid == upcoming[0] else "UNKNOWN"})
    monkeypatch.setattr(monitors[1], "_analyze_video_type", inspect)
    result = run_once(config, state_dir=tmp_path, scraper=monitors[0], youtube=monitors[1], notifier=notifier)
    assert result.exit_code == 1 and result.sources[1].status == "FAILED"
    assert [c.args[0] for c in inspect.call_args_list] == upcoming
    proposed = read_json(tmp_path / "youtube_cache.json", [])
    assert len(proposed) == 125
    assert sum(i["status"] == "UNKNOWN" for i in proposed) == 5
    assert all(i["last_known_status"] == "UPCOMING" for i in proposed if i["video_id"] in upcoming)
    assert source.read_bytes() == raw
    telegram.assert_not_called()
