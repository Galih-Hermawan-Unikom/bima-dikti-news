"""Offline reconciliation of explicitly selected, operator-authenticated multi-tab ZIPs."""
import argparse
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import zipfile

from reliability import SourceError, atomic_json, read_json
from youtube_multitab_snapshot import (SOURCE, WORKFLOW, REPOSITORY, TABS, validate_snapshot,
                                       utc_time, require)
from youtube_shadow_live import outside_git
from youtube_shadow import new_shadow, observe_snapshot, classification
from youtube_videos_parser import VideosPage

MAX_ZIP = 16 * 1024 * 1024
MAX_JSON = 4 * 1024 * 1024
TRUST_FIELDS = {"version", "source", "repository", "workflow_path", "baseline_sha256",
                "verified_by", "verified_at_utc", "artifacts"}
ARTIFACT_FIELDS = {"artifact_id", "artifact_name", "zip_sha256", "snapshot_sha256", "commit_sha",
                   "github_run_id", "github_run_attempt", "workflow_id", "event", "head_branch",
                   "run_status", "run_conclusion", "artifact_upload_conclusion", "authentication"}


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def validate_trust(trust):
    require(isinstance(trust, dict) and set(trust) == TRUST_FIELDS and trust["version"] == 1
            and type(trust["version"]) is int and trust["source"] == SOURCE
            and trust["repository"] == REPOSITORY and trust["workflow_path"] == WORKFLOW,
            "TRUST_MANIFEST_INVALID")
    require(isinstance(trust["verified_by"], str) and trust["verified_by"].strip()
            and sha(trust["baseline_sha256"]) and isinstance(trust["artifacts"], dict)
            and trust["artifacts"], "TRUST_MANIFEST_INVALID")
    utc_time(trust["verified_at_utc"])
    workflow_ids = set()
    for name, evidence in trust["artifacts"].items():
        require(isinstance(name, str) and Path(name).name == name
                and isinstance(evidence, dict) and set(evidence) == ARTIFACT_FIELDS,
                "ARTIFACT_PROVENANCE_INVALID")
        require(all(isinstance(evidence[k], str) and re.fullmatch(r"[1-9][0-9]*", evidence[k])
                    for k in ("artifact_id", "workflow_id", "github_run_id"))
                and type(evidence["github_run_attempt"]) is int and evidence["github_run_attempt"] > 0
                and isinstance(evidence["commit_sha"], str)
                and re.fullmatch(r"[a-f0-9]{40}", evidence["commit_sha"])
                and sha(evidence["zip_sha256"]) and sha(evidence["snapshot_sha256"]),
                "ARTIFACT_PROVENANCE_INVALID")
        require(evidence["artifact_name"] == f'youtube-shadow-multitab-{evidence["github_run_id"]}-{evidence["github_run_attempt"]}'
                and evidence["event"] == "workflow_dispatch" and evidence["head_branch"] == "master"
                and evidence["run_status"] == "completed"
                and evidence["run_conclusion"] in {"success", "failure"}
                and evidence["artifact_upload_conclusion"] == "success"
                and evidence["authentication"] == "operator-verified-github-api",
                "ARTIFACT_PROVENANCE_INVALID")
        workflow_ids.add(evidence["workflow_id"])
    require(len(workflow_ids) == 1, "WORKFLOW_LINEAGE_INCOMPATIBLE")


def read_artifact(path, trust):
    path = Path(path)
    require(path.name in trust["artifacts"], "ARTIFACT_NOT_TRUSTED")
    evidence = trust["artifacts"][path.name]
    require(path.stat().st_size <= MAX_ZIP, "ARTIFACT_TOO_LARGE")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == evidence["zip_sha256"], "ZIP_CHECKSUM_INVALID")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            require(len(entries) == 1 and entries[0].filename == "snapshot.json"
                    and entries[0].file_size <= MAX_JSON and not entries[0].is_dir()
                    and entries[0].external_attr >> 16 & 0o170000 != 0o120000,
                    "ARTIFACT_LAYOUT_INVALID")
            snapshot = validate_snapshot(json.loads(archive.read(entries[0]).decode("utf-8")))
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, ValueError):
        raise SourceError("YouTube multi-tab: ARTIFACT_INVALID") from None
    require(all(snapshot[k] == evidence[k] for k in
                ("commit_sha", "github_run_id", "github_run_attempt"))
            and snapshot["sha256"] == evidence["snapshot_sha256"]
            and snapshot["baseline"]["sha256"] == trust["baseline_sha256"], "PROVENANCE_MISMATCH")
    require(snapshot["overall_health"] != "FAILED" or evidence["run_conclusion"] == "failure",
            "RUN_HEALTH_MISMATCH")
    require(utc_time(snapshot["observed_at_utc"]) <= utc_time(trust["verified_at_utc"]),
            "TRUST_TIME_INVALID")
    return snapshot


