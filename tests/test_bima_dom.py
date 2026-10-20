"""Offline DOM/identity regressions, independent of operational cache growth."""

import json
import os
from copy import deepcopy
from html import escape
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from migrate_cache import migrate
from monitor_runner import run_once
from reliability import SourceError, atomic_json
from scraper import (BimaIdentityError, BimaScraper, _publication_date,
                     announcement_identity, canonical_surat, get_browser_path,
                     match_announcement, new_announcements)
from youtube_monitor import YouTubeMonitor


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("BIMA_TEST_BROWSER") or get_browser_path())
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    page = browser.new_page()
    page.route("**/*", lambda route: route.abort())
    yield page
    page.close()


def cards(page, fixtures, name="bima_ap_card.html"):
    page.set_content((fixtures / name).read_text(encoding="utf-8"))
    return BimaScraper()._collect_cards(page)


def test_new_dom_all_attributes_and_document_indices(page, fixtures):
    result = cards(page, fixtures)
    assert len(result) == 3
    for i, (prefix, names) in enumerate([
        ("Undangan Penandatanganan Kontrak", ["Undangan Penandatanganan Kontrak Program Aksara Mahasiswa Tahun Anggaran 2026.pdf"]),
        ("Pengumuman Penerima Pendanaan", ["Pengumuman Penerima Pendanaan Program Aksara Mahasiswa Tahun Anggaran 2026.pdf"]),
        ('Pengumuman Riset & Inovasi "Terapan" \u2013 Uji Aman', ["Riset & Inovasi.pdf", 'Lampiran "A" \u2013 Data.docx'])]):
        item = result[i]
        assert item["title"] == (prefix + " Program Aksara Mahasiswa Tahun Anggaran 2026" if i < 2 else prefix)
        assert item["card_index"] == i and item["layout"] == "ap-card"
        assert item["surat"] == ("1386/DST/C3/DT.06.01/2026" if i < 2 else "999/TEST/C3/2026")
        assert item["date"] == ("07 Oktober 2026" if i < 2 else "16 Oktober 2026")
        assert item["documents"] == [{"doc_index": j, "name": name} for j, name in enumerate(names)]


def test_old_dom_uses_same_parser(page, fixtures):
    item, = cards(page, fixtures, "bima.html")
    assert (item["title"], item["surat"], item["date"], item["layout"]) == (
        "Pengumuman Pendanaan Penelitian Tahun 2026", "256/DST/C2/PP.01.07/2026", "16 April 2026", "legacy")
    assert item["documents"] == [{"doc_index": 0, "name": "Panduan.pdf"}]


@pytest.mark.parametrize("layout", ["ap-card", "legacy"])
def test_fetch_path_preserves_metadata_and_document_button_mapping(page, fixtures, monkeypatch, layout):
    expected = cards(page, fixtures, "bima_ap_card.html" if layout == "ap-card" else "bima.html")
    scraper = BimaScraper()
    browser = Mock()
    browser.new_context.return_value.new_page.return_value = page
    context_manager = Mock()
    context_manager.__enter__ = Mock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=Mock(return_value=browser))))
    context_manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("scraper.sync_playwright", lambda: context_manager)
    monkeypatch.setattr(page, "goto", Mock(return_value=SimpleNamespace(status=200)))
    monkeypatch.setattr(page, "wait_for_timeout", Mock())
    visited = []
    def capture(page, link, name):
        assert name in " ".join(link.inner_text().split())
        visited.append(name)
        return "https://example.invalid/synthetic.pdf"
    monkeypatch.setattr(scraper, "_capture_document_url", capture)
    result = scraper.fetch_announcements()
    assert len(result) == len(expected)
    for actual, raw in zip(result, expected):
        assert {k: actual[k] for k in ["title", "surat", "date"]} == {k: raw[k] for k in ["title", "surat", "date"]}
        assert actual["url"] == scraper.url and actual["scraped_at"]
        assert [d["name"] for d in actual["documents"]] == [d["name"] for d in raw["documents"]]
        assert all(d["download_url"] == "https://example.invalid/synthetic.pdf" for d in actual["documents"])
    assert visited == [d["name"] for i in expected for d in i["documents"]]
    browser.close.assert_called_once()


