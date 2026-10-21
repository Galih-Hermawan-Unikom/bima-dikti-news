from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import yaml

from reliability import SourceError, atomic_json, awaiting_youtube_verification
from youtube_shadow import classification
from youtube_shadow_live import (REPOSITORY, baseline_from_state, collect_snapshot, digest,
                                 main, metadata, validate_snapshot)
from youtube_shadow_reconcile import (main as reconcile_main, reconcile_snapshots,
                                      run_reconciliation)
from youtube_videos_parser import VIDEOS_URL, VideosPage, YouTubeVideosReader, parse_videos_html

OLD, NEW = "K94OBen4CiU", "shadownew01"
T0 = datetime(2026, 10, 22, tzinfo=timezone.utc)


@pytest.fixture
def baseline_dir(tmp_path):
    root = tmp_path / "baseline"
    root.mkdir()
    atomic_json(root / "youtube_cache.json", [{"video_id": OLD}, {"video_id": OLD}])
    atomic_json(root / "delivery_state.json", {"version": 1, "batches": [
        {"items": [{"source": "youtube", "video_id": OLD}],
         "event_keys": ["youtube:video:" + OLD + ":initial"], "delivered": True}],
        "youtube_rss_cutover": {"version": 1, "cutover_at": "2026-10-21T01:32:44.160012+00:00",
                                "baseline_ids": [OLD]}})
    return root


def item(vid=OLD, title="Synthetic video"):
    return {"source": "youtube", "observation_format": "youtube-videos-observation-v1",
            "video_id": vid, "title": title, "channel_title": "Synthetic channel",
            "url": "https://www.youtube.com/watch?v=" + vid,
            "thumbnail": "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg?signature=DO_NOT_PERSIST"}


def snapshot(baseline_dir, ids=(OLD,), *, run="100", attempt=1, minute=0, title="Synthetic video"):
    reader = Mock()
    reader.fetch.return_value = VideosPage([item(i, title) for i in ids], empty_verified=not ids)
    return collect_snapshot(reader, baseline_from_state(baseline_dir),
                            metadata(REPOSITORY, "a" * 40, run, attempt),
                            now=lambda: T0.replace(minute=minute))


def trust_for(*snapshots):
    # Tests use synthetic trusted evidence. Operators must authenticate actual GitHub artifacts.
    return {"version": 1, "repository": REPOSITORY, "snapshots": {
        s["snapshot_id"]: {k: s[k] for k in ("sha256", "commit_sha", "github_run_id", "github_run_attempt")}
        for s in snapshots}}


def reseal(s):
    s["sha256"] = digest({k: v for k, v in s.items() if k != "sha256"})
    return s


def mock_get(monkeypatch, html="", status=200, error=None):
    response = Mock(status_code=status, text=html)
    get = Mock(side_effect=error, return_value=response)
    monkeypatch.setattr(requests.Session, "get", get)
    return get


def test_one_fetch_uses_existing_parser(monkeypatch, fixtures, baseline_dir):
    html = (fixtures / "youtube_videos.html").read_text(encoding="utf-8")
    get = mock_get(monkeypatch, html)
    reader = YouTubeVideosReader()
    try:
        s = collect_snapshot(reader, baseline_from_state(baseline_dir),
                             metadata(REPOSITORY, "a" * 40, "101", 1), now=lambda: T0)
    finally:
        reader.close()
    assert s["parser_health"] == "HEALTHY" and s["detected_count"] == 1
    assert s["observations"][0]["knowledge"] == "KNOWN"
    get.assert_called_once_with(VIDEOS_URL, timeout=15, allow_redirects=False)
    assert s["observations"][0]["title"] == parse_videos_html(html).items[0]["title"]


