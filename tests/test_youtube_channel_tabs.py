from copy import deepcopy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from reliability import SourceError, atomic_json, awaiting_youtube_verification
from youtube_channel_tabs import (FORMAT, TABS, URLS, ChannelTabPage, YouTubeChannelTabsReader,
                                  combine_pages, main, parse_channel_tab_html, read_channel_tabs)
from youtube_videos_parser import CHANNEL_ID, VideosPageError, YouTubeVideosReader, parse_videos_html
from youtube_videos_preflight import load_known_ids

OLD = "K94OBen4CiU"
BASELINE_ONLY = ["vH4lgwut_wI", "SPzLQ-H4Fcw", "4ZlcLMqtPYI", "oC6JCsrKdoc"]


@pytest.fixture
def data(fixtures):
    return json.loads((fixtures / "youtube_streams_initial.json").read_text(encoding="utf-8"))


@pytest.fixture
def video_data(fixtures):
    return json.loads((fixtures / "youtube_videos_initial.json").read_text(encoding="utf-8"))


@pytest.fixture
def state_dir(tmp_path):
    root = tmp_path / "historical"
    root.mkdir()
    atomic_json(root / "youtube_cache.json", [{"video_id": OLD, "status": "UNKNOWN"}] * 2)
    atomic_json(root / "announcements_cache.json", [{"title": "Synthetic BIMA baseline"}])
    atomic_json(root / "delivery_state.json", {"version": 1, "batches": [
        {"items": [{"source": "youtube", "video_id": OLD}],
         "event_keys": ["youtube:video:" + OLD + ":initial"], "delivered": True}],
        "youtube_rss_cutover": {"version": 1, "cutover_at": "2026-10-21T01:32:44.160012+00:00",
            "baseline_ids": [OLD, *BASELINE_ONLY, *[f"base{i:07d}" for i in range(128)]]}})
    return root


def html(data):
    return "<html><script>var ytInitialData = " + json.dumps(data) + ";</script></html>"


def tab(data):
    return data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][1]["tabRenderer"]


def rows(data):
    return tab(data)["content"]["richGridRenderer"]["contents"]


def lockup(data, index=0):
    return rows(data)[index]["richItemRenderer"]["content"]["lockupViewModel"]


def set_id(data, video_id):
    card = lockup(data)
    card["contentId"] = video_id
    card["rendererContext"]["commandContext"]["onTap"]["innertubeCommand"]["watchEndpoint"]["videoId"] = video_id


def response(data, status=200):
    return Mock(status_code=status, text=html(data))


def test_streams_synthetic_fields_and_schedule_not_playback(data):
    page = parse_channel_tab_html(html(data), "streams")
    assert isinstance(page, ChannelTabPage) and page.source_tab == "streams"
    assert page.has_continuation and not page.empty_verified and len(page.items) == 2
    item = page.items[0]
    assert item["video_id"] == "streamnew01" and item["source_tabs"] == ["streams"]
    assert item["title"] == "Synthetic scheduled research event & discussion"
    assert item["channel_title"] == "Synthetic Kemdiktisaintek Channel"
    assert item["url"] == "https://www.youtube.com/watch?v=streamnew01"
    assert item["thumbnail"] == "https://i.ytimg.com/vi/streamnew01/hqdefault.jpg"
    assert item["source_metadata"] == {"relative_text": ["1 penonton menunggu", "Tayang 23/10/26, 02.00"],
                                        "badges": ["Mendatang"], "schedule_timezone": None}
    for record in page.items:
        assert not {"status", "published", "published_at", "date", "delivery_format", "scheduled_start"} & record.keys()
        assert awaiting_youtube_verification(record)
    assert "SYNTHETIC_REMOVE" not in json.dumps(page.items)
    assert "SYNTHETIC_DO_NOT_FETCH_OR_PERSIST" not in json.dumps(page.items)


