"""Read-only observations: KNOWN is not delivered, UNKNOWN is not new."""
import argparse
import json
from pathlib import Path
import re

from reliability import SourceError, read_json
from youtube_monitor import known_video_ids, validate_cutover, youtube_identity
from youtube_videos_parser import VideosPageError, YouTubeVideosReader


def load_known_ids(root):
    try:
        for name in ("youtube_cache.json", "delivery_state.json"):
            if not (root / name).is_file():
                raise ValueError()
        cache = read_json(root / "youtube_cache.json", None)
        journal = read_json(root / "delivery_state.json", None)
        if not isinstance(cache, list) or any(not youtube_identity(i) for i in cache):
            raise ValueError()
        if (not isinstance(journal, dict) or journal.get("version") != 1
                or not isinstance(journal.get("batches"), list)):
            raise ValueError()
        for batch in journal["batches"]:
            if (not isinstance(batch, dict) or not isinstance(batch.get("items"), list)
                    or any(not isinstance(i, dict) for i in batch["items"])
                    or not isinstance(batch.get("event_keys"), list)
                    or any(not isinstance(k, str) for k in batch["event_keys"])
                    or not isinstance(batch.get("delivered"), bool)):
                raise ValueError()
        marker = validate_cutover(journal.get("youtube_rss_cutover"))
        known = known_video_ids(cache, journal)
        known |= set(marker["baseline_ids"]) if marker else set()
        if any(not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", i) for i in known):
            raise ValueError()
        return known
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise SourceError("YouTube videos preflight: STATE_INVALID") from None


def classify_observations(items, known):
    return [dict(item, knowledge="KNOWN" if item["video_id"] in known else "UNKNOWN") for item in items]


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent)
    options = args.parse_args(argv)
    reader = None
    try:
        known = load_known_ids(options.state_dir)
        reader = YouTubeVideosReader()
        result = reader.fetch()
        print(json.dumps({"http_status": result.http_status, "videos": len(result.items),
                          "parsing_errors": result.parsing_errors, "empty_verified": result.empty_verified}))
        for item in classify_observations(result.items, known):
            print(json.dumps({k: item[k] for k in ("video_id", "title", "knowledge")}, ensure_ascii=True))
        return 0
    except VideosPageError as exc:
        print(json.dumps({"http_status": exc.http_status, "videos": exc.video_count,
                          "parsing_errors": exc.parsing_errors, "error": exc.category}))
        return 1
    except SourceError:
        print(json.dumps({"http_status": None, "videos": 0, "parsing_errors": 0, "error": "STATE_INVALID"}))
        return 1
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
