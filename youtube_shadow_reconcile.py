"""Offline reconciliation of explicitly selected, operator-authenticated artifacts."""
import argparse
from pathlib import Path

from reliability import SourceError, atomic_json, read_json
from youtube_monitor import publication_time
from youtube_shadow import classification, new_shadow, observe_snapshot
from youtube_shadow_live import REPOSITORY, as_page, outside_git, require, validate_snapshot


def reconcile_snapshots(snapshots, trust):
    require(isinstance(trust, dict) and set(trust) == {"version", "repository", "snapshots"}
            and type(trust["version"]) is int and trust["version"] == 1
            and trust["repository"] == REPOSITORY and isinstance(trust["snapshots"], dict), "TRUST_MANIFEST_INVALID")
    require(isinstance(snapshots, list) and bool(snapshots), "NO_SNAPSHOTS")
    selected = {}
    baseline = None
    for snapshot in snapshots:
        validate_snapshot(snapshot)
        sid = snapshot["snapshot_id"]
        evidence = {k: snapshot[k] for k in ("sha256", "commit_sha", "github_run_id", "github_run_attempt")}
        require(trust["snapshots"].get(sid) == evidence, "ARTIFACT_NOT_TRUSTED")
        require(baseline is None or snapshot["baseline"] == baseline, "BASELINE_INCOMPATIBLE")
        baseline = snapshot["baseline"]
        require(sid not in selected or snapshot == selected[sid], "SNAPSHOT_ID_CONFLICT")
        selected[sid] = snapshot
    ordered = sorted(selected.values(), key=lambda s: (publication_time(s["observed_at_utc"]),
                                                      int(s["github_run_id"]), s["github_run_attempt"]))
    state = new_shadow(baseline["ids"])
    for snapshot in ordered:
        args = {"snapshot_id": snapshot["snapshot_id"], "observed_at": snapshot["observed_at_utc"]}
        if snapshot["parser_health"] == "HEALTHY":
            state = observe_snapshot(state, **args, page=as_page(snapshot))
        else:
            category = snapshot["error_category"]
            category = category if category in {"HTTP_FAILURE", "TIMEOUT", "NETWORK_ERROR"} else "PARSER_FAILURE"
            state = observe_snapshot(state, **args, error_category=category)
    counts = {key: 0 for key in ("KNOWN", "UNKNOWN", "NEW_CANDIDATE", "APPROVED")}
    for video in state["videos"].values():
        counts[classification(video)] += 1
    summary = {"mode": "shadow-only", "selected_files": len(snapshots), "unique_snapshots": len(ordered),
               "duplicate_files": len(snapshots) - len(ordered), "baseline_sha256": baseline["sha256"],
               "healthy_snapshots": sum(s["parser_health"] == "HEALTHY" for s in ordered),
               "failed_snapshots": sum(s["parser_health"] == "FAILED" for s in ordered),
               "classifications": counts, "delivery_authorized": False}
    return state, summary


def run_reconciliation(paths, trust_path, output_dir):
    output = outside_git(output_dir)
    snapshots = [read_json(Path(p), None) for p in paths]
    state, summary = reconcile_snapshots(snapshots, read_json(Path(trust_path), None))
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "shadow_state.json", state)
    atomic_json(output / "shadow_summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, nargs="+", required=True)
    parser.add_argument("--trust-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        import json
        print(json.dumps(run_reconciliation(args.snapshots, args.trust_manifest, args.output_dir), sort_keys=True))
        return 0
    except (SourceError, OSError, ValueError, TypeError, KeyError, AttributeError):
        print("Shadow reconciliation FAILED: invalid, incompatible or untrusted input; no production writes")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
