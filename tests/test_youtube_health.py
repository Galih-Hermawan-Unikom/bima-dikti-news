"""Historical warnings must not mask current-source or delivery failures."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
import requests

from monitor_runner import run_once
from reliability import SourceError, atomic_json, read_json
from youtube_monitor import YouTubeMonitor, historical_unavailable

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
UNAVAILABLE = {"playabilityStatus": {"status": "ERROR", "reason": "Video tidak tersedia"}}


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def video(index=0, days=8, status="UPCOMING"):
    return {"source": "youtube", "video_id": f"history{index:04}", "title": f"Historical stream {index}",
            "status": status, "scheduled_start": (NOW - timedelta(days=days)).isoformat(),
            "documents": []}


@pytest.fixture
def setup_health(tmp_path, monitors, announcement, monkeypatch):
    monkeypatch.setattr("youtube_monitor.datetime", Clock)
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    atomic_json(tmp_path / "youtube_cache.json", [video(i) for i in range(5)])
    response = requests.Response()
    response.status_code = 200
    response._content = ("ytInitialPlayerResponse = " + json.dumps(UNAVAILABLE) + ";").encode()
    request = Mock(return_value=response)
    monkeypatch.setattr(monitors[1].session, "get", request)
    return request


def run(tmp_path, config, monitors, notifier, **kwargs):
    return run_once(config, state_dir=tmp_path, scraper=monitors[0], youtube=monitors[1],
                    notifier=notifier, **kwargs)


@pytest.mark.parametrize("reason", ["Video tidak tersedia", "Video ini tidak tersedia",
                                   "Video unavailable", "This video is unavailable"])
def test_five_old_unavailable_warn_without_replay(tmp_path, config, monitors, notifier, telegram,
                                                setup_health, reason):
    setup_health.return_value._content = ("ytInitialPlayerResponse = " + json.dumps(
        {"playabilityStatus": {"status": "ERROR", "reason": reason}})).encode()
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0 and result.sources[1].status == "WARNING"
    assert result.sources[1].items == [] and result.pending == 0
    assert result.sources[1].error == "Historical unavailable inspections: 5"
    saved = read_json(tmp_path / "youtube_cache.json", [])
    assert len(saved) == 5
    assert all(i["status"] == "UNKNOWN" and i["last_known_status"] == "UPCOMING" for i in saved)
    assert all("unavailable" not in i for i in saved)
    assert read_json(tmp_path / "delivery_state.json", {})["batches"] == []
    again = run(tmp_path, config, monitors, notifier)
    assert again.exit_code == 0 and again.sources[1].status == "WARNING"
    assert setup_health.call_count == 10  # UNKNOWN outside RSS is still inspected.
    telegram.assert_not_called()


def test_valid_new_vod_delivered_once_with_warnings(tmp_path, config, monitors, notifier, telegram,
                                                 setup_health, monkeypatch):
    fresh = dict(video(99), status="", title="New valid VOD")
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [deepcopy(fresh)])
    unavailable_response = setup_health.return_value
    valid = requests.Response()
    valid.status_code = 200
    valid._content = b'ytInitialPlayerResponse = {"playabilityStatus":{"status":"OK"},"videoDetails":{"isLiveContent":false}};'
    setup_health.side_effect = lambda url, **kw: valid if fresh["video_id"] in url else unavailable_response
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0 and result.sources[1].status == "WARNING"
    assert [(i["video_id"], i["status"]) for i in result.sources[1].items] == [(fresh["video_id"], "VOD")]
    assert read_json(tmp_path / "delivery_state.json", {})["batches"][0]["delivered"]
    assert len(read_json(tmp_path / "youtube_cache.json", [])) == 6
    assert telegram.call_count == 1
    assert run(tmp_path, config, monitors, notifier).sources[1].items == []
    assert telegram.call_count == 1


def test_new_unknown_is_degraded_even_with_old_schedule(tmp_path, config, monitors, notifier,
                                                      telegram, setup_health, monkeypatch):
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [video(99, days=30)])
    result = run(tmp_path, config, monitors, notifier, dry_run=True)
    assert result.exit_code == 1 and result.sources[1].status == "DEGRADED"
    assert result.sources[1].items[0]["status"] == "UNKNOWN"
    assert monitors[1].historical_warning_count == 5
    telegram.assert_not_called()


@pytest.mark.parametrize("days", [-1, 0, 6, 6.99999, 7])
def test_upcoming_age_threshold(tmp_path, config, monitors, notifier, telegram, setup_health, days):
    atomic_json(tmp_path / "youtube_cache.json", [video(days=days)])
    result = run(tmp_path, config, monitors, notifier)
    assert result.sources[1].status == ("WARNING" if days >= 7 else "DEGRADED")
    assert result.exit_code == (0 if days >= 7 else 1)
    telegram.assert_not_called()


@pytest.mark.parametrize("start", [None, "", "invalid", 42, "2026-01-01T00:00:00", "2026-02-30T00:00:00Z"])
def test_missing_ambiguous_schedule_stays_degraded(tmp_path, config, monitors, notifier,
                                                setup_health, start):
    atomic_json(tmp_path / "youtube_cache.json", [dict(video(), scheduled_start=start)])
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.sources[1].status == "DEGRADED"


@pytest.mark.parametrize("failure", ["timeout", "http", "missing", "malformed", "login", "other_error"])
def test_old_age_does_not_excuse_other_failures(tmp_path, config, monitors, notifier, setup_health, failure):
    response = setup_health.return_value
    if failure == "timeout":
        setup_health.side_effect = requests.Timeout()
    elif failure == "http":
        response.status_code = 503
    else:
        response._content = {
            "missing": b"no player",
            "malformed": b"ytInitialPlayerResponse = invalid;",
            "login": b'ytInitialPlayerResponse = {"playabilityStatus":{"status":"LOGIN_REQUIRED","reason":"Video unavailable"}};',
            "other_error": b'ytInitialPlayerResponse = {"playabilityStatus":{"status":"ERROR","reason":"Rate limited"}};',
        }[failure]
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.sources[1].status == "DEGRADED"
    assert monitors[1].historical_warning_count == 0


@pytest.mark.parametrize("failure", [404, "timeout"])
def test_failed_rss_overrides_historical_warning(tmp_path, config, monitors, notifier,
                                               setup_health, monkeypatch, failure):
    monitor = monitors[1]
    monkeypatch.setattr(monitor, "fetch_videos", YouTubeMonitor.fetch_videos.__get__(monitor))
    unavailable_response = setup_health.return_value
    def get(url, **kwargs):
        if "/feeds/" not in url:
            return unavailable_response
        if failure == "timeout":
            raise requests.Timeout()
        response = requests.Response()
        response.status_code = failure
        return response
    setup_health.side_effect = get
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.sources[1].status == "FAILED"
    assert monitors[1].historical_warning_count == 5


def test_bima_failure_not_hidden(tmp_path, config, monitors, notifier, setup_health, monkeypatch):
    monkeypatch.setattr(monitors[0], "fetch_announcements", Mock(side_effect=SourceError("BIMA failed")))
    result = run(tmp_path, config, monitors, notifier)
    assert result.sources[0].status == "FAILED" and result.sources[1].status == "WARNING"
    assert result.exit_code == 1


@pytest.mark.parametrize("failure", ["telegram", "checkpoint", "persist"])
def test_critical_delivery_errors_still_fail(tmp_path, config, monitors, notifier, telegram,
                                           announcement, setup_health, monkeypatch, failure):
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [dict(announcement, title="New synthetic notice", surat="999/2026")])
    if failure == "telegram":
        telegram.return_value.json.return_value = {"ok": False}
    else:
        import monitor_runner
        original = monitor_runner.atomic_json
        def write(path, state):
            if failure == "persist" or (state.get("batches") and state["batches"][0]["sent_chunks"]):
                raise OSError("synthetic persistence error")
            original(path, state)
        monkeypatch.setattr(monitor_runner, "atomic_json", write)
    result = run(tmp_path, config, monitors, notifier)
    assert result.sources[1].status == "WARNING" and result.exit_code == 1 and result.errors
    if failure == "persist":
        telegram.assert_not_called()
    else:
        assert not read_json(tmp_path / "delivery_state.json", {})["batches"][0]["delivered"]


@pytest.mark.parametrize("target", ["LIVE", "VOD"])
def test_warning_recovers_transition_without_replay(tmp_path, config, monitors, notifier,
                                                  telegram, setup_health, monkeypatch, target):
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    analyze = Mock(return_value={"status": target})
    monkeypatch.setattr(monitors[1], "_analyze_video_type", analyze)
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0 and result.sources[1].status == "NEW_ITEMS"
    assert monitors[1].historical_warning_count == 0
    assert len(result.sources[1].items) == 5
    assert all(i["is_status_update"] for i in result.sources[1].items)
    sent = telegram.call_count
    assert run(tmp_path, config, monitors, notifier).sources[1].items == []
    assert telegram.call_count == sent


def test_age_threshold_uses_aware_instant():
    assert historical_unavailable({"scheduled_start": "2026-10-13T19:00:00+07:00"}, True, NOW)
    assert not historical_unavailable({"scheduled_start": "2026-10-13T19:00:01+07:00"}, True, NOW)


def test_warning_dry_run_has_no_side_effects(tmp_path, config, monitors, notifier, telegram, setup_health):
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = run(tmp_path, config, monitors, notifier, dry_run=True)
    assert result.exit_code == 0 and result.sources[1].status == "WARNING"
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    telegram.assert_not_called()


def test_historical_in_rss_keeps_schedule_for_next_inspection(tmp_path, config, monitors, notifier,
                                                           telegram, setup_health, monkeypatch):
    fresh = dict(video(), scheduled_start="", status="")
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [deepcopy(fresh)])
    for _ in range(2):
        result = run(tmp_path, config, monitors, notifier)
        assert result.exit_code == 0 and result.sources[1].status == "WARNING"
        saved = read_json(tmp_path / "youtube_cache.json", [])
        assert saved[0]["scheduled_start"] == video()["scheduled_start"]
        assert saved[0]["status"] == "UNKNOWN" and saved[0]["last_known_status"] == "UPCOMING"
    assert setup_health.call_count == 10
    telegram.assert_not_called()
