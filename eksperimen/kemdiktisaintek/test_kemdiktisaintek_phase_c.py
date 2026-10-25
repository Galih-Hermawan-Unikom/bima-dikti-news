"""Offline Phase C automation, BIMA reference, scheduler, and delivery regressions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
from unittest.mock import Mock

import pytest
import requests
import yaml

from stage3_fixtures import AT, NOW, TARGET, Clock, FixtureGitHub, FixtureSource, FixtureTelegram
from kemdiktisaintek_bima_reference import create_reference, validate_reference as validate_bima_reference
from kemdiktisaintek_automation import run_poll
from kemdiktisaintek_delivery_state import migrate_v1
from kemdiktisaintek_monitor import capture_window, initialize
from kemdiktisaintek_remote_state import RemoteState
from kemdiktisaintek_state import StateError, StateConflict, fingerprint
from kemdiktisaintek_telegram import Credentials, TelegramGateway
from kemdiktisaintek_scheduler import plan, dispatch

HERE = Path(__file__).parent
REPO = "Galih-Hermawan-Unikom/bima-dikti-news"
FEATURE = "feat/kemdiktisaintek-telegram-stage3"
SHA = "a" * 40


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Live HTTP/Telegram is forbidden in Phase C tests")
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def env(*, enabled=True, auto=True):
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/" + FEATURE,
        "GITHUB_SHA": SHA, "KEMDIKTISAINTEK_REVIEWED_CODE_SHA": SHA,
        "GITHUB_WORKFLOW_REF": REPO + "/.github/workflows/kemdiktisaintek-experiment.yml@refs/heads/" + FEATURE,
        "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1",
        "KEMDIKTISAINTEK_EXPERIMENT_ENABLED": "true" if enabled else "false",
        "KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND": "true" if auto else "false",
        "YOUTUBE_EXPERIMENT_BOT_TOKEN": "73:synthetic_fixture_only",
        "YOUTUBE_EXPERIMENT_CHAT_ID": TARGET,
        "YOUTUBE_EXPERIMENT_CHAT_ID_SHA256": hashlib.sha256(TARGET.encode()).hexdigest(),
        "TELEGRAM_BOT_TOKEN": "production-token-must-not-be-read",
        "TELEGRAM_CHAT_ID": "-1009999999999"}


class BimaFixture:
    def __init__(self, rows=None, *, pagination="FIRST_PAGE_COMPLETE", sorted_desc=True, error=None):
        self.rows = ([{"title": "BIMA Hibah Tahun 2026", "surat": "9/ABC/2026",
            "date": "20 Oktober 2026", "documents": [{"name": "Panduan.pdf"}]}] if rows is None else rows)
        self.pagination, self.sorted_desc, self.error = pagination, sorted_desc, error

    def fetch(self):
        if self.error:
            raise self.error
        return deepcopy(self.rows), self.pagination, self.sorted_desc


def make_context(*, historical=False, irrelevant=False, now=NOW, enabled=True, auto=True,
                 bima=None, telegram_outcome="success", detail_patch=None):
    source = FixtureSource(historical=historical, irrelevant=irrelevant)
    class Sink:
        def create(self, state):
            self.state = state
    baseline = initialize(Sink(), capture_window(source, at=AT))
    github = FixtureGitHub(migrate_v1(baseline, at=AT))
    remote = RemoteState("fixture-state-token", session=github)
    source.include_new = True
    if detail_patch:
        original = source.fetch
        def patched(url):
            result = original(url)
            return detail_patch(url, result)
        source.fetch = patched
    envs = env(enabled=enabled, auto=auto)
    telegram = FixtureTelegram()
    telegram.outcome = telegram_outcome
    gateway = TelegramGateway(Credentials.from_environment(envs), session=telegram)
    clock = Clock(now)
    return {"source": source, "github": github, "remote": remote, "env": envs,
            "telegram": telegram, "gateway": gateway, "clock": clock,
            "bima": bima or BimaFixture()}


def run(c):
    return run_poll(c["env"], backend=c["remote"], clock=c["clock"],
                    source=c["source"], bima_collector=c["bima"], gateway=c["gateway"])


def test_new_relevant_article_runs_full_pipeline_once_then_known_after_restart():
    c = make_context()
    report = run(c)
    state, _ = c["remote"].load()
    assert report["api_health"] == "HEALTHY"
    assert report["status_counts"]["NEW_CANDIDATE"] == 1
    assert report["reserved_this_poll"] == report["delivered_this_poll"] == 1
    assert c["telegram"].posts[0]["chat_id"] == TARGET
    assert len(c["telegram"].posts) == 1
    row = next(r for r in state["outbox"].values() if r["event_type"] == "INITIAL")
    assert row["status"] == "DELIVERED" and row["receipt"]["ok"] is True
    c["clock"].value = "2026-10-24T10:20:00Z"
    run(c)
    restarted = RemoteState("fixture-state-token", session=c["github"])
    c["remote"] = restarted
    c["clock"].value = "2026-10-24T10:25:00Z"
    report = run(c)
    assert report["status_counts"]["NEW_CANDIDATE"] == 0
    assert len(c["telegram"].posts) == 1


@pytest.mark.parametrize("case,expected", [("historical", "HOLD"), ("irrelevant", "REJECTED")])
def test_historical_or_nonacademic_items_never_post(case, expected):
    c = make_context(historical=case == "historical", irrelevant=case == "irrelevant")
    report = run(c)
    assert report["status_counts"][expected] >= 1
    assert c["telegram"].posts == []


def test_bima_confirmed_duplicate_is_rejected_without_post():
    def with_letter(url, value):
        return value.replace("Perguruan tinggi membuka pendaftaran",
            "No Surat: 123/ABC/2026. Perguruan tinggi membuka pendaftaran") if url.endswith("fixture-new") else value
    bima = BimaFixture([{"title": "Penawaran Hibah Penelitian untuk Dosen",
        "surat": "123/ABC/2026", "date": "24 Oktober 2026", "documents": []}])
    c = make_context(bima=bima, detail_patch=with_letter)
    report = run(c)
    assert report["status_counts"]["DUPLICATE"] == 1
    assert report["status_counts"]["REJECTED"] == 1
    assert c["telegram"].posts == []


@pytest.mark.parametrize("bima", [BimaFixture(error=TimeoutError()),
    BimaFixture(pagination="UNKNOWN"), BimaFixture(rows=[])])
def test_missing_failed_or_incomplete_bima_reference_holds_candidate(bima):
    c = make_context(bima=bima)
    report = run(c)
    state, _ = c["remote"].load()
    assert report["status_counts"]["HOLD"] + report["status_counts"]["NEEDS_REVIEW"] >= 1
    assert not state["admissions"] and c["telegram"].posts == []


def test_stale_corrupt_and_uncovered_bima_evidence_fails_closed():
    producer = {"repository": REPO, "workflow_ref": env()["GITHUB_WORKFLOW_REF"],
        "workflow_sha": SHA, "run_id": "12345", "run_attempt": "1",
        "event": "workflow_dispatch", "ref": "refs/heads/" + FEATURE}
    ref = create_reference(BimaFixture().rows, capture_started_at=NOW,
        capture_finished_at=NOW, pagination_state="FIRST_PAGE_COMPLETE", newest_first=True,
        producer=producer)
    digest = fingerprint(ref)
    with pytest.raises(StateError):
        validate_bima_reference(ref, digest, at="2026-10-24T11:11:01Z", env=env())
    with pytest.raises(StateError):
        validate_bima_reference(ref, "0" * 64, at=NOW, env=env())
    incomplete = create_reference([{"title": "Recent BIMA", "surat": "1/A/2026",
        "date": "24 Oktober 2026", "documents": []}], capture_started_at=NOW,
        capture_finished_at=NOW, pagination_state="FIRST_PAGE_HAS_NEXT", newest_first=True,
        producer=producer)
    with pytest.raises(StateError, match="DOES_NOT_COVER"):
        validate_bima_reference(incomplete, fingerprint(incomplete), at=NOW, env=env(),
            candidate={"published_at": "2026-10-24T10:05:00Z"})
    with pytest.raises(StateError, match="COVERAGE_UNKNOWN"):
        create_reference(BimaFixture().rows, capture_started_at=NOW,
            capture_finished_at=NOW, pagination_state="UNKNOWN", newest_first=True, producer=producer)


def test_bima_empty_or_fetch_error_never_yields_no_match():
    c = make_context(bima=BimaFixture(error=TimeoutError("secret-bearing network error")))
    report = run(c)
    state, _ = c["remote"].load()
    row = next(r for r in state["observation_state"]["observations"].values()
               if r["title"] == "Penawaran Hibah Penelitian untuk Dosen")
    assert row["detection_status"] == "HOLD"
    assert "secret-bearing" not in json.dumps(report)
    assert not state["admissions"] and not c["telegram"].posts


def test_revision_candidate_is_review_only_and_kemdiktisaintek_api_failure_is_reported():
    c = make_context()
    c["source"].include_new = False
    c["source"].revised = True
    report = run(c)
    state, _ = c["remote"].load()
    assert report["status_counts"]["NEEDS_REVIEW"] >= 1
    assert state["observation_state"]["revisions"] and not c["telegram"].posts
    class FailedSource:
        def fetch(self, url):
            raise TimeoutError("fixture API timeout")
    c = make_context()
    before, _ = c["remote"].load()
    report = run_poll(c["env"], backend=c["remote"], clock=c["clock"],
                       source=FailedSource(), bima_collector=c["bima"], gateway=c["gateway"])
    after, _ = c["remote"].load()
    assert report["api_health"] == "FAILED" and report["capture_error"] == "TimeoutError"
    assert before == after and c["telegram"].posts == []


def test_switch_off_and_wrong_destination_never_post():
    c = make_context(enabled=False, auto=False)
    report = run(c)
    assert report["delivery_enabled"] is False and c["telegram"].posts == []
    c = make_context()
    c["telegram"].target = c["telegram"].production_id
    report = run(c)
    assert c["telegram"].posts == []
    assert "DELIVERY_BLOCKED" in report["bima_reference_status"]


def test_cas_conflict_before_post_and_timeout_after_claim_do_not_retry():
    c = make_context()
    c["github"].fail_put = c["github"].put_count + 1
    with pytest.raises(StateConflict):
        run(c)
    assert c["telegram"].posts == []
    c = make_context(telegram_outcome="timeout")
    report = run(c)
    assert report["ambiguous_this_poll"] == 1 and len(c["telegram"].posts) == 1
    c["clock"].value = "2026-10-24T10:20:00Z"
    run(c)
    assert len(c["telegram"].posts) == 1
    state, _ = c["remote"].load()
    assert next(r for r in state["outbox"].values() if r["event_type"] == "INITIAL")["status"] == "AMBIGUOUS"


def test_receipt_checkpoint_failure_is_reconciled_without_resend():
    c = make_context()
    c["github"].fail_put = c["github"].put_count + 4  # observation, reservation, claim, receipt
    report = run(c)
    assert report["ambiguous_this_poll"] == 1 and len(c["telegram"].posts) == 1
    state, _ = c["remote"].load()
    assert next(r for r in state["outbox"].values() if r["event_type"] == "INITIAL")["status"] == "SENDING"
    c["github"].fail_put = None
    c["clock"].value = "2026-10-24T10:15:00Z"
    run(c)
    state, _ = c["remote"].load()
    assert len(c["telegram"].posts) == 1
    assert next(r for r in state["outbox"].values() if r["event_type"] == "INITIAL")["status"] == "AMBIGUOUS"


def test_delayed_poll_holds_then_clean_recovery_can_recheck_without_backfill():
    c = make_context(now="2026-10-24T10:51:00Z")
    report = run(c)
    assert report["poll_gap_seconds"] == 3060
    assert report["status_counts"]["HOLD"] >= 1 and c["telegram"].posts == []
    c["clock"].value = "2026-10-24T11:21:00Z"
    report = run(c)
    assert report["status_counts"]["NEW_CANDIDATE"] == 1
    assert len(c["telegram"].posts) == 1


def test_scheduler_on_master_dispatches_only_pinned_feature_sha():
    approved = "b" * 40
    context = {"GITHUB_REF": "refs/heads/master", "GITHUB_REPOSITORY": REPO,
        "KEMDIKTISAINTEK_SCHEDULER_ENABLED": "true",
        "KEMDIKTISAINTEK_EXPERIMENT_APPROVED_CODE_SHA": approved}
    assert plan(context, approved)["dispatch"] is True
    assert plan({**context, "GITHUB_REF": "refs/heads/feat/other"}, approved)["dispatch"] is False
    assert plan(context, "c" * 40)["dispatch"] is False
    requests_seen = []
    def fake_request(method, path, token, payload=None):
        requests_seen.append((method, path, payload))
        if method == "GET":
            return 200, {"object": {"sha": approved}}
        return 204, None
    result = dispatch({**context, "GITHUB_TOKEN": "mock"}, request=fake_request)
    assert result["dispatch"] is True
    assert [r[0] for r in requests_seen] == ["GET", "POST"]
    assert requests_seen[-1][2]["inputs"]["mode"] == "POLL"
    assert requests_seen[-1][2]["inputs"]["reviewed_code_sha"] == approved
    assert dispatch({**context, "GITHUB_TOKEN": "mock", "KEMDIKTISAINTEK_SCHEDULER_ENABLED": "false"},
        request=fake_request)["dispatch"] is False
    assert len([r for r in requests_seen if r[0] == "POST"]) == 1


def test_scheduler_workflow_cron_permissions_defaults_and_feature_environment_boundary():
    workflow = yaml.safe_load((HERE.parents[1] / ".github/workflows/kemdiktisaintek-scheduler.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"] == [{"cron": "11,41 * * * *"}]
    job = workflow["jobs"]["dispatch-poll"]
    assert job["if"] == "github.ref == 'refs/heads/master'"
    assert job["permissions"] == {"actions": "write", "contents": "read"}
    assert job["concurrency"] if "concurrency" in job else workflow["concurrency"]["cancel-in-progress"] is False
    text = (HERE.parents[1] / ".github/workflows/kemdiktisaintek-scheduler.yml").read_text()
    assert "KEMDIKTISAINTEK_SCHEDULER_ENABLED || 'false'" in text
    assert "persist-credentials: false" in text and "secrets.TELEGRAM_BOT_TOKEN" not in text
    assert "kemdiktisaintek-experiment" in (HERE.parents[1] / ".github/workflows/kemdiktisaintek-experiment.yml").read_text()


def test_poll_runtime_uses_no_production_telegram_credential_or_target():
    c = make_context()
    run(c)
    assert c["telegram"].posts and c["telegram"].posts[0]["chat_id"] == TARGET
    assert c["telegram"].posts[0]["chat_id"] != "-1009999999999"
    assert "TELEGRAM_BOT_TOKEN" not in c["gateway"].credentials.mapping()
