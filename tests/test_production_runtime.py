from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from notifier import Notifier
import production_runtime as prod
from production_preflight import verify_production
from youtube_experiment_preflight import PreflightError
from youtube_delivery import YouTubeStateStore
import youtube_eligibility as e


NOW = datetime(2026, 10, 25, 3, tzinfo=timezone.utc)
FENCE = datetime(2026, 10, 23, 3, tzinfo=timezone.utc)
VID = "newvideo001"


def bima_state(initialized=True):
    return {"schema_version": 1, "source": "bima", "activation_at": FENCE.isoformat(),
        "initialized": initialized, "inventory": [], "first_seen": {}, "outbox": {}}


def test_poll_gap_observability_reports_elapsed_seconds_and_missing_baseline():
    previous = "2026-10-26T15:00:00+00:00"
    current = "2026-10-26T18:30:00+00:00"
    assert prod.poll_gap_seconds(previous, current) == 12600
    assert prod.poll_gap_seconds(None, current) is None
    assert prod.poll_gap_warning(3601, 1800) == "UNHEALTHY_POLL_GAP"
    assert prod.poll_gap_warning(3600, 1800) is None


def test_bima_state_accepts_legacy_poll_timestamp_and_validates_new_value():
    state = bima_state()
    prod.validate_bima(state)
    state["last_successful_poll_at"] = NOW.isoformat()
    prod.validate_bima(state)
    state["last_successful_poll_at"] = "invalid"
    with pytest.raises(ValueError, match="successful poll timestamp"):
        prod.validate_bima(state)


def test_failed_bima_cycle_after_initial_checkpoint_does_not_advance_poll_timestamp(monkeypatch):
    state = bima_state()
    previous = "2026-10-24T15:00:00+00:00"
    state["last_successful_poll_at"] = previous
    persisted = []

    def checkpoint():
        persisted.append(deepcopy(state))

    def fail_after_initial_checkpoint(*args, **kwargs):
        checkpoint()
        raise RuntimeError("simulated cycle failure after initial checkpoint")

    monkeypatch.setattr(prod, "bima_cycle", fail_after_initial_checkpoint)
    with pytest.raises(RuntimeError, match="after initial checkpoint"):
        prod.bima_poll(state, [], [], fake_notifier(), checkpoint,
                       enabled=True, at=NOW)
    assert persisted[0]["last_successful_poll_at"] == previous
    assert state["last_successful_poll_at"] == previous


def test_successful_bima_cycle_checkpoints_poll_timestamp_after_cycle(monkeypatch):
    state = bima_state()
    persisted = []

    def checkpoint():
        persisted.append(deepcopy(state))

    def successful_cycle(*args, **kwargs):
        checkpoint()
        return {"source": "bima", "eligible": 0, "hold": 0}

    monkeypatch.setattr(prod, "bima_cycle", successful_cycle)
    report = prod.bima_poll(state, [], [], fake_notifier(), checkpoint,
                            enabled=True, at=NOW)
    assert persisted[0].get("last_successful_poll_at") is None
    assert persisted[1]["last_successful_poll_at"] == NOW.isoformat()
    assert report["poll_gap_seconds"] is None


def announcement():
    return {"title": "Pendanaan Riset Baru untuk Perguruan Tinggi", "surat": "123/DST/2026",
        "date": "25 Oktober 2026", "documents": [], "url": "https://example.invalid/bima"}


def fake_notifier(ok=True):
    notifier = Mock()
    notifier._build_summary_chunks.return_value = ["summary"]
    notifier.last_delivery_receipts = []
    def send(items, *, progress, checkpoint):
        notifier.last_delivery_receipts = [{"http_status": 200, "ok": True, "message_id": 88}]
        progress["sent_chunks"] = [1]
        checkpoint()
        return ok
    notifier.send_notification.side_effect = send
    return notifier


def test_bima_startup_never_sends_and_historical_cache_is_not_modified():
    state, item, notifier = bima_state(False), announcement(), fake_notifier()
    history = [deepcopy(item)]
    before = deepcopy(history)
    report = prod.bima_cycle(state, [item], history, notifier, lambda: None, enabled=True, at=NOW)
    assert report["startup"] and report["confirmed_messages"] == 0
    notifier.send_notification.assert_not_called()
    assert history == before and len(state["inventory"]) == 1 and state["outbox"] == {}


@pytest.mark.parametrize("date", ["22 Oktober 2026", "23 Oktober 2026", "99 Oktober 2026", "26 Oktober 2026", "unknown"])
def test_bima_old_same_day_invalid_and_future_publications_hold(date):
    item, state, notifier = dict(announcement(), date=date), bima_state(), fake_notifier()
    report = prod.bima_cycle(state, [item], [], notifier, lambda: None, enabled=True, at=NOW)
    assert report["hold"] == 1 and state["outbox"] == {}
    notifier.send_notification.assert_not_called()


