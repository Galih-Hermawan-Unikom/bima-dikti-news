from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from reliability import SourceError, atomic_json, awaiting_youtube_verification
from youtube_shadow import (ShadowError, classification, new_shadow, observe_snapshot,
                            review_observation, validate_shadow)
from youtube_shadow_simulation import main, run_simulation, simulate_scenarios
from youtube_videos_parser import VideosPage
from youtube_videos_preflight import load_known_ids

ID = "shadownew01"
OLD = "K94OBen4CiU"
T0, T1, T2 = "2026-10-22T00:00:00Z", "2026-10-22T00:02:00Z", "2026-10-22T00:04:00Z"


def page(*ids, title="Synthetic A"):
    return VideosPage([{"source": "youtube", "observation_format": "youtube-videos-observation-v1",
                       "video_id": i, "title": title, "relative_metadata": ["2 days ago"]} for i in ids],
                      empty_verified=not ids)


def observe(state, ids=(ID,), *, sid="s1", at=T0, title="Synthetic A"):
    return observe_snapshot(state, snapshot_id=sid, observed_at=at, page=page(*ids, title=title))


def repeated():
    return observe(observe(new_shadow([OLD])), sid="s2", at=T1)


def review(state, decision="NEW_CANDIDATE", *, rid="r1", at=T1, video_id=ID, reference="fixture-history-review"):
    return review_observation(state, video_id=video_id, decision=decision, review_id=rid,
                              reviewer="fixture-operator", history_review_reference=reference, reviewed_at=at)


@pytest.fixture
def baseline(tmp_path):
    root = tmp_path / "historical"
    root.mkdir()
    atomic_json(root / "youtube_cache.json", [{"video_id": OLD, "title": "Historical", "status": "UNKNOWN"},
                                             {"video_id": OLD, "title": "Historical", "status": "UNKNOWN"}])
    atomic_json(root / "delivery_state.json", {"version": 1, "batches": [],
        "youtube_rss_cutover": {"version": 1, "cutover_at": "2026-10-21T01:32:44.160012+00:00",
                               "baseline_ids": [OLD]}})
    return root


def test_first_seen_is_observation_not_publication():
    state = observe(new_shadow([OLD]))
    video = state["videos"][ID]
    assert video["first_seen_at"] == video["last_seen_at"] == "2026-10-22T00:00:00+00:00"
    assert video["observation_count"] == 1 and classification(video) == "UNKNOWN"
    assert not {"published", "date", "status", "delivery_format"} & video.keys()


def test_repeated_never_automatically_promotes():
    state = repeated()
    state = observe(state, sid="s3", at=T2)
    assert state["videos"][ID]["observation_count"] == 3
    assert classification(state["videos"][ID]) == "UNKNOWN"
    assert state["reviews"] == []


def test_four_classifications_and_shadow_approval_only():
    known = observe(new_shadow([OLD]), ids=(OLD,))
    unknown = repeated()
    candidate = review(unknown)
    approved = review(candidate, "APPROVED", rid="r2")
    assert [classification(s["videos"][i]) for s, i in
            [(known, OLD), (unknown, ID), (candidate, ID), (approved, ID)]] == [
                "KNOWN", "UNKNOWN", "NEW_CANDIDATE", "APPROVED"]
    assert approved["videos"][ID]["knowledge"] == "UNKNOWN"
    assert len(approved["reviews"]) == 2


def test_known_historical_reappears_not_new():
    state = observe(new_shadow([OLD]), ids=(OLD,))
    state = observe(state, ids=(), sid="s2", at=T1)
    absent = deepcopy(state["videos"][OLD])
    assert not absent["present_in_last_snapshot"]
    assert absent["observation_count"] == 1 and absent["first_seen_at"] == absent["last_seen_at"]
    state = observe(state, ids=(OLD,), sid="s3", at=T2)
    assert classification(state["videos"][OLD]) == "KNOWN"
    assert state["videos"][OLD]["first_seen_at"] == absent["first_seen_at"]
    assert state["videos"][OLD]["observation_count"] == 2