@pytest.mark.parametrize("label", ["LIVE", "UPCOMING", "VOD"])
def test_explicit_badge_remains_source_text_only(data, label):
    lockup(data)["contentImage"]["thumbnailViewModel"]["overlays"][0]["thumbnailBottomOverlayViewModel"]["badges"][0]["thumbnailBadgeViewModel"]["text"] = label
    item = parse_channel_tab_html(html(data), "streams").items[0]
    assert item["source_metadata"]["badges"] == [label]
    assert "status" not in item and item["source_metadata"]["schedule_timezone"] is None
    assert awaiting_youtube_verification(item)


def test_videos_existing_parser_preserved(video_data):
    document = html(video_data)
    old = parse_videos_html(document)
    new = parse_channel_tab_html(document, "videos")
    assert [i["video_id"] for i in new.items] == [i["video_id"] for i in old.items]
    for a, b in zip(old.items, new.items):
        assert all(a[k] == b[k] for k in ("video_id", "title", "channel_title", "url", "thumbnail"))
        assert a["relative_metadata"] == b["source_metadata"]["relative_text"]
        assert b["source_tabs"] == ["videos"] and b["observation_format"] == FORMAT


@pytest.mark.parametrize("value", [None, "", "UCwrong_channel", 1])
def test_channel_id_strict(data, value):
    data["metadata"]["channelMetadataRenderer"]["externalId"] = value
    with pytest.raises(VideosPageError, match="CHANNEL_METADATA_INVALID"):
        parse_channel_tab_html(html(data), "streams")


def test_channel_id_required_even_for_dual_videos(video_data):
    del video_data["metadata"]["channelMetadataRenderer"]["externalId"]
    assert len(parse_videos_html(html(video_data)).items) == 2  # Phase 3 behavior unchanged.
    with pytest.raises(VideosPageError, match="CHANNEL_METADATA_INVALID"):
        parse_channel_tab_html(html(video_data), "videos")


@pytest.mark.parametrize("url", ["/@kemdiktisaintek/videos", "/@kemdiktisaintek/shorts", "/@other/streams",
    "https://example.invalid/@kemdiktisaintek/streams", "https://www.youtube.com/@kemdiktisaintek/streams",
    "//@kemdiktisaintek/streams", "/@kemdiktisaintek/streams?tracking=secret", "/@kemdiktisaintek/streams#extra", None])
def test_strict_tab_endpoint(data, url):
    tab(data)["endpoint"]["commandMetadata"]["webCommandMetadata"]["url"] = url
    with pytest.raises(VideosPageError, match="UNEXPECTED_TAB"):
        parse_channel_tab_html(html(data), "streams")


def test_tab_endpoint_missing_or_conflicting_fails(data):
    tab(data)["navigationEndpoint"] = {"commandMetadata": {"webCommandMetadata": {"url": "/@kemdiktisaintek/shorts"}}}
    with pytest.raises(VideosPageError, match="UNEXPECTED_TAB"):
        parse_channel_tab_html(html(data), "streams")
    del tab(data)["navigationEndpoint"]
    del tab(data)["endpoint"]
    with pytest.raises(VideosPageError, match="UNEXPECTED_TAB"):
        parse_channel_tab_html(html(data), "streams")


@pytest.mark.parametrize("endpoint", ["endpoint", "navigationEndpoint"])
def test_channel_path_alias_allowed(data, endpoint):
    tab(data).pop("endpoint")
    tab(data)[endpoint] = {"commandMetadata": {"webCommandMetadata": {"url": "/channel/" + CHANNEL_ID + "/streams"}}}
    assert len(parse_channel_tab_html(html(data), "streams").items) == 2


@pytest.mark.parametrize("number", [0, 2])
def test_selected_tab_cardinality(data, number):
    tab(data)["selected"] = False
    if number == 2:
        tab(data)["selected"] = True
        data["contents"]["twoColumnBrowseResultsRenderer"]["tabs"][0]["tabRenderer"]["selected"] = True
    with pytest.raises(VideosPageError, match="TAB_STRUCTURE_CHANGED"):
        parse_channel_tab_html(html(data), "streams")


