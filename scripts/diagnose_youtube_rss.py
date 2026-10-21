"""One public RSS GET, production parser and User-Agent; no state or credentials."""
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import re
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from youtube_monitor import DEFAULT_CHANNEL, YouTubeMonitor
from reliability import SourceError


def main():
    monitor = YouTubeMonitor()
    expected = "https://www.youtube.com/feeds/videos.xml?channel_id=" + DEFAULT_CHANNEL
    result = {"utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "requests": requests.__version__, "channel_expected": monitor.channel_id == DEFAULT_CHANNEL,
              "http_status": None, "redirect": False, "redirect_target": "NONE",
              "content_type": None, "valid_feed": False, "entries": None,
              "category": "NONE", "requests_count": 0}
    original = monitor.session.get

    def one_get(url, **kwargs):
        if url != expected or result["requests_count"]:
            raise AssertionError("Only one exact RSS request allowed")
        result["requests_count"] += 1
        # Do not turn a redirect into an unapproved second network request.
        response = original(url, allow_redirects=False, **kwargs)
        result["http_status"] = response.status_code
        result["redirect"] = 300 <= response.status_code < 400
        content_type = response.headers.get("Content-Type", "")
        result["content_type"] = content_type if re.fullmatch(r"[a-zA-Z0-9/;= ._-]{0,120}", content_type) else "OTHER"
        if response.headers.get("Location"):
            result["redirect_target"] = "EXPECTED_RSS" if response.headers["Location"] == expected else "OTHER"
        return response

    monitor.session.get = one_get
    try:
        items = monitor.fetch_videos()
        result["valid_feed"] = True
        result["entries"] = len(items)
    except requests.HTTPError:
        result["category"] = "HTTP_ERROR"
    except requests.Timeout:
        result["category"] = "TIMEOUT"
    except requests.RequestException:
        result["category"] = "NETWORK_ERROR"
    except SourceError:
        result["category"] = "INVALID_FEED"
    except Exception:
        result["category"] = "OTHER"
    print("RSS_DIAGNOSTIC " + json.dumps(result, sort_keys=True))
    assert result["requests_count"] == 1


if __name__ == "__main__":
    main()
