"""Read-only /videos + /streams observations. Never a delivery contract."""
import argparse
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import requests

from reliability import SourceError
from youtube_shadow_live import baseline_from_state, safe_observation, validate_baseline
from youtube_videos_parser import (CHANNEL_ID, VideosPageError, YouTubeVideosReader,
                                   _empty_text, _parse_lockup, _path, _text,
                                   extract_initial_data, parse_videos_html)

TABS = ("videos", "streams")
URLS = {tab: "https://www.youtube.com/@kemdiktisaintek/" + tab for tab in TABS}
FORMAT = "youtube-channel-tabs-observation-v1"
EMPTY_STREAMS = {"this channel has no live streams.", "channel ini belum memiliki live stream."}


@dataclass
class ChannelTabPage:
    source_tab: str
    items: list
    has_continuation: bool
    empty_verified: bool = False
    http_status: int = 200
    parsing_errors: int = 0


def _supported(tab):
    if tab not in TABS:
        raise VideosPageError("TAB_NOT_ALLOWED", parsing_errors=0)


def _selected(data, tab):
    channel = _text(_path(data, "metadata", "channelMetadataRenderer", "title"))
    if not channel or _path(data, "metadata", "channelMetadataRenderer", "externalId") != CHANNEL_ID:
        raise VideosPageError("CHANNEL_METADATA_INVALID")
    tabs = _path(data, "contents", "twoColumnBrowseResultsRenderer", "tabs")
    if not isinstance(tabs, list):
        raise VideosPageError("TAB_STRUCTURE_CHANGED")
    selected = [_path(t, "tabRenderer") for t in tabs if _path(t, "tabRenderer", "selected") is True]
    if len(selected) != 1:
        raise VideosPageError("TAB_STRUCTURE_CHANGED")
    selected = selected[0]
    # Do not accept a localized tab label as proof of the requested endpoint.
    targets = []
    for key in ("endpoint", "navigationEndpoint"):
        if key not in selected:
            continue
        target = _path(selected[key], "commandMetadata", "webCommandMetadata", "url")
        if not isinstance(target, str):
            raise VideosPageError("UNEXPECTED_TAB")
        parts = urlsplit(target)
        if (parts.scheme or parts.netloc or parts.query or parts.fragment
                or parts.path.rstrip("/") not in {"/@kemdiktisaintek/" + tab,
                                                  "/channel/" + CHANNEL_ID + "/" + tab}):
            raise VideosPageError("UNEXPECTED_TAB")
        targets.append(parts.path.rstrip("/"))
    if not targets:
        raise VideosPageError("UNEXPECTED_TAB")
    content = selected.get("content")
    rows = _path(content, "richGridRenderer", "contents")
    if rows is None:
        sections = _path(content, "sectionListRenderer", "contents")
        if isinstance(sections, list) and len(sections) == 1:
            rows = _path(sections[0], "itemSectionRenderer", "contents")
    if not isinstance(rows, list):
        raise VideosPageError("GRID_STRUCTURE_CHANGED")
    return channel, rows


def _metadata(lockup, parsed):
    relative_rows = _path(lockup, "metadata", "lockupMetadataViewModel", "metadata",
                          "contentMetadataViewModel", "metadataRows")
    if relative_rows is not None:
        if not isinstance(relative_rows, list):
            raise VideosPageError("LOCKUP_METADATA_INVALID")
        for row in relative_rows:
            parts = _path(row, "metadataParts")
            if not isinstance(parts, list) or any(not isinstance(_path(p, "text", "content"), str) for p in parts):
                raise VideosPageError("LOCKUP_METADATA_INVALID")
    overlays = _path(lockup, "contentImage", "thumbnailViewModel", "overlays")
    overlays = [] if overlays is None else overlays
    if not isinstance(overlays, list):
        raise VideosPageError("LOCKUP_METADATA_INVALID")
    badges = []
    for overlay in overlays:
        bottom = _path(overlay, "thumbnailBottomOverlayViewModel")
        if bottom is None:
            continue
        values = _path(bottom, "badges")
        if not isinstance(values, list):
            raise VideosPageError("LOCKUP_METADATA_INVALID")
        for value in values:
            badge = _path(value, "thumbnailBadgeViewModel")
            text = _path(badge, "text")
            if not isinstance(text, str) or not text.strip():
                raise VideosPageError("LOCKUP_METADATA_INVALID")
            badges.append(_text(text))
    # Display text only: neither a publication timestamp nor a playback classification.
    return {"relative_text": parsed["relative_metadata"], "badges": badges,
            "schedule_timezone": None}