@pytest.mark.parametrize("kind", ["bad-id", "missing-title", "conflicting-id", "missing-thumbnail", "shorts-renderer", "bad-badges", "bad-relative"])
def test_invalid_card_fails_whole_page(data, kind):
    card = lockup(data)
    if kind == "bad-id":
        set_id(data, "invalid")
    elif kind == "missing-title":
        del card["metadata"]["lockupMetadataViewModel"]["title"]
    elif kind == "conflicting-id":
        card["rendererContext"]["commandContext"]["onTap"]["innertubeCommand"]["watchEndpoint"]["videoId"] = "conflict001"
    elif kind == "missing-thumbnail":
        card["contentImage"] = {}
    elif kind == "shorts-renderer":
        rows(data)[0] = {"richItemRenderer": {"content": {"shortsLockupViewModel": {}}}}
    elif kind == "bad-badges":
        card["contentImage"]["thumbnailViewModel"]["overlays"][0]["thumbnailBottomOverlayViewModel"]["badges"] = {}
    elif kind == "bad-relative":
        card["metadata"]["lockupMetadataViewModel"]["metadata"]["contentMetadataViewModel"]["metadataRows"] = {}
    with pytest.raises(VideosPageError):
        parse_channel_tab_html(html(data), "streams")


def test_duplicates_deduplicate_only_same_id_same_evidence(data):
    rows(data).insert(1, deepcopy(rows(data)[0]))
    assert len(parse_channel_tab_html(html(data), "streams").items) == 2
    lockup(data, 1)["metadata"]["lockupMetadataViewModel"]["title"]["content"] = "Conflicting title"
    with pytest.raises(VideosPageError):
        parse_channel_tab_html(html(data), "streams")


@pytest.mark.parametrize("message,healthy", [("This channel has no live streams.", True),
    ("Channel ini belum memiliki live stream.", True), ("Try again later", False), (None, False)])
def test_empty_must_be_explicit(data, message, healthy):
    tab(data)["content"]["richGridRenderer"]["contents"] = [] if message is None else [
        {"messageRenderer": {"text": {"simpleText": message}}}]
    if healthy:
        p = parse_channel_tab_html(html(data), "streams")
        assert p.items == [] and p.empty_verified
    else:
        with pytest.raises(VideosPageError):
            parse_channel_tab_html(html(data), "streams")


def test_empty_conflicts_with_cards(data):
    rows(data).append({"messageRenderer": {"text": {"simpleText": "This channel has no live streams."}}})
    with pytest.raises(VideosPageError):
        parse_channel_tab_html(html(data), "streams")


def test_no_initial_data_or_invalid_grid(data):
    with pytest.raises(VideosPageError, match="INITIAL_DATA_MISSING"):
        parse_channel_tab_html("<html>changed</html>", "streams")
    del tab(data)["content"]["richGridRenderer"]
    with pytest.raises(VideosPageError, match="GRID_STRUCTURE_CHANGED"):
        parse_channel_tab_html(html(data), "streams")


@pytest.mark.parametrize("status", [404, 429, 500, 503, 301, 302])
def test_http_failure_once_no_retry(monkeypatch, data, status):
    res = response(data, status)
    get = Mock(return_value=res)
    monkeypatch.setattr(requests.Session, "get", get)
    reader = YouTubeChannelTabsReader()
    try:
        with pytest.raises(VideosPageError) as error:
            reader.fetch("streams")
        assert error.value.http_status == status
        assert error.value.category == ("UNEXPECTED_REDIRECT" if status < 400 else "HTTP_FAILURE")
        get.assert_called_once_with(URLS["streams"], timeout=15, allow_redirects=False)
        res.close.assert_called_once()
    finally:
        reader.close()


@pytest.mark.parametrize("exc,category", [(requests.Timeout("SECRET_URL"), "TIMEOUT"),
    (requests.ConnectionError("SECRET_URL"), "NETWORK_ERROR")])
def test_network_failures_safe(monkeypatch, exc, category):
    get = Mock(side_effect=exc)
    monkeypatch.setattr(requests.Session, "get", get)
    reader = YouTubeChannelTabsReader()
    try:
        with pytest.raises(VideosPageError) as error:
            reader.fetch("streams")
        assert error.value.category == category and "SECRET_URL" not in str(error.value)
        assert get.call_count == 1
    finally:
        reader.close()