def test_bima_claim_checkpoint_precedes_send_and_restart_deduplicates():
    state, notifier, checkpoints = bima_state(), fake_notifier(), []
    def checkpoint():
        checkpoints.append(deepcopy(state))
    def send(items, *, progress, checkpoint):
        row = next(iter(checkpoints[-1]["outbox"].values()))
        assert row["status"] == "sending" and row["attempts"] == 1
        notifier.last_delivery_receipts = [{"http_status": 200, "ok": True, "message_id": 88}]
        checkpoint()
        return True
    notifier.send_notification.side_effect = send
    first = prod.bima_cycle(state, [announcement()], [], notifier, checkpoint, enabled=True, at=NOW)
    assert first["delivered_events"] == 1
    restart = json.loads(json.dumps(state))
    prod.bima_cycle(restart, [announcement()], [], notifier, lambda: None, enabled=True, at=NOW)
    assert notifier.send_notification.call_count == 1


@pytest.mark.parametrize("fail_status", ["reserved", "sending"])
def test_bima_remote_checkpoint_failure_prevents_post(fail_status):
    state, notifier = bima_state(), fake_notifier()
    def checkpoint():
        if any(row["status"] == fail_status for row in state["outbox"].values()):
            raise RuntimeError("CAS conflict")
    with pytest.raises(RuntimeError, match="CAS conflict"):
        prod.bima_cycle(state, [announcement()], [], notifier, checkpoint, enabled=True, at=NOW)
    notifier.send_notification.assert_not_called()


def test_bima_unconfirmed_result_is_ambiguous_and_not_retried():
    state, notifier = bima_state(), fake_notifier(False)
    with pytest.raises(RuntimeError, match="ambiguous"):
        prod.bima_cycle(state, [announcement()], [], notifier, lambda: None, enabled=True, at=NOW)
    assert next(iter(state["outbox"].values()))["status"] == "ambiguous"
    prod.bima_cycle(state, [announcement()], [], notifier, lambda: None, enabled=True, at=NOW)
    assert notifier.send_notification.call_count == 1


def test_bima_kill_switch_stops_send():
    state, notifier = bima_state(), fake_notifier()
    prod.bima_cycle(state, [announcement()], [], notifier, lambda: None, enabled=False, at=NOW)
    notifier.send_notification.assert_not_called()
    assert state["outbox"] == {}


def test_bima_pdf_upload_retains_metadata_name_and_confirmed_receipts(tmp_path, monkeypatch):
    import requests
    config = {"notification_method": "telegram", "telegram_bot_token": "prod-test",
        "telegram_chat_id": "-1001234567", "telegram_send_files": True}
    notifier = Notifier(config)
    item = announcement()
    item["documents"] = [{"name": "Lampiran.pdf", "download_url": "https://example.invalid/file.pdf"}]
    pdf = tmp_path / "temp-random.pdf"
    pdf.write_bytes(b"%PDF-test")
    monkeypatch.setattr(notifier, "_download_file", lambda *args: str(pdf))
    responses = [Mock(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 11}}),
                 Mock(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 12}})]
    post = Mock(side_effect=responses)
    monkeypatch.setattr(requests, "post", post)
    state = bima_state()
    report = prod.bima_cycle(state, [item], [], notifier, lambda: None, enabled=True, at=NOW)
    assert report["confirmed_messages"] == 2 and report["delivered_events"] == 1
    assert post.call_args_list[1].kwargs["files"]["document"][0] == "Lampiran.pdf"
    assert [r["message_id"] for r in next(iter(state["outbox"].values()))["receipts"]] == [11, 12]


def yt_setup(tmp_path):
    store = YouTubeStateStore(tmp_path / "state.json", activation_at=FENCE, policy_version="tabs-only-v1")
    checkpoints = []
    store.set_commit_hook(lambda path: checkpoints.append(json.loads(path.read_text())))
    decision = e.Decision(VID, f"youtube:video:{VID}:initial", e.Knowledge.UNKNOWN,
        e.Eligibility.ELIGIBLE, e.DeliveryState.NONE, ("verified-fixture",), requires_atomic_reservation=True)
    item = {"source": "youtube", "kind": "video", "video_id": VID, "title": "Mock regular video",
        "url": "https://www.youtube.com/watch?v=" + VID, "delivery_format": "youtube-tabs-preview-v1"}
    result = SimpleNamespace(errors=(), health={tab: {"health": "HEALTHY", "detected_count": 30}
        for tab in ("videos", "streams")}, decisions=(decision,), preview_items=(item,))
    adapter = Mock(evaluate=Mock(return_value=result))
    notifier = Mock(last_delivery_receipts=[])
    notifier._build_summary_chunks.return_value = ["summary"]
    return store, adapter, notifier, checkpoints


@pytest.mark.parametrize("startup,enabled", [(True, True), (False, False)])
def test_youtube_startup_and_kill_switch_never_send(tmp_path, startup, enabled):
    store, adapter, notifier, _ = yt_setup(tmp_path)
    prod.youtube_cycle(store, adapter, [], {}, notifier, enabled=enabled, startup=startup)
    notifier.send_youtube_production_notification.assert_not_called()
    assert store._state["outbox"] == {}