def parse_channel_tab_html(html, tab):
    _supported(tab)
    try:
        channel, rows = _selected(extract_initial_data(html), tab)
        if tab == "videos":
            # Preserve the existing Phase 3 parser and its contract unchanged.
            parse_videos_html(html)
        items, seen, errors, empty = [], {}, 0, False
        for row in rows:
            if isinstance(row, dict) and "continuationItemRenderer" in row:
                continue
            message = _path(row, "messageRenderer")
            if tab == "streams" and message is not None:
                if _empty_text(message) in EMPTY_STREAMS:
                    empty = True
                else:
                    errors += 1
                continue
            if tab == "videos" and message is not None:
                empty = True  # Already validated by the unchanged videos parser.
                continue
            try:
                lockup = _path(row, "richItemRenderer", "content", "lockupViewModel")
                parsed = _parse_lockup(lockup, channel)
                safe = safe_observation(parsed, ())
                safe.pop("knowledge")
                item = {"source": "youtube", "observation_format": FORMAT, **safe,
                        "source_tabs": [tab], "source_metadata": _metadata(lockup, parsed)}
                previous = seen.get(item["video_id"])
                if previous is not None:
                    if item != previous:
                        raise VideosPageError("CONFLICTING_DUPLICATE")
                    continue
                seen[item["video_id"]] = item
                items.append(item)
            except SourceError:
                errors += 1
        if errors or (empty and items):
            raise VideosPageError("LOCKUP_STRUCTURE_OR_METADATA_INVALID",
                                  parsing_errors=max(errors, 1), video_count=len(items))
        if not items and not empty:
            raise VideosPageError("EMPTY_UNVERIFIED")
        return ChannelTabPage(tab, items, any(isinstance(r, dict) and "continuationItemRenderer" in r
                                              for r in rows), empty_verified=empty)
    except VideosPageError:
        raise
    except (TypeError, ValueError, KeyError, AttributeError):
        raise VideosPageError("PAGE_STRUCTURE_CHANGED") from None