def test_title_changes_keep_identity_and_invalidate_review():
    state = review(review(repeated()), "APPROVED", rid="r2")
    audit = deepcopy(state["reviews"])
    state = observe(state, title="Revised", sid="s3", at=T2)
    video = state["videos"][ID]
    assert len(state["videos"]) == 1 and video["observation_count"] == 3
    assert len(video["title_history"]) == 2 and video["title"] == "Revised"
    assert classification(video) == "UNKNOWN" and state["reviews"] == audit
    assert video["stable_observation_count"] == 1


def test_known_title_changes_remain_known():
    state = observe(new_shadow([OLD]), ids=(OLD,))
    state = observe(state, ids=(OLD,), sid="s2", at=T1, title="Changed historical title")
    assert classification(state["videos"][OLD]) == "KNOWN"
    assert len(state["videos"][OLD]["title_history"]) == 2


def test_disappearance_reappearance_requires_stability_again():
    state = review(repeated())
    first = state["videos"][ID]["first_seen_at"]
    state = observe(state, ids=(), sid="s3", at=T2)
    state = observe(state, sid="s4", at="2026-10-22T00:06:00Z")
    assert state["videos"][ID]["first_seen_at"] == first
    assert state["videos"][ID]["observation_count"] == 3
    assert classification(state["videos"][ID]) == "UNKNOWN"
    with pytest.raises(ShadowError, match="INSUFFICIENT_STABLE"):
        review(state, rid="r3", at="2026-10-22T00:06:00Z")


def test_multiple_new_videos_independent():
    state = observe(new_shadow([OLD]), ids=(ID, "shadownew02", "shadownew03"))
    state = observe(state, ids=(ID, "shadownew02"), sid="s2", at=T1)
    assert [state["videos"][i]["observation_count"] for i in (ID, "shadownew02", "shadownew03")] == [2, 2, 1]
    assert all(classification(i) == "UNKNOWN" for i in state["videos"].values())


@pytest.mark.parametrize("category", ["PARSER_FAILURE", "HTTP_FAILURE", "TIMEOUT", "NETWORK_ERROR"])
def test_failed_parser_does_not_fabricate_observation_or_absence(category):
    state = repeated()
    original = deepcopy(state["videos"])
    result = observe_snapshot(state, snapshot_id="s3", observed_at=T2, error_category=category)
    assert result["snapshots"][-1]["health"] == "FAILED"
    assert result["videos"] == original and state["videos"] == original
    with pytest.raises(ShadowError, match="NOT_HEALTHY"):
        review(result, at=T2)


def test_snapshot_replay_idempotent_even_after_newer_snapshot():
    state = repeated()
    assert observe(state) == state
    assert observe_snapshot(state, snapshot_id="s1", observed_at="2026-10-22T07:00:00+07:00",
                            page=page(ID)) == state


def test_snapshot_id_conflict_fails_without_mutation():
    state = repeated()
    before = deepcopy(state)
    with pytest.raises(ShadowError, match="SNAPSHOT_ID_REUSED"):
        observe(state, sid="s1", at=T0, title="Conflicting title")
    assert state == before


def test_duplicate_video_once_per_snapshot():
    state = observe(new_shadow([OLD]), ids=(ID, ID))
    assert state["videos"][ID]["observation_count"] == 1
    assert state["snapshots"][0]["video_ids"] == [ID]


def test_conflicting_duplicate_not_partially_observed():
    p = page(ID, ID)
    p.items[1]["title"] = "Different"
    state = new_shadow([OLD])
    with pytest.raises(ShadowError, match="CONFLICTING_DUPLICATE"):
        observe_snapshot(state, snapshot_id="s1", observed_at=T0, page=p)
    assert state["snapshots"] == [] and state["videos"] == {}


def test_legacy_cache_duplicates_preserved(baseline):
    before = {p.name: p.read_bytes() for p in baseline.iterdir()}
    known = load_known_ids(baseline)
    state = observe(new_shadow(known), ids=(OLD,))
    assert classification(state["videos"][OLD]) == "KNOWN" and state["baseline_ids"] == [OLD]
    assert before == {p.name: p.read_bytes() for p in baseline.iterdir()}
    assert len(json.loads((baseline / "youtube_cache.json").read_text())) == 2