@pytest.mark.parametrize("tabname", ["shorts", "watch", "https://example.invalid", "", None])
def test_no_shorts_or_arbitrary_requests(monkeypatch, tabname):
    get = Mock()
    monkeypatch.setattr(requests.Session, "get", get)
    reader = YouTubeChannelTabsReader()
    try:
        with pytest.raises(VideosPageError, match="TAB_NOT_ALLOWED"):
            reader.fetch(tabname)
        with pytest.raises(VideosPageError, match="TAB_NOT_ALLOWED"):
            parse_channel_tab_html("not read", tabname)
        assert not get.called
    finally:
        reader.close()


def test_transport_session_matches_phase3():
    original, new = YouTubeVideosReader(), YouTubeChannelTabsReader()
    try:
        assert dict(original.session.headers) == dict(new.session.headers)
        assert new.session.trust_env is False and new.session.auth is None and not new.session.cookies
    finally:
        original.close()
        new.close()


def test_two_requests_and_cross_tab_merge(monkeypatch, video_data, data):
    set_id(data, OLD)
    lockup(data)["metadata"]["lockupMetadataViewModel"]["title"]["content"] = "Different source title for same identity"
    get = Mock(side_effect=[response(video_data), response(data)])
    monkeypatch.setattr(requests.Session, "get", get)
    reader = YouTubeChannelTabsReader()
    try:
        result = read_channel_tabs(reader, [OLD])
    finally:
        reader.close()
    assert result["health"] == "HEALTHY" and len(result["observations"]) == 3
    assert [c.args[0] for c in get.call_args_list] == [URLS["videos"], URLS["streams"]]
    assert all(c.kwargs == {"timeout": 15, "allow_redirects": False} for c in get.call_args_list)
    item = next(i for i in result["observations"] if i["video_id"] == OLD)
    assert item["source_tabs"] == ["videos", "streams"] and item["title_conflict"]
    assert set(item["titles_by_tab"]) == set(item["metadata_by_tab"]) == set(TABS)
    assert item["knowledge"] == "KNOWN" and not result["delivery_authorized"]
    assert all(awaiting_youtube_verification(i) for i in result["observations"])
    assert not {"status", "published", "delivery_format"} & item.keys()


@pytest.mark.parametrize("failed_tab", ["videos", "streams"])
def test_partial_failure_preserves_other_tab(monkeypatch, video_data, data, failed_tab):
    values = [response(video_data), response(data)]
    values[TABS.index(failed_tab)] = response(data, 503)
    get = Mock(side_effect=values)
    monkeypatch.setattr(requests.Session, "get", get)
    reader = YouTubeChannelTabsReader()
    try:
        result = read_channel_tabs(reader, [])
    finally:
        reader.close()
    assert get.call_count == 2 and result["health"] == "FAILED"
    assert result["pages"][failed_tab]["health"] == "FAILED" and result["pages"][failed_tab]["detected_count"] == 0
    good = next(t for t in TABS if t != failed_tab)
    assert result["pages"][good]["health"] == "HEALTHY" and len(result["observations"]) == 2
    assert all(i["source_tabs"] == [good] for i in result["observations"])


def test_historical_shorts_ids_remain_known_without_fetching_shorts(data, state_dir):
    known = load_known_ids(state_dir)
    assert len(known) == 133 and set(BASELINE_ONLY) <= known
    original = set(known)
    set_id(data, BASELINE_ONLY[0])
    pages = {"streams": parse_channel_tab_html(html(data), "streams")}
    result = combine_pages(pages, known)
    assert result[0]["knowledge"] == "KNOWN" and result[0]["source_tabs"] == ["streams"]
    assert known == original
    assert "shorts" not in URLS