@pytest.mark.parametrize("status", [404, 429, 500, 503, 302])
def test_http_failure_safe_snapshot(monkeypatch, baseline_dir, status):
    get = mock_get(monkeypatch, "SECRET_BODY_NOT_FOR_LOG", status)
    reader = YouTubeVideosReader()
    try:
        s = collect_snapshot(reader, baseline_from_state(baseline_dir),
                             metadata(REPOSITORY, "a" * 40, "101", 1), now=lambda: T0)
    finally:
        reader.close()
    assert get.call_count == 1 and s["parser_health"] == "FAILED" and s["http_status"] == status
    assert s["observations"] == [] and s["detected_count"] == 0 and not s["empty_verified"]
    assert "SECRET_BODY" not in json.dumps(s)


@pytest.mark.parametrize("error,category", [(requests.Timeout("SECRET_URL"), "TIMEOUT"),
    (requests.ConnectionError("SECRET_URL"), "NETWORK_ERROR"), (RuntimeError("SECRET_URL"), "INTERNAL_FAILURE")])
def test_network_and_unexpected_errors_sanitized(monkeypatch, baseline_dir, error, category):
    get = mock_get(monkeypatch, error=error)
    s = collect_snapshot(YouTubeVideosReader(), baseline_from_state(baseline_dir),
                         metadata(REPOSITORY, "a" * 40, "101", 1), now=lambda: T0)
    assert get.call_count == 1 and s["error_category"] == category
    assert s["observations"] == [] and "SECRET_URL" not in json.dumps(s)


@pytest.mark.parametrize("html", ["<html>changed structure</html>",
    '<script>var ytInitialData={"metadata":{}};</script>'])
def test_changed_structure_fails_not_empty(monkeypatch, baseline_dir, html):
    mock_get(monkeypatch, html)
    s = collect_snapshot(YouTubeVideosReader(), baseline_from_state(baseline_dir),
                         metadata(REPOSITORY, "a" * 40, "101", 1), now=lambda: T0)
    assert s["parser_health"] == "FAILED" and s["parsing_errors"] > 0
    assert not s["empty_verified"] and s["observations"] == []


@pytest.mark.parametrize("page,healthy", [(VideosPage([], empty_verified=True), True),
    (VideosPage([]), False), (VideosPage([item(), item()]), False),
    (VideosPage([item(title="")]), False), (VideosPage([item("bad-id")]), False)])
def test_invalid_or_empty_reader_contract_has_failure_artifact(baseline_dir, page, healthy):
    reader = Mock()
    reader.fetch.return_value = page
    s = collect_snapshot(reader, baseline_from_state(baseline_dir),
                         metadata(REPOSITORY, "a" * 40, "101", 1), now=lambda: T0)
    assert (s["parser_health"] == "HEALTHY") == healthy
    assert s["http_status"] == 200 and s["observations"] == []
    if not healthy:
        assert s["error_category"] == "INVALID_PARSER_RESULT" and not s["empty_verified"]


def test_thirty_known_and_legacy_duplicates(baseline_dir):
    ids = [f"known{i:06d}" for i in range(30)]
    atomic_json(baseline_dir / "youtube_cache.json", [{"video_id": i} for i in ids + ids])
    s = snapshot(baseline_dir, ids)
    assert s["detected_count"] == 30 and len(s["observations"]) == 30
    assert all(i["knowledge"] == "KNOWN" for i in s["observations"])
    assert len(s["baseline"]["ids"]) == 31  # delivered initial remains part of known identity


@pytest.mark.parametrize("ids,count", [((OLD, NEW), 1), ((OLD, NEW, "shadownew02"), 2)])
def test_unknown_never_promoted(baseline_dir, ids, count):
    a = snapshot(baseline_dir, ids)
    b = snapshot(baseline_dir, ids, run="101", minute=2)
    state, summary = reconcile_snapshots([b, a], trust_for(a, b))
    assert summary["classifications"]["UNKNOWN"] == count
    assert summary["classifications"]["NEW_CANDIDATE"] == summary["classifications"]["APPROVED"] == 0
    assert state["reviews"] == []
    for v in state["videos"].values():
        assert v["observation_count"] == 2
        assert awaiting_youtube_verification(v)
        assert "published_at" not in v and "published" not in v
    assert not summary["delivery_authorized"]


