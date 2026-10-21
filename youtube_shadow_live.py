"""One read-only /videos fetch; write a sanitized snapshot outside Git only."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from reliability import SourceError, atomic_json, read_json
from youtube_monitor import publication_time, validate_cutover
from youtube_shadow import new_shadow, observe_snapshot
from youtube_videos_parser import CHANNEL_ID, VIDEOS_URL, VideosPage, VideosPageError, YouTubeVideosReader
from youtube_videos_preflight import load_known_ids

REPOSITORY = "Galih-Hermawan-Unikom/bima-dikti-news"
SOURCE = "youtube-videos-shadow-v1"
ERRORS = {"HTTP_FAILURE", "UNEXPECTED_REDIRECT", "TIMEOUT", "NETWORK_ERROR",
          "INITIAL_DATA_MISSING", "INITIAL_DATA_INVALID", "INITIAL_DATA_AMBIGUOUS",
          "CHANNEL_METADATA_INVALID", "TAB_STRUCTURE_CHANGED", "UNEXPECTED_TAB",
          "GRID_STRUCTURE_CHANGED", "LOCKUP_STRUCTURE_OR_METADATA_INVALID",
          "EMPTY_UNVERIFIED", "PAGE_STRUCTURE_CHANGED", "INVALID_PARSER_RESULT",
          "INTERNAL_FAILURE"}
OBSERVATION_FIELDS = {"video_id", "title", "channel_title", "url", "thumbnail", "knowledge"}
SNAPSHOT_FIELDS = {"schema_version", "source", "channel_id", "source_url", "repository",
                   "commit_sha", "github_run_id", "github_run_attempt", "snapshot_id",
                   "observed_at_utc", "http_status", "parser_health", "parsing_errors",
                   "detected_count", "empty_verified", "error_category", "baseline",
                   "observations", "sha256"}


def require(condition, category):
    if not condition:
        raise SourceError("YouTube live shadow: " + category)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def outside_git(path):
    resolved = Path(path).resolve()
    require(not any((p / ".git").exists() for p in (resolved, *resolved.parents)), "OUTPUT_INSIDE_GIT")
    require(not resolved.exists(), "OUTPUT_ALREADY_EXISTS")
    return resolved


def baseline_from_state(root):
    root = Path(root)
    ids = sorted(load_known_ids(root))
    marker = validate_cutover(read_json(root / "delivery_state.json", None).get("youtube_rss_cutover"))
    require(marker is not None, "CUTOVER_BASELINE_MISSING")
    core = {"ids": ids, "cutover_version": marker["version"], "cutover_at": marker["cutover_at"]}
    return dict(core, sha256=digest(core))


def validate_baseline(baseline):
    require(isinstance(baseline, dict) and set(baseline) ==
            {"ids", "cutover_version", "cutover_at", "sha256"}, "BASELINE_INVALID")
    ids = baseline["ids"]
    require(isinstance(ids, list) and all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", v)
                                        for v in ids), "BASELINE_INVALID")
    require(ids == sorted(set(ids)) and baseline["cutover_version"] == 1
            and type(baseline["cutover_version"]) is int
            and publication_time(baseline["cutover_at"]) is not None, "BASELINE_INVALID")
    require(baseline["sha256"] == digest({k: v for k, v in baseline.items() if k != "sha256"}),
            "BASELINE_CHECKSUM_INVALID")


def metadata(repository, commit_sha, run_id, attempt):
    require(repository == REPOSITORY and isinstance(commit_sha, str)
            and re.fullmatch(r"[0-9a-f]{40}", commit_sha), "RUN_METADATA_INVALID")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]*", run_id)
            and type(attempt) is int and attempt > 0, "RUN_METADATA_INVALID")
    return {"repository": repository, "commit_sha": commit_sha, "github_run_id": run_id,
            "github_run_attempt": attempt, "snapshot_id": f"gh-{run_id}-attempt-{attempt}"}


def safe_observation(item, known):
    video_id = item.get("video_id")
    require(isinstance(video_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id), "INVALID_PARSER_RESULT")
    title, channel = item.get("title"), item.get("channel_title")
    require(isinstance(title, str) and title.strip() and isinstance(channel, str) and channel.strip(),
            "INVALID_PARSER_RESULT")
    # Only a public thumbnail path is retained; never persist signed query parameters.
    parts = urlsplit(item.get("thumbnail", ""))
    thumbnail = ""
    if (parts.scheme == "https" and parts.netloc in {"i.ytimg.com", "i9.ytimg.com"}
            and re.fullmatch(r"/(?:vi|vi_webp)/[A-Za-z0-9_-]{11}/[A-Za-z0-9_.-]+", parts.path)):
        thumbnail = "https://" + parts.netloc + parts.path
    return {"video_id": video_id, "title": title, "channel_title": channel,
            "url": "https://www.youtube.com/watch?v=" + video_id, "thumbnail": thumbnail,
            "knowledge": "KNOWN" if video_id in known else "UNKNOWN"}


def as_page(snapshot):
    return VideosPage([dict(i, source="youtube", observation_format="youtube-videos-observation-v1")
                       for i in snapshot["observations"]], empty_verified=snapshot["empty_verified"])


def validate_snapshot(value):
    require(isinstance(value, dict) and set(value) == SNAPSHOT_FIELDS, "SCHEMA_INVALID")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["source"] == SOURCE and value["channel_id"] == CHANNEL_ID
            and value["source_url"] == VIDEOS_URL, "SCHEMA_INCOMPATIBLE")
    run = metadata(value["repository"], value["commit_sha"], value["github_run_id"], value["github_run_attempt"])
    require(all(value[k] == v for k, v in run.items()), "RUN_METADATA_INVALID")
    when = publication_time(value["observed_at_utc"])
    require(when is not None and isinstance(value["observed_at_utc"], str)
            and datetime.fromisoformat(value["observed_at_utc"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0,
            "OBSERVATION_TIME_INVALID")
    validate_baseline(value["baseline"])
    require(value["sha256"] == digest({k: v for k, v in value.items() if k != "sha256"}), "CHECKSUM_INVALID")
    require(value["http_status"] is None or (type(value["http_status"]) is int
            and 100 <= value["http_status"] <= 599), "HTTP_STATUS_INVALID")
    require(type(value["parsing_errors"]) is int and value["parsing_errors"] >= 0
            and type(value["detected_count"]) is int and value["detected_count"] >= 0
            and type(value["empty_verified"]) is bool and isinstance(value["observations"], list), "RESULT_INVALID")
    ids = set()
    for item in value["observations"]:
        require(isinstance(item, dict) and set(item) == OBSERVATION_FIELDS, "OBSERVATION_INVALID")
        require(safe_observation(item, value["baseline"]["ids"]) == item, "OBSERVATION_INVALID")
        require(item["video_id"] not in ids, "DUPLICATE_OBSERVATION")
        ids.add(item["video_id"])
    require(value["detected_count"] == len(ids), "COUNT_INVALID")
    if value["parser_health"] == "HEALTHY":
        require(value["http_status"] == 200 and value["parsing_errors"] == 0
                and value["error_category"] is None, "RESULT_INVALID")
        observe_snapshot(new_shadow(value["baseline"]["ids"]), snapshot_id=value["snapshot_id"],
                         observed_at=value["observed_at_utc"], page=as_page(value))
    else:
        require(value["parser_health"] == "FAILED" and value["error_category"] in ERRORS
                and not ids and not value["empty_verified"], "FAILURE_INVALID")
    return value


def collect_snapshot(reader, baseline, run, now=None):
    validate_baseline(baseline)
    require(run == metadata(run.get("repository"), run.get("commit_sha"), run.get("github_run_id"),
                            run.get("github_run_attempt")), "RUN_METADATA_INVALID")
    result = {"schema_version": 1, "source": SOURCE, "channel_id": CHANNEL_ID, "source_url": VIDEOS_URL,
              **run, "baseline": baseline, "http_status": None, "parser_health": "FAILED",
              "parsing_errors": 0, "detected_count": 0, "observations": [], "empty_verified": False,
              "error_category": "INTERNAL_FAILURE"}
    try:
        page = reader.fetch()
        if isinstance(page, VideosPage) and type(page.http_status) is int and 100 <= page.http_status <= 599:
            result["http_status"] = page.http_status
        # Reuse the shadow engine's parser-contract checks before trusting a successful page.
        observe_snapshot(new_shadow(baseline["ids"]), snapshot_id=run["snapshot_id"],
                         observed_at="2000-01-01T00:00:00+00:00", page=page)
        items = [safe_observation(i, baseline["ids"]) for i in page.items]
        require(len({i["video_id"] for i in items}) == len(items), "INVALID_PARSER_RESULT")
        result.update(http_status=page.http_status, parser_health="HEALTHY", error_category=None,
                      observations=items, detected_count=len(items), empty_verified=page.empty_verified)
    except VideosPageError as exc:
        result.update(http_status=exc.http_status, parsing_errors=exc.parsing_errors,
                      error_category=exc.category if exc.category in ERRORS else "INVALID_PARSER_RESULT")
    except SourceError:
        result.update(error_category="INVALID_PARSER_RESULT", parsing_errors=1)
    except Exception:
        # Never include untrusted exception text, response bodies or request URLs.
        result.update(error_category="INTERNAL_FAILURE", parsing_errors=1)
    when = (now or (lambda: datetime.now(timezone.utc)))()
    require(isinstance(when, datetime) and when.tzinfo is not None and when.utcoffset() is not None,
            "OBSERVATION_TIME_INVALID")
    result["observed_at_utc"] = when.astimezone(timezone.utc).isoformat()
    result["sha256"] = digest(result)
    return validate_snapshot(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    reader = None
    try:
        output = outside_git(args.output_dir)
        baseline = baseline_from_state(args.baseline_dir)
        run = metadata(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_SHA"),
                       os.environ.get("GITHUB_RUN_ID"), int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")))
        reader = YouTubeVideosReader()
        snapshot = collect_snapshot(reader, baseline, run)
        output.mkdir(parents=True, exist_ok=False)
        atomic_json(output / "snapshot.json", snapshot)
        print(json.dumps({k: snapshot[k] for k in ("snapshot_id", "http_status", "parser_health",
                                                  "parsing_errors", "detected_count", "error_category")}))
        return 0 if snapshot["parser_health"] == "HEALTHY" else 1
    except (SourceError, OSError, ValueError, TypeError, KeyError):
        print("Live shadow FAILED: configuration, validation or output failure; no production writes")
        return 1
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