@pytest.mark.parametrize("at", ["bad", "2026-10-22T00:00:00", None])
def test_observation_time_must_be_explicit_timezone(at):
    with pytest.raises(ShadowError, match="INVALID_OBSERVATION_TIME"):
        observe(new_shadow([OLD]), at=at)


def test_clock_backwards_rejected():
    state = repeated()
    with pytest.raises(ShadowError, match="CLOCK_MOVED_BACKWARDS"):
        observe(state, sid="s3", at=T0)


def test_same_time_repeated_not_sufficient():
    state = observe(observe(new_shadow([OLD])), sid="s2", at=T0)
    assert state["videos"][ID]["observation_count"] == 2
    with pytest.raises(ShadowError, match="INSUFFICIENT_STABLE"):
        review(state)


@pytest.mark.parametrize("bad", ["bad", "!!!!!!!!!!!", None])
def test_invalid_id_fails(bad):
    with pytest.raises(ShadowError):
        observe(new_shadow([OLD]), ids=(bad,))


def test_missing_history_review_reference_rejected():
    with pytest.raises(ShadowError, match="INVALID_REFERENCE"):
        review(repeated(), reference="")


def test_unknown_cannot_jump_directly_to_approved():
    with pytest.raises(ShadowError, match="REVIEW_SEQUENCE"):
        review(repeated(), "APPROVED")


def test_historical_never_promoted():
    state = observe(observe(new_shadow([OLD]), ids=(OLD,)), ids=(OLD,), sid="s2", at=T1)
    with pytest.raises(ShadowError, match="HISTORICAL_ID"):
        review(state, video_id=OLD)


def test_review_idempotency_and_conflict():
    state = review(repeated())
    assert review(state) == state
    with pytest.raises(ShadowError, match="REVIEW_ID_REUSED"):
        review(state, "APPROVED")


def test_whitespace_title_change_not_new_identity():
    state = observe(new_shadow([OLD]), title="Synthetic   A")
    state = observe(state, sid="s2", at=T1, title=" Synthetic A ")
    assert len(state["videos"][ID]["title_history"]) == 1


@pytest.mark.parametrize("corruption", ["count", "mode", "baseline", "approval", "publication"])
def test_corrupt_state_fail_closed(corruption):
    state = repeated()
    if corruption == "count":
        state["videos"][ID]["observation_count"] = 99
    elif corruption == "mode":
        state["mode"] = "production"
    elif corruption == "baseline":
        state["baseline_ids"].append("bad")
    elif corruption == "approval":
        state["videos"][ID]["verification_status"] = "APPROVED"
    else:
        state["videos"][ID]["published"] = state["videos"][ID]["first_seen_at"]
    with pytest.raises(ShadowError):
        validate_shadow(state)


def test_unverified_empty_never_marks_disappearance():
    state = repeated()
    before = deepcopy(state)
    with pytest.raises(ShadowError, match="UNVERIFIED_EMPTY"):
        observe_snapshot(state, snapshot_id="s3", observed_at=T2, page=VideosPage([]))
    assert state == before


def test_failed_page_metadata_not_accepted():
    p = page(ID)
    p.parsing_errors = 1
    with pytest.raises(ShadowError, match="INVALID_PARSER"):
        observe_snapshot(new_shadow([OLD]), snapshot_id="s1", observed_at=T0, page=p)


@pytest.mark.parametrize("field", ["published", "status", "delivery_format"])
def test_rss_or_delivery_metadata_not_accepted(field):
    p = page(ID)
    p.items[0][field] = "unexpected"
    with pytest.raises(ShadowError, match="CONTRACT"):
        observe_snapshot(new_shadow([OLD]), snapshot_id="s1", observed_at=T0, page=p)


def test_fixture_scenarios_and_real_parser_failure(fixtures):
    scenarios = json.loads((fixtures / "youtube_shadow_scenarios.json").read_text())
    results = simulate_scenarios(scenarios, [OLD])
    assert len(results) == 8
    assert classification(results["unknown_repeated"]["videos"][ID]) == "UNKNOWN"
    assert classification(results["explicit_shadow_review_only"]["videos"][ID]) == "APPROVED"
    assert classification(results["title_changes_after_review"]["videos"][ID]) == "UNKNOWN"
    failed = results["parser_fails"]
    assert failed["snapshots"][-1]["health"] == "FAILED"
    assert failed["videos"][ID]["observation_count"] == 1
    assert failed["videos"][ID]["last_seen_at"] == failed["videos"][ID]["first_seen_at"]


