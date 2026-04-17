import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime

import requests


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

    def _sanitize_item_for_cache(self, item):
        return {
            "source": "youtube",
            "kind": "video",
            "channel_title": normalize_text(item.get("channel_title", "")),
            "title": normalize_text(item.get("title", "")),
            "video_id": normalize_text(item.get("video_id", "")),
            "date": normalize_text(item.get("date", "")),
            "published": normalize_text(item.get("published", "")),
            "description": (item.get("description", "") or "").strip()[:1000],
            "thumbnail": normalize_text(item.get("thumbnail", "")),
            "url": normalize_text(item.get("url", "")),
            "scraped_at": normalize_text(item.get("scraped_at", "")),
        }

    def load_cache(self):
        if not os.path.exists(self.cache_file):
            return []

        try:
            with open(self.cache_file, "r", encoding="utf-8") as file:
                raw_cache = json.load(file)
        except (json.JSONDecodeError, OSError):
            return []

        if not isinstance(raw_cache, list):
            return []

        return [
            item
            for item in (self._sanitize_item_for_cache(entry) for entry in raw_cache)
            if youtube_identity(item)
        ]

    def save_cache(self, items):
        sanitized = [
            item
            for item in (self._sanitize_item_for_cache(entry) for entry in items)
            if youtube_identity(item)
        ]

        with open(self.cache_file, "w", encoding="utf-8") as file:
            json.dump(sanitized, file, ensure_ascii=False, indent=2)

    def _resolve_channel_id(self):
        if self.channel_id:
            return self.channel_id

        if not self.channel_url:
            return ""

        try:
            response = self.session.get(self.channel_url, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"  [YouTube] Gagal membuka channel page: {exc}")
            return ""

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

    def fetch_videos(self):
        channel_id = self._resolve_channel_id()
        if not channel_id:
            print("  [YouTube] Channel ID tidak ditemukan")
            return []

        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        print(f"  [YouTube] Mengambil RSS: {url}")

        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"  [YouTube] Gagal mengambil RSS: {exc}")
            return []

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            print(f"  [YouTube] RSS tidak valid: {exc}")
            return []

        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }

        channel_title = normalize_text(root.findtext("atom:title", default="", namespaces=ns))
        items = []

        for entry in root.findall("atom:entry", ns):
            link_el = entry.find("atom:link", ns)
            link = normalize_text(link_el.attrib.get("href", "")) if link_el is not None else ""
            if "/shorts/" in link:
                continue

            video_id = normalize_text(entry.findtext("yt:videoId", default="", namespaces=ns))
            if not video_id:
                continue

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
                }
            )

        return items

    def check_new_videos(self):
        current = self.fetch_videos()
        cached = self.load_cache()

        print(f"  [YouTube] Ditemukan {len(current)} video di feed")
        print(f"  [YouTube] Cache berisi {len(cached)} video")

        if not current and cached:
            print("  [YouTube] Menggunakan cache terakhir karena feed kosong")
            return []

        cached_keys = {youtube_identity(item) for item in cached}
        new_items = [
            item for item in current if youtube_identity(item) not in cached_keys
        ]

        if new_items or not cached:
            all_items = current + cached
            unique = []
            seen = set()
            for item in all_items:
                key = youtube_identity(item)
                if not key or key in seen:
                    continue
                seen.add(key)
                unique.append(item)
            self.save_cache(unique)

        return new_items
