"""Bounded public-feed retry: no real network, delivery or production-state writes."""
from copy import deepcopy
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
import requests

from monitor_runner import run_once
from reliability import SourceError, atomic_json, read_json
from youtube_monitor import DEFAULT_CHANNEL, YouTubeMonitor

URL = "https://www.youtube.com/feeds/videos.xml?channel_id=" + DEFAULT_CHANNEL
FAILURES = ["404", "429", "500", "503", "timeout", "network", "xml"]


def response(status=200, content=b"", headers=None):
    result = requests.Response()
    result.status_code = status
    result.url = URL
    result._content = content
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


def fail(kind):
    if kind == "timeout":
        return requests.Timeout("DO_NOT_LOG_PRIVATE_TIMEOUT_URL")
    if kind == "network":
        return requests.ConnectionError("DO_NOT_LOG_PRIVATE_NETWORK_URL")
    if kind == "xml":
        return response(200, b"<broken>DO_NOT_LOG_PRIVATE_BODY")
    return response(int(kind), b"DO_NOT_LOG_PRIVATE_HTTP_BODY")


@pytest.fixture
def sleep(monkeypatch):
    delay = Mock()
    monkeypatch.setattr("youtube_monitor.time.sleep", delay)
    return delay


@pytest.mark.parametrize("kind", FAILURES)
def test_one_retry_recovers(kind, fixtures, monkeypatch, sleep, capsys):
    monitor = YouTubeMonitor()
    get = Mock(side_effect=[fail(kind), response(200, (fixtures / "youtube.xml").read_bytes())])
    monkeypatch.setattr(monitor.session, "get", get)
    assert monitor.fetch_videos()[0]["video_id"] == "fixture1234"
    assert get.call_count == 2
    sleep.assert_called_once_with(2)
    for call in get.call_args_list:
        assert call.args == (URL,)
        assert call.kwargs == {"timeout": 15, "allow_redirects": False}
    log = capsys.readouterr().out
    assert "retry=1/1" in log and "DO_NOT_LOG" not in log


@pytest.mark.parametrize("kind", FAILURES)
def test_repeated_failure_is_explicit(kind, monkeypatch, sleep, capsys):
    monitor = YouTubeMonitor()
    get = Mock(side_effect=[fail(kind), fail(kind)])
    monkeypatch.setattr(monitor.session, "get", get)
    with pytest.raises(SourceError, match="YouTube RSS failed: .*attempts=2") as error:
        monitor.fetch_videos()
    assert get.call_count == 2
    sleep.assert_called_once_with(2)
    assert "DO_NOT_LOG" not in str(error.value) + capsys.readouterr().out


@pytest.mark.parametrize("status", [400, 401, 403, 410])
def test_permanent_http_not_retried(status, monkeypatch, sleep):
    monitor = YouTubeMonitor()
    get = Mock(return_value=response(status))
    monkeypatch.setattr(monitor.session, "get", get)
    with pytest.raises(SourceError, match=f"HTTP_{status}; attempts=1"):
        monitor.fetch_videos()
    get.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("hint", ["120", "Wed, 21 Oct 2026 12:00:00 GMT", "invalid"])
def test_retry_after_not_overridden(status, hint, monkeypatch, sleep):
    monitor = YouTubeMonitor()
    get = Mock(return_value=response(status, headers={"Retry-After": hint}))
    monkeypatch.setattr(monitor.session, "get", get)
    with pytest.raises(SourceError, match="attempts=1"):
        monitor.fetch_videos()
    get.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("hint", ["0", "1", "2"])
def test_short_retry_after_bounded(hint, fixtures, monkeypatch, sleep):
    monitor = YouTubeMonitor()
    get = Mock(side_effect=[response(429, headers={"Retry-After": hint}),
                            response(200, (fixtures / "youtube.xml").read_bytes())])
    monkeypatch.setattr(monitor.session, "get", get)
    assert len(monitor.fetch_videos()) == 1
    assert get.call_count == 2
    sleep.assert_called_once_with(2)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_never_follows_video_page(status, monkeypatch, sleep):
    monitor = YouTubeMonitor()
    get = Mock(return_value=response(status, headers={"Location": "https://www.youtube.com/watch?v=fixture1234"}))
    monkeypatch.setattr(monitor.session, "get", get)
    with pytest.raises(SourceError, match="redirect rejected"):
        monitor.fetch_videos()
    get.assert_called_once_with(URL, timeout=15, allow_redirects=False)
    sleep.assert_not_called()


def test_first_valid_empty_feed_no_retry(monkeypatch, sleep):
    monitor = YouTubeMonitor()
    get = Mock(return_value=response(200, b'<feed xmlns="http://www.w3.org/2005/Atom"/>'))
    monkeypatch.setattr(monitor.session, "get", get)
    assert monitor.fetch_videos() == []
    get.assert_called_once()
    sleep.assert_not_called()








def test_invalid_metadata_is_not_transient(fixtures, monkeypatch, sleep):
    raw = (fixtures / "youtube.xml").read_bytes().replace(b"fixture1234", b"bad")
    monitor = YouTubeMonitor()
    get = Mock(return_value=response(200, raw))
    monkeypatch.setattr(monitor.session, "get", get)
    with pytest.raises(SourceError, match="missing identity"):
        monitor.fetch_videos()
    get.assert_called_once()
    sleep.assert_not_called()