def test_fixture_html_can_be_observed_without_network(fixtures):
    scenarios = [{"name": "html_fixture", "frames": [{"snapshot_id": "s1", "observed_at": T0,
        "html": (fixtures / "youtube_videos.html").read_text()}]}]
    result = simulate_scenarios(scenarios, [OLD])
    assert classification(result["html_fixture"]["videos"][OLD]) == "KNOWN"


def test_simulation_artifacts_are_separate_and_no_network(
        tmp_path, baseline, fixtures, monkeypatch, telegram, notifier):
    get = Mock()
    monkeypatch.setattr(requests.Session, "get", get)
    before = {p.name: p.read_bytes() for p in baseline.iterdir()}
    output = tmp_path / "new-shadow-output"
    summary = run_simulation(baseline, fixtures / "youtube_shadow_scenarios.json", output)
    assert summary["explicit_shadow_review_only"]["classifications"]["APPROVED"] == 1
    assert sorted(p.name for p in output.iterdir()) == ["shadow_summary.json", "youtube_shadow_state.json"]
    states = json.loads((output / "youtube_shadow_state.json").read_text())
    for state in states.values():
        for video in state["videos"].values():
            assert awaiting_youtube_verification(video)
            assert not notifier.send_notification([video])
    get.assert_not_called()
    telegram.assert_not_called()
    assert before == {p.name: p.read_bytes() for p in baseline.iterdir()}


def test_133_id_baseline_compatible():
    ids = [f"known{i:06d}" for i in range(132)] + [OLD]
    state = observe(new_shadow(ids), ids=(OLD, ID))
    assert len(state["baseline_ids"]) == 133
    assert classification(state["videos"][OLD]) == "KNOWN"
    assert classification(state["videos"][ID]) == "UNKNOWN"


def test_output_existing_history_not_overwritten(tmp_path, baseline, fixtures):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "history.txt").write_text("keep")
    with pytest.raises(SourceError, match="OUTPUT_ALREADY_EXISTS"):
        run_simulation(baseline, fixtures / "youtube_shadow_scenarios.json", output)
    assert (output / "history.txt").read_text() == "keep"


def test_output_inside_worktree_rejected(baseline, fixtures):
    output = Path(__file__).resolve().parents[1] / "forbidden-shadow-artifacts"
    with pytest.raises(SourceError, match="OUTPUT_INSIDE_GIT"):
        run_simulation(baseline, fixtures / "youtube_shadow_scenarios.json", output)
    assert not output.exists()


def test_persistence_failure_cannot_touch_production(tmp_path, baseline, fixtures, monkeypatch):
    before = {p.name: p.read_bytes() for p in baseline.iterdir()}
    monkeypatch.setattr("youtube_shadow_simulation.atomic_json", Mock(side_effect=OSError("disk")))
    with pytest.raises(OSError):
        run_simulation(baseline, fixtures / "youtube_shadow_scenarios.json", tmp_path / "output")
    assert before == {p.name: p.read_bytes() for p in baseline.iterdir()}


def test_cli_offline_success(tmp_path, baseline, fixtures, capsys):
    assert main(["--baseline-dir", str(baseline), "--scenarios", str(fixtures / "youtube_shadow_scenarios.json"),
                 "--output-dir", str(tmp_path / "output")]) == 0
    assert "APPROVED" in capsys.readouterr().out


def test_cli_fail_closed_on_corrupt_history(tmp_path, baseline, fixtures):
    (baseline / "delivery_state.json").write_text("corrupt")
    assert main(["--baseline-dir", str(baseline), "--scenarios", str(fixtures / "youtube_shadow_scenarios.json"),
                 "--output-dir", str(tmp_path / "output")]) == 1
    assert not (tmp_path / "output").exists()