def reconcile(snapshots):
    require(bool(snapshots), "SNAPSHOTS_MISSING")
    unique, baseline = {}, None
    for snapshot in snapshots:
        validate_snapshot(snapshot)
        baseline = baseline or snapshot["baseline"]
        require(snapshot["baseline"] == baseline, "BASELINE_LINEAGE_INCOMPATIBLE")
        old = unique.get(snapshot["snapshot_id"])
        require(old is None or old == snapshot, "SNAPSHOT_ID_REUSED")
        unique[snapshot["snapshot_id"]] = snapshot
    ordered = sorted(unique.values(), key=lambda s: (utc_time(s["observed_at_utc"]),
                     int(s["github_run_id"]), s["github_run_attempt"]))
    state, evidence, changes, previous = new_shadow(baseline["known"]["ids"]), {}, [], {}
    last_seen_rows = {}
    for snapshot in ordered:
        sid, when = snapshot["snapshot_id"], snapshot["observed_at_utc"]
        if snapshot["overall_health"] == "FAILED":
            state = observe_snapshot(state, snapshot_id=sid, observed_at=when, error_category="PARSER_FAILURE")
            changes.append({"snapshot_id": sid, "health": "FAILED", "is_partial": snapshot["is_partial"],
                            "partial_evidence_ids": [r["video_id"] for r in snapshot["observations"]]})
            continue
        rows = {r["video_id"]: r for r in snapshot["observations"]}
        title_changes, tab_changes = [], []
        for vid, row in rows.items():
            old = last_seen_rows.get(vid)
            if old is not None:
                for tab in set(row["source_tabs"]) & set(old["source_tabs"]):
                    if row["titles_by_tab"][tab] != old["titles_by_tab"][tab]:
                        title_changes.append({"video_id": vid, "tab": tab,
                            "before": old["titles_by_tab"][tab], "after": row["titles_by_tab"][tab]})
                if old["source_tabs"] != row["source_tabs"]:
                    tab_changes.append({"video_id": vid, "before": old["source_tabs"], "after": row["source_tabs"]})
            evidence.setdefault(vid, []).append({"snapshot_id": sid, "observed_at_utc": when,
                **{k: deepcopy(row[k]) for k in ("source_tabs", "titles_by_tab", "metadata_by_tab", "title_conflict")}})
        # Internal projection only: reuse pure transitions, not the Phase 3 artifact schema.
        page = VideosPage([{"source": "youtube", "observation_format": "youtube-videos-observation-v1",
                           "video_id": r["video_id"], "title": r["title"]} for r in rows.values()],
                         empty_verified=not rows)
        known_before = set(state["videos"])
        state = observe_snapshot(state, snapshot_id=sid, observed_at=when, page=page)
        changes.append({"snapshot_id": sid, "health": "HEALTHY", "is_partial": False,
                        "first_observed_ids": sorted(set(rows) - known_before),
                        "not_visible_in_capture": sorted(set(previous) - set(rows)),
                        "reappeared_ids": sorted(set(rows) & (known_before - set(previous))),
                        "title_changes": title_changes, "tab_changes": tab_changes,
                        "cross_tab_ids": sorted(v for v, r in rows.items() if len(r["source_tabs"]) == 2)})
        previous = rows
        last_seen_rows.update(rows)
    return {"schema_version": 1, "source": SOURCE, "baseline": baseline, "shadow": state,
            "tab_history": evidence, "comparisons": changes,
            "classifications": {v: classification(r) for v, r in state["videos"].items()},
            "delivery_authorized": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trust-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("artifacts", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        output = outside_git(args.output_dir)
        trust = read_json(args.trust_manifest, None)
        validate_trust(trust)
        result = reconcile([read_artifact(path, trust) for path in args.artifacts])
        output.mkdir(parents=True, exist_ok=False)
        atomic_json(output / "reconciliation.json", result)
        print(json.dumps({"snapshots": len(result["comparisons"]), "videos": len(result["shadow"]["videos"]),
                          "failed_snapshots": sum(c["health"] == "FAILED" for c in result["comparisons"]),
                          "delivery_authorized": False}))
        return 0
    except (SourceError, OSError, ValueError, TypeError, KeyError, AttributeError):
        print("Multi-tab reconciliation FAILED: untrusted, incompatible or invalid evidence")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
