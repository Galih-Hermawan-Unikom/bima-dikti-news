from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock
import zipfile

import pytest
import requests
import yaml

from reliability import SourceError, atomic_json, awaiting_youtube_verification
from youtube_channel_tabs import TABS, URLS, FORMAT, ChannelTabPage, YouTubeChannelTabsReader
import youtube_multitab_snapshot as collector
import youtube_multitab_reconcile as offline
from youtube_shadow_live import digest, validate_snapshot as validate_phase3
from youtube_videos_parser import VideosPageError


@pytest.fixture
def synthetic(fixtures):
    return json.loads((fixtures / "youtube_multitab_synthetic.json").read_text(encoding="utf-8"))


@pytest.fixture
def baseline(synthetic):
    core = {"ids": sorted(synthetic["historical_ids"]), "cutover_version": 1,
            "cutover_at": "2026-10-21T01:32:44.160012+00:00"}
    value = {"known": dict(core, sha256=digest(core)), "historical_ids": core["ids"]}
    return dict(value, sha256=digest(value))


def page(data, tab):
    items = [{"source": "youtube", "observation_format": FORMAT, **row,
              "channel_title": "Synthetic Channel", "url": "https://www.youtube.com/watch?v=" + row["video_id"],
              "thumbnail": "", "source_tabs": [tab], "source_metadata": deepcopy(data["source_metadata"])}
             for row in data[tab]]
    return ChannelTabPage(tab, items, True, empty_verified=not items)


def snapshot(data, baseline, run="101", attempt=1, minute=0, failures=()):
    reader = Mock()
    def fetch(tab):
        if tab in failures:
            raise VideosPageError("HTTP_FAILURE", http_status=503, parsing_errors=0)
        return page(data, tab)
    reader.fetch.side_effect = fetch
    value = collector.collect_snapshot(reader, baseline,
        collector.run_metadata(collector.REPOSITORY, "a" * 40, run, attempt),
        {f: "b" * 64 for f in collector.STATE_FILES},
        now=lambda: datetime(2026, 10, 22, 0, minute, tzinfo=timezone.utc))
    assert reader.fetch.call_args_list == [(("videos",),), (("streams",),)]
    return value


