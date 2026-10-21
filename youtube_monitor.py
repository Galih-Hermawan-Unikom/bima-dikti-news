"""Public RSS discovery only; publication is not evidence of playback status."""
from copy import deepcopy
from datetime import datetime, timezone
import re
import xml.etree.ElementTree as ET

import requests

from reliability import SourceError, atomic_json, read_json

RSS_FORMAT = "youtube-rss-v1"
DEFAULT_CHANNEL = "UCGo_6l_6kp8H8OHcKcSIeDw"


def normalize_text(value):
    return " ".join((value or "").split()).strip() if value else ""


def youtube_identity(item):
    if not isinstance(item, dict):
        return ""
    video_id = normalize_text(item.get("video_id", ""))
    return "video:" + video_id if video_id else ""


def publication_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


def known_video_ids(cache, delivery):
    ids = {i["video_id"] for i in cache if youtube_identity(i)}
    for batch in delivery.get("batches", []):
        ids.update(i["video_id"] for i in batch["items"] if i.get("source") == "youtube" and youtube_identity(i))
        for key in batch["event_keys"]:
            match = re.fullmatch(r"youtube:video:([^:]+):[^:]+", key)
            if match:
                ids.add(match.group(1))
    return ids


def validate_cutover(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("version") != 1
            or publication_time(value.get("cutover_at")) is None
            or not isinstance(value.get("baseline_ids"), list)
            or any(not isinstance(i, str) or not i.strip() for i in value["baseline_ids"])
            or len(set(value["baseline_ids"])) != len(value["baseline_ids"])):
        raise ValueError("Invalid YouTube RSS cutover baseline; operator review required")
    return value


def plan_rss_cutover(cache, delivery, current, cutover_at):
    """Pure proposal only: no writes, delivery acknowledgement or automatic approval."""
    if publication_time(cutover_at) is None:
        raise ValueError("Explicit timezone-aware cutover required")
    known = known_video_ids(cache, delivery)
    return {"proposed_marker": {"version": 1, "cutover_at": cutover_at, "baseline_ids": sorted(known)},
            "known_current_ids": [i["video_id"] for i in current if i["video_id"] in known],
            "requires_operator_review": [{k: i.get(k) for k in ("video_id", "title", "published")}
                                         for i in current if i["video_id"] not in known]}


class YouTubeMonitor:
    def __init__(self, channel_id=DEFAULT_CHANNEL, channel_url="https://www.youtube.com/@kemdiktisaintek",
                 cache_file="youtube_cache.json"):
        self.channel_id = normalize_text(channel_id) or DEFAULT_CHANNEL
        self.channel_url = channel_url
        self.cache_file = cache_file
        self.delivery_state = {"version": 1, "batches": []}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})

    def load_cache(self):
        items = read_json(self.cache_file, [])
        if not isinstance(items, list) or any(not youtube_identity(i) for i in items):
            raise ValueError("YouTube cache must contain stable video IDs")
        return items

    def save_cache(self, items):
        atomic_json(self.cache_file, items)

    def fetch_videos(self):
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", self.channel_id):
            raise SourceError("Invalid YouTube RSS channel ID")
        response = self.session.get("https://www.youtube.com/feeds/videos.xml?channel_id=" + self.channel_id, timeout=15)
        response.raise_for_status()
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise SourceError("Invalid YouTube RSS XML") from None
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
              "media": "http://search.yahoo.com/mrss/"}
        if root.tag != "{" + ns["atom"] + "}feed":
            raise SourceError("Invalid YouTube feed root")
        channel = normalize_text(root.findtext("atom:title", "", ns))
        items, seen = [], set()
        for entry in root.findall("atom:entry", ns):
            video_id = normalize_text(entry.findtext("yt:videoId", "", ns))
            title = normalize_text(entry.findtext("atom:title", "", ns))
            published = normalize_text(entry.findtext("atom:published", "", ns))
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not title or publication_time(published) is None:
                raise SourceError("YouTube RSS entry missing identity, title or publication time")
            if video_id in seen:
                raise SourceError("Duplicate YouTube RSS video ID")
            seen.add(video_id)
            group = entry.find("media:group", ns)
            description = group.findtext("media:description", "", ns) if group is not None else ""
            thumb = group.find("media:thumbnail", ns) if group is not None else None
            items.append({"source": "youtube", "kind": "video", "delivery_format": RSS_FORMAT,
                          "channel_title": channel or "Kemdiktisaintek", "video_id": video_id,
                          "title": title, "date": published, "published": published,
                          "description": (description or "").strip()[:1000],
                          "url": "https://www.youtube.com/watch?v=" + video_id,
                          "thumbnail": thumb.get("url", "") if thumb is not None else "",
                          "documents": [], "scraped_at": datetime.now(timezone.utc).isoformat()})
        return items

    def check_new_videos(self):
        self.proposed_cache = None
        self.reconciliation_error = ""
        self.held_items = []
        cached = self.load_cache()
        current = self.fetch_videos()
        self.current_items = current
        marker = validate_cutover(self.delivery_state.get("youtube_rss_cutover"))
        known = known_video_ids(cached, self.delivery_state)
        if marker:
            known.update(marker["baseline_ids"])
        cutoff = publication_time(marker["cutover_at"]) if marker else None
        eligible = []
        for item in current:
            if item["video_id"] in known:
                continue
            published = publication_time(item.get("published"))
            if cutoff is None or published is None or published <= cutoff:
                self.held_items.append(item)
                reason = "NO_APPROVED_CUTOVER" if cutoff is None else "PRE_CUTOVER_OR_INVALID_PUBLICATION"
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", item["video_id"]):
                    print(f"[RSS CUTOVER] held video={item['video_id']} reason={reason}")
            else:
                eligible.append(item)
            known.add(item["video_id"])
        if self.held_items:
            self.reconciliation_error = f"YouTube RSS cutover reconciliation required: {len(self.held_items)} unknown historical/unapproved IDs"
        # Preserve all old rows, metadata and ordering, including old status fields.
        self.proposed_cache = deepcopy(cached) + deepcopy(eligible)
        return eligible
