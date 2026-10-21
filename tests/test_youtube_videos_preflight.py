from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import yaml

from monitor_runner import event_key
from reliability import SourceError, awaiting_youtube_verification, atomic_json
from youtube_monitor import known_video_ids
from youtube_videos_parser import (VIDEOS_URL, VideosPageError, YouTubeVideosReader,
                                   extract_initial_data, parse_videos_html)
from youtube_videos_preflight import classify_observations, load_known_ids, main


def html(data):
    return "<html><script>var ytInitialData = " + json.dumps(data) + ";</script></html>"


def rows(data):
    return data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]["content"]["richGridRenderer"]["contents"]


@pytest.fixture
def data(fixtures):
    return json.loads((fixtures / "youtube_videos_initial.json").read_text(encoding="utf-8"))


@pytest.fixture
def page_response(fixtures):
    response = requests.Response()
    response.status_code = 200
    response._content = (fixtures / "youtube_videos.html").read_bytes()
    response._content_consumed = True
    return response


@pytest.fixture
def baseline(tmp_path):
    atomic_json(tmp_path / "youtube_cache.json", [{"video_id": "K94OBen4CiU", "status": "UNKNOWN"}])
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [], "youtube_rss_cutover": {
        "version": 1, "cutover_at": "2026-10-21T01:32:44.160012+00:00", "baseline_ids": ["twhITZaDKgA"]}})
    return tmp_path


def test_html_fixture(fixtures):
    result = parse_videos_html((fixtures / "youtube_videos.html").read_text(encoding="utf-8"))
    assert len(result.items) == 1 and not result.empty_verified
    item = result.items[0]
    assert item["title"] == "Video sintetis & aman"
    assert item["video_id"] == "K94OBen4CiU"
    assert item["channel_title"] == "Kemdiktisaintek Fixture"
    assert item["url"] == "https://www.youtube.com/watch?v=K94OBen4CiU"
    assert item["thumbnail"] == "https://i.ytimg.com/vi/K94OBen4CiU/hqdefault.jpg"


def test_multiple_and_no_status_or_publication(data):
    result = parse_videos_html(html(data))
    assert [i["video_id"] for i in result.items] == ["K94OBen4CiU", "newvideo001"]
    assert result.items[0]["relative_metadata"] == ["2 hari yang lalu"]
    for item in result.items:
        assert not {"published", "date", "status", "delivery_format", "is_status_update"} & item.keys()
        assert awaiting_youtube_verification(item)


@pytest.mark.parametrize("assignment", ["var ytInitialData =", "window['ytInitialData'] =", 'window["ytInitialData"] ='])
def test_json_boundaries_and_assignments(data, assignment):
    rows(data)[0]["richItemRenderer"]["content"]["lockupViewModel"]["metadata"]["lockupMetadataViewModel"]["title"]["content"] = 'Judul }; dan var ytInitialData = tidak dieksekusi'
    assert len(parse_videos_html("<script>" + assignment + json.dumps(data) + "; other();</script>").items) == 2


def test_duplicate_id_deduplicated(data):
    rows(data).append(deepcopy(rows(data)[0]))
    assert len(parse_videos_html(html(data)).items) == 2


def test_conflicting_duplicate_fails(data):
    duplicate = deepcopy(rows(data)[0])
    duplicate["richItemRenderer"]["content"]["lockupViewModel"]["metadata"]["lockupMetadataViewModel"]["title"]["content"] = "Different"
    rows(data).append(duplicate)
    with pytest.raises(VideosPageError):
        parse_videos_html(html(data))


@pytest.mark.parametrize("video_id", ["bad", "!!!!!!!!!!!", "", None, 123])
def test_invalid_ids_fail_even_with_valid_items(data, video_id):
    rows(data)[0]["richItemRenderer"]["content"]["lockupViewModel"]["contentId"] = video_id
    with pytest.raises(VideosPageError) as error:
        parse_videos_html(html(data))
    assert error.value.parsing_errors == 1 and error.value.video_count == 1


def test_endpoint_id_fallback(data):
    lockup = rows(data)[0]["richItemRenderer"]["content"]["lockupViewModel"]
    lockup.pop("contentId")
    lockup["rendererContext"] = {"commandContext": {"onTap": {"innertubeCommand": {"watchEndpoint": {"videoId": "K94OBen4CiU"}}}}}
    assert parse_videos_html(html(data)).items[0]["video_id"] == "K94OBen4CiU"


