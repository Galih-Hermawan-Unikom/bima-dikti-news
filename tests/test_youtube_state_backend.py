from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

import youtube_channel_tabs as tabs
import youtube_tabs_monitor as tabs_monitor
from monitor_runner import run_once
from youtube_delivery import YouTubeStateStore
from youtube_state_backend import DurableStateConflict, DurableStateError, GitHubStateBranch


NOW = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
VID = "video000001"


class Response:
    def __init__(self, status, content=b"", body=None):
        self.status_code = status
        self.content = content
        self._body = body or {}

    def json(self):
        return self._body


class FakeGitHub:
    def __init__(self):
        self.files = {}
        self.shas = {}
        self.puts = 0

    def get(self, url, **kwargs):
        if "/git/ref/heads/" in url:
            return Response(200, body={"ref": "refs/heads/youtube-state"})
        if "/contents/" in url:
            path = unquote(url.split("/contents/", 1)[1].split("?", 1)[0])
            return Response(404) if path not in self.files else Response(200, self.files[path])
        raise AssertionError("Unexpected GitHub endpoint")

    def put(self, url, *, json, **kwargs):
        self.puts += 1
        path = unquote(url.split("/contents/", 1)[1].split("?", 1)[0])
        if json.get("sha") != self.shas.get(path):
            return Response(409)
        import base64
        data = base64.b64decode(json["content"])
        sha = GitHubStateBranch._git_blob_sha(data)
        self.shas[path] = sha
        self.files[path] = data
        return Response(201, body={"content": {"sha": sha}})


def backend(remote, *, initialize=False):
    return GitHubStateBranch("owner/repo", "youtube-state", ".bot-state/state.sqlite3",
                             "synthetic-github-token-not-a-secret", initialize=initialize,
                             session=remote)


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
    return Decision(VID, f"youtube:video:{VID}:initial", Knowledge.UNKNOWN, Eligibility.ELIGIBLE,
                    DeliveryState.NONE, ("TEST_SIGNED_EVIDENCE",), requires_atomic_reservation=True)


def observation():
    return {"video_id": VID, "source_tabs": ["videos", "streams"], "title": "fixture",
            "url": f"https://www.youtube.com/watch?v={VID}"}


def test_state_blob_survives_fresh_actions_scratch_directory(tmp_path):
    remote = FakeGitHub()
    first = store(tmp_path / "run-a" / "state.sqlite3", backend(remote, initialize=True))
    first.record_capture([observation()], NOW, healthy=True)
    expected = first.observation(VID)

    second = store(tmp_path / "run-b" / "state.sqlite3", backend(remote))
    actual = second.observation(VID)
    assert actual["first_seen_at"] == expected["first_seen_at"]
    assert actual["initial_inventory"] is True
    assert len(actual["captures"]) == 1


def test_compare_and_swap_rejects_concurrent_stale_writer(tmp_path):
    remote = FakeGitHub()
    initial = store(tmp_path / "init.sqlite3", backend(remote, initialize=True))
    initial.record_capture([], NOW, healthy=True)
    left = store(tmp_path / "left.sqlite3", backend(remote))
    right = store(tmp_path / "right.sqlite3", backend(remote))
    left.record_capture([observation()], NOW + timedelta(minutes=10), healthy=True)
    with pytest.raises(DurableStateConflict):
        right.record_capture([], NOW + timedelta(minutes=12), healthy=True)


def test_send_claim_must_be_durable_before_notifier_is_called(tmp_path):
    remote = FakeGitHub()
    first = store(tmp_path / "setup.sqlite3", backend(remote, initialize=True))
    first.reserve(eligible_decision(), item(), now=NOW)
    stale = store(tmp_path / "stale.sqlite3", backend(remote))
    winner = store(tmp_path / "winner.sqlite3", backend(remote))
    winner.record_capture([], NOW + timedelta(minutes=1), healthy=True)

    called = []
    with pytest.raises(DurableStateConflict):
        stale.send_once(eligible_decision().event_key,
                        lambda payload, ticket: called.append(ticket), enabled=True,
                        now=NOW + timedelta(minutes=2))
    assert called == []
    recovered = store(tmp_path / "recovered.sqlite3", backend(remote))
    assert recovered.outbox_record(eligible_decision().event_key)["status"] == "reserved"


def test_confirmed_delivery_and_ambiguous_recovery_survive_restart(tmp_path):
    remote = FakeGitHub()
    first = store(tmp_path / "run-a.sqlite3", backend(remote, initialize=True))
    first.reserve(eligible_decision(), item(), now=NOW)
    from youtube_delivery import SendConfirmation
    event = eligible_decision().event_key
    assert first.send_once(event, lambda *_: SendConfirmation(200, True, 88),
                           enabled=True, now=NOW) == "delivered"
    second = store(tmp_path / "run-b.sqlite3", backend(remote))
    assert second.outbox_record(event)["status"] == "delivered"
    assert second.outbox_record(event)["message_id"] == 88
    assert second.send_once(event, lambda *_: pytest.fail("must not replay"),
                            enabled=True, now=NOW + timedelta(minutes=1)) == "delivered"


def test_no_database_or_credentials_are_printed_on_backend_error(tmp_path, capsys):
    class Denied(FakeGitHub):
        def get(self, url, **kwargs):
            return Response(403)
    with pytest.raises(Exception, match="HTTP 403"):
        backend(Denied()).load(tmp_path / "state.sqlite3")
    assert capsys.readouterr().out == ""


