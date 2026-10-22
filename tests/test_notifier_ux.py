"""Offline presentation checks, including serialized multipart and durable retries."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
import tempfile
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfoNotFoundError

import pytest
import requests

from monitor_runner import run_once
from notifier import _format_rss_publication
from reliability import read_json


@pytest.mark.parametrize("name,expected,mime", [
    ("Panduan.pdf", "Panduan.pdf", "application/pdf"),
    ("Proposal dan RAB 2026.pdf", "Proposal dan RAB 2026.pdf", "application/pdf"),
    ("Panduan café 日本語.pdf", "Panduan café 日本語.pdf", "application/pdf"),
    ("PANDUAN.PDF", "PANDUAN.PDF", "application/pdf"),
    ("Proposal.docx", "Proposal.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("catatan.unknownext", "catatan.unknownext", "application/octet-stream"),
    ("../../Panduan.pdf", "Panduan.pdf", "application/pdf"),
    (r"..\..\Panduan.pdf", "Panduan.pdf", "application/pdf"),
    (r"C:\private\Proposal dan RAB.pdf", "Proposal dan RAB.pdf", "application/pdf"),
    ("/private/Panduan.pdf", "Panduan.pdf", "application/pdf"),
    ("Panduan\r\n\x00\x7f\x85.pdf", "Panduan.pdf", "application/pdf"),
    ("Panduan\u202e.pdf", "Panduan.pdf", "application/pdf"),
    ('Panduan<>:"|?*.pdf', "Panduan_______.pdf", "application/pdf"),
    ("", "dokumen", "application/octet-stream"),
    (" . .. ", "dokumen", "application/octet-stream"),
    ("../", "dokumen", "application/octet-stream"),
    ("\x00\u202e", "dokumen", "application/octet-stream"),
    (None, "dokumen", "application/octet-stream"),
])
def test_serialized_multipart_uses_safe_metadata_filename(
        tmp_path, notifier, telegram, name, expected, mime):
    local = tmp_path / "tmpRANDOM-prefix-local.pdf"
    local.write_bytes(b"%PDF-1.4\nfixture bytes")
    payloads = []

    def post(url, **kwargs):
        upload_name, stream, content_type = kwargs["files"]["document"]
        assert upload_name == expected and content_type == mime
        assert not stream.closed
        request = requests.Request("POST", "https://example.invalid/upload",
                                   data=kwargs["data"], files=kwargs["files"]).prepare()
        payloads.append(request.body)
        return Mock(status_code=200, json=lambda: {"ok": True})

    telegram.side_effect = post
    assert notifier._send_document("TEST_ONLY_TOKEN", "TEST_ONLY_CHAT", str(local), name)
    assert telegram.call_count == 1
    body = payloads[0]
    assert b'filename="' + expected.encode("utf-8") + b'"\r\n' in body
    assert f"Content-Type: {mime}\r\n".encode() in body
    assert local.read_bytes() in body
    assert local.name.encode() not in body
    assert not any(char in expected for char in ("/", "\\", "\r", "\n", "\x00"))
    assert local.exists(), "Cleanup remains the caller's responsibility"
    assert telegram.call_args.kwargs["files"]["document"][1].closed


def test_download_still_uses_tempfile_and_upload_cleanup(
        tmp_path, notifier, telegram, monkeypatch, announcement):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    pdf = b"%PDF-1.4\nlocal fixture"
    response = Mock(status_code=200, headers={"content-length": str(len(pdf))})
    response.iter_content.return_value = [pdf]
    get = Mock(return_value=response)
    monkeypatch.setattr(requests, "get", get)
    announcement["documents"] = [{"name": "Proposal dan RAB.pdf",
                                  "download_url": "https://example.invalid/pdf"}]
    paths = []

    def post(url, **kwargs):
        filename, stream, mime = kwargs["files"]["document"]
        paths.append(Path(stream.name))
        assert paths[-1].parent == tmp_path
        assert paths[-1].name != filename
        assert filename == "Proposal dan RAB.pdf" and mime == "application/pdf"
        assert stream.read() == pdf
        return Mock(status_code=200, json=lambda: {"ok": True})

    telegram.side_effect = post
    progress = {"sent_documents": []}
    checkpoints = []
    assert notifier._send_telegram_documents("TEST_ONLY_TOKEN", "TEST_ONLY_CHAT", [announcement],
                                             progress, lambda: checkpoints.append(deepcopy(progress)))
    assert progress["sent_documents"] == ["0:0"]
    assert checkpoints == [{"sent_documents": ["0:0"]}]
    assert len(paths) == 1 and not paths[0].exists()
    get.assert_called_once()


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-10-20T10:24:20+00:00", "20 Oktober 2026, 17:24 WIB"),
    ("2026-10-20T10:24:20Z", "20 Oktober 2026, 17:24 WIB"),
    ("2026-10-20T10:24:20.123456+00:00", "20 Oktober 2026, 17:24 WIB"),
    ("2026-10-20T10:24:20-05:00", "20 Oktober 2026, 22:24 WIB"),
    ("2026-10-20T10:24:20+09:00", "20 Oktober 2026, 08:24 WIB"),
    ("2026-10-20T10:24:20+07:00", "20 Oktober 2026, 10:24 WIB"),
    ("2026-12-31T20:24:20+00:00", "1 Januari 2027, 03:24 WIB"),
    ("2026-03-01T00:30:00+09:00", "28 Februari 2026, 22:30 WIB"),
])
def test_aware_rss_publication_is_displayed_in_jakarta(
        notifier, rss_video, capsys, timestamp, expected):
    rss_video["published"] = timestamp
    before = deepcopy(rss_video)
    assert expected in notifier._format_html_item(1, rss_video)
    notifier._send_console([rss_video])
    assert expected in capsys.readouterr().out
    assert rss_video == before


@pytest.mark.parametrize("month,name", enumerate([
    "Januari", "Februari", "Maret", "April", "Mei", "Juni",
    "Juli", "Agustus", "September", "Oktober", "November", "Desember",
], 1))
def test_indonesian_month_names_are_locale_independent(month, name):
    assert _format_rss_publication(f"2026-{month:02d}-02T03:04:00+07:00") == f"2 {name} 2026, 03:04 WIB"


@pytest.mark.parametrize("timestamp", [
    "invalid", "2026-10-20T10:24:20", "2026-10-20", "2026-02-30T10:00:00Z",
    "2026-10-20T10:24:20+25:00", "2026-13-20T10:00:00Z", "4 jam lalu",
    '<invalid & "date">', "", None, 42,
])
def test_invalid_or_naive_publication_never_invents_timezone(notifier, rss_video, timestamp):
    rss_video["published"] = timestamp
    before = deepcopy(rss_video)
    text = notifier._format_html_item(1, rss_video)
    rendered = "".join(ET.fromstring("<root>" + text + "</root>").itertext())
    if timestamp:
        assert str(timestamp) in rendered
    else:
        assert "Publikasi (RSS)" not in rendered
    assert "WIB" not in text
    assert rss_video == before


def test_missing_timezone_database_keeps_original_timestamp(monkeypatch):
    def missing(key):
        assert key == "Asia/Jakarta"
        raise ZoneInfoNotFoundError(key)
    monkeypatch.setattr("notifier.ZoneInfo", missing)
    raw = "2026-10-20T10:24:20+00:00"
    assert _format_rss_publication(raw) == raw


def test_bima_date_stream_metadata_and_escaping_are_untouched(notifier, announcement, rss_video):
    announcement.update(date="13 Oktober 2026", title='BIMA <panduan> & "RAB"')
    rss_video.update(title="Video <contoh> & ringkasan", description="Isi <aman> & jelas",
                     published="2026-10-20T10:24:20+00:00", source_tabs=["streams"],
                     metadata_by_tab={"streams": {"relative_text": ["4 jam lalu"],
                                                 "badges": ["LIVE"], "schedule_timezone": None}})
    before = deepcopy([announcement, rss_video])
    bima = notifier._format_html_item(1, announcement)
    youtube = notifier._format_html_item(2, rss_video)
    assert "13 Oktober 2026" in bima and "WIB" not in bima
    assert "&lt;panduan&gt; &amp; &quot;RAB&quot;" in bima
    assert "&lt;contoh&gt; &amp; ringkasan" in youtube
    assert "&lt;aman&gt; &amp; jelas" in youtube
    assert "4 jam lalu" not in youtube and "LIVE" not in youtube
    for text in (bima, youtube):
        ET.fromstring("<root>" + text + "</root>")
    assert [announcement, rss_video] == before
    # A raw shadow observation has no RSS publication field to convert.
    observation = deepcopy(rss_video)
    observation.pop("published")
    snapshot = deepcopy(observation)
    assert "Publikasi (RSS)" not in notifier._format_html_item(1, observation)
    assert observation == snapshot


@pytest.mark.parametrize("category", ["bima", "youtube", "mixed"])
def test_footer_changes_without_changing_category_or_html_transport(
        notifier, announcement, rss_video, telegram, category):
    items = {"bima": [announcement], "youtube": [rss_video], "mixed": [announcement, rss_video]}[category]
    header = {"bima": "📢 PENGUMUMAN BARU BIMA KEMDIKTISAINTEK",
              "youtube": "▶️ VIDEO BARU — YOUTUBE KEMDIKTISAINTEK",
              "mixed": "📢 UPDATE BARU BIMA + YOUTUBE KEMDIKTISAINTEK"}[category]
    assert notifier.send_notification(items)
    payload = telegram.call_args.kwargs["json"]
    text = payload["text"]
    assert payload["parse_mode"] == "HTML"
    assert text.startswith(f"<b>{header}</b>\n\n")
    assert text.endswith("<i>Bot Dikti News</i>")
    assert "Bot Notifikasi BIMA" not in text
    assert "**" not in text and "&#x20;" not in text and r"\n" not in text
    assert "\n" in text
    ET.fromstring("<root>" + text + "</root>")


def test_metadata_filenames_keep_partial_retry_checkpoints_and_dedup(
        tmp_path, config, monitors, notifier, telegram, monkeypatch, announcement):
    config["telegram_send_files"] = True
    announcement["documents"] = [
        {"name": "../Proposal dan RAB.pdf", "download_url": "https://example.invalid/one"},
        {"name": "Lampiran 日本語.pdf", "download_url": "https://example.invalid/two"},
    ]
    paths, calls = [], []
    failures = [False]

    def download(*args):
        fd, path = tempfile.mkstemp(dir=tmp_path, prefix="random-local-", suffix=".pdf")
        import os
        os.close(fd)
        Path(path).write_bytes(b"%PDF-1.4\nfixture")
        paths.append(Path(path))
        return path

    def post(url, **kwargs):
        batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
        assert not batch["delivered"] and batch["attempts"] in (1, 2)
        if "files" not in kwargs:
            calls.append("summary")
            assert batch["sent_chunks"] == []
        else:
            name, _, mime = kwargs["files"]["document"]
            assert mime == "application/pdf" and batch["sent_chunks"] == [1]
            calls.append(name)
            if name == "Lampiran 日本語.pdf" and not failures[0]:
                assert batch["sent_documents"] == ["0:0"]
                failures[0] = True
                return Mock(status_code=503, json=lambda: {"ok": False})
        return Mock(status_code=200, json=lambda: {"ok": True})

    monkeypatch.setattr(notifier, "_download_file", download)
    telegram.side_effect = post
    kwargs = dict(state_dir=tmp_path, scraper=monitors[0], notifier=notifier)
    first = run_once(config, **kwargs)
    assert first.exit_code == 1 and first.pending == 1
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    saved_chunks = batch["chunks"][:]
    assert batch["sent_documents"] == ["0:0"] and not batch["delivered"]
    assert all(not path.exists() for path in paths)
    monkeypatch.setattr(notifier, "_build_summary_chunks", Mock(side_effect=AssertionError("saved payload must be reused")))
    second = run_once(config, **kwargs)
    assert second.exit_code == 0 and second.pending == 0
    assert calls == ["summary", "Proposal dan RAB.pdf", "Lampiran 日本語.pdf", "Lampiran 日本語.pdf"]
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert batch["delivered"] and batch["attempts"] == 2
    assert batch["chunks"] == saved_chunks and batch["sent_documents"] == ["0:0", "0:1"]
    assert all(not path.exists() for path in paths)
    spy = Mock(wraps=notifier.send_notification)
    monkeypatch.setattr(notifier, "send_notification", spy)
    telegram.reset_mock()
    third = run_once(config, **kwargs)
    assert third.exit_code == 0 and third.pending == 0
    spy.assert_not_called()
    telegram.assert_not_called()