class YouTubeChannelTabsReader(YouTubeVideosReader):
    """Same Session/User-Agent as Phase 3; fixed two-tab allowlist, no retries."""

    def fetch(self, tab):
        _supported(tab)
        response = None
        try:
            response = self.session.get(URLS[tab], timeout=15, allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise VideosPageError("UNEXPECTED_REDIRECT", http_status=response.status_code, parsing_errors=0)
            if response.status_code != 200:
                raise VideosPageError("HTTP_FAILURE", http_status=response.status_code, parsing_errors=0)
            try:
                return parse_channel_tab_html(response.text, tab)
            except VideosPageError as exc:
                exc.http_status = response.status_code
                raise
        except requests.Timeout:
            raise VideosPageError("TIMEOUT", parsing_errors=0) from None
        except requests.RequestException:
            raise VideosPageError("NETWORK_ERROR", parsing_errors=0) from None
        finally:
            if response is not None:
                response.close()


def combine_pages(pages, known_ids):
    if not isinstance(pages, dict) or any(t not in TABS for t in pages):
        raise SourceError("YouTube channel tabs: INVALID_PAGES")
    try:
        known = set(known_ids)
    except TypeError:
        raise SourceError("YouTube channel tabs: INVALID_BASELINE") from None
    if any(not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", i) for i in known):
        raise SourceError("YouTube channel tabs: INVALID_BASELINE")
    combined = {}
    for tab in TABS:
        page = pages.get(tab)
        if page is None:
            continue
        if (not isinstance(page, ChannelTabPage) or page.source_tab != tab or page.http_status != 200
                or type(page.parsing_errors) is not int or page.parsing_errors != 0
                or type(page.empty_verified) is not bool or type(page.has_continuation) is not bool
                or not isinstance(page.items, list) or bool(page.items) == bool(page.empty_verified)):
            raise SourceError("YouTube channel tabs: INVALID_PAGE_RESULT")
        for item in page.items:
            if (not isinstance(item, dict) or set(item) != {"source", "observation_format", "video_id",
                    "title", "channel_title", "url", "thumbnail", "source_tabs", "source_metadata"}
                    or item.get("source") != "youtube" or item.get("observation_format") != FORMAT
                    or item.get("source_tabs") != [tab]):
                raise SourceError("YouTube channel tabs: INVALID_OBSERVATION")
            safe = safe_observation(item, known)
            if any(item[k] != value for k, value in safe.items() if k != "knowledge"):
                raise SourceError("YouTube channel tabs: INVALID_OBSERVATION")
            meta = item["source_metadata"]
            if (not isinstance(meta, dict) or set(meta) != {"relative_text", "badges", "schedule_timezone"}
                    or meta["schedule_timezone"] is not None
                    or any(not isinstance(meta[k], list) or any(not isinstance(v, str) or not v.strip()
                           for v in meta[k]) for k in ("relative_text", "badges"))):
                raise SourceError("YouTube channel tabs: INVALID_SOURCE_METADATA")
            video_id = item["video_id"]
            if video_id not in combined:
                combined[video_id] = {k: deepcopy(item[k]) for k in
                                      ("source", "observation_format", "video_id", "title", "channel_title", "url", "thumbnail")}
                combined[video_id].update(source_tabs=[], titles_by_tab={}, metadata_by_tab={},
                                          knowledge="KNOWN" if video_id in known else "UNKNOWN")
            record = combined[video_id]
            if tab in record["source_tabs"]:
                raise SourceError("YouTube channel tabs: DUPLICATE_PAGE_ID")
            record["source_tabs"].append(tab)
            record["titles_by_tab"][tab] = item["title"]
            record["metadata_by_tab"][tab] = deepcopy(item["source_metadata"])
    for item in combined.values():
        item["title_conflict"] = len(set(item["titles_by_tab"].values())) > 1
    return list(combined.values())


def read_channel_tabs(reader, known_ids):
    try:
        known_ids = tuple(known_ids)
    except TypeError:
        raise SourceError("YouTube channel tabs: INVALID_BASELINE") from None
    combine_pages({}, known_ids)  # Reject invalid identity evidence before issuing either request.
    pages, health = {}, {}
    for tab in TABS:
        try:
            page = reader.fetch(tab)
            # Validate each result independently before retaining a successful tab.
            combine_pages({tab: page}, known_ids)
            pages[tab] = page
            health[tab] = {"health": "HEALTHY", "http_status": page.http_status,
                           "detected_count": len(page.items), "parsing_errors": 0,
                           "has_continuation": page.has_continuation, "error_category": None}
        except VideosPageError as exc:
            health[tab] = {"health": "FAILED", "http_status": exc.http_status,
                           "detected_count": 0, "parsing_errors": exc.parsing_errors,
                           "has_continuation": None, "error_category": exc.category}
        except Exception:
            health[tab] = {"health": "FAILED", "http_status": None,
                           "detected_count": 0, "parsing_errors": 1,
                           "has_continuation": None, "error_category": "INVALID_RESULT_OR_INTERNAL_FAILURE"}
    return {"mode": "read-only-shadow-evaluation", "observation_format": FORMAT,
            "observed_at_utc": datetime.now(timezone.utc).isoformat(), "channel_id": CHANNEL_ID,
            "source_tabs": list(TABS), "excluded_tabs": ["shorts"], "pages": health,
            "health": "HEALTHY" if len(pages) == len(TABS) else "FAILED",
            "observations": combine_pages(pages, known_ids), "delivery_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    reader = None
    try:
        baseline = baseline_from_state(args.state_dir)
        validate_baseline(baseline)
        known = set(baseline["ids"])
        reader = YouTubeChannelTabsReader()
        result = read_channel_tabs(reader, known)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result["health"] == "HEALTHY" else 1
    except (SourceError, OSError, ValueError, TypeError, KeyError):
        print("YouTube channel tabs FAILED: invalid state or evaluation; no delivery or state writes")
        return 1
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
