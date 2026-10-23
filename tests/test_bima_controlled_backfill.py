from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from unittest.mock import Mock

import pytest
import requests

import bima_controlled_backfill as backfill
import production_runtime as prod
from notifier import Notifier
from monitor_runner import stable_item
from scraper import announcement_identity
from youtube_experiment_preflight import PreflightError


AT = datetime(2026, 10, 23, 4, tzinfo=timezone.utc)


@pytest.fixture
def case(tmp_path, monkeypatch):
    data = b"%PDF-offline-test-only"
    path = tmp_path / "validated-main.pdf"
    path.write_bytes(data)
    monkeypatch.setattr(backfill, "PDF_BYTES", len(data))
    monkeypatch.setattr(backfill, "PDF_SHA256", hashlib.sha256(data).hexdigest())
    item = {"title": backfill.TITLE, "surat": backfill.LETTER, "date": backfill.DATE,
        "url": backfill.SOURCE, "documents": [{"name": backfill.PDF_NAME,
            "download_url": "https://storage.googleapis.com/offline-test/main.pdf"}]}
    state = {"schema_version": 1, "source": "bima", "activation_at": "2026-10-23T03:05:28+00:00",
        "initialized": True, "inventory": [stable_item(item)], "first_seen": {}, "outbox": {}}
    notifier = Notifier({"notification_method": "telegram", "telegram_bot_token": "TEST_ONLY_PROD_TOKEN",
        "telegram_chat_id": "-1001234567", "telegram_send_files": True})
    download = Mock(return_value=str(path))
    monkeypatch.setattr(notifier, "_download_file", download)
    checkpoints = []
    def checkpoint():
        checkpoints.append(deepcopy(state))
    post = Mock()
    def send(url, **kwargs):
        record = next(iter(checkpoints[-1]["outbox"].values()))
        assert record["status"] == "sending" and record["attempts"] == 1
        assert record["controlled_backfill"]["pdf_sha256"] == hashlib.sha256(data).hexdigest()
        if url.endswith("sendDocument"):
            filename, file, mime = kwargs["files"]["document"]
            assert filename == backfill.PDF_NAME and mime == "application/pdf"
            assert file.read() == data
            message_id = 702
        else:
            assert url.endswith("sendMessage")
            assert backfill.TITLE in kwargs["json"]["text"]
            message_id = 701
        return Mock(status_code=200, json=lambda: {"ok": True, "result": {"message_id": message_id}})
    post.side_effect = send
    monkeypatch.setattr(requests, "post", post)
    return state, item, notifier, checkpoint, checkpoints, post, download, path


def perform(case, journal=None):
    state, item, notifier, checkpoint, *_ = case
    return backfill.perform(state, [item], [], journal or {"batches": []}, notifier, checkpoint, at=AT)


def test_exact_exception_uses_production_claim_pdf_receipts_and_skips_reexecution(case):
    state, item, notifier, checkpoint, snapshots, post, download, path = case
    before = deepcopy(state)
    report = perform(case)
    assert report["backfill"] == "SENT" and report["message_ids"] == [701, 702]
    assert report["pdf"] == "DELIVERED" and post.call_count == 2
    assert snapshots[0]["outbox"][next(iter(state["outbox"]))]["status"] == "reserved"
    record = next(iter(state["outbox"].values()))
    assert record["status"] == "delivered" and record["attempts"] == 1
    assert [r["method"] for r in record["receipts"]] == ["sendMessage", "sendDocument"]
    for key in ("activation_at", "inventory", "first_seen"):
        assert state[key] == before[key]
    assert perform(case)["backfill"] == "SKIPPED"
    assert post.call_count == 2 and download.call_count == 1


@pytest.mark.parametrize("field,value", [("title", "Another announcement"), ("surat", "1455/DST/C3/DT.06.01/2026"),
    ("date", "23 Oktober 2026"), ("url", "https://example.invalid")])