def test_journal_message271_stays_known_and_no_delivery(data, video_data, state_dir):
    reader = Mock()
    reader.fetch.side_effect = [parse_channel_tab_html(html(video_data), "videos"), parse_channel_tab_html(html(data), "streams")]
    before = {p: p.read_bytes() for p in state_dir.iterdir()}
    first = read_channel_tabs(reader, iter(load_known_ids(state_dir)))
    reader.fetch.side_effect = [parse_channel_tab_html(html(video_data), "videos"), parse_channel_tab_html(html(data), "streams")]
    second = read_channel_tabs(reader, load_known_ids(state_dir))
    assert first["observations"] == second["observations"]
    assert next(i for i in first["observations"] if i["video_id"] == OLD)["knowledge"] == "KNOWN"
    assert before == {p: p.read_bytes() for p in before}
    assert not first["delivery_authorized"] and not second["delivery_authorized"]


def test_same_title_different_ids_not_merged(data):
    lockup(data, 1)["metadata"]["lockupMetadataViewModel"]["title"]["content"] = lockup(data)["metadata"]["lockupMetadataViewModel"]["title"]["content"]
    result = combine_pages({"streams": parse_channel_tab_html(html(data), "streams")}, [])
    assert len(result) == 2 and result[0]["title"] == result[1]["title"]
    assert result[0]["video_id"] != result[1]["video_id"]


def test_invalid_baseline_type_stops_before_fetch():
    reader = Mock()
    with pytest.raises(SourceError, match="INVALID_BASELINE"):
        read_channel_tabs(reader, None)
    assert not reader.fetch.called


@pytest.mark.parametrize("kind", ["wrong-tab", "empty-unverified", "delivery-field", "bad-url", "bad-timezone", "duplicate"])
def test_combine_rejects_invalid_contract(data, kind):
    page = parse_channel_tab_html(html(data), "streams")
    if kind == "wrong-tab":page.source_tab = "videos"
    elif kind == "empty-unverified":page.items = []
    elif kind == "delivery-field":page.items[0]["delivered"] = True
    elif kind == "bad-url":page.items[0]["url"] = "https://example.invalid"
    elif kind == "bad-timezone":page.items[0]["source_metadata"]["schedule_timezone"] = "WIB"
    elif kind == "duplicate":page.items.append(deepcopy(page.items[0]))
    with pytest.raises(SourceError):
        combine_pages({"streams": page}, [])


def test_combine_refuses_shorts_and_invalid_baseline(data):
    page = parse_channel_tab_html(html(data), "streams")
    with pytest.raises(SourceError):combine_pages({"shorts": page}, [])
    with pytest.raises(SourceError):combine_pages({"streams": page}, ["invalid-id"])
    reader = Mock()
    with pytest.raises(SourceError):read_channel_tabs(reader, ["invalid-id"])
    assert not reader.fetch.called


@pytest.mark.parametrize("failed", [False, True])
def test_cli_read_only_no_telegram_or_state_writes(monkeypatch, capsys, state_dir, video_data, data, failed):
    get = Mock(side_effect=[response(video_data), response(data, 404 if failed else 200)])
    monkeypatch.setattr(requests.Session, "get", get)
    post = Mock(side_effect=AssertionError("Telegram forbidden"))
    monkeypatch.setattr(requests, "post", post)
    root = Path(__file__).resolve().parents[1]
    paths = list(state_dir.iterdir()) + [root / n for n in ("youtube_cache.json", "announcements_cache.json", "delivery_state.json")]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    assert main(["--state-dir", str(state_dir)]) == int(failed)
    output = json.loads(capsys.readouterr().out)
    assert output["health"] == ("FAILED" if failed else "HEALTHY")
    assert not post.called and get.call_count == 2
    assert [c.args[0] for c in get.call_args_list] == [URLS[t] for t in TABS]
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_cli_invalid_baseline_no_requests(monkeypatch, tmp_path, capsys):
    get = Mock()
    monkeypatch.setattr(requests.Session, "get", get)
    assert main(["--state-dir", str(tmp_path)]) == 1
    assert "FAILED" in capsys.readouterr().out and not get.called


def test_cli_requires_historical_marker_before_requests(monkeypatch, state_dir, capsys):
    path = state_dir / "delivery_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["youtube_rss_cutover"]
    atomic_json(path, state)
    get = Mock()
    monkeypatch.setattr(requests.Session, "get", get)
    assert main(["--state-dir", str(state_dir)]) == 1
    assert "FAILED" in capsys.readouterr().out and not get.called