@pytest.mark.parametrize("html", ["", "<html><body>Maintenance</body></html>", '<article class="ap-card"></article>', '<div class="card-body p-4"></div>'])
def test_unrecognized_or_empty_cards_fail(page, html):
    page.set_content(html)
    with pytest.raises(SourceError):
        BimaScraper()._collect_cards(page)


@pytest.mark.parametrize("change", ["title", "date", "number", "documents", "empty_doc", "duplicate_title", "duplicate_number", "duplicate_time", "duplicate_doc_name", "bad_iso", "date_conflict", "year_day", "mixed"])
def test_invalid_or_ambiguous_card_fails_whole_page(page, fixtures, change):
    cards(page, fixtures)
    first = page.locator('article.ap-card').first
    mutations = {
        "title": "c.querySelector('.ap-card__title').remove()",
        "date": "c.querySelector('time').remove()",
        "number": "c.querySelector('.ap-card__number').remove()",
        "documents": "c.querySelector('.ap-file').remove()",
        "empty_doc": "c.querySelector('.ap-file__name').textContent=' '",
        "duplicate_title": "c.append(c.querySelector('.ap-card__title').cloneNode(true))",
        "duplicate_number": "c.append(c.querySelector('.ap-card__number').cloneNode(true))",
        "duplicate_time": "c.append(c.querySelector('time').cloneNode(true))",
        "duplicate_doc_name": "c.querySelector('.ap-file').append(c.querySelector('.ap-file__name').cloneNode(true))",
        "bad_iso": "c.querySelector('time').setAttribute('datetime','2026-02-30')",
        "date_conflict": "c.querySelector('time').setAttribute('title','06 Oktober 2026')",
        "year_day": "c.querySelector('.ap-card__number').textContent+='07'",
        "mixed": "c.insertAdjacentHTML('afterend','<div class=\"card-body p-4\"></div>')",
    }
    first.evaluate("c => {" + mutations[change] + "}")
    with pytest.raises(SourceError, match="BIMA"):
        BimaScraper()._collect_cards(page)


@pytest.mark.parametrize("extra", ["<h5>Judul Kedua Yang Ambigu</h5>", "<span>17 April 2026</span>", "<span>No. Surat: 257/A/2026</span>"])
def test_legacy_ambiguous_metadata_fails(page, fixtures, extra):
    cards(page, fixtures, "bima.html")
    page.locator('.card-body').evaluate("(c, extra) => c.insertAdjacentHTML('beforeend',extra)", extra)
    with pytest.raises(SourceError, match="ambiguous"):
        BimaScraper()._collect_cards(page)


def test_aksara_distinct_same_letter_date_without_historical_replay(page, fixtures):
    invitation, funding, _ = cards(page, fixtures)
    history = json.loads((fixtures / "bima_history_89.json").read_bytes())
    original = deepcopy(history)
    assert new_announcements([invitation, funding], history) == [funding]
    assert new_announcements([funding, invitation], history) == [funding]
    assert new_announcements([funding, invitation], history + [funding]) == []
    # The PDF correction remains attached to this invitation, never to number 1386 globally.
    corrected = dict(invitation, surat="1385/DST/C3/DT.06.01/2026")
    assert new_announcements([corrected, funding], history) == [funding]
    assert history == original


def publication(title):
    return {"title": title, "surat": "42/TEST/2026", "date": "07 Oktober 2026", "documents": [{"name": title + ".pdf"}]}


def test_generic_distinct_publications_are_not_a_number_exception():
    old, new = publication("Undangan Diskusi Penelitian"), publication("Pengumuman Hasil Pendanaan")
    assert new_announcements([new], [old]) == [new]
    assert new_announcements([old, new], [old, new]) == []


@pytest.mark.parametrize("change", ["one_word", "generic_file", "missing_file", "shared_file", "unanchored", "missing_date", "same_title", "containment", "no_extension"])
def test_same_number_date_requires_strong_local_evidence(change):
    old, new = publication("Undangan Diskusi Penelitian"), publication("Pengumuman Hasil Pendanaan")
    if change == "one_word":
        new = publication("Undangan Diskusi Pengabdian")
    elif change == "generic_file":
        new["documents"] = [{"name": "Panduan.pdf"}]
    elif change == "missing_file":
        new["documents"] = []
    elif change == "shared_file":
        new["documents"] += old["documents"]
    elif change == "unanchored":
        old["documents"] = [{"name": "Dokumen.pdf"}]
    elif change == "missing_date":
        new["date"] = ""
    elif change == "same_title":
        new["title"], new["surat"] = old["title"], "43/TEST/2026"
    elif change == "no_extension":
        new["documents"] = [{"name": new["title"]}]
    else:
        new = publication(old["title"] + " Program Baru")
    with pytest.raises(BimaIdentityError, match="ambiguous"):
        match_announcement(new, [old])


