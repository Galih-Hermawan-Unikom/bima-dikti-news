import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from reliability import SourceError, atomic_json, read_json


def normalize_text(value):
    return " ".join((value or "").split()).strip() if value else ""


def youtube_identity(item):
    if not isinstance(item, dict):
        return ""

    video_id = normalize_text(item.get("video_id", ""))
    if video_id:
        return f"video:{video_id}"

    link = normalize_text(item.get("url", ""))
    if link:
        return f"url:{link}"

    return ""


def historical_unavailable(previous, unavailable, now):
    if not previous or not unavailable:
        return False
    try:
        start = datetime.fromisoformat(previous.get("scheduled_start", "").replace("Z", "+00:00"))
        return start.tzinfo is not None and now - start >= timedelta(days=7)
    except (ValueError, TypeError, AttributeError):
        return False


class YouTubeMonitor:
    def __init__(
        self,
        channel_id="UCGo_6l_6kp8H8OHcKcSIeDw",
        channel_url="https://www.youtube.com/@kemdiktisaintek",
        cache_file="youtube_cache.json",
    ):
        self.channel_id = normalize_text(channel_id)
        self.channel_url = normalize_text(channel_url)
        self.cache_file = cache_file
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            }
        )

    def load_cache(self):
        items = read_json(self.cache_file, [])
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            raise ValueError("YouTube cache must be a list of objects")
        return items

    def save_cache(self, items):
        atomic_json(self.cache_file, items)

    def _resolve_channel_id(self):
        if self.channel_id:
            return self.channel_id

        if not self.channel_url:
            return ""

        try:
            response = self.session.get(self.channel_url, timeout=15)
            response.raise_for_status()
        except requests.RequestException:
            raise SourceError("YouTube channel lookup failed") from None

        patterns = [
            r'"channelId":"(UC[a-zA-Z0-9_-]{22})"',
            r'"externalId":"(UC[a-zA-Z0-9_-]{22})"',
        ]

        for pattern in patterns:
            match = re.search(pattern, response.text)
            if match:
                self.channel_id = match.group(1)
                return self.channel_id

        return ""

    def _analyze_video_type(self, video_id):
        try:
            response = self.session.get(f"https://www.youtube.com/watch?v={video_id}", timeout=15)
            response.raise_for_status()
            match = re.search(r"ytInitialPlayerResponse\s*=\s*", response.text)
            if not match:
                return {"status": "UNKNOWN"}
            data, _ = json.JSONDecoder().raw_decode(response.text[match.end():].lstrip())
            details = data.get("videoDetails", {})
            playability = data.get("playabilityStatus", {})
            # Only explicit unavailable-player evidence can lower severity.
            if (not details and playability.get("status") == "ERROR"
                    and normalize_text(playability.get("reason", "")).casefold() in {
                        "video unavailable", "this video is unavailable",
                        "video tidak tersedia", "video ini tidak tersedia"}):
                return {"status": "UNKNOWN", "unavailable": True}
            if not details or data.get("playabilityStatus", {}).get("status", "OK") not in {"OK", "LIVE_STREAM_OFFLINE"}:
                return {"status": "UNKNOWN"}
            live = data.get("microformat", {}).get("playerMicroformatRenderer", {}).get("liveBroadcastDetails", {})
            start = live.get("startTimestamp", "")
            end = live.get("endTimestamp", "")
            if live.get("isLiveNow") or details.get("isLive"):
                status = "LIVE"
            elif end:
                status = "VOD"
            elif details.get("isUpcoming") or (details.get("isLiveContent") and start):
                # A scheduled start in the past does not prove the stream ended.
                status = "UPCOMING"
            elif details.get("isLiveContent"):
                status = "UNKNOWN"
            elif details.get("isLiveContent") is False:
                status = "VOD"
            else:
                status = "UNKNOWN"
            return {"status": status, "scheduled_start": start, "actual_end": end}
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return {"status": "UNKNOWN"}

    def fetch_videos(self):
        channel_id = self._resolve_channel_id()
        if not channel_id:
            raise SourceError("YouTube channel ID not found")

        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        print(f"  [YouTube] Mengambil RSS: {url}")

        response = self.session.get(url, timeout=15)
        response.raise_for_status()

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise SourceError("Invalid YouTube RSS XML") from None

        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }

        if root.tag != "{" + ns["atom"] + "}feed":
            raise SourceError("Invalid YouTube feed root")

        channel_title = normalize_text(root.findtext("atom:title", default="", namespaces=ns))
        items = []

        for entry in root.findall("atom:entry", ns):
            link_el = entry.find("atom:link", ns)
            link = normalize_text(link_el.attrib.get("href", "")) if link_el is not None else ""
            if "/shorts/" in link:
                continue

            video_id = normalize_text(entry.findtext("yt:videoId", default="", namespaces=ns))
            if not video_id or not entry.findtext("atom:title", default="", namespaces=ns):
                raise SourceError("YouTube entry missing identity or title")

            media_group = entry.find("media:group", ns)
            description = ""
            thumbnail = ""
            if media_group is not None:
                description = media_group.findtext("media:description", default="", namespaces=ns)
                thumb_el = media_group.find("media:thumbnail", ns)
                thumbnail = normalize_text(thumb_el.attrib.get("url", "")) if thumb_el is not None else ""

            published = normalize_text(
                entry.findtext("atom:published", default="", namespaces=ns)
            )

            # Catatan: status video TIDAK diambil di sini.
            # _analyze_video_type hanya dipanggil di check_new_videos untuk
            # video baru atau yang masih berstatus UPCOMING di cache.
            items.append(
                {
                    "source": "youtube",
                    "kind": "video",
                    "channel_title": channel_title or "Kemdiktisaintek",
                    "title": normalize_text(
                        entry.findtext("atom:title", default="", namespaces=ns)
                    ),
                    "video_id": video_id,
                    "date": published,
                    "published": published,
                    "description": (description or "").strip()[:1000],
                    "thumbnail": thumbnail,
                    "url": link or f"https://www.youtube.com/watch?v={video_id}",
                    "documents": [],
                    "scraped_at": datetime.now().isoformat(),
                    "status": "",
                    "scheduled_start": "",
                    "actual_end": "",
                }
            )

        return items

    def check_new_videos(self):
        self.inspection_failed = False
        self.historical_warning_count = 0
        self.rss_error = ""
        self.proposed_cache = None
        cached = self.load_cache()
        try:
            current = self.fetch_videos()
        except (requests.RequestException, SourceError) as exc:
            # RSS discovers videos; its outage must not block known video IDs.
            detail = str(exc) if isinstance(exc, SourceError) else type(exc).__name__
            response = getattr(exc, "response", None)
            if response is not None:
                detail += f" HTTP {response.status_code}"
            self.rss_error = "YouTube RSS failed: " + detail
            if not cached:
                raise SourceError(self.rss_error) from None
            current = []
        old = {youtube_identity(i): dict(i) for i in cached}
        self.current_items = current
        new_items = []
        updated = {}
        now = datetime.now(timezone.utc)
        # Recheck unresolved streams even after they leave the short RSS window.
        current_keys = {youtube_identity(v) for v in current}
        missing = [dict(i) for i in cached if i.get("status", "") in {"UNKNOWN", "UPCOMING", "LIVE", ""}
                   and youtube_identity(i) not in current_keys]
        for item in current + missing:
            key = youtube_identity(item)
            previous = old.get(key)
            previous_status = previous.get("status", "") if previous else ""
            if previous is None or previous_status in {"", "UNKNOWN", "UPCOMING", "LIVE"}:
                analysis = dict(self._analyze_video_type(item["video_id"]))
                unavailable = analysis.pop("unavailable", False) is True
                status = analysis.get("status", "UNKNOWN")
                if status == "UNKNOWN":
                    if historical_unavailable(previous, unavailable, now):
                        self.historical_warning_count += 1
                    else:
                        self.inspection_failed = True
                    item["last_known_status"] = (previous or {}).get("last_known_status", previous_status)
                    if previous and previous.get("scheduled_start") and not analysis.get("scheduled_start"):
                        analysis["scheduled_start"] = previous["scheduled_start"]
                else:
                    item["last_known_status"] = status
                item.update(analysis)
            else:
                item.update({name: previous.get(name, "") for name in
                             ("status", "scheduled_start", "actual_end", "last_known_status")})
            last_known = (previous or {}).get("last_known_status") or (previous_status if previous_status != "UNKNOWN" else "")
            if previous is None:
                new_items.append(item)
            elif ((last_known == "UPCOMING" and item["status"] in {"LIVE", "VOD"})
                  or (previous_status == "UNKNOWN" and not last_known and item["status"] in {"UPCOMING", "LIVE"})):
                item["is_status_update"] = True
                new_items.append(item)
            updated[key] = item
        self.proposed_cache = [updated.pop(youtube_identity(i), i) for i in cached] + list(updated.values())
        return new_items
