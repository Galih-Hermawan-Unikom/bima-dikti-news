"""Offline fixtures only. Writes new shadow artifacts outside all Git worktrees."""
import argparse
import json
from pathlib import Path

from reliability import SourceError, atomic_json, read_json
from youtube_shadow import classification, new_shadow, observe_snapshot, review_observation
from youtube_videos_parser import VideosPage, VideosPageError, parse_videos_html
from youtube_videos_preflight import load_known_ids


def simulate_scenarios(scenarios, known_ids):
    if not isinstance(scenarios, list):
        raise SourceError("YouTube shadow: INVALID_SCENARIOS")
    results = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("name"), str) or scenario["name"] in results:
            raise SourceError("YouTube shadow: INVALID_SCENARIOS")
        state = new_shadow(known_ids)
        if not isinstance(scenario.get("frames"), list) or not scenario["frames"]:
            raise SourceError("YouTube shadow: INVALID_SCENARIOS")
        for frame in scenario["frames"]:
            if not isinstance(frame, dict) or frame.get("action") not in {None, "review"}:
                raise SourceError("YouTube shadow: INVALID_SCENARIOS")
            if frame.get("action") == "review":
                state = review_observation(state, **{k: v for k, v in frame.items() if k != "action"})
                continue
            args = {k: frame[k] for k in ("snapshot_id", "observed_at")}
            if frame.get("error_category"):
                state = observe_snapshot(state, **args, error_category=frame["error_category"])
            else:
                if "html" in frame:
                    try:
                        page = parse_videos_html(frame["html"])
                    except VideosPageError:
                        state = observe_snapshot(state, **args, error_category="PARSER_FAILURE")
                        continue
                else:
                    items = [{"source": "youtube", "observation_format": "youtube-videos-observation-v1", **i}
                             for i in frame["items"]]
                    page = VideosPage(items, empty_verified=frame.get("empty_verified", False))
                state = observe_snapshot(state, **args, page=page)
        results[scenario["name"]] = state
    return results


def run_simulation(baseline_dir, scenarios_path, output_dir):
    known = load_known_ids(Path(baseline_dir))
    scenarios = read_json(scenarios_path, None)
    output = Path(output_dir).resolve()
    if any((p / ".git").exists() for p in (output, *output.parents)):
        raise SourceError("YouTube shadow: OUTPUT_INSIDE_GIT_WORKTREE")
    if output.exists():
        raise SourceError("YouTube shadow: OUTPUT_ALREADY_EXISTS")
    results = simulate_scenarios(scenarios, known)
    summary = {}
    for name, state in results.items():
        counts = {k: 0 for k in ("KNOWN", "UNKNOWN", "NEW_CANDIDATE", "APPROVED")}
        for video in state["videos"].values():
            counts[classification(video)] += 1
        summary[name] = {"health": state["snapshots"][-1]["health"], "classifications": counts,
                         "snapshots": len(state["snapshots"]), "shadow_reviews": len(state["reviews"])}
    output.mkdir(parents=True)
    atomic_json(output / "youtube_shadow_state.json", results)
    atomic_json(output / "shadow_summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--scenarios", type=Path, default=Path(__file__).resolve().parent / "tests/fixtures/youtube_shadow_scenarios.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = run_simulation(args.baseline_dir, args.scenarios, args.output_dir)
        print(json.dumps(summary, sort_keys=True))
        return 0
    except (SourceError, OSError, ValueError, TypeError, KeyError):
        print("YouTube shadow simulation FAILED; no delivery or production writes")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
