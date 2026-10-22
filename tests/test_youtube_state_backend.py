"""GitHub Contents API JSON-state persistence tests; no live network."""
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
import hashlib
import json

import pytest

import youtube_channel_tabs as tabs
import youtube_tabs_monitor as tabs_monitor
from monitor_runner import run_once
from youtube_delivery import SendConfirmation, YouTubeStateStore
from youtube_state_backend import DurableStateConflict, DurableStateError, GitHubStateBranch


NOW = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
VID = "video000001"
REMOTE_PATH = ".bot-state/youtube_delivery_state.json"


def blob_sha(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


class Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


class FakeGitHub:
    def __init__(self):
        self.files = {}
        self.shas = {}
        self.puts = []

    def get(self, url, **kwargs):
        if "/git/ref/heads/" in url:
            return Response(200, {"ref": "refs/heads/youtube-state"})
        if "/contents/" in url:
            path = unquote(url.split("/contents/", 1)[1].split("?", 1)[0])
            if path not in self.files:
                return Response(404)
            return Response(200, {"sha": self.shas[path], "encoding": "base64",
                "content": b64encode(self.files[path]).decode("ascii")})
        raise AssertionError("Unexpected GitHub endpoint")

    def put(self, url, *, json, **kwargs):
        path = unquote(url.split("/contents/", 1)[1].split("?", 1)[0])
        self.puts.append(path)
        if json.get("sha") != self.shas.get(path):
            return Response(409)
        data = b64decode(json["content"])
        sha = blob_sha(data)
        self.files[path], self.shas[path] = data, sha
        return Response(201, {"content": {"sha": sha}})


def backend(remote, *, initialize=False):
    return GitHubStateBranch("owner/repo", "youtube-state", REMOTE_PATH,
        "synthetic-github-token-not-a-secret", initialize=initialize, session=remote)


def store(path, sync):
    sync.load(path)
    value = YouTubeStateStore(path, activation_at=NOW - timedelta(hours=1),
                              policy_version="state-test-v1")
    value.set_commit_hook(sync.persist)
    value.flush()
    return value


def item():
    return {"source": "youtube", "kind": "video", "video_id": VID,
        "title": "fixture", "url": f"https://www.youtube.com/watch?v={VID}",
        "delivery_format": "youtube-tabs-preview-v1"}


def eligible_decision():
    from youtube_eligibility import Decision, Knowledge, Eligibility, DeliveryState
    return Decision(VID, f"youtube:video:{VID}:initial", Knowledge.UNKNOWN,
        Eligibility.ELIGIBLE, DeliveryState.NONE, ("TEST_SIGNED_EVIDENCE",),
        requires_atomic_reservation=True)


def observation():
    return {"video_id": VID, "source_tabs": ["videos", "streams"], "title": "fixture",
        "url": f"https://www.youtube.com/watch?v={VID}"}


def test_state_is_one_versioned_json_blob_across_fresh_actions_directories(tmp_path):
    remote = FakeGitHub()
    first = store(tmp_path / "run-a" / "state.json", backend(remote, initialize=True))
    first.record_capture([observation()], NOW, healthy=True)
    expected = first.observation(VID)
    assert set(remote.files) == {REMOTE_PATH}
    state = json.loads(remote.files[REMOTE_PATH])
    assert state["schema_version"] == 1
    assert {"observations", "captures", "outbox", "capture_seq"} <= set(state)

    second = store(tmp_path / "run-b" / "state.json", backend(remote))
    actual = second.observation(VID)
    assert actual["first_seen_at"] == expected["first_seen_at"]
    assert actual["initial_inventory"] is True
    assert len(actual["captures"]) == 1


def test_each_checkpoint_is_one_api_update_and_stale_writer_fails_closed(tmp_path):
    remote = FakeGitHub()
    initial = store(tmp_path / "init.json", backend(remote, initialize=True))
    assert remote.puts == [REMOTE_PATH]
    initial.record_capture([], NOW, healthy=True)
    assert remote.puts[-1] == REMOTE_PATH and len(remote.files) == 1
    left, right = store(tmp_path / "left.json", backend(remote)), store(tmp_path / "right.json", backend(remote))
    left.record_capture([observation()], NOW + timedelta(minutes=10), healthy=True)
    with pytest.raises(DurableStateConflict):
        right.record_capture([], NOW + timedelta(minutes=12), healthy=True)


def test_send_claim_must_be_durable_before_notifier_is_called(tmp_path):
    remote = FakeGitHub()
    first = store(tmp_path / "setup.json", backend(remote, initialize=True))
    first.reserve(eligible_decision(), item(), now=NOW)
    stale, winner = store(tmp_path / "stale.json", backend(remote)), store(tmp_path / "winner.json", backend(remote))
    winner.record_capture([], NOW + timedelta(minutes=1), healthy=True)
    called = []
    with pytest.raises(DurableStateConflict):
        stale.send_once(eligible_decision().event_key,
            lambda payload, ticket: called.append(ticket), enabled=True, now=NOW + timedelta(minutes=2))
    assert called == []
    recovered = store(tmp_path / "recovered.json", backend(remote))
    assert recovered.outbox_record(eligible_decision().event_key)["status"] == "reserved"


def test_confirmed_delivery_and_ambiguous_recovery_survive_restart(tmp_path):
    remote = FakeGitHub()
    first = store(tmp_path / "run-a.json", backend(remote, initialize=True))
    first.reserve(eligible_decision(), item(), now=NOW)
    event = eligible_decision().event_key
    assert first.send_once(event, lambda *_: SendConfirmation(200, True, 88), enabled=True, now=NOW) == "delivered"
    second = store(tmp_path / "run-b.json", backend(remote))
    assert second.outbox_record(event)["status"] == "delivered"
    assert second.outbox_record(event)["message_id"] == 88
    assert second.send_once(event, lambda *_: pytest.fail("must not replay"), enabled=True,
                            now=NOW + timedelta(minutes=1)) == "delivered"

    other = eligible_decision()
    other_item = {**item(), "video_id": "video000002", "url": "https://www.youtube.com/watch?v=video000002"}
    from dataclasses import replace
    other = replace(other, video_id="video000002", event_key="youtube:video:video000002:initial")
    second.reserve(other, other_item, now=NOW + timedelta(minutes=2))
    assert second.send_once(other.event_key, lambda *_: None, enabled=True,
                            now=NOW + timedelta(minutes=2)) == "ambiguous"
    restarted = store(tmp_path / "run-c.json", backend(remote))
    assert restarted.outbox_record(other.event_key)["status"] == "ambiguous"


def test_missing_or_invalid_state_fails_closed_without_secret_output(tmp_path, capsys):
    with pytest.raises(DurableStateError, match="HTTP 404"):
        backend(FakeGitHub()).load(tmp_path / "missing.json")
    assert capsys.readouterr().out == ""
    remote = FakeGitHub()
    remote.files[REMOTE_PATH] = b'{"schema_version":99}'
    remote.shas[REMOTE_PATH] = blob_sha(remote.files[REMOTE_PATH])
    with pytest.raises(DurableStateError, match="schema"):
        backend(remote).load(tmp_path / "invalid.json")


def test_runner_reloads_json_state_across_fresh_actions_dirs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    remote = FakeGitHub()
    original_init = GitHubStateBranch.__init__
    def with_remote(self, *args, **kwargs):
        kwargs["session"] = remote
        original_init(self, *args, **kwargs)
    monkeypatch.setattr(GitHubStateBranch, "__init__", with_remote)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    clock = [NOW]
    monkeypatch.setattr(tabs, "datetime", SimpleNamespace(now=lambda tz: clock[0]))

    class Reader:
        def fetch(self, tab):
            row = {"source": "youtube", "observation_format": tabs.FORMAT, "video_id": VID,
                "title": "fixture", "channel_title": "Fixture", "url": f"https://www.youtube.com/watch?v={VID}",
                "thumbnail": "", "source_tabs": [tab],
                "source_metadata": {"relative_text": [], "badges": [], "schedule_timezone": None}}
            return tabs.ChannelTabPage(tab, [row], False, empty_verified=False)

    original_adapter = tabs_monitor.ChannelTabsAdapter
    reader = Reader()
    def build_adapter(*, channel_id, policy, reviews, review_verifier):
        return original_adapter(channel_id=channel_id, policy=policy, reviews=reviews,
            review_verifier=review_verifier, tabs_reader=reader, clock=lambda: clock[0])
    monkeypatch.setattr(tabs_monitor, "ChannelTabsAdapter", build_adapter)
    config = {"youtube_monitor_enabled": True, "youtube_observation_enabled": True,
        "youtube_delivery_enabled": False, "youtube_delivery_send_enabled": False,
        "youtube_channel_id": tabs.CHANNEL_ID, "youtube_activation_at": (NOW - timedelta(hours=1)).isoformat(),
        "youtube_policy_version": "durable-runner-test", "youtube_policy_approved": False,
        "youtube_window_seconds": 86400, "youtube_ttl_seconds": 1800, "youtube_skew_seconds": 300,
        "youtube_capture_interval_seconds": 600, "youtube_state_branch": "youtube-state",
        "youtube_state_token": "test-only-github-token", "youtube_state_initialize": True,
        "youtube_review_source_hosts": [], "notification_method": "console"}

    def scratch(path):
        path.mkdir()
        (path / "youtube_cache.json").write_text("[]", encoding="utf-8")
        (path / "announcements_cache.json").write_text("[]", encoding="utf-8")
        (path / "delivery_state.json").write_text(json.dumps({"version": 1, "batches": [],
            "youtube_rss_cutover": {"version": 1, "cutover_at": NOW.isoformat(), "baseline_ids": []}}), encoding="utf-8")
        from unittest.mock import Mock
        scraper, notifier = Mock(), Mock()
        scraper.check_new_announcements.return_value = []
        scraper.current_items = []
        return scraper, notifier

    first_dir = tmp_path / "actions-run-1"
    scraper, notifier = scratch(first_dir)
    first = run_once(config, dry_run=False, state_dir=first_dir, scraper=scraper, notifier=notifier)
    assert remote.files.keys() == {REMOTE_PATH} and first.youtube_eligibility
    clock[0] += timedelta(minutes=12)
    second_dir = tmp_path / "actions-run-2"
    scraper, notifier = scratch(second_dir)
    second = run_once(config, dry_run=False, state_dir=second_dir, scraper=scraper, notifier=notifier)
    assert second.youtube_eligibility["decisions"][0]["eligibility"] == "HOLD_FOR_REVIEW"
    persisted = YouTubeStateStore(second_dir / "youtube_delivery_state.json",
        activation_at=NOW - timedelta(hours=1), policy_version="durable-runner-test")
    record = persisted.observation(VID)
    assert record["first_seen_at"] == NOW.isoformat()
    assert len(record["captures"]) == 2
    assert notifier.send_notification.call_count == 0


def test_actions_refuses_ephemeral_fallback_when_durable_backend_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    config = {"youtube_monitor_enabled": True, "youtube_observation_enabled": True,
        "youtube_delivery_enabled": False, "youtube_delivery_send_enabled": False,
        "youtube_channel_id": "UCGo_6l_6kp8H8OHcKcSIeDw", "youtube_activation_at": NOW.isoformat(),
        "youtube_policy_version": "fail-closed-test", "notification_method": "console"}
    (tmp_path / "delivery_state.json").write_text(json.dumps({"version": 1, "batches": [],
        "youtube_rss_cutover": {"version": 1, "cutover_at": NOW.isoformat(), "baseline_ids": []}}), encoding="utf-8")
    from unittest.mock import Mock
    scraper = Mock()
    scraper.check_new_announcements.return_value = []
    scraper.current_items = []
    result = run_once(config, dry_run=False, state_dir=tmp_path, scraper=scraper, notifier=Mock())
    source = next(source for source in result.sources if source.source == "youtube")
    assert source.status == "FAILED"
    assert not (tmp_path / "youtube_delivery_state.json").exists()