def test_youtube_production_durable_claim_receipt_restart_and_duplicate(tmp_path):
    store, adapter, notifier, checkpoints = yt_setup(tmp_path)
    def send(item, ticket):
        assert checkpoints[-1]["outbox"][ticket.event_key]["status"] == "sending"
        notifier.last_delivery_receipts = [{"http_status": 200, "ok": True, "message_id": 99}]
        return True
    notifier.send_youtube_production_notification.side_effect = send
    result = prod.youtube_cycle(store, adapter, [], {}, notifier, enabled=True, startup=False)
    assert result["confirmed_messages"] == 1
    restored = YouTubeStateStore(store.path, activation_at=FENCE, policy_version="tabs-only-v1")
    restored.set_commit_hook(lambda path: None)
    prod.youtube_cycle(restored, adapter, [], {}, notifier, enabled=True, startup=False)
    assert notifier.send_youtube_production_notification.call_count == 1


def test_youtube_ambiguous_result_never_retried(tmp_path):
    store, adapter, notifier, _ = yt_setup(tmp_path)
    notifier.send_youtube_production_notification.return_value = False
    with pytest.raises(RuntimeError, match="ambiguous"):
        prod.youtube_cycle(store, adapter, [], {}, notifier, enabled=True, startup=False)
    prod.youtube_cycle(store, adapter, [], {}, notifier, enabled=True, startup=False)
    assert notifier.send_youtube_production_notification.call_count == 1
    assert next(iter(store._state["outbox"].values()))["status"] == "ambiguous"


@pytest.mark.parametrize("configured_target", ["-1001234567", "@dikti_news"])
def test_production_preflight_requires_official_channel_and_ignores_experiment_credentials(configured_target):
    target = "-1001234567"
    env = {"TELEGRAM_BOT_TOKEN": "prod-only", "TELEGRAM_CHAT_ID": configured_target,
        "PRODUCTION_TELEGRAM_CHAT_ID_SHA256": hashlib.sha256(target.encode()).hexdigest(),
        "YOUTUBE_EXPERIMENT_BOT_TOKEN": "must-not-use"}
    def response(body):
        return Mock(status_code=200, json=lambda: {"ok": True, "result": body})
    replies = [response({"id": 7, "is_bot": True}),
        response({"id": int(target), "username": "dikti_news", "type": "channel"}),
        response({"id": int(target), "type": "channel"}),
        response({"user": {"id": 7}, "status": "administrator", "can_post_messages": True})]
    get = Mock(side_effect=replies)
    assert verify_production(env, get=get)["telegram_bot_token"] == "prod-only"
    assert all("must-not-use" not in call.args[0] for call in get.call_args_list)
    env["TELEGRAM_BOT_TOKEN"] = ""
    with pytest.raises(PreflightError):
        verify_production(env, get=Mock(side_effect=AssertionError("no API without production token")))


@pytest.mark.parametrize("bad_identity,bad_permission", [(True, False), (False, True)])
def test_production_preflight_blocks_wrong_identity_or_missing_post_rights(bad_identity, bad_permission):
    target = "-1001234567"
    env = {"TELEGRAM_BOT_TOKEN": "prod-only", "TELEGRAM_CHAT_ID": target,
        "PRODUCTION_TELEGRAM_CHAT_ID_SHA256": hashlib.sha256(target.encode()).hexdigest()}
    bodies = [{"id": 7, "is_bot": True},
        {"id": int(target), "username": "wrong" if bad_identity else "dikti_news", "type": "channel"},
        {"id": int(target), "type": "channel"},
        {"user": {"id": 7}, "status": "administrator", "can_post_messages": not bad_permission}]
    get = Mock(side_effect=[Mock(status_code=200, json=lambda body=body: {"ok": True, "result": body}) for body in bodies])
    with pytest.raises(PreflightError):
        verify_production(env, get=get)


def test_production_notifier_never_uses_experiment_token(tmp_path, monkeypatch):
    store, adapter, _, _ = yt_setup(tmp_path)
    decision = adapter.evaluate.return_value.decisions[0]
    item = adapter.evaluate.return_value.preview_items[0]
    store.reserve(decision, item, now=NOW)
    target = "-1001234567"
    config = {"notification_method": "telegram", "telegram_bot_token": "PROD_TOKEN",
        "telegram_chat_id": target, "production_chat_id_sha256": hashlib.sha256(target.encode()).hexdigest(),
        "youtube_delivery_enabled": True, "youtube_delivery_send_enabled": True,
        "youtube_production_send_enabled": True, "youtube_experiment_bot_token": "EXPERIMENT_TOKEN"}
    notifier = Notifier(config)
    import requests
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 42}}))
    monkeypatch.setattr(requests, "post", post)
    def send(payload, ticket):
        assert notifier.send_youtube_production_notification(payload, ticket) is True
        return prod.SendConfirmation(200, True, notifier.last_delivery_receipts[0]["message_id"])
    assert store.send_once(decision.event_key, send, enabled=True, now=NOW) == "delivered"
    assert "PROD_TOKEN" in post.call_args.args[0] and "EXPERIMENT_TOKEN" not in post.call_args.args[0]
    assert post.call_args.kwargs["json"]["chat_id"] == target
