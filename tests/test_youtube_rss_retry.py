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


@pytest.mark.parametrize("kind", FAILURES)
def test_feed_failure_never_advances_history_bima_continues(
        kind, tmp_path, config, monitors, notifier, telegram, announcement, monkeypatch, sleep):
    cached = [{"video_id": "K94OBen4CiU", "title": "Historical", "status": "UNKNOWN"}]
    cache = tmp_path / "youtube_cache.json"
    atomic_json(cache, cached)
    before = cache.read_bytes()
    youtube = YouTubeMonitor()
    get = Mock(side_effect=[fail(kind), fail(kind)])
    monkeypatch.setattr(youtube.session, "get", get)
    result = run_once(config, state_dir=tmp_path, scraper=monitors[0], youtube=youtube, notifier=notifier)
    assert result.exit_code == 1 and result.sources[1].status == "FAILED"
    assert cache.read_bytes() == before
    assert youtube.proposed_cache is None
    assert result.sources[0].status == "NEW_ITEMS"
    assert telegram.call_count == 1
    batches = read_json(tmp_path / "delivery_state.json", {})["batches"]
    assert len(batches) == 1 and batches[0]["delivered"]
    assert all(i.get("source") != "youtube" for i in batches[0]["items"])


def test_recovery_with_133_id_cutover_and_message271(
        tmp_path, config, monitors, notifier, telegram, announcement, fixtures, monkeypatch, sleep):
    ids = [f"hist{i:07d}" for i in range(132)] + ["K94OBen4CiU"]
    history = [{"source": "youtube", "video_id": i, "title": "Historical " + i,
                "status": "UNKNOWN", "documents": []} for i in ids[:128] + ids[-1:]]
    marker = {"version": 1, "cutover_at": "2026-10-21T01:32:44.160012+00:00", "baseline_ids": ids}
    batch = {"event_keys": ["youtube:video:K94OBen4CiU:initial"], "items": [history[-1]],
             "chunks": ["already confirmed historical message 271"], "sent_chunks": [1],
             "sent_documents": [], "send_files": False, "delivered": True, "attempts": 1}
    old_batch = deepcopy(batch)
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    atomic_json(tmp_path / "youtube_cache.json", history)
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [batch], "youtube_rss_cutover": marker})
    feed = ET.fromstring((fixtures / "youtube.xml").read_bytes())
    template = deepcopy(feed.find("{http://www.w3.org/2005/Atom}entry"))
    feed.remove(feed.find("{http://www.w3.org/2005/Atom}entry"))
    for video_id in ids[128:] + ["postcut0001"]:
        entry = deepcopy(template)
        entry.find("{http://www.youtube.com/xml/schemas/2015}videoId").text = video_id
        entry.find("{http://www.w3.org/2005/Atom}published").text = "2026-10-21T01:33:00Z"
        feed.append(entry)
    raw = ET.tostring(feed)
    youtube = YouTubeMonitor()
    get = Mock(side_effect=[response(404), response(200, raw), response(200, raw), response(200, raw)])
    monkeypatch.setattr(youtube.session, "get", get)
    for _ in range(3):
        assert run_once(config, state_dir=tmp_path, scraper=monitors[0],
                        youtube=youtube, notifier=notifier).exit_code == 0
    assert telegram.call_count == 1
    state = read_json(tmp_path / "delivery_state.json", {})
    assert state["youtube_rss_cutover"] == marker
    assert state["batches"][0] == old_batch
    assert len(state["batches"]) == 2 and state["batches"][1]["delivered"]
    assert state["batches"][1]["event_keys"] == ["youtube:video:postcut0001:initial"]
    after = read_json(tmp_path / "youtube_cache.json", [])
    assert after[:129] == history and len(after) == 130
    assert get.call_count == 4
    sleep.assert_called_once_with(2)


def test_retry_then_dry_run_never_writes_or_sends(
        tmp_path, config, monitors, notifier, telegram, approved_cutover, fixtures, monkeypatch, sleep):
    youtube = YouTubeMonitor()
    get = Mock(side_effect=[response(500), response(200, (fixtures / "youtube.xml").read_bytes())])
    monkeypatch.setattr(youtube.session, "get", get)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = run_once(config, dry_run=True, state_dir=tmp_path, scraper=monitors[0],
                      youtube=youtube, notifier=notifier)
    assert result.exit_code == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    telegram.assert_not_called()


def test_invalid_metadata_is_not_transient(fixtures, monkeypatch, sleep):
    raw = (fixtures / "youtube.xml").read_bytes().replace(b"fixture1234", b"bad")
    monitor = YouTubeMonitor()
    get = Mock(return_value=response(200, raw))
    monkeypatch.setattr(monitor.session, "get", get)
    with pytest.raises(SourceError, match="missing identity"):
        monitor.fetch_videos()
    get.assert_called_once()
    sleep.assert_not_called()