def test_other_identities_are_never_authorized(case, field, value):
    state, item, _, _, _, post, *_ = case
    item[field] = value
    with pytest.raises(PreflightError, match="EXACT_SOURCE_IDENTITY_NOT_UNIQUE"):
        perform(case)
    assert state["outbox"] == {} and post.call_count == 0


@pytest.mark.parametrize("status", ["reserved", "sending", "ambiguous"])
def test_existing_uncertain_outbox_blocks_before_download_or_post(case, status):
    state, item, _, _, _, post, download, _ = case
    key = "bima:" + announcement_identity(item) + ":initial"
    state["outbox"][key] = {"event_key": key, "items": [stable_item(item)], "status": status,
        "attempts": 0 if status == "reserved" else 1, "receipts": []}
    before = deepcopy(state)
    with pytest.raises(PreflightError, match="EXISTING_OUTBOX"):
        perform(case)
    assert state == before and post.call_count == 0 and download.call_count == 0


@pytest.mark.parametrize("delivered", [True, False])
def test_historical_delivery_or_pending_batch_prevents_duplicate(case, delivered):
    _, item, _, _, _, post, download, _ = case
    journal = {"batches": [{"items": [stable_item(item)], "delivered": delivered}]}
    if delivered:
        assert perform(case, journal)["backfill"] == "SKIPPED"
    else:
        with pytest.raises(PreflightError, match="HISTORICAL_PENDING"):
            perform(case, journal)
    assert post.call_count == 0 and download.call_count == 0


def test_changed_pdf_blocks_before_reservation(case):
    state, _, _, _, _, post, _, path = case
    path.write_bytes(b"%PDF-different")
    with pytest.raises(PreflightError, match="PDF_CONTENT_CHANGED"):
        perform(case)
    assert state["outbox"] == {} and post.call_count == 0


def test_additional_document_blocks_before_reservation(case):
    state, item, _, _, _, post, *_ = case
    item["documents"].append({"name": "other.pdf"})
    with pytest.raises(PreflightError, match="EXPECTED_MAIN_PDF_NOT_UNIQUE"):
        perform(case)
    assert state["outbox"] == {} and post.call_count == 0


def test_cas_failure_stops_before_telegram(case):
    state, item, notifier, _, _, post, *_ = case
    def fail():
        raise RuntimeError("remote CAS conflict")
    with pytest.raises(RuntimeError, match="CAS conflict"):
        backfill.perform(state, [item], [], {"batches": []}, notifier, fail, at=AT)
    assert post.call_count == 0


def test_partial_send_becomes_ambiguous_and_cannot_retry(case):
    state, item, notifier, checkpoint, _, post, *_ = case
    post.side_effect = [Mock(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 701}}),
                        Mock(status_code=503, json=lambda: {"ok": False})]
    with pytest.raises(RuntimeError, match="ambiguous"):
        perform(case)
    record = next(iter(state["outbox"].values()))
    assert record["status"] == "ambiguous" and record["sent_chunks"] == [1]
    assert [r["message_id"] for r in record["receipts"]] == [701]
    with pytest.raises(PreflightError, match="EXISTING_OUTBOX"):
        perform(case)
    assert post.call_count == 2


def test_scheduled_and_youtube_paths_cannot_use_exception(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    preflight = Mock(side_effect=AssertionError("must stop before API"))
    monkeypatch.setattr(prod, "verify_production", preflight)
    with pytest.raises(ValueError, match="manual and BIMA-only"):
        prod.run("bima", "backfill-1454")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    with pytest.raises(ValueError, match="manual and BIMA-only"):
        prod.run("youtube", "backfill-1454")
    preflight.assert_not_called()


def test_normal_production_cycle_does_not_backfill_target(case):
    state, item, notifier, checkpoint, _, post, *_ = case
    report = prod.bima_cycle(state, [item], [], notifier, checkpoint, enabled=True, at=AT)
    assert report["confirmed_messages"] == 0 and state["outbox"] == {}
    assert post.call_count == 0