def test_missing_or_mismatched_manifest_fails_closed(tmp_path):
    remote = FakeGitHub()
    initialized = backend(remote, initialize=True)
    first = store(tmp_path / "first.sqlite3", initialized)
    first.record_capture([], NOW, healthy=True)
    assert len(remote.files) == 2

    db_path = initialized.path
    del remote.files[db_path]
    with pytest.raises(DurableStateError):
        backend(remote).load(tmp_path / "missing.sqlite3")

    remote = FakeGitHub()
    first = store(tmp_path / "second.sqlite3", backend(remote, initialize=True))
    first.record_capture([], NOW, healthy=True)
    remote.files[".bot-state/state.sqlite3"] = b"tampered database"
    with pytest.raises(DurableStateError, match="manifest mismatch"):
        backend(remote).load(tmp_path / "tampered.sqlite3")


def test_runner_reloads_durable_observations_across_fresh_actions_dirs(tmp_path, monkeypatch):
    import json
    import youtube_state_backend
    from types import SimpleNamespace
    from unittest.mock import Mock

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
            rows = [{"source": "youtube", "observation_format": tabs.FORMAT,
                "video_id": VID, "title": "fixture", "channel_title": "Fixture",
                "url": f"https://www.youtube.com/watch?v={VID}", "thumbnail": "",
                "source_tabs": [tab], "source_metadata": {"relative_text": [], "badges": [],
                    "schedule_timezone": None}}]
            return tabs.ChannelTabPage(tab, rows, False, empty_verified=False)

    original_adapter = tabs_monitor.ChannelTabsAdapter
    reader = Reader()
    def build_adapter(*, channel_id, policy, reviews, review_verifier):
        return original_adapter(channel_id=channel_id, policy=policy, reviews=reviews,
            review_verifier=review_verifier, tabs_reader=reader, clock=lambda: clock[0])
    monkeypatch.setattr(tabs_monitor, "ChannelTabsAdapter", build_adapter)
    config = {
        "youtube_monitor_enabled": True, "youtube_observation_enabled": True,
        "youtube_delivery_enabled": False, "youtube_delivery_send_enabled": False,
        "youtube_channel_id": tabs.CHANNEL_ID, "youtube_activation_at": (NOW - timedelta(hours=1)).isoformat(),
        "youtube_policy_version": "durable-runner-test", "youtube_policy_approved": False,
        "youtube_window_seconds": 86400, "youtube_ttl_seconds": 1800,
        "youtube_skew_seconds": 300, "youtube_capture_interval_seconds": 600,
        "youtube_state_branch": "youtube-state", "youtube_state_token": "test-only-github-token",
        "youtube_state_initialize": True,
        "youtube_review_source_hosts": [], "notification_method": "console",
    }

    def scratch(path):
        path.mkdir()
        (path / "youtube_cache.json").write_text("[]", encoding="utf-8")
        (path / "announcements_cache.json").write_text("[]", encoding="utf-8")
        (path / "delivery_state.json").write_text(json.dumps({"version": 1, "batches": [],
            "youtube_rss_cutover": {"version": 1, "cutover_at": NOW.isoformat(), "baseline_ids": []}}),
            encoding="utf-8")
        scraper = Mock()
        scraper.check_new_announcements.return_value = []
        scraper.current_items = []
        notifier = Mock()
        return scraper, notifier

    first_dir = tmp_path / "actions-run-1"
    scraper, notifier = scratch(first_dir)
    first = run_once(config, dry_run=False, state_dir=first_dir, scraper=scraper, notifier=notifier)
    assert remote.files and first.youtube_eligibility
    clock[0] += timedelta(minutes=12)
    second_dir = tmp_path / "actions-run-2"
    scraper, notifier = scratch(second_dir)
    second = run_once(config, dry_run=False, state_dir=second_dir, scraper=scraper, notifier=notifier)
    assert second.youtube_eligibility["decisions"][0]["eligibility"] == "HOLD_FOR_REVIEW"
    durable = YouTubeStateStore(second_dir / "youtube_delivery.sqlite3",
        activation_at=NOW - timedelta(hours=1), policy_version="durable-runner-test")
    observation_record = durable.observation(VID)
    assert observation_record["first_seen_at"] == NOW.isoformat()
    assert len(observation_record["captures"]) == 2
    assert notifier.send_notification.call_count == 0


def test_actions_refuses_ephemeral_fallback_when_durable_backend_missing(tmp_path, monkeypatch):
    import json
    from unittest.mock import Mock
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    config = {
        "youtube_monitor_enabled": True, "youtube_observation_enabled": True,
        "youtube_delivery_enabled": False, "youtube_delivery_send_enabled": False,
        "youtube_channel_id": "UCGo_6l_6kp8H8OHcKcSIeDw",
        "youtube_activation_at": NOW.isoformat(), "youtube_policy_version": "fail-closed-test",
        "notification_method": "console",
    }
    (tmp_path / "delivery_state.json").write_text(json.dumps({"version": 1, "batches": [],
        "youtube_rss_cutover": {"version": 1, "cutover_at": NOW.isoformat(), "baseline_ids": []}}),
        encoding="utf-8")
    scraper = Mock()
    scraper.check_new_announcements.return_value = []
    scraper.current_items = []
    result = run_once(config, dry_run=False, state_dir=tmp_path, scraper=scraper, notifier=Mock())
    source = next(source for source in result.sources if source.source == "youtube")
    assert source.status == "FAILED"
    assert not (tmp_path / "youtube_delivery.sqlite3").exists()


