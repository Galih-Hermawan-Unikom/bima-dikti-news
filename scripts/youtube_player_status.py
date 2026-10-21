"""One-request observer of the production player parser; sanitized output only."""
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import platform
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from youtube_monitor import YouTubeMonitor

VIDEO_ID = "K94OBen4CiU"
PLAYABILITY = {"OK", "ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "LIVE_STREAM_OFFLINE",
               "CONTENT_CHECK_REQUIRED", "AGE_CHECK_REQUIRED"}
CATEGORIES = {"HTTP_ERROR", "TIMEOUT", "NETWORK_ERROR", "PLAYER_RESPONSE_MISSING",
              "PLAYER_JSON_INVALID", "PLAYABILITY_DENIED", "VIDEO_DETAILS_MISSING",
              "STATUS_METADATA_INSUFFICIENT", "OTHER"}


def main():
    report = {"video_id": VIDEO_ID, "http_status": None, "player_response_found": False,
              "playability_status": "OTHER", "video_details_available": False,
              "python": platform.python_version(), "requests": requests.__version__,
              "utc": datetime.now(timezone.utc).isoformat(), "http_attempts": 0,
              "additional_request_blocked": False,
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    monitor = YouTubeMonitor()
    original_decoder = json.JSONDecoder
    original_send = monitor.session.send

    class ObservedDecoder(original_decoder):
        def raw_decode(self, text, idx=0):
            # Observe the exact object parsed by production, without reparsing.
            data, end = super().raw_decode(text, idx)
            if isinstance(data, dict):
                playability = data.get("playabilityStatus", {})
                status = playability.get("status") if isinstance(playability, dict) else None
                report["playability_status"] = status if status in PLAYABILITY else "OTHER"
                report["video_details_available"] = bool(data.get("videoDetails"))
            return data, end

    def observe_response(response, *args, **kwargs):
        report["http_status"] = response.status_code
        report["player_response_found"] = bool(re.search(r"ytInitialPlayerResponse\s*=\s*", response.text))
        return response

    def one_request(prepared, **kwargs):
        if report["http_attempts"] or prepared.method != "GET" or prepared.url != "https://www.youtube.com/watch?v=" + VIDEO_ID:
            report["additional_request_blocked"] = True
            raise requests.RequestException("Diagnostic request budget reached")
        report["http_attempts"] += 1
        return original_send(prepared, **kwargs)

    monitor.session.hooks["response"].append(observe_response)
    monitor.session.send = one_request
    json.JSONDecoder = ObservedDecoder
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = monitor._analyze_video_type(VIDEO_ID)
        status = result.get("status", "UNKNOWN")
        report["classification"] = status if status in {"VOD", "LIVE", "UPCOMING"} else "UNKNOWN"
        category = getattr(monitor, "inspection_diagnostics", {}).get(VIDEO_ID, "OTHER")
        report["category"] = (category if category in CATEGORIES else "OTHER") if report["classification"] == "UNKNOWN" else "NONE"
    except Exception:
        report.update(classification="UNKNOWN", category="OTHER")
    finally:
        json.JSONDecoder = original_decoder
        monitor.session.close()
    print("PLAYER_DIAGNOSTIC " + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
