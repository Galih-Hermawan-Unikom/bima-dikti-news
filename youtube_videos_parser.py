"""Read-only /videos observations, deliberately not an eligible delivery format."""
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

import requests

from reliability import SourceError

VIDEOS_URL = "https://www.youtube.com/@kemdiktisaintek/videos"
CHANNEL_ID = "UCGo_6l_6kp8H8OHcKcSIeDw"
EMPTY_MESSAGES = {"this channel has no videos.", "channel ini belum memiliki video."}


class VideosPageError(SourceError):
    def __init__(self, category, *, http_status=None, parsing_errors=1, video_count=0):
        self.category = category
        self.http_status = http_status
        self.parsing_errors = parsing_errors
        self.video_count = video_count
        suffix = f" HTTP {http_status}" if http_status is not None else ""
        super().__init__(f"YouTube videos preflight: {category}{suffix}")


@dataclass
class VideosPage:
    items: list
    empty_verified: bool = False
    parsing_errors: int = 0
    http_status: int = 200


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts, self.parts, self.inside = [], [], False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inside, self.parts = True, []

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.inside:
            self.scripts.append("".join(self.parts))
            self.inside = False


def _path(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def extract_initial_data(html):
    scripts = _Scripts()
    scripts.feed(html)
    assignment = re.compile(r"""(?:\bytInitialData\s*|window\[["']ytInitialData["']\])\s*=\s*""")
    found = []
    for script in scripts.scripts:
        offset = 0
        while (match := assignment.search(script, offset)) is not None:
            value = script[match.end():].lstrip()
            try:
                data, end = json.JSONDecoder().raw_decode(value)
            except json.JSONDecodeError:
                raise VideosPageError("INITIAL_DATA_INVALID") from None
            if not isinstance(data, dict):
                raise VideosPageError("INITIAL_DATA_INVALID")
            found.append(data)
            offset = len(script) - len(value) + end
    if not found:
        raise VideosPageError("INITIAL_DATA_MISSING")
    if any(data != found[0] for data in found[1:]):
        raise VideosPageError("INITIAL_DATA_AMBIGUOUS")
    return found[0]


def _parse_lockup(lockup, channel):
    if not isinstance(lockup, dict):
        raise VideosPageError("INVALID_LOCKUP")
    if lockup.get("contentType", "LOCKUP_CONTENT_TYPE_VIDEO") != "LOCKUP_CONTENT_TYPE_VIDEO":
        raise VideosPageError("UNSUPPORTED_LOCKUP_TYPE")
    direct = lockup.get("contentId")
    endpoint = _path(lockup, "rendererContext", "commandContext", "onTap", "innertubeCommand",
                     "watchEndpoint", "videoId")
    video_id = direct or endpoint
    if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
            or (direct and endpoint and direct != endpoint)):
        raise VideosPageError("INVALID_VIDEO_ID")
    metadata = _path(lockup, "metadata", "lockupMetadataViewModel")
    title = _text(_path(metadata, "title", "content"))
    sources = _path(lockup, "contentImage", "thumbnailViewModel", "image", "sources")
    if not title or not isinstance(sources, list) or not sources:
        raise VideosPageError("LOCKUP_METADATA_MISSING")
    thumbnail = _path(sources[-1], "url")
    try:
        parts = urlsplit(thumbnail) if isinstance(thumbnail, str) else None
        valid_thumbnail = (parts is not None and parts.scheme == "https" and parts.hostname
                           and not parts.username and not parts.password)
    except ValueError:
        valid_thumbnail = False
    if not valid_thumbnail:
        raise VideosPageError("LOCKUP_METADATA_MISSING")
    rows = _path(metadata, "metadata", "contentMetadataViewModel", "metadataRows") or []
    relative = []
    if not isinstance(rows, list):
        raise VideosPageError("INVALID_LOCKUP")
    for row in rows:
        values = _path(row, "metadataParts") or []
        if not isinstance(values, list):
            raise VideosPageError("INVALID_LOCKUP")
        relative.extend(t for part in values if (t := _text(_path(part, "text", "content"))))
    return {"source": "youtube", "kind": "video", "observation_format": "youtube-videos-observation-v1",
            "video_id": video_id, "title": title, "channel_title": channel,
            "url": "https://www.youtube.com/watch?v=" + video_id, "thumbnail": thumbnail,
            "relative_metadata": relative, "documents": []}