@pytest.mark.parametrize("mutation", ["lockup", "tabs", "grid", "title", "thumbnail", "channel", "streams"])
def test_structure_changes_fail_closed(data, mutation):
    tab = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]
    lockup = rows(data)[0]["richItemRenderer"]["content"]["lockupViewModel"]
    if mutation == "lockup":
        rows(data)[0]["richItemRenderer"]["content"] = {"newRenderer": {}}
    elif mutation == "tabs":
        data["contents"] = {}
    elif mutation == "grid":
        tab["content"] = {}
    elif mutation == "title":
        lockup["metadata"] = {}
    elif mutation == "thumbnail":
        lockup["contentImage"] = {}
    elif mutation == "channel":
        data["metadata"]["channelMetadataRenderer"]["externalId"] = "OTHER"
    else:
        tab["endpoint"]["commandMetadata"]["webCommandMetadata"]["url"] = "/@kemdiktisaintek/streams"
    with pytest.raises(VideosPageError):
        parse_videos_html(html(data))


@pytest.mark.parametrize("contents", [[], [{"continuationItemRenderer": {}}], [{"unknownRenderer": {}}]])
def test_empty_unrecognized_not_pass(data, contents):
    rows(data)[:] = contents
    with pytest.raises(VideosPageError):
        parse_videos_html(html(data))


@pytest.mark.parametrize("message", ["This channel has no videos.", "Channel ini belum memiliki video."])
def test_explicit_empty_verified(data, message):
    rows(data)[:] = [{"messageRenderer": {"text": {"simpleText": message}}}]
    result = parse_videos_html(html(data))
    assert result.items == [] and result.empty_verified


def test_unknown_empty_message_not_trusted(data):
    rows(data)[:] = [{"messageRenderer": {"text": {"simpleText": "Consent required"}}}]
    with pytest.raises(VideosPageError):
        parse_videos_html(html(data))


@pytest.mark.parametrize("body,category", [("<html>Consent</html>", "INITIAL_DATA_MISSING"),
    ("<script>var ytInitialData = INVALID;</script>", "INITIAL_DATA_INVALID"),
    ("<script>var ytInitialData = []; </script>", "INITIAL_DATA_INVALID")])
def test_initial_data_failures(body, category):
    with pytest.raises(VideosPageError, match=category):
        extract_initial_data(body)


@pytest.mark.parametrize("status", [404, 429, 500, 503, 302])
def test_http_failure_no_retry(monkeypatch, page_response, status):
    page_response.status_code = status
    page_response._content = b"DO_NOT_LOG_RESPONSE_BODY"
    reader = YouTubeVideosReader()
    get = Mock(return_value=page_response)
    monkeypatch.setattr(reader.session, "get", get)
    with pytest.raises(SourceError) as error:
        reader.fetch()
    assert error.value.http_status == status
    assert "DO_NOT_LOG" not in str(error.value)
    get.assert_called_once_with(VIDEOS_URL, timeout=15, allow_redirects=False)


@pytest.mark.parametrize("exception,category", [(requests.Timeout("PRIVATE_URL"), "TIMEOUT"),
    (requests.ConnectionError("PRIVATE_URL"), "NETWORK_ERROR")])
def test_network_errors_safe(monkeypatch, exception, category):
    reader = YouTubeVideosReader()
    get = Mock(side_effect=exception)
    monkeypatch.setattr(reader.session, "get", get)
    with pytest.raises(VideosPageError, match=category) as error:
        reader.fetch()
    assert "PRIVATE_URL" not in str(error.value)
    get.assert_called_once()
    assert reader.session.auth is None and not reader.session.cookies and not reader.session.trust_env


def test_knowledge_uses_cache_journal_and_baseline(baseline):
    journal = json.loads((baseline / "delivery_state.json").read_text())
    journal["batches"] = [{"items": [{"source": "youtube", "video_id": "history0001"}],
                           "event_keys": ["youtube:video:history0001:initial"], "delivered": True}]
    atomic_json(baseline / "delivery_state.json", journal)
    known = load_known_ids(baseline)
    assert known == {"K94OBen4CiU", "twhITZaDKgA", "history0001"}
    assert known_video_ids([], journal) == {"history0001"}


def test_known_unknown_and_delivery_guard(data, notifier, telegram):
    items = parse_videos_html(html(data)).items
    compared = classify_observations(items, {"K94OBen4CiU"})
    assert [i["knowledge"] for i in compared] == ["KNOWN", "UNKNOWN"]
    assert event_key(compared[0]) == "youtube:video:K94OBen4CiU:initial"
    assert not notifier.send_notification(compared)
    telegram.assert_not_called()