def test_duplicate_artifacts_idempotent(baseline_dir):
    s = snapshot(baseline_dir, (OLD, NEW))
    state, summary = reconcile_snapshots([s, deepcopy(s), s], trust_for(s))
    assert summary["duplicate_files"] == 2 and summary["unique_snapshots"] == 1
    assert state["videos"][NEW]["observation_count"] == 1


def test_rerun_unique_id_and_deterministic_order(baseline_dir):
    a = snapshot(baseline_dir)
    b = snapshot(baseline_dir, attempt=2)
    state, _ = reconcile_snapshots([b, a], trust_for(a, b))
    assert a["snapshot_id"] != b["snapshot_id"]
    assert [s["snapshot_id"] for s in state["snapshots"]] == [a["snapshot_id"], b["snapshot_id"]]
    assert state["videos"][OLD]["observation_count"] == 2


def test_title_changes_and_historical_reappearance(baseline_dir):
    frames = [snapshot(baseline_dir), snapshot(baseline_dir, (), run="101", minute=1),
              snapshot(baseline_dir, run="102", minute=2, title="Revised synthetic title")]
    state, _ = reconcile_snapshots(frames[::-1], trust_for(*frames))
    v = state["videos"][OLD]
    assert len(state["videos"]) == 1 and v["observation_count"] == 2 and len(v["title_history"]) == 2
    assert classification(v) == "KNOWN" and v["first_seen_at"] == T0.isoformat()


def test_failed_frame_not_absence_or_sighting(baseline_dir):
    from youtube_videos_parser import VideosPageError
    a = snapshot(baseline_dir, (OLD, NEW))
    reader = Mock()
    reader.fetch.side_effect = VideosPageError("HTTP_FAILURE", http_status=503, parsing_errors=0)
    failed = collect_snapshot(reader, a["baseline"], metadata(REPOSITORY, "a" * 40, "101", 1),
                              now=lambda: T0.replace(minute=1))
    before, _ = reconcile_snapshots([a], trust_for(a))
    after, summary = reconcile_snapshots([a, failed], trust_for(a, failed))
    assert after["videos"] == before["videos"] and summary["failed_snapshots"] == 1
    b = snapshot(baseline_dir, (OLD, NEW), run="102", minute=2)
    final, _ = reconcile_snapshots([b, failed, a], trust_for(a, b, failed))
    assert final["videos"][NEW]["observation_count"] == 2 and len(final["snapshots"]) == 3


@pytest.mark.parametrize("field,value", [("schema_version", 2), ("observed_at_utc", None),
    ("observed_at_utc", "2026-10-22T00:00:00"), ("observed_at_utc", "bad-time"),
    ("observed_at_utc", "2026-10-22T07:00:00+07:00"), ("detected_count", 30),
    ("parser_health", "EMPTY"), ("empty_verified", True), ("github_run_attempt", 0),
    ("repository", "other/repo"), ("published_at", "2026-10-22T00:00:00Z")])
def test_invalid_schema_time_metadata_fail_closed(baseline_dir, field, value):
    s = snapshot(baseline_dir)
    s[field] = value
    reseal(s)
    with pytest.raises(SourceError):
        validate_snapshot(s)


def test_corrupted_artifact_and_forged_checksum(baseline_dir):
    s = snapshot(baseline_dir)
    trust = trust_for(s)
    s["observations"][0]["title"] = "Tampered"
    with pytest.raises(SourceError, match="CHECKSUM_INVALID"):
        reconcile_snapshots([s], trust)
    reseal(s)
    with pytest.raises(SourceError, match="ARTIFACT_NOT_TRUSTED"):
        reconcile_snapshots([s], trust)


def test_incompatible_baseline_rejected(baseline_dir):
    a = snapshot(baseline_dir)
    atomic_json(baseline_dir / "youtube_cache.json", [{"video_id": OLD}, {"video_id": NEW}])
    b = snapshot(baseline_dir, run="101", minute=1)
    with pytest.raises(SourceError, match="BASELINE_INCOMPATIBLE"):
        reconcile_snapshots([a, b], trust_for(a, b))


