"""Manual, read-only two-tab collection; output is evidence, never delivery state."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from reliability import SourceError, atomic_json, read_json
from youtube_channel_tabs import (TABS, FORMAT, ChannelTabPage, YouTubeChannelTabsReader,
                                  combine_pages, read_channel_tabs)
from youtube_shadow_live import (REPOSITORY, ERRORS, baseline_from_state, digest, metadata,
                                 outside_git, require, validate_baseline)
from youtube_videos_parser import CHANNEL_ID
from youtube_monitor import publication_time, validate_cutover

SOURCE = "youtube-videos-streams-shadow-v1"
WORKFLOW = ".github/workflows/youtube-shadow-multitab.yml"
STATE_FILES = ("announcements_cache.json", "youtube_cache.json", "delivery_state.json")
FAILURES = ERRORS | {"LOCKUP_METADATA_INVALID", "INVALID_RESULT_OR_INTERNAL_FAILURE"}
FIELDS = {"schema_version", "source", "repository", "commit_sha", "github_run_id",
          "github_run_attempt", "snapshot_id", "workflow_path", "observed_at_utc",
          "channel_id", "requested_tabs", "pages", "overall_health", "is_partial",
          "baseline", "source_state_fingerprint", "observations", "tab_timings", "sha256"}
ROW_FIELDS = {"source", "observation_format", "video_id", "title", "channel_title", "url",
              "thumbnail", "source_tabs", "titles_by_tab", "metadata_by_tab", "knowledge",
              "title_conflict"}
PAGE_FIELDS = {"health", "http_status", "detected_count", "parsing_errors", "has_continuation",
               "error_category", "empty_verified"}


def utc_time(value):
    require(isinstance(value, str) and publication_time(value) is not None,
            "OBSERVATION_TIME_INVALID")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.utcoffset().total_seconds() == 0, "OBSERVATION_TIME_NOT_UTC")
    return parsed


def fingerprints(root):
    return {name: hashlib.sha256((Path(root) / name).read_bytes()).hexdigest() for name in STATE_FILES}


def load_baseline(root):
    known = baseline_from_state(root)
    marker = validate_cutover(read_json(Path(root) / "delivery_state.json", None).get("youtube_rss_cutover"))
    core = {"known": known, "historical_ids": sorted(marker["baseline_ids"])}
    result = dict(core, sha256=digest(core))
    validate_multitab_baseline(result)
    return result


def validate_multitab_baseline(value):
    require(isinstance(value, dict) and set(value) == {"known", "historical_ids", "sha256"},
            "BASELINE_INVALID")
    validate_baseline(value["known"])
    ids = value["historical_ids"]
    require(isinstance(ids, list) and ids and all(isinstance(i, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{11}", i) for i in ids), "BASELINE_INVALID")
    require(ids == sorted(set(ids)) and set(ids) <= set(value["known"]["ids"]), "BASELINE_INVALID")
    require(value["sha256"] == digest({k: v for k, v in value.items() if k != "sha256"}),
            "BASELINE_CHECKSUM_INVALID")


def run_metadata(repository, sha, run_id, attempt):
    value = metadata(repository, sha, run_id, attempt)
    value["snapshot_id"] = f"gh-multitab-{run_id}-attempt-{attempt}"
    require(len(value["snapshot_id"]) <= 96, "RUN_METADATA_INVALID")
    return dict(value, workflow_path=WORKFLOW)


def seal(value):
    result = {k: v for k, v in value.items() if k != "sha256"}
    return dict(result, sha256=digest(result))


def validate_snapshot(value):
    require(isinstance(value, dict) and set(value) == FIELDS, "SCHEMA_INVALID")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1
            and value["source"] == SOURCE and value["channel_id"] == CHANNEL_ID
            and value["requested_tabs"] == list(TABS), "SCHEMA_INCOMPATIBLE")
    run = run_metadata(value["repository"], value["commit_sha"], value["github_run_id"],
                       value["github_run_attempt"])
    require(all(value[k] == v for k, v in run.items()), "PROVENANCE_INVALID")
    observed = utc_time(value["observed_at_utc"])
    timings = value["tab_timings"]
    require(isinstance(timings, dict) and set(timings) == set(TABS), "TAB_TIMINGS_INVALID")
    previous_finish = None
    for tab in TABS:
        timing = timings[tab]
        require(isinstance(timing, dict) and set(timing) == {"started_at_utc", "finished_at_utc"},
                "TAB_TIMINGS_INVALID")
        started, finished = utc_time(timing["started_at_utc"]), utc_time(timing["finished_at_utc"])
        require(started <= finished <= observed and (previous_finish is None or previous_finish <= started),
                "TAB_TIMINGS_OUT_OF_ORDER")
        previous_finish = finished
    validate_multitab_baseline(value["baseline"])
    require(value["sha256"] == seal(value)["sha256"], "CHECKSUM_INVALID")
    fp = value["source_state_fingerprint"]
    require(isinstance(fp, dict) and set(fp) == set(STATE_FILES) and all(isinstance(v, str)
            and re.fullmatch(r"[a-f0-9]{64}", v) for v in fp.values()), "STATE_FINGERPRINT_INVALID")
    require(isinstance(value["pages"], dict) and set(value["pages"]) == set(TABS)
            and isinstance(value["observations"], list), "RESULT_INVALID")
    known, pages, ids = value["baseline"]["known"]["ids"], {}, set()
    for row in value["observations"]:
        require(isinstance(row, dict) and set(row) == ROW_FIELDS, "OBSERVATION_INVALID")
        tabs = row["source_tabs"]
        require(isinstance(tabs, list) and tabs and all(t in TABS for t in tabs)
                and tabs == [t for t in TABS if t in tabs]
                and isinstance(row["titles_by_tab"], dict) and set(row["titles_by_tab"]) == set(tabs)
                and isinstance(row["metadata_by_tab"], dict) and set(row["metadata_by_tab"]) == set(tabs),
                "OBSERVATION_TABS_INVALID")
        require(isinstance(row["video_id"], str) and row["video_id"] not in ids,
                "DUPLICATE_OBSERVATION")
        require(type(row["title_conflict"]) is bool, "OBSERVATION_INVALID")
        ids.add(row["video_id"])
    for tab in TABS:
        p = value["pages"][tab]
        require(isinstance(p, dict) and set(p) == PAGE_FIELDS, "PAGE_INVALID")
        require((p["http_status"] is None or type(p["http_status"]) is int
                 and 100 <= p["http_status"] <= 599)
                and type(p["detected_count"]) is int and p["detected_count"] >= 0
                and type(p["parsing_errors"]) is int and p["parsing_errors"] >= 0
                and type(p["empty_verified"]) is bool, "PAGE_INVALID")
        rows = [r for r in value["observations"] if tab in r["source_tabs"]]
        require(len(rows) == p["detected_count"], "PAGE_COUNT_INVALID")
        if p["health"] == "HEALTHY":
            require(type(p["has_continuation"]) is bool and p["error_category"] is None,
                    "PAGE_INVALID")
            items = [{k: r[k] for k in ("source", "observation_format", "video_id", "channel_title",
                                      "url", "thumbnail")} for r in rows]
            for item, row in zip(items, rows):
                item.update(title=row["titles_by_tab"][tab], source_tabs=[tab],
                            source_metadata=row["metadata_by_tab"][tab])
            pages[tab] = ChannelTabPage(tab, items, p["has_continuation"], p["empty_verified"],
                                        p["http_status"], p["parsing_errors"])
        else:
            require(p["health"] == "FAILED" and isinstance(p["error_category"], str)
                    and p["error_category"] in FAILURES and not rows
                    and p["has_continuation"] is None and not p["empty_verified"], "FAILURE_INVALID")
    expected = combine_pages(pages, known)
    require(sorted(expected, key=lambda r: r["video_id"]) ==
            sorted(value["observations"], key=lambda r: r["video_id"]), "OBSERVATION_INVALID")
    require(value["overall_health"] == ("HEALTHY" if len(pages) == 2 else "FAILED")
            and type(value["is_partial"]) is bool and value["is_partial"] == (len(pages) == 1),
            "HEALTH_INCONSISTENT")
    return value


def collect_snapshot(reader, baseline, run, state_fingerprint, now=None):
    validate_multitab_baseline(baseline)
    require(run == run_metadata(run.get("repository"), run.get("commit_sha"), run.get("github_run_id"),
                                run.get("github_run_attempt")), "RUN_METADATA_INVALID")
    empty, timings = {}, {}
    clock = now or (lambda: datetime.now(timezone.utc))

    def timestamp():
        when = clock()
        require(isinstance(when, datetime) and when.tzinfo is not None and when.utcoffset() is not None,
                "OBSERVATION_TIME_INVALID")
        return when.astimezone(timezone.utc).isoformat()

    class RecordingReader:
        def fetch(self, tab):
            timings[tab] = {"started_at_utc": timestamp()}
            try:
                page = reader.fetch(tab)
                empty[tab] = page.empty_verified
                return page
            finally:
                # Preserve the completed attempt interval even when fetch/parsing raises.
                timings[tab]["finished_at_utc"] = timestamp()

    result = read_channel_tabs(RecordingReader(), baseline["known"]["ids"])
    for tab, page in result["pages"].items():
        page["empty_verified"] = empty.get(tab, False) if page["health"] == "HEALTHY" else False
        if page["health"] == "FAILED" and page["error_category"] not in FAILURES:
            page["error_category"] = "INVALID_RESULT_OR_INTERNAL_FAILURE"
    value = {"schema_version": 1, "source": SOURCE, **run, "channel_id": CHANNEL_ID,
             "observed_at_utc": timestamp(), "tab_timings": timings, "requested_tabs": list(TABS),
             "pages": result["pages"], "overall_health": result["health"],
             "is_partial": sum(p["health"] == "HEALTHY" for p in result["pages"].values()) == 1,
             "baseline": baseline, "source_state_fingerprint": state_fingerprint,
             "observations": result["observations"]}
    return validate_snapshot(seal(value))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    reader, output = None, None
    try:
        output = outside_git(args.output_dir)
        output.mkdir(parents=True, exist_ok=False)
        before = fingerprints(args.state_dir)
        baseline = load_baseline(args.state_dir)
        run = run_metadata(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_SHA"),
                           os.environ.get("GITHUB_RUN_ID"), int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")))
        reader = YouTubeChannelTabsReader()
        snapshot = collect_snapshot(reader, baseline, run, before)
        require(fingerprints(args.state_dir) == before, "SOURCE_STATE_CHANGED")
        atomic_json(output / "snapshot.json", snapshot)
        print(json.dumps({k: snapshot[k] for k in ("snapshot_id", "overall_health", "is_partial", "pages", "sha256")}))
        print(json.dumps({"detected_count": len(snapshot["observations"]),
                          "known": sum(r["knowledge"] == "KNOWN" for r in snapshot["observations"]),
                          "unknown": sum(r["knowledge"] == "UNKNOWN" for r in snapshot["observations"])}))
        return 0 if snapshot["overall_health"] == "HEALTHY" else 1
    except (SourceError, OSError, ValueError, TypeError, KeyError):
        if output is not None and output.is_dir():
            try:
                atomic_json(output / "failure.json", {"source": SOURCE, "overall_health": "FAILED",
                            "category": "CONFIGURATION_VALIDATION_OR_OUTPUT_FAILURE"})
            except OSError:
                pass
        print("Multi-tab shadow FAILED; safe diagnostic only; no production writes")
        return 1
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
