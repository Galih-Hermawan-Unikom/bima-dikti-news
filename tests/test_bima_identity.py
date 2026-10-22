"""BIMA identity regressions; real history is read only, all writes are temporary."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from migrate_cache import migrate
from monitor_runner import run_once
from reliability import atomic_json, read_json
from scraper import (BimaIdentityError, BimaScraper, announcement_identity,
                     canonical_surat, match_announcement, new_announcements)


@pytest.fixture
def history(fixtures):
    return json.loads((fixtures / "bima_history_89.json").read_bytes())


@pytest.fixture
def september(history):
    monitoring = next(i for i in history if "(PBK)" in i["title"] and i["date"] == "06 September 2026")
    contract = next(i for i in history if i["title"] == "Undangan Penandatanganan Kontrak Program Kosabangsa Tahun 2026")
    return deepcopy(monitoring), deepcopy(contract)


def record(title="Pengumuman Penelitian", surat="1/A/2026", date="16 April 2026", docs=("Pengumuman Penelitian.pdf",)):
    return {"title": title, "surat": surat, "date": date,
            "documents": [{"name": name} for name in docs]}


def old_batch(item, *, delivered=False):
    return {"event_keys": ["bima:surat:" + canonical_surat(item) + ":initial"],
            "items": [deepcopy(item)], "chunks": ["Stored original summary"],
            "sent_chunks": [1] if delivered else [], "sent_documents": [],
            "send_files": False, "delivered": delivered, "attempts": 0}


def execute(tmp_path, config, monitors, notifier):
    return run_once(config, state_dir=tmp_path, scraper=monitors[0], notifier=notifier)


def test_september_collision_is_two_announcements(september):
    monitoring, contract = september
    assert canonical_surat(monitoring) == canonical_surat(contract)
    assert announcement_identity(monitoring) != announcement_identity(contract)
    assert new_announcements([monitoring, contract], []) == [monitoring, contract]
    assert new_announcements([monitoring], [contract]) == [monitoring]
    assert new_announcements([contract], [monitoring]) == [contract]


def test_june_duplicate_is_recognized_without_deleting_history(history):
    workshop = [i for i in history if i["title"] == "Undangan Peserta Workshop Penulisan Artikel Ilmiah Nasional Tahun 2026"]
    original = deepcopy(history)
    assert len(workshop) == 2
    assert workshop[0]["surat"] != workshop[1]["surat"]
    assert announcement_identity(workshop[0]) == announcement_identity(workshop[1])
    assert new_announcements(workshop, history) == []
    assert len(new_announcements(workshop, [])) == 1
    assert history == original


@pytest.mark.parametrize("date,day", [("16 April 2026", "16"), ("06 September 2026", "06"), ("2 September 2026", "02")])
def test_known_suffix_is_compatible(date, day):
    clean = record(date=date)
    broken = dict(clean, surat=clean["surat"] + day)
    assert announcement_identity(clean) == announcement_identity(broken)
    assert new_announcements([clean], [broken]) == []


def test_official_monitoring_number_correction_is_not_news(history, september):
    monitoring, contract = september
    corrected = dict(monitoring, surat="1173/DST/C3/DT.06.01/2026")
    assert announcement_identity(corrected) != announcement_identity(monitoring)
    assert match_announcement(corrected, [monitoring, contract]) == 0
    assert new_announcements([corrected, contract], history) == []
    assert new_announcements([corrected], [contract]) == [corrected]
    assert monitoring["surat"].endswith("202606")
    assert contract["surat"].endswith("202602")


@pytest.mark.parametrize("change", ["case_space", "typography", "month_language", "letter_case", "title_typo", "document_order"])
def test_minor_metadata_does_not_replay(change):
    old = record(title="Penelitian - 'Program'", docs=("Penelitian.pdf", "Ketentuan.pdf"))
    current = deepcopy(old)
    if change == "case_space":
        current["title"] = "  PENELITIAN   -  'PROGRAM'  "
    elif change == "typography":
        current["title"] = "Penelitian \u2014 \u2018Program\u2019"
    elif change == "month_language":
        old["date"], current["date"] = "16 Mei 2026", "16 May 2026"
        current["surat"] = "2/A/2026"
    elif change == "letter_case":
        current["surat"] = "1/a/2026"
    elif change == "title_typo":
        current["title"] = "Peneltian - 'Program'"
    else:
        current["documents"].reverse()
        current["surat"] = "2/A/2026"
    assert new_announcements([current], [old]) == []


@pytest.mark.parametrize("change", ["same_letter_date", "missing_documents", "changed_date", "changed_documents", "both_changed", "missing_title"])
def test_ambiguous_metadata_fails_closed(change):
    old = record()
    current = deepcopy(old)
    if change == "same_letter_date":
        current.update(title="Pengumuman Lain", documents=[{"name": "Lain.pdf"}])
    elif change == "missing_documents":
        current.update(title="Pengumuman Lain", date="17 April 2026", documents=[])
    elif change == "changed_date":
        current.update(surat="2/A/2026", date="17 April 2026")
    elif change == "changed_documents":
        current.update(surat="2/A/2026", documents=[{"name": "Lain.pdf"}])
    elif change == "both_changed":
        current.update(surat="2/A/2026", title="Pengumuman Lain")
    else:
        current["title"] = ""
    with pytest.raises(BimaIdentityError, match="ambiguous"):
        new_announcements([current], [old])


def test_multiple_historical_matches_are_not_merged():
    old = record()
    other = dict(old, surat="2/A/2026")
    with pytest.raises(BimaIdentityError, match="multiple historical"):
        match_announcement(dict(old, surat="3/A/2026"), [old, other])


def test_same_composite_key_with_conflicting_evidence_is_ambiguous():
    old = record()
    current = dict(old, date="17 April 2026", documents=[{"name": "Different.pdf"}])
    assert announcement_identity(current) == announcement_identity(old)
    with pytest.raises(BimaIdentityError, match="conflicting dates"):
        match_announcement(current, [old])


@pytest.mark.parametrize("documents", [None, "Invalid.pdf", [None]])
def test_invalid_document_metadata_fails_closed(documents):
    with pytest.raises(BimaIdentityError, match="invalid document"):
        new_announcements([record(docs=()) | {"documents": documents}], [])


def test_title_only_with_conflicting_documents_is_ambiguous():
    old = record(surat="")
    current = dict(old, documents=[{"name": "Different.pdf"}])
    with pytest.raises(BimaIdentityError, match="title alone"):
        match_announcement(current, [old])


def test_missing_historical_title_is_not_silently_ignored():
    with pytest.raises(BimaIdentityError, match="historical announcement title missing"):
        match_announcement(record(), [record(title="")])


def test_shared_generic_attachment_is_not_an_alias():
    old = record(docs=("Panduan.pdf", "Pengumuman Pertama.pdf"))
    new = record(title="Program Berbeda", surat="2/A/2026", docs=("Panduan.pdf", "Pengumuman Kedua.pdf"))
    assert new_announcements([new], [old]) == [new]


def test_new_announcement_and_reused_title_next_year_are_detected():
    old = record()
    new = record(title="Program Baru", surat="2/A/2026", docs=("Program Baru.pdf",))
    next_year = dict(old, surat="1/A/2027", date="16 April 2027")
    assert new_announcements([new, next_year], [old]) == [new, next_year]


@pytest.mark.parametrize("migrated", [False, True])
@pytest.mark.parametrize("corrected", [False, True])
def test_entire_history_no_replay(tmp_path, config, monitors, notifier, telegram, monkeypatch, history, september, migrated, corrected):
    path = tmp_path / "announcements_cache.json"
    original = deepcopy(history)
    atomic_json(path, history)
    if migrated:
        assert migrate(path, apply=True) == 80
    baseline = read_json(path, [])
    current = [dict(i, surat=canonical_surat(i)) for i in history]
    if corrected:
        current[next(i for i, x in enumerate(current) if x["title"] == september[0]["title"])]["surat"] = "1173/DST/C3/DT.06.01/2026"
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: deepcopy(current))
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0 and result.pending == 0
    assert result.sources[0].status == "NO_NEW_ITEMS"
    assert read_json(path, []) == baseline
    assert len(baseline) == len(history) == 89
    assert len({announcement_identity(i) for i in baseline}) == 88
    assert history == original
    assert read_json(tmp_path / "delivery_state.json", {})["batches"] == []
    telegram.assert_not_called()


def test_migration_preserves_rows_keys_duplicates_and_backup(tmp_path, history):
    path = tmp_path / "cache.json"
    atomic_json(path, history)
    original = path.read_bytes()
    assert migrate(path) == 80 and path.read_bytes() == original
    assert migrate(path, apply=True) == 80
    repaired = read_json(path, [])
    assert len(repaired) == 89
    assert [announcement_identity(i) for i in repaired] == [announcement_identity(i) for i in history]
    assert len({announcement_identity(i) for i in repaired}) == 88
    assert next(tmp_path.glob("*.bak")).read_bytes() == original
    assert [dict(i, surat=old["surat"]) for i, old in zip(repaired, history)] == history
    assert migrate(path, apply=True) == 0


def test_entire_history_plus_genuine_new_item(tmp_path, config, monitors, notifier, telegram, monkeypatch, history):
    atomic_json(tmp_path / "announcements_cache.json", history)
    new = record(title="Pengumuman Sintetis Baru", surat="9999/TEST/2026", date="20 Oktober 2026", docs=())
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: deepcopy(history + [new]))
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0
    assert result.sources[0].items == [new]
    assert len(read_json(tmp_path / "announcements_cache.json", [])) == 90
    assert telegram.call_count == 1
    assert [i["title"] for i in read_json(tmp_path / "delivery_state.json", {})["batches"][0]["items"]] == [new["title"]]


@pytest.mark.parametrize("delivered", [False, True])
def test_old_journal_keys_and_corrected_number_do_not_duplicate_batch(tmp_path, config, monitors, notifier, telegram, monkeypatch, september, delivered):
    monitoring, _ = september
    batch = old_batch(monitoring, delivered=delivered)
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [batch]})
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [dict(monitoring, surat="1173/DST/C3/DT.06.01/2026")])
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0
    batches = read_json(tmp_path / "delivery_state.json", {})["batches"]
    assert len(batches) == 1 and batches[0]["delivered"]
    assert batches[0]["items"] == batch["items"]
    assert batches[0]["event_keys"] == batch["event_keys"]
    assert batches[0]["chunks"] == batch["chunks"]
    assert telegram.call_count == (0 if delivered else 1)
    if not delivered:
        assert telegram.call_args.kwargs["json"]["text"] == batch["chunks"][0]


def test_old_journal_letter_is_not_a_global_alias(tmp_path, config, monitors, notifier, telegram, monkeypatch, september):
    monitoring, contract = september
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [old_batch(contract, delivered=True)]})
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [deepcopy(monitoring)])
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0 and telegram.call_count == 1
    assert len(read_json(tmp_path / "delivery_state.json", {})["batches"]) == 2


def test_cache_failure_then_number_correction_retries_original_batch(tmp_path, config, monitors, notifier, telegram, monkeypatch, september):
    monitoring, _ = september
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [deepcopy(monitoring)])
    save = monitors[0].save_cache
    monkeypatch.setattr(monitors[0], "save_cache", Mock(side_effect=OSError("disk")))
    assert execute(tmp_path, config, monitors, notifier).exit_code == 1
    telegram.assert_not_called()
    original = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    monkeypatch.setattr(monitors[0], "save_cache", save)
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [dict(monitoring, surat="1173/DST/C3/DT.06.01/2026")])
    assert execute(tmp_path, config, monitors, notifier).exit_code == 0
    batches = read_json(tmp_path / "delivery_state.json", {})["batches"]
    assert len(batches) == 1 and batches[0]["chunks"] == original["chunks"]
    assert batches[0]["items"] == original["items"]
    assert telegram.call_count == 1


@pytest.mark.parametrize("cached", [False, True])
def test_ambiguity_blocks_pending_bima_without_advancing_history(tmp_path, config, monitors, notifier, telegram, monkeypatch, cached):
    old = record()
    path = tmp_path / "announcements_cache.json"
    atomic_json(path, [old] if cached else [])
    before = path.read_bytes()
    batch = old_batch(old)
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [batch]})
    ambiguous = dict(old, surat="2/A/2026", date="17 April 2026")
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [ambiguous])
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.pending == 1
    assert result.sources[0].status == "FAILED"
    assert "ambiguous" in result.sources[0].error
    assert path.read_bytes() == before
    assert read_json(tmp_path / "delivery_state.json", {})["batches"] == [batch]
    telegram.assert_not_called()


def test_ambiguous_bima_does_not_fall_back_to_legacy_youtube(tmp_path, config, monitors, notifier, telegram, monkeypatch, rss_video, approved_cutover):
    old = record()
    atomic_json(tmp_path / "announcements_cache.json", [old])
    before = (tmp_path / "announcements_cache.json").read_bytes()
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [dict(old, title="Tidak Pasti", documents=[])])
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.sources[0].status == "FAILED"
    assert result.sources[1].status == "DISABLED" and telegram.call_count == 0
    assert (tmp_path / "announcements_cache.json").read_bytes() == before
    assert read_json(tmp_path / "delivery_state.json", {})["batches"] == []


def test_ambiguous_mixed_pending_batch_is_deferred_intact(tmp_path, config, monitors, notifier, telegram, monkeypatch):
    old = record()
    atomic_json(tmp_path / "announcements_cache.json", [old])
    batch = old_batch(old)
    batch["items"].append({"source": "youtube", "title": "Stored fixture", "video_id": "old"})
    batch["event_keys"].append("youtube:old:initial")
    atomic_json(tmp_path / "delivery_state.json", {"version": 1, "batches": [batch]})
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: [dict(old, surat="2/A/2026", date="17 April 2026")])
    result = execute(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.pending == 2
    assert read_json(tmp_path / "delivery_state.json", {})["batches"] == [batch]
    telegram.assert_not_called()