def trust_zip(tmp_path, value, name="evidence.zip"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("snapshot.json", json.dumps(value))
    entry = {"artifact_id": "234", "artifact_name": f'youtube-shadow-multitab-{value["github_run_id"]}-{value["github_run_attempt"]}',
             "zip_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "snapshot_sha256": value["sha256"],
             "commit_sha": value["commit_sha"], "github_run_id": value["github_run_id"],
             "github_run_attempt": value["github_run_attempt"], "workflow_id": "123", "event": "workflow_dispatch",
             "head_branch": "master", "run_status": "completed",
             "run_conclusion": "success" if value["overall_health"] == "HEALTHY" else "failure",
             "artifact_upload_conclusion": "success", "authentication": "operator-verified-github-api"}
    trust = {"version": 1, "source": collector.SOURCE, "repository": collector.REPOSITORY,
             "workflow_path": collector.WORKFLOW, "baseline_sha256": value["baseline"]["sha256"],
             "verified_by": "synthetic-operator", "verified_at_utc": "2026-10-23T00:00:00+00:00",
             "artifacts": {name: entry}}
    return path, trust


@pytest.mark.parametrize("failures,health,partial,count", [
    ((), "HEALTHY", False, 3), (("videos",), "FAILED", True, 2),
    (("streams",), "FAILED", True, 2), (TABS, "FAILED", False, 0)])
def test_health_truth_table(synthetic, baseline, failures, health, partial, count):
    value = snapshot(synthetic, baseline, failures=failures)
    assert (value["overall_health"], value["is_partial"], len(value["observations"])) == (health, partial, count)
    assert value["baseline"]["historical_ids"] == sorted(synthetic["historical_ids"])
    assert all(awaiting_youtube_verification(r) for r in value["observations"])


def test_cross_tab_metadata_and_unknown_not_approved(synthetic, baseline):
    value = snapshot(synthetic, baseline)
    row = value["observations"][0]
    assert row["source_tabs"] == list(TABS) and row["title_conflict"]
    assert row["metadata_by_tab"]["streams"]["schedule_timezone"] is None
    assert value["pages"]["streams"]["has_continuation"]
    result = offline.reconcile([value, deepcopy(value)])
    assert result["shadow"]["videos"]["knownvid001"]["observation_count"] == 1
    assert result["classifications"]["unknown0001"] == "UNKNOWN"
    assert not result["delivery_authorized"] and not result["shadow"]["reviews"]
    with pytest.raises(SourceError):
        validate_phase3(value)


def test_partial_failure_never_updates_videos_or_reviews(synthetic, baseline):
    first = snapshot(synthetic, baseline)
    modified = deepcopy(synthetic)
    modified["streams"][0]["title"] = "Changed during failed capture"
    failed = snapshot(modified, baseline, "102", minute=1, failures=("videos",))
    before = offline.reconcile([first])
    after = offline.reconcile([first, failed])
    assert before["shadow"]["videos"] == after["shadow"]["videos"]
    assert before["shadow"]["reviews"] == after["shadow"]["reviews"]
    assert before["tab_history"] == after["tab_history"]
    assert after["comparisons"][-1]["health"] == "FAILED"
    assert len(offline.reconcile([failed])["shadow"]["videos"]) == 0


def test_move_title_disappearance_reappearance_rerun(synthetic, baseline):
    first = snapshot(synthetic, baseline)
    moved = deepcopy(synthetic)
    moved["videos"] = []
    moved["streams"][0]["title"] = "Changed in same tab"
    second = snapshot(moved, baseline, "102", minute=1)
    third = snapshot(synthetic, baseline, "102", attempt=2, minute=2)
    failed = snapshot(synthetic, baseline, "103", minute=3, failures=TABS)
    result = offline.reconcile([third, first, failed, second, second])
    assert result["comparisons"][1]["not_visible_in_capture"] == ["unknown0001"]
    assert result["comparisons"][1]["tab_changes"][0]["after"] == ["streams"]
    assert result["comparisons"][1]["title_changes"][0]["tab"] == "streams"
    assert result["comparisons"][2]["reappeared_ids"] == ["unknown0001"]
    row = result["shadow"]["videos"]["unknown0001"]
    assert row["observation_count"] == 2 and row["first_seen_at"] == first["observed_at_utc"]
    assert row["last_seen_at"] == third["observed_at_utc"] and row["knowledge"] == "UNKNOWN"
    assert result["shadow"]["videos"]["knownvid001"]["observation_count"] == 3


@pytest.mark.parametrize("mutate", [
    lambda v: v.update(source="youtube-videos-shadow-v1"),
    lambda v: v.update(schema_version=2),
    lambda v: v.update(repository="other/repository"),
    lambda v: v.update(workflow_path=".github/workflows/main.yml"),
    lambda v: v.update(observed_at_utc="2026-10-22"),
    lambda v: v.update(observed_at_utc="2026-10-22T07:00:00+07:00"),
    lambda v: v.update(requested_tabs=["videos", "shorts"]),
    lambda v: v.update(is_partial=True),
    lambda v: v["pages"]["videos"].update(detected_count=999),
    lambda v: v["observations"].append(deepcopy(v["observations"][0])),
    lambda v: v["observations"][0].update(published_at="made-up"),
    lambda v: v["observations"][0].update(title_conflict=False),
    lambda v: v["observations"][0].update(knowledge="UNKNOWN"),
    lambda v: v["observations"][0].update(source_tabs=["streams", "videos"]),
    lambda v: v["observations"][0]["metadata_by_tab"]["videos"].update(schedule_timezone="WIB"),
    lambda v: v["source_state_fingerprint"].update(extra="c" * 64),
])
def test_strict_snapshot_validation(synthetic, baseline, mutate):
    value = snapshot(synthetic, baseline)
    mutate(value)
    with pytest.raises((SourceError, ValueError, TypeError)):
        collector.validate_snapshot(collector.seal(value))


def test_hash_and_baseline_lineage(synthetic, baseline):
    first = snapshot(synthetic, baseline)
    broken = deepcopy(first)
    broken["sha256"] = "0" * 64
    with pytest.raises(SourceError, match="CHECKSUM"):
        collector.validate_snapshot(broken)
    other = deepcopy(baseline)
    other["known"]["cutover_at"] = "2026-10-22T00:00:00+00:00"
    other["known"] = collector.seal(other["known"])
    other = collector.seal(other)
    second = snapshot(synthetic, other, "102", minute=1)
    with pytest.raises(SourceError, match="LINEAGE"):
        offline.reconcile([first, second])
    reused = deepcopy(first)
    reused["observed_at_utc"] = second["observed_at_utc"]
    with pytest.raises(SourceError, match="REUSED"):
        offline.reconcile([first, collector.seal(reused)])


def test_authenticated_zip_and_offline_cli(synthetic, baseline, tmp_path):
    value = snapshot(synthetic, baseline)
    path, trust = trust_zip(tmp_path, value)
    offline.validate_trust(trust)
    assert offline.read_artifact(path, trust) == value
    assert trust["artifacts"][path.name]["zip_sha256"] != value["sha256"]
    manifest = tmp_path / "trust.json"
    atomic_json(manifest, trust)
    output = tmp_path / "result"
    assert offline.main(["--trust-manifest", str(manifest), "--output-dir", str(output), str(path)]) == 0
    result = json.loads((output / "reconciliation.json").read_text(encoding="utf-8"))
    assert result["classifications"]["unknown0001"] == "UNKNOWN"
    assert offline.main(["--trust-manifest", str(manifest), "--output-dir", str(output), str(path)]) == 1


@pytest.mark.parametrize("key,value", [
    ("commit_sha", "c" * 40), ("github_run_id", "999"), ("github_run_attempt", 2),
    ("snapshot_sha256", "0" * 64), ("zip_sha256", "0" * 64),
    ("artifact_upload_conclusion", "failure"), ("authentication", "self-declared"),
    ("event", "push"), ("head_branch", "untrusted-pr"), ("workflow_id", ""),
    ("artifact_name", "youtube-shadow-101-1"), ("run_status", "in_progress")])
def test_provenance_rejected(synthetic, baseline, tmp_path, key, value):
    path, trust = trust_zip(tmp_path, snapshot(synthetic, baseline))
    trust["artifacts"][path.name][key] = value
    with pytest.raises(SourceError):
        offline.validate_trust(trust)
        offline.read_artifact(path, trust)


@pytest.mark.parametrize("kind", ["corrupt", "traversal", "duplicate", "invalid_json", "phase3"])
def test_bad_artifact(synthetic, baseline, tmp_path, kind):
    value = snapshot(synthetic, baseline)
    path, trust = trust_zip(tmp_path, value)
    if kind == "corrupt":
        path.write_bytes(b"not a zip")
    else:
        with zipfile.ZipFile(path, "w") as archive:
            if kind == "phase3":
                value["source"] = "youtube-videos-shadow-v1"
            archive.writestr("../snapshot.json" if kind == "traversal" else "snapshot.json",
                             "{" if kind == "invalid_json" else json.dumps(value))
            if kind == "duplicate":
                archive.writestr("extra.json", "{}")
    trust["artifacts"][path.name]["zip_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises((SourceError, ValueError)):
        offline.read_artifact(path, trust)


@pytest.mark.parametrize("failure", [404, 429, 500, 503, "timeout", "network", "structure"])
def test_mocked_http_two_requests_only(fixtures, baseline, failure):
    session = Mock()
    document = (fixtures / "youtube_streams_initial.json").read_text(encoding="utf-8")
    good = Mock(status_code=200, text="<script>var ytInitialData = " + document + ";</script>")
    if isinstance(failure, int):
        bad = Mock(status_code=failure)
    elif failure == "timeout":
        bad = requests.Timeout("SECRET_DO_NOT_LOG")
    elif failure == "network":
        bad = requests.ConnectionError("SECRET_DO_NOT_LOG")
    else:
        bad = Mock(status_code=200, text="<html>SECRET_DO_NOT_LOG</html>")
    session.get.side_effect = [bad, good]
    reader = YouTubeChannelTabsReader()
    reader.session.close()
    reader.session = session
    value = collector.collect_snapshot(reader, baseline,
        collector.run_metadata(collector.REPOSITORY, "a" * 40, "101", 1),
        {f: "b" * 64 for f in collector.STATE_FILES})
    assert value["overall_health"] == "FAILED" and value["is_partial"]
    assert session.get.call_count == 2
    assert [c.args[0] for c in session.get.call_args_list] == [URLS[t] for t in TABS]
    assert all(c.kwargs == {"timeout": 15, "allow_redirects": False} for c in session.get.call_args_list)
    assert "SECRET_DO_NOT_LOG" not in json.dumps(value)


def test_collector_cli_state_immutable_and_no_env(monkeypatch, tmp_path, synthetic):
    root = tmp_path / "state"
    root.mkdir()
    ids = synthetic["historical_ids"] + [f"base{i:07d}" for i in range(131)]
    atomic_json(root / "announcements_cache.json", [{"synthetic": True}])
    atomic_json(root / "youtube_cache.json", [{"video_id": "knownvid001"}] * 2)
    atomic_json(root / "delivery_state.json", {"version": 1, "batches": [], "youtube_rss_cutover": {
        "version": 1, "cutover_at": "2026-10-21T01:32:44.160012+00:00", "baseline_ids": ids}})
    before = collector.fingerprints(root)
    for key, value in {"GITHUB_REPOSITORY": collector.REPOSITORY, "GITHUB_SHA": "a" * 40,
                       "GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(key, value)
    reader = Mock()
    reader.fetch.side_effect = lambda tab: page(synthetic, tab)
    monkeypatch.setattr(collector, "YouTubeChannelTabsReader", lambda: reader)
    output = tmp_path / "outside"
    assert collector.main(["--state-dir", str(root), "--output-dir", str(output)]) == 0
    reader.close.assert_called_once()
    result = json.loads((output / "snapshot.json").read_text(encoding="utf-8"))
    assert len(result["baseline"]["historical_ids"]) == 133
    assert "shortold001" in result["baseline"]["historical_ids"]
    assert collector.fingerprints(root) == before


def test_workflow_manual_read_only_and_upload_failure_not_hidden():
    root = Path(collector.__file__).parent
    text = (root / collector.WORKFLOW).read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    assert workflow.get("on", workflow.get(True)) == {"workflow_dispatch": None}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["collect"]
    assert job["if"] == "github.ref == 'refs/heads/master'" and job["timeout-minutes"] == 5
    checkout = job["steps"][0]
    assert checkout["with"]["persist-credentials"] is False
    upload = job["steps"][-1]
    assert upload["if"] == "always()" and upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["retention-days"] == 14 and upload["with"]["overwrite"] is False
    assert upload["with"]["if-no-files-found"] == "error"
    assert not any(step.get("continue-on-error", False) for step in job["steps"])
    assert "runner.temp" in upload["with"]["path"]
    for forbidden in ("secrets.", "main.py", "run_github", "monitor_runner", "git push", "shorts", "schedule:"):
        assert forbidden not in text


def test_no_production_imports_or_http_in_reconciler():
    text = Path(offline.__file__).read_text(encoding="utf-8")
    for forbidden in ("import requests", "Notifier", "sendMessage", "sendDocument", "load_dotenv", "getenv"):
        assert forbidden not in text


@pytest.mark.parametrize("failures", [("videos",), ("streams",), TABS])
def test_failed_artifact_is_failure_not_empty_success(synthetic, baseline, tmp_path, failures):
    value = snapshot(synthetic, baseline, failures=failures)
    path, trust = trust_zip(tmp_path, value)
    offline.validate_trust(trust)
    checked = offline.read_artifact(path, trust)
    result = offline.reconcile([checked])
    assert not result["shadow"]["videos"] and not result["tab_history"]
    assert result["comparisons"][0]["health"] == "FAILED"
    trust["artifacts"][path.name]["run_conclusion"] = "success"
    with pytest.raises(SourceError, match="HEALTH"):
        offline.read_artifact(path, trust)


def test_title_change_after_absence(synthetic, baseline):
    first = snapshot(synthetic, baseline)
    absent = deepcopy(synthetic)
    absent["videos"] = []
    second = snapshot(absent, baseline, "102", minute=1)
    returned = deepcopy(synthetic)
    returned["videos"][1]["title"] = "Changed while not visible"
    third = snapshot(returned, baseline, "103", minute=2)
    result = offline.reconcile([first, second, third])
    assert any(c["video_id"] == "unknown0001" for c in result["comparisons"][-1]["title_changes"])


@pytest.mark.parametrize("which", ["historical", "known", "fingerprint"])
def test_bad_baseline_or_fingerprint(synthetic, baseline, which):
    value = snapshot(synthetic, baseline)
    if which == "historical":
        value["baseline"]["historical_ids"].append("otherid0001")
    elif which == "known":
        value["baseline"]["known"]["ids"].append("invalid")
    else:
        value["source_state_fingerprint"]["youtube_cache.json"] = "bad"
    with pytest.raises(SourceError):
        collector.validate_snapshot(collector.seal(value))


def test_healthy_empty_is_explicit_not_parser_failure(synthetic, baseline):
    empty = deepcopy(synthetic)
    empty["videos"], empty["streams"] = [], []
    value = snapshot(empty, baseline)
    assert value["overall_health"] == "HEALTHY" and value["observations"] == []
    assert all(p["empty_verified"] for p in value["pages"].values())
    value["pages"]["videos"]["empty_verified"] = False
    with pytest.raises(SourceError):
        collector.validate_snapshot(collector.seal(value))


@pytest.mark.parametrize("failure", ["configuration", "output", "write"])
def test_collector_fail_closed_safe_report(monkeypatch, tmp_path, capsys, failure):
    output = tmp_path / "new-output"
    if failure == "output":
        output = Path(collector.__file__).parent / "forbidden-output"
    elif failure == "write":
        monkeypatch.setattr(collector, "fingerprints", lambda root: (_ for _ in ()).throw(OSError("SECRET")))
        monkeypatch.setattr(collector, "atomic_json", lambda *args: (_ for _ in ()).throw(OSError("SECRET")))
    assert collector.main(["--state-dir", str(tmp_path / "missing"), "--output-dir", str(output)]) == 1
    assert "SECRET" not in capsys.readouterr().out
    assert not (output / "snapshot.json").exists()
    if failure == "configuration":
        assert (output / "failure.json").exists()


def test_no_secret_fields_or_delivery_in_snapshot(synthetic, baseline):
    value = snapshot(synthetic, baseline)
    def keys(obj):
        if isinstance(obj, dict):
            return set(obj) | set().union(*(keys(v) for v in obj.values()), set())
        if isinstance(obj, list):
            return set().union(*(keys(v) for v in obj), set())
        return set()
    assert not keys(value) & {"published_at", "status", "eligibility", "token", "cookies",
                               "Authorization", "delivered", "event_keys"}


def test_workflow_lineage_must_match(synthetic, baseline, tmp_path):
    first = snapshot(synthetic, baseline)
    _, trust = trust_zip(tmp_path, first)
    _, second = trust_zip(tmp_path, snapshot(synthetic, baseline, "102", minute=1), "second.zip")
    trust["artifacts"].update(second["artifacts"])
    trust["artifacts"]["second.zip"]["workflow_id"] = "999"
    with pytest.raises(SourceError, match="LINEAGE"):
        offline.validate_trust(trust)


@pytest.mark.parametrize("failures", [(), ("videos",), ("streams",), TABS])
def test_tab_intervals_recorded_on_success_and_failure(synthetic, baseline, failures):
    times = [*synthetic["tab_timings_example"]["videos"].values(),
             *synthetic["tab_timings_example"]["streams"].values(), "2026-10-22T00:00:04+00:00"]
    clock = Mock(side_effect=[datetime.fromisoformat(t) for t in times])
    reader = Mock()
    def fetch(tab):
        assert clock.call_count == (1 if tab == "videos" else 3)
        if tab in failures:
            raise requests.Timeout("DO_NOT_LOG")
        return page(synthetic, tab)
    reader.fetch.side_effect = fetch
    value = collector.collect_snapshot(reader, baseline,
        collector.run_metadata(collector.REPOSITORY, "a" * 40, "101", 1),
        {f: "b" * 64 for f in collector.STATE_FILES}, now=clock)
    assert value["tab_timings"] == synthetic["tab_timings_example"]
    assert value["observed_at_utc"] == times[-1] and clock.call_count == 5
    assert reader.fetch.call_count == 2
    result = offline.reconcile([value])
    assert len(result["shadow"]["videos"]) == (0 if failures else 3)


@pytest.mark.parametrize("tab", TABS)
@pytest.mark.parametrize("field", ["started_at_utc", "finished_at_utc"])
@pytest.mark.parametrize("invalid", [None, "invalid", "2026-10-22T00:00:00", "2026-10-22T07:00:00+07:00"])
def test_invalid_tab_timestamp_rejected(synthetic, baseline, tab, field, invalid):
    value = snapshot(synthetic, baseline)
    value["tab_timings"][tab][field] = invalid
    with pytest.raises(SourceError):
        collector.validate_snapshot(collector.seal(value))


@pytest.mark.parametrize("mutation", ["missing_map", "missing_tab", "missing_field", "extra_tab",
                                     "reverse_interval", "overlap", "after_global"])
def test_tab_timing_shape_and_order_fail_closed(synthetic, baseline, mutation):
    value = snapshot(synthetic, baseline)
    value["tab_timings"] = deepcopy(synthetic["tab_timings_example"])
    value["observed_at_utc"] = "2026-10-22T00:00:04+00:00"
    if mutation == "missing_map":
        del value["tab_timings"]
    elif mutation == "missing_tab":
        del value["tab_timings"]["streams"]
    elif mutation == "missing_field":
        del value["tab_timings"]["videos"]["finished_at_utc"]
    elif mutation == "extra_tab":
        value["tab_timings"]["shorts"] = deepcopy(value["tab_timings"]["videos"])
    elif mutation == "reverse_interval":
        value["tab_timings"]["videos"]["started_at_utc"] = "2026-10-22T00:00:02+00:00"
    elif mutation == "overlap":
        value["tab_timings"]["streams"]["started_at_utc"] = "2026-10-22T00:00:00+00:00"
    else:
        value["observed_at_utc"] = "2026-10-22T00:00:02+00:00"
    with pytest.raises(SourceError):
        collector.validate_snapshot(collector.seal(value))


def test_offline_artifact_timing_validation(synthetic, baseline, tmp_path):
    value = snapshot(synthetic, baseline)
    value["tab_timings"]["streams"]["finished_at_utc"] = "2026-10-23T00:00:00+00:00"
    path, trust = trust_zip(tmp_path, collector.seal(value))
    with pytest.raises(SourceError, match="ARTIFACT_INVALID"):
        offline.read_artifact(path, trust)


def test_collector_clock_regression_and_naive_clock_rejected(synthetic, baseline):
    run = collector.run_metadata(collector.REPOSITORY, "a" * 40, "101", 1)
    reader = Mock()
    reader.fetch.side_effect = lambda tab: page(synthetic, tab)
    with pytest.raises(SourceError):
        collector.collect_snapshot(reader, baseline, run, {f: "b" * 64 for f in collector.STATE_FILES},
            now=lambda: datetime(2026, 10, 22))
    times = [datetime(2026, 10, 22, 0, 0, s, tzinfo=timezone.utc) for s in [2, 1, 3, 4, 5]]
    with pytest.raises(SourceError, match="OUT_OF_ORDER"):
        collector.collect_snapshot(reader, baseline, run, {f: "b" * 64 for f in collector.STATE_FILES},
            now=Mock(side_effect=times))


def test_aware_clock_normalized_to_utc(synthetic, baseline):
    reader = Mock()
    reader.fetch.side_effect = lambda tab: page(synthetic, tab)
    value = collector.collect_snapshot(reader, baseline,
        collector.run_metadata(collector.REPOSITORY, "a" * 40, "101", 1),
        {f: "b" * 64 for f in collector.STATE_FILES},
        now=lambda: datetime.fromisoformat("2026-10-22T07:00:00+07:00"))
    assert value["observed_at_utc"] == "2026-10-22T00:00:00+00:00"
    assert all(set(t.values()) == {value["observed_at_utc"]} for t in value["tab_timings"].values())