def test_preflight_read_only_no_telegram(baseline, monkeypatch, page_response, telegram, capsys):
    request = Mock(return_value=page_response)
    monkeypatch.setattr(requests.Session, "get", request)
    before = {p.name: p.read_bytes() for p in baseline.iterdir()}
    assert main(["--state-dir", str(baseline)]) == 0
    output = capsys.readouterr().out
    assert '"knowledge": "KNOWN"' in output
    assert '"parsing_errors": 0' in output
    assert before == {p.name: p.read_bytes() for p in baseline.iterdir()}
    telegram.assert_not_called()
    request.assert_called_once_with(VIDEOS_URL, timeout=15, allow_redirects=False)


def test_preflight_failure_exit_nonzero(baseline, monkeypatch, page_response, telegram, capsys):
    page_response._content = b"<html>DO_NOT_LOG_BODY</html>"
    monkeypatch.setattr(requests.Session, "get", Mock(return_value=page_response))
    before = {p.name: p.read_bytes() for p in baseline.iterdir()}
    assert main(["--state-dir", str(baseline)]) == 1
    output = capsys.readouterr().out
    assert "INITIAL_DATA_MISSING" in output and "DO_NOT_LOG_BODY" not in output
    assert before == {p.name: p.read_bytes() for p in baseline.iterdir()}
    telegram.assert_not_called()


def test_corrupt_state_stops_before_network(baseline, monkeypatch, capsys):
    (baseline / "delivery_state.json").write_text("broken")
    request = Mock()
    monkeypatch.setattr(requests.Session, "get", request)
    assert main(["--state-dir", str(baseline)]) == 1
    assert "STATE_INVALID" in capsys.readouterr().out
    request.assert_not_called()


def test_workflow_is_manual_read_only():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/youtube-videos-preflight.yml").read_text()
    workflow = yaml.safe_load(text)
    events = workflow.get("on", workflow.get(True))
    assert events == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["preflight"]
    assert job["timeout-minutes"] == 3
    assert job["steps"][0]["with"]["persist-credentials"] is False
    for forbidden in ("secrets.", "git push", "run_github.py", "main.py", "Notifier", "schedule:", "playwright"):
        assert forbidden.lower() not in text.lower()


def test_initial_data_ambiguous(data):
    body = html(data) + html({"different": True})
    with pytest.raises(VideosPageError, match="INITIAL_DATA_AMBIGUOUS"):
        parse_videos_html(body)


def test_empty_section_layout(data):
    tab = data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]
    tab["content"] = {"sectionListRenderer": {"contents": [{"itemSectionRenderer": {
        "contents": [{"messageRenderer": {"text": {"runs": [{"text": "This channel has no videos."}]}}}]
    }}]}}
    result = parse_videos_html(html(data))
    assert result.empty_verified and result.items == []


@pytest.mark.parametrize("target", ["title", "thumbnail", "channel_title"])
def test_missing_required_metadata_fails(data, target):
    if target == "channel_title":
        data["metadata"] = {}
        data["header"] = {}
    elif target == "title":
        rows(data)[0]["richItemRenderer"]["content"]["lockupViewModel"]["metadata"] = {"lockupMetadataViewModel": {"title": {"content": ""}}}
    else:
        rows(data)[0]["richItemRenderer"]["content"]["lockupViewModel"]["contentImage"] = {
            "thumbnailViewModel": {"image": {"sources": [{"url": "javascript:do_not_execute"}]}}}
    with pytest.raises(VideosPageError):
        parse_videos_html(html(data))


def test_invalid_state_id_stops_before_http(baseline, monkeypatch):
    atomic_json(baseline / "youtube_cache.json", [{"video_id": "not-an-id"}])
    get = Mock()
    monkeypatch.setattr(requests.Session, "get", get)
    assert main(["--state-dir", str(baseline)]) == 1
    get.assert_not_called()


def test_all_observations_blocked_even_known_and_no_history_mutation(data, notifier, telegram, baseline):
    original = {p.name: p.read_bytes() for p in baseline.iterdir()}
    items = parse_videos_html(html(data)).items
    observations = classify_observations(items, {i["video_id"] for i in items})
    for item in observations:
        assert item["knowledge"] == "KNOWN"
        assert not notifier.send_notification([item])
    telegram.assert_not_called()
    assert {p.name: p.read_bytes() for p in baseline.iterdir()} == original