def _empty_text(renderer):
    text = _path(renderer, "text")
    if not isinstance(text, dict):
        return ""
    if isinstance(text.get("simpleText"), str):
        return _text(text["simpleText"]).lower()
    runs = text.get("runs", [])
    if not isinstance(runs, list):
        return ""
    return _text("".join(_path(r, "text") or "" for r in runs if isinstance(_path(r, "text"), str))).lower()


def parse_videos_html(html):
    data = extract_initial_data(html)
    channel = _text(_path(data, "metadata", "channelMetadataRenderer", "title")) or _text(
        _path(data, "header", "pageHeaderRenderer", "pageTitle"))
    external_id = _path(data, "metadata", "channelMetadataRenderer", "externalId")
    if not channel or (external_id is not None and external_id != CHANNEL_ID):
        raise VideosPageError("CHANNEL_METADATA_INVALID")
    tabs = _path(data, "contents", "twoColumnBrowseResultsRenderer", "tabs")
    if not isinstance(tabs, list):
        raise VideosPageError("TAB_STRUCTURE_CHANGED")
    selected = [_path(t, "tabRenderer") for t in tabs if _path(t, "tabRenderer", "selected") is True]
    if len(selected) != 1:
        raise VideosPageError("TAB_STRUCTURE_CHANGED")
    tab = selected[0]
    endpoint = tab.get("endpoint") or tab.get("navigationEndpoint")
    target = _path(endpoint, "commandMetadata", "webCommandMetadata", "url")
    if target is not None:
        if not isinstance(target, str) or urlsplit(target).path.rstrip("/") not in {
                "/@kemdiktisaintek/videos", "/channel/" + CHANNEL_ID + "/videos"}:
            raise VideosPageError("UNEXPECTED_TAB")
    elif _text(tab.get("title")).lower() not in {"video", "videos"}:
        raise VideosPageError("UNEXPECTED_TAB")
    content = tab.get("content")
    rows = _path(content, "richGridRenderer", "contents")
    if rows is None:
        sections = _path(content, "sectionListRenderer", "contents")
        if isinstance(sections, list) and len(sections) == 1:
            rows = _path(sections[0], "itemSectionRenderer", "contents")
    if not isinstance(rows, list):
        raise VideosPageError("GRID_STRUCTURE_CHANGED")
    items, seen, errors, empty = [], {}, 0, False
    for row in rows:
        if isinstance(row, dict) and "continuationItemRenderer" in row:
            continue
        message = _path(row, "messageRenderer")
        if message is not None:
            if _empty_text(message) in EMPTY_MESSAGES:
                empty = True
            else:
                errors += 1
            continue
        lockup = _path(row, "richItemRenderer", "content", "lockupViewModel")
        try:
            item = _parse_lockup(lockup, channel)
            old = seen.get(item["video_id"])
            if old is not None:
                if old["title"] != item["title"]:
                    raise VideosPageError("CONFLICTING_DUPLICATE")
                continue
            seen[item["video_id"]] = item
            items.append(item)
        except VideosPageError:
            errors += 1
    if errors or (empty and items):
        raise VideosPageError("LOCKUP_STRUCTURE_OR_METADATA_INVALID",
                              parsing_errors=max(errors, 1), video_count=len(items))
    if not items and not empty:
        raise VideosPageError("EMPTY_UNVERIFIED")
    return VideosPage(items, empty_verified=empty)


class YouTubeVideosReader:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"})

    def fetch(self):
        response = None
        try:
            response = self.session.get(VIDEOS_URL, timeout=15, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise VideosPageError("UNEXPECTED_REDIRECT", http_status=response.status_code, parsing_errors=0)
            if response.status_code != 200:
                raise VideosPageError("HTTP_FAILURE", http_status=response.status_code, parsing_errors=0)
            try:
                return parse_videos_html(response.text)
            except VideosPageError as exc:
                exc.http_status = response.status_code
                raise
            except (TypeError, ValueError, KeyError, AttributeError):
                raise VideosPageError("PAGE_STRUCTURE_CHANGED", http_status=200) from None
        except requests.Timeout:
            raise VideosPageError("TIMEOUT", parsing_errors=0) from None
        except requests.RequestException:
            raise VideosPageError("NETWORK_ERROR", parsing_errors=0) from None
        finally:
            if response is not None:
                response.close()

    def close(self):
        self.session.close()