@pytest.mark.parametrize("layout", ["legacy", "ap-card"])
def test_all_89_historical_records_roundtrip_dom_without_replay(page, fixtures, tmp_path, layout):
    history = json.loads((fixtures / "bima_history_89.json").read_bytes())
    html = []
    for item in history:
        title, number, date = escape(item["title"]), canonical_surat(item), escape(item["date"])
        docs = [escape(d["name"]) for d in item["documents"]]
        if layout == "legacy":
            html.append(f'<div class="card-body p-4"><h5>{title}</h5><div>No. Surat: {number}</div><span>{date}</span><ul>' + ''.join(f'<li><span class="text-primary">{d}</span></li>' for d in docs) + '</ul></div>')
        else:
            iso = _publication_date(item).strftime('%Y-%m-%d')
            html.append(f'<article class="ap-card"><h3 class="ap-card__title">{title}</h3><p class="ap-card__number">{number}</p><time class="ap-date" datetime="{iso}" title="{date}"></time>' + ''.join(f'<button class="ap-file"><span class="ap-file__name">{d}</span></button>' for d in docs) + '</article>')
    page.set_content(''.join(html))
    parsed = BimaScraper()._collect_cards(page)
    assert len(parsed) == len(history) == 89
    assert new_announcements(parsed, history) == []
    path = tmp_path / 'baseline.json'
    atomic_json(path, history)
    assert migrate(path, apply=True) == 80
    migrated = json.loads(path.read_bytes())
    assert new_announcements(parsed, migrated) == []
    assert migrate(path, apply=True) == 0
    assert [announcement_identity(i) for i in migrated] == [announcement_identity(i) for i in history]


def test_valid_page_without_news_is_healthy_and_dry_run_has_no_writes(page, fixtures, tmp_path, config, notifier, telegram, monkeypatch):
    current = cards(page, fixtures)
    path = tmp_path / "announcements_cache.json"
    atomic_json(path, current)
    before = path.read_bytes()
    scraper = BimaScraper()
    monkeypatch.setattr(scraper, "fetch_announcements", lambda: deepcopy(current))
    result = run_once(dict(config, youtube_monitor_enabled=False), dry_run=True, state_dir=tmp_path, scraper=scraper, notifier=notifier)
    assert result.exit_code == 0 and result.sources[-1].status == "NO_NEW_ITEMS" and result.pending == 0
    assert path.read_bytes() == before and not (tmp_path / "delivery_state.json").exists()
    telegram.assert_not_called()


@pytest.mark.parametrize("growth", [0, 1, 12])
def test_operational_cache_growth_does_not_change_frozen_baselines(fixtures, tmp_path, monkeypatch, growth):
    history = json.loads((fixtures / "bima_history_89.json").read_bytes())
    videos = json.loads((fixtures / "youtube_history_125.json").read_bytes())
    grown = history + [dict(publication(f"Publikasi Tambahan Sintetis {i}"), surat=f"{9000+i}/TEST/2026") for i in range(growth)]
    assert new_announcements([dict(i, surat=canonical_surat(i)) for i in grown], grown) == []
    fresh = dict(publication("Pengumuman Pendanaan Tambahan"), surat="99999/TEST/2026")
    assert new_announcements([*grown, fresh], grown) == [fresh]
    cache = videos + [{"source": "youtube", "title": "Synthetic VOD", "video_id": f"growth{i}", "status": "VOD"} for i in range(growth)]
    monitor = YouTubeMonitor(cache_file=tmp_path / "youtube_cache.json")
    atomic_json(monitor.cache_file, cache)
    monkeypatch.setattr(monitor, "fetch_videos", lambda: [])
    monkeypatch.setattr(monitor, "_analyze_video_type", lambda _: {"status": "UPCOMING"})
    assert monitor.check_new_videos() == []
    assert len(monitor.proposed_cache) == len(cache)
    assert len(history) == 89 and len(videos) == 125