def test_incompatible_cutover_lineage_rejected(baseline_dir):
    a = snapshot(baseline_dir)
    b = deepcopy(a)
    b.update(metadata(REPOSITORY, "a" * 40, "101", 1))
    b["baseline"]["cutover_at"] = "2026-10-20T00:00:00Z"
    b["baseline"]["sha256"] = digest({k: v for k, v in b["baseline"].items() if k != "sha256"})
    reseal(b)
    with pytest.raises(SourceError, match="BASELINE_INCOMPATIBLE"):
        reconcile_snapshots([a, b], trust_for(a, b))


@pytest.mark.parametrize("uri", ["https://evil.invalid/path?token=DO_NOT_PERSIST",
    "https://i.ytimg.com/vi/K94OBen4CiU/hqdefault.jpg?signature=DO_NOT_PERSIST",
    "https://user:DO_NOT_PERSIST@i.ytimg.com/vi/K94OBen4CiU/hqdefault.jpg"])
def test_no_signed_urls_or_credentials_retained(baseline_dir, uri):
    reader = Mock()
    obj = item()
    obj.update(thumbnail=uri, cookies="DO_NOT_PERSIST", authorization="DO_NOT_PERSIST")
    reader.fetch.return_value = VideosPage([obj])
    s = collect_snapshot(reader, baseline_from_state(baseline_dir),
                         metadata(REPOSITORY, "a" * 40, "101", 1), now=lambda: T0)
    assert s["parser_health"] == "HEALTHY" and "DO_NOT_PERSIST" not in json.dumps(s)
    assert set(s["observations"][0]) == {"video_id", "title", "channel_title", "url", "thumbnail", "knowledge"}


