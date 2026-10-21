"""One RSS request only; read-only baseline, no production runner or retry."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from youtube_monitor import DEFAULT_CHANNEL, YouTubeMonitor, publication_time, validate_cutover

EXPECTED_CUTOVER = "2026-10-21T01:32:44.160012+00:00"
URL = "https://www.youtube.com/feeds/videos.xml?channel_id=" + DEFAULT_CHANNEL
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"


def main():
    state = json.loads((ROOT / "delivery_state.json").read_text(encoding="utf-8"))
    marker = validate_cutover(state.get("youtube_rss_cutover"))
    if marker is None or marker["cutover_at"] != EXPECTED_CUTOVER:
        raise RuntimeError("Cutover baseline mismatch; no request performed")
    baseline = set(marker["baseline_ids"])
    cached = json.loads((ROOT / "youtube_cache.json").read_text(encoding="utf-8"))
    cached_ids = {i["video_id"] for i in cached}
    approved_historical = {"vH4lgwut_wI", "SPzLQ-H4Fcw", "4ZlcLMqtPYI", "oC6JCsrKdoc"}
    keys = [k for b in state["batches"] if b["delivered"] for k in b["event_keys"]]
    if (len(baseline) != 133 or len(cached) != 129 or len(cached_ids) != 129
            or baseline != cached_ids | approved_historical
            or keys.count("youtube:video:K94OBen4CiU:initial") != 1
            or any(not b["delivered"] for b in state["batches"])):
        raise RuntimeError("Historical baseline/journal mismatch; no request performed")
    now = datetime.now(timezone.utc)
    report = {"utc": now.isoformat(), "wib": now.astimezone(timezone(timedelta(hours=7))).isoformat(),
              "http_status": None, "content_type": None, "redirect": None,
              "xml_valid": None, "video_entries": None, "unique_video_ids": None,
              "baseline_matches": None, "new_id_count": None, "new_items": []}
    monitor = YouTubeMonitor()
    monitor.session.trust_env = False  # No ambient proxy, netrc or credentials.
    response = None
    try:
        response = monitor.session.get(URL, timeout=15, allow_redirects=False)
        report["http_status"] = response.status_code
        report["redirect"] = 300 <= response.status_code < 400
        content_type = response.headers.get("Content-Type", "")
        report["content_type"] = content_type if re.fullmatch(r"[a-zA-Z0-9/;= ._-]{0,120}", content_type) else "OTHER"
        if response.status_code == 200:
            report["xml_valid"] = False
            root = ET.fromstring(response.content)
            if root.tag != ATOM + "feed" or root.findtext(YT + "channelId") != DEFAULT_CHANNEL:
                raise ValueError("Invalid YouTube Atom feed")
            items = []
            for entry in root.findall(ATOM + "entry"):
                video_id = (entry.findtext(YT + "videoId") or "").strip()
                title = (entry.findtext(ATOM + "title") or "").strip()
                published = (entry.findtext(ATOM + "published") or "").strip()
                timestamp = publication_time(published)
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) or not title or timestamp is None:
                    raise ValueError("Required feed metadata unavailable")
                items.append({"video_id": video_id, "title": title, "published": published,
                              "after_cutover": timestamp > publication_time(EXPECTED_CUTOVER)})
            ids = {i["video_id"] for i in items}
            new_ids = ids - baseline
            report.update(xml_valid=True, video_entries=len(items), unique_video_ids=len(ids),
                          baseline_matches=len(ids & baseline), new_id_count=len(new_ids),
                          new_items=[i for i in items if i["video_id"] in new_ids])
    except (requests.RequestException, ValueError, ET.ParseError):
        # Report safe fields only; never raw exceptions, bodies or headers.
        pass
    finally:
        if response is not None:
            response.close()
        monitor.session.close()
    print("RSS_CUTOVER_DIAGNOSTIC " + json.dumps(report, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
