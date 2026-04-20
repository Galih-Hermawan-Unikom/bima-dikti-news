import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

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
            "status": normalize_text(item.get("status", "")),
            "scheduled_start": normalize_text(item.get("scheduled_start", "")),
            "actual_end": normalize_text(item.get("actual_end", "")),
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

    def _analyze_video_type(self, video_id):
        url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            html = response.text
            match = re.search(r"ytInitialPlayerResponse\s*=\s*({.+?});", html)
            if not match:
                return {"status": "VOD"}

            data = json.loads(match.group(1))
            video_details = data.get("videoDetails", {})
            is_live_content = video_details.get("isLiveContent", False)
            
            microformat = data.get("microformat", {}).get("playerMicroformatRenderer", {})
            live_details = microformat.get("liveBroadcastDetails", {})
            
            is_live_now = live_details.get("isLiveNow", False)
            start_timestamp = live_details.get("startTimestamp")
            end_timestamp = live_details.get("endTimestamp")

            now = datetime.now(timezone.utc)

            if is_live_content:
                if is_live_now:
                    status = "LIVE"
                elif end_timestamp:
                    status = "VOD"
                elif start_timestamp:
                    start_dt = datetime.fromisoformat(start_timestamp.replace("Z", "+00:00"))
                    if start_dt > now:
                        status = "UPCOMING"
                    else:
                        status = "VOD"
                else:
                    status = "VOD"
            else:
                status = "VOD"

            return {
                "status": status,
                "scheduled_start": start_timestamp,
                "actual_end": end_timestamp
            }
        except Exception as e:
            print(f"  [YouTube] Gagal memproses status video {video_id}: {e}")
            return {"status": "VOD"}

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
        current = self.fetch_videos()
        cached = self.load_cache()

        print(f"  [YouTube] Ditemukan {len(current)} video di feed")
        print(f"  [YouTube] Cache berisi {len(cached)} video")

        if not current and cached:
            print("  [YouTube] Menggunakan cache terakhir karena feed kosong")
            return []

        cached_dict = {youtube_identity(item): item for item in cached}
        new_items = []

        # Status yang perlu dicek ulang tiap siklus (selama masih di RSS feed):
        # - UPCOMING : bisa berubah ke LIVE atau VOD -> kirim notifikasi jika berubah
        # - LIVE     : bisa berubah ke VOD setelah selesai -> update cache, tanpa notifikasi baru
        # - ""       : data lama tanpa status -> isi sekali, tanpa notifikasi baru
        RECHECK_STATUSES = {"UPCOMING", "LIVE", ""}
        current_ids = {item["video_id"] for item in current}
        ids_to_inspect = set()

        for item in current:
            key = youtube_identity(item)
            if key not in cached_dict:
                # Video baru
                ids_to_inspect.add(item["video_id"])
            elif cached_dict[key].get("status", "") in RECHECK_STATUSES:
                # Video lama yang masih perlu dicek ulang
                ids_to_inspect.add(item["video_id"])

        if ids_to_inspect:
            print(f"  [YouTube] Deep inspection pada {len(ids_to_inspect)} video...")

        analysis_cache = {}
        for video_id in ids_to_inspect:
            print(f"  [YouTube] Mengecek status: {video_id}")
            analysis_cache[video_id] = self._analyze_video_type(video_id)

        for item in current:
            key = youtube_identity(item)
            video_id = item["video_id"]

            if key not in cached_dict:
                # --- Video BARU ---
                analysis = analysis_cache.get(video_id, {"status": "VOD"})
                item["status"] = analysis.get("status", "VOD")
                item["scheduled_start"] = analysis.get("scheduled_start") or ""
                item["actual_end"] = analysis.get("actual_end") or ""
                new_items.append(item)

            else:
                cached_item = cached_dict[key]
                cached_status = cached_item.get("status", "")

                if cached_status == "UPCOMING":
                    # --- Video UPCOMING: cek apakah status berubah ---
                    analysis = analysis_cache.get(video_id, {"status": "UPCOMING"})
                    new_status = analysis.get("status", "UPCOMING")
                    item["scheduled_start"] = analysis.get("scheduled_start") or cached_item.get("scheduled_start", "")
                    item["actual_end"] = analysis.get("actual_end") or ""
                    if new_status != "UPCOMING":
                        print(f"  [YouTube] Status berubah (UPCOMING -> {new_status}): {item['title']}")
                        item["status"] = new_status
                        item["is_status_update"] = True
                        new_items.append(item)
                    else:
                        item["status"] = "UPCOMING"

                elif cached_status == "LIVE":
                    # --- Video LIVE: cek apakah sudah selesai (VOD) ---
                    analysis = analysis_cache.get(video_id, {"status": "LIVE"})
                    new_status = analysis.get("status", "LIVE")
                    item["scheduled_start"] = analysis.get("scheduled_start") or cached_item.get("scheduled_start", "")
                    item["actual_end"] = analysis.get("actual_end") or ""
                    item["status"] = new_status
                    if new_status != "LIVE":
                        print(f"  [YouTube] Status berubah (LIVE -> {new_status}): {item['title']}")
                    # Tidak kirim notifikasi baru — sudah pernah dikirim saat pertama terdeteksi

                elif cached_status == "":
                    # --- Video tanpa status (data lama): isi sekali, tanpa notifikasi baru ---
                    if video_id in analysis_cache:
                        analysis = analysis_cache[video_id]
                        item["status"] = analysis.get("status", "VOD")
                        item["scheduled_start"] = analysis.get("scheduled_start") or ""
                        item["actual_end"] = analysis.get("actual_end") or ""
                        print(f"  [YouTube] Mengisi status lama: {item['title']} -> {item['status']}")
                    else:
                        item["status"] = cached_item.get("status", "")
                        item["scheduled_start"] = cached_item.get("scheduled_start", "")
                        item["actual_end"] = cached_item.get("actual_end", "")

                else:
                    # --- VOD biasa: pertahankan dari cache ---
                    item["status"] = cached_item.get("status", "VOD")
                    item["scheduled_start"] = cached_item.get("scheduled_start", "")
                    item["actual_end"] = cached_item.get("actual_end", "")

        # Terapkan juga ke cached_dict untuk video yang tidak ada di current (old_cache_only)
        for video_id, analysis in analysis_cache.items():
            key = f"video:{video_id}"
            if key in cached_dict and cached_dict[key].get("status", "") in RECHECK_STATUSES:
                cached_dict[key]["status"] = analysis.get("status", "VOD")
                cached_dict[key]["scheduled_start"] = analysis.get("scheduled_start") or ""
                cached_dict[key]["actual_end"] = analysis.get("actual_end") or ""

        # Simpan cache jika:
        # - ada item baru, atau
        # - cache belum ada, atau
        # - ada video yang statusnya baru saja diinspeksi
        status_filled = bool(ids_to_inspect)
        if new_items or not cached or status_filled:
            # Untuk setiap item di current yang statusnya masih kosong,
            # pertahankan status dari cached_dict (data lebih kaya dari sebelumnya)
            for item in current:
                if not item.get("status"):
                    key = youtube_identity(item)
                    cached_item = cached_dict.get(key, {})
                    if cached_item.get("status"):
                        item["status"] = cached_item["status"]
                        item["scheduled_start"] = cached_item.get("scheduled_start", "")
                        item["actual_end"] = cached_item.get("actual_end", "")

            # Gabungkan current (sudah diupdate) + sisa cache yang tidak ada di feed
            current_keys = {youtube_identity(i) for i in current}
            old_cache_only = [cached_dict[youtube_identity(i)] for i in cached
                              if youtube_identity(i) not in current_keys
                              and youtube_identity(i) in cached_dict]
            all_items = current + old_cache_only
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
