"""RSS cutover, historical compatibility and delivery safety. All network is mocked."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
import requests

from monitor_runner import event_key, run_once
from reliability import SourceError, atomic_json, read_json
from youtube_monitor import YouTubeMonitor, plan_rss_cutover, validate_cutover




@pytest.fixture
def setup(tmp_path, monitors, announcement, rss_video, approved_cutover, monkeypatch):
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    monkeypatch.setattr(monitors[1], "fetch_videos", lambda: [deepcopy(rss_video)])














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


















def test_only_rss_http_no_login_or_player(fixtures, monkeypatch):
    monitor = YouTubeMonitor()
    request = Mock(return_value=Mock(content=(fixtures / "youtube.xml").read_bytes()))
    monkeypatch.setattr(monitor.session, "get", request)
    videos = monitor.fetch_videos()
    request.assert_called_once_with(
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCGo_6l_6kp8H8OHcKcSIeDw",
        timeout=15, allow_redirects=False)
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








def test_invalid_channel_never_requests(monkeypatch):
    monitor = YouTubeMonitor(channel_id="invalid")
    request = Mock()
    monkeypatch.setattr(monitor.session, "get", request)
    with pytest.raises(SourceError):
        monitor.fetch_videos()
    request.assert_not_called()