@pytest.mark.parametrize("failure", [False, True])
def test_collector_cli_artifact_and_no_production_writes(monkeypatch, fixtures, baseline_dir, tmp_path, capsys, failure):
    root = Path(__file__).resolve().parents[1]
    paths = [root / n for n in ("youtube_cache.json", "delivery_state.json", "announcements_cache.json")]
    paths += list(baseline_dir.iterdir())
    before = {p: p.read_bytes() for p in paths}
    mock_get(monkeypatch, (fixtures / "youtube_videos.html").read_text(encoding="utf-8"), 404 if failure else 200)
    post = Mock(side_effect=AssertionError("Telegram forbidden"))
    monkeypatch.setattr(requests, "post", post)
    for name, value in {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": "a" * 40,
                        "GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(name, value)
    output = tmp_path / "collector"
    assert main(["--baseline-dir", str(baseline_dir), "--output-dir", str(output)]) == int(failure)
    saved = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
    assert validate_snapshot(saved) and saved["parser_health"] == ("FAILED" if failure else "HEALTHY")
    assert before == {p: p.read_bytes() for p in paths}
    assert not post.called and "SECRET" not in capsys.readouterr().out
    # Neither a second invocation nor an unrelated caller can overwrite an existing artifact directory.
    assert main(["--baseline-dir", str(baseline_dir), "--output-dir", str(output)]) == 1


def test_reconciliation_writes_only_new_outside_output(baseline_dir, tmp_path):
    a, b = snapshot(baseline_dir, (OLD, NEW)), snapshot(baseline_dir, (OLD, NEW), run="101", minute=2)
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for p, s in zip(paths, [a, b]):
        atomic_json(p, s)
    trust = tmp_path / "trust.json"
    atomic_json(trust, trust_for(a, b))
    before = {p: p.read_bytes() for p in [*paths, trust, *baseline_dir.iterdir()]}
    out = tmp_path / "reconciled"
    summary = run_reconciliation(paths[::-1], trust, out)
    assert summary["classifications"] == {"KNOWN": 1, "UNKNOWN": 1, "NEW_CANDIDATE": 0, "APPROVED": 0}
    assert (out / "shadow_state.json").is_file() and before == {p: p.read_bytes() for p in before}
    assert reconcile_main(["--snapshots", *map(str, paths), "--trust-manifest", str(trust),
                           "--output-dir", str(out)]) == 1


def test_invalid_json_no_output(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not JSON", encoding="utf-8")
    out = tmp_path / "must-not-exist"
    assert reconcile_main(["--snapshots", str(bad), "--trust-manifest", str(bad),
                           "--output-dir", str(out)]) == 1
    assert not out.exists()


def test_output_cannot_be_inside_git_worktree(baseline_dir, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    get = mock_get(monkeypatch)
    out = root / "must-not-create-shadow-output"
    assert main(["--baseline-dir", str(baseline_dir), "--output-dir", str(out)]) == 1
    assert not out.exists() and not get.called


def test_missing_baseline_or_run_identity_stops_before_network(baseline_dir, tmp_path, monkeypatch):
    get = mock_get(monkeypatch)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert main(["--baseline-dir", str(baseline_dir), "--output-dir", str(tmp_path / "bad-run")]) == 1
    journal = json.loads((baseline_dir / "delivery_state.json").read_text(encoding="utf-8"))
    del journal["youtube_rss_cutover"]
    atomic_json(baseline_dir / "delivery_state.json", journal)
    assert main(["--baseline-dir", str(baseline_dir), "--output-dir", str(tmp_path / "missing-marker")]) == 1
    assert not get.called


def test_failed_snapshot_cannot_contain_observations(baseline_dir):
    s = snapshot(baseline_dir)
    s.update(parser_health="FAILED", error_category="HTTP_FAILURE", http_status=404)
    reseal(s)
    with pytest.raises(SourceError, match="FAILURE_INVALID"):
        validate_snapshot(s)


def test_conflicting_duplicate_and_untrusted_commit_rejected(baseline_dir):
    a = snapshot(baseline_dir)
    b = snapshot(baseline_dir, title="Conflicting snapshot with same run identity")
    with pytest.raises(SourceError):
        reconcile_snapshots([a, b], trust_for(a, b))
    trust = trust_for(a)
    trust["snapshots"][a["snapshot_id"]]["commit_sha"] = "b" * 40
    with pytest.raises(SourceError, match="ARTIFACT_NOT_TRUSTED"):
        reconcile_snapshots([a], trust)


def test_workflow_manual_read_only_isolated():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/youtube-shadow-live.yml"
    text = path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert workflow.get("on", workflow.get(True)) == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["collect"]
    assert job["if"] == "github.ref == 'refs/heads/master'" and job["timeout-minutes"] == 5
    checkout = job["steps"][0]
    assert checkout["with"]["persist-credentials"] is False
    upload = job["steps"][-1]
    assert upload["if"] == "always()" and upload["with"]["retention-days"] == 14
    assert upload["with"]["overwrite"] is False and upload["with"]["if-no-files-found"] == "error"
    assert "github.run_id" in upload["with"]["name"] and "github.run_attempt" in upload["with"]["name"]
    assert "runner.temp" in upload["with"]["path"]
    for forbidden in ("secrets.", "run_github.py", "monitor_runner.py", "main.py", "git push", "cron", "telegram"):
        assert forbidden not in text.lower()


def test_production_133_baseline_and_message271_remain_known():
    root = Path(__file__).resolve().parents[1]
    baseline = baseline_from_state(root)
    marker = json.loads((root / "delivery_state.json").read_text(encoding="utf-8"))["youtube_rss_cutover"]
    assert set(marker["baseline_ids"]) <= set(baseline["ids"]) and len(marker["baseline_ids"]) == 133
    assert OLD in baseline["ids"]
    # This test verifies membership, not an operational cache size that may grow.


def test_snapshot_and_reconciliation_synthetic_examples():
    root = Path(__file__).parent / "fixtures/youtube_shadow_live"
    snapshots = [json.loads((root / n).read_text(encoding="utf-8")) for n in ("snapshot-1.json", "snapshot-2.json")]
    trust = json.loads((root / "trust-synthetic.json").read_text(encoding="utf-8"))
    _, summary = reconcile_snapshots(snapshots, trust)
    expected = json.loads((root / "summary-synthetic.json").read_text(encoding="utf-8"))
    assert summary == expected
