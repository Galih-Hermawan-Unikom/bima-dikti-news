from copy import deepcopy
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

from monitor_runner import run_once
from reliability import read_json


@pytest.mark.parametrize("failed_index", [1, 2])
@pytest.mark.parametrize("status,body", [(503, {"ok": True}), (200, {"ok": False})])
def test_summary_stops_at_failed_chunk_and_retry_preserves_order(
        tmp_path, config, monitors, notifier, telegram, monkeypatch, failed_index, status, body):
    chunks = ["chunk one", "chunk two", "chunk three"]
    monkeypatch.setattr(notifier, "_build_summary_chunks", lambda items: chunks[:])
    ok = Mock(status_code=200, json=lambda: {"ok": True})
    failed = Mock(status_code=status, json=lambda: body)
    telegram.side_effect = [ok] * (failed_index - 1) + [failed, ok, ok]
    run_kwargs = dict(state_dir=tmp_path, scraper=monitors[0], youtube=monitors[1], notifier=notifier)

    result = run_once(config, **run_kwargs)
    assert result.exit_code == 1
    assert [call.kwargs["json"]["text"] for call in telegram.call_args_list] == chunks[:failed_index]
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert batch["sent_chunks"] == list(range(1, failed_index))
    assert not batch["delivered"]

    # Retry must use the saved chunks, not rebuild a potentially different message.
    monkeypatch.setattr(notifier, "_build_summary_chunks", Mock(side_effect=AssertionError("must not rebuild")))
    telegram.reset_mock()
    telegram.side_effect = None
    telegram.return_value = ok
    result = run_once(config, **run_kwargs)
    assert result.exit_code == 0
    assert [call.kwargs["json"]["text"] for call in telegram.call_args_list] == chunks[failed_index - 1:]
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert batch["sent_chunks"] == [1, 2, 3]
    assert batch["delivered"]


def rendered_chunks(chunks):
    rendered = []
    assert chunks
    for chunk in chunks:
        # Bound the actual serialized payload, not just its rendered Python length.
        assert 0 < len(chunk.encode("utf-16-le")) // 2 <= 4000
        tree = ET.fromstring("<root>" + chunk + "</root>")
        assert all(element.tag in {"root", "b", "i"} and not element.attrib for element in tree.iter())
        visible = "".join(tree.itertext())
        assert visible.strip(), "Do not emit a whitespace-only Telegram message"
        assert len(visible.encode("utf-16-le")) // 2 <= 4096
        rendered.append(visible)
    return "".join(rendered)


def test_one_item_many_attachments_stays_readable_and_complete(notifier, announcement):
    announcement["documents"] = [{"name": f"Lampiran {i:03d} <panduan> & ringkasan.pdf"} for i in range(180)]
    chunks = notifier._build_summary_chunks([announcement])
    assert len(chunks) > 1
    visible = rendered_chunks(chunks)
    body = "".join(ET.fromstring("<root>" + notifier._format_html_item(1, announcement) + "</root>").itertext())
    assert body in visible
    assert visible.count(announcement["title"]) == 1
    for document in announcement["documents"]:
        assert visible.count(document["name"]) == 1
        assert any(document["name"] in "".join(ET.fromstring("<root>" + chunk + "</root>").itertext())
                   for chunk in chunks), "Keep ordinary attachment names on one message"
    assert "Dokumen: 180 file" in visible
    assert visible.endswith("Bot Notifikasi BIMA")


@pytest.mark.parametrize("field", ["title", "document", "surat", "url", "channel_title"])
def test_single_oversized_field_keeps_html_entities_and_emoji_intact(notifier, announcement, field):
    long_text = ("A<&\"'" + chr(0x1F4CE)) * 1300
    item = deepcopy(announcement)
    if field == "document":
        item["documents"] = [{"name": long_text}]
    elif field == "channel_title":
        item.update(source="youtube", channel_title=long_text, status="LIVE")
    else:
        item[field] = long_text
    chunks = notifier._build_summary_chunks([item])
    assert len(chunks) > 1
    visible = rendered_chunks(chunks)
    body = "".join(ET.fromstring("<root>" + notifier._format_html_item(1, item) + "</root>").itertext())
    assert body in visible
    assert long_text in visible


def test_long_title_prefers_word_boundaries(notifier, announcement):
    announcement["title"] = " ".join(f"Pembahasan-{i:04d}" for i in range(1000))
    chunks = notifier._build_summary_chunks([announcement])
    visible = rendered_chunks(chunks)
    assert announcement["title"] in visible
    for chunk in chunks:
        part = "".join(ET.fromstring("<root>" + chunk + "</root>").itertext())
        for word in part.split():
            if word.startswith("Pembahasan-"):
                assert len(word) == len("Pembahasan-0000")


def test_short_message_keeps_existing_html_format(notifier, announcement):
    chunks = notifier._build_summary_chunks([announcement])
    assert len(chunks) == 1
    assert notifier._format_html_item(1, announcement) in chunks[0]
    assert chunks[0].startswith("<b>")
    assert chunks[0].endswith("<i>Bot Notifikasi BIMA</i>")
    rendered_chunks(chunks)


def test_large_mixed_summary_payloads_sent_under_limit(notifier, announcement, telegram):
    video = {"source": "youtube", "title": "Video <baru>", "channel_title": "Kemdiktisaintek",
             "status": "LIVE", "url": "https://example.invalid/video", "description": "Rangkuman & panduan"}
    announcement["documents"] = [{"name": f"Dokumen {i:03d}.pdf"} for i in range(250)]
    items = [announcement, video]
    chunks = notifier._build_summary_chunks(items)
    assert len(chunks) > 1
    visible = rendered_chunks(chunks)
    assert "UPDATE BARU BIMA + YOUTUBE" in visible
    assert video["title"] in visible
    assert notifier.send_notification(items)
    sent = [call.kwargs["json"]["text"] for call in telegram.call_args_list]
    rendered_chunks(sent)
    assert all(call.kwargs["json"]["parse_mode"] == "HTML" for call in telegram.call_args_list)


@pytest.mark.parametrize("length", [3999, 4000, 4001, 4096, 4097])
def test_summary_length_boundaries(notifier, announcement, length):
    original = notifier._build_summary_chunks([announcement])[0]
    overhead = len(original.encode("utf-16-le")) // 2 - len(announcement["title"])
    announcement["title"] = "X" * (length - overhead)
    chunks = notifier._build_summary_chunks([announcement])
    assert (len(chunks) == 1) == (length <= 4000)
    assert announcement["title"] in rendered_chunks(chunks)


@pytest.mark.parametrize("invalid_chunk", ["", "X" * 4001])
def test_previously_saved_invalid_chunk_is_not_sent_or_reformatted(notifier, announcement, telegram, invalid_chunk):
    progress = {"chunks": [invalid_chunk, "later chunk"], "sent_chunks": []}
    assert not notifier._send_summary_message("TEST_ONLY_TOKEN", "TEST_ONLY_CHAT", [announcement], progress)
    telegram.assert_not_called()
    assert progress == {"chunks": [invalid_chunk, "later chunk"], "sent_chunks": []}
