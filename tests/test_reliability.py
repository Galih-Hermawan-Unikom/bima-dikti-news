import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import yaml

from migrate_cache import migrate
from monitor_runner import run_once
from reliability import SourceError, atomic_json, read_json
from scraper import BimaScraper, announcement_identity, canonical_surat, extract_surat
from youtube_monitor import YouTubeMonitor


def run(tmp_path, config, monitors, notifier):
    return run_once(config, state_dir=tmp_path, scraper=monitors[0], notifier=notifier)


def test_no_new_items(tmp_path, config, monitors, notifier, telegram, announcement):
    atomic_json(tmp_path / "announcements_cache.json", [announcement])
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0
    assert all(s.status in {"NO_NEW_ITEMS", "DISABLED"} for s in result.sources)
    telegram.assert_not_called()


def test_detection_before_delivery_and_success(tmp_path, config, monitors, notifier, telegram):
    def post(*args, **kwargs):
        state = read_json(tmp_path / "delivery_state.json", {})
        assert state["batches"][0]["delivered"] is False
        assert read_json(tmp_path / "announcements_cache.json", [])
        return Mock(status_code=200, json=lambda: {"ok": True})
    telegram.side_effect = post
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0
    assert result.sources[0].status == "NEW_ITEMS"
    assert read_json(tmp_path / "delivery_state.json", {})["batches"][0]["delivered"]


@pytest.mark.parametrize("failure", ["ok_false", "http", "timeout", "invalid_json"])
def test_send_failure_retry_next_execution(tmp_path, config, monitors, notifier, telegram, failure):
    if failure == "timeout":
        telegram.side_effect = requests.Timeout("request not confirmed")
    elif failure == "invalid_json":
        telegram.return_value.json.side_effect = ValueError("invalid json")
    else:
        telegram.return_value.status_code = 503 if failure == "http" else 200
        telegram.return_value.json.return_value = {"ok": False}
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.pending == 1
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert not batch["delivered"] and batch["items"][0]["title"]
    telegram.reset_mock(side_effect=True, return_value=True)
    telegram.return_value = Mock(status_code=200, json=lambda: {"ok": True})
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 0 and result.pending == 0
    assert result.sources[0].status == "NO_NEW_ITEMS"
    assert telegram.call_count == 1


def test_partial_chunks_only_retry_unconfirmed(tmp_path, config, monitors, notifier, telegram, monkeypatch):
    monkeypatch.setattr(notifier, "_build_summary_chunks", lambda items: ["chunk one", "chunk two"])
    ok = Mock(status_code=200, json=lambda: {"ok": True})
    failed = Mock(status_code=200, json=lambda: {"ok": False})
    telegram.side_effect = [ok, failed]
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    batch = read_json(tmp_path / "delivery_state.json", {})["batches"][0]
    assert batch["sent_chunks"] == [1] and not batch["delivered"]
    telegram.reset_mock()
    telegram.side_effect = [ok]
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert telegram.call_count == 1
    assert telegram.call_args.kwargs["json"]["text"] == "chunk two"


def test_cache_write_failure_has_retry_record_no_send(tmp_path, config, monitors, notifier, telegram, monkeypatch):
    monkeypatch.setattr(monitors[0], "save_cache", Mock(side_effect=OSError("disk")))
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1
    assert not read_json(tmp_path / "delivery_state.json", {})["batches"][0]["delivered"]
    telegram.assert_not_called()


def test_outbox_failure_does_not_advance_detection(tmp_path, config, monitors, notifier, telegram, monkeypatch):
    monkeypatch.setattr("monitor_runner.atomic_json", Mock(side_effect=OSError("disk")))
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    assert not (tmp_path / "announcements_cache.json").exists()
    telegram.assert_not_called()






@pytest.mark.parametrize("text,expected", [
    ("No. Surat: 256/DST/C2/PP.01.07/2026 16 April 2026", "256/DST/C2/PP.01.07/2026"),
    ("No Surat: 1/A/2025\n01 Januari 2025", "1/A/2025"),
    ("No. Surat: 256/A/202616 April 2026", ""),
    ("No. Surat: 256/A/20267", ""),
    ("No. Surat: 256/A/2026ABC", ""),
    ("Tanpa nomor surat", ""),
])
def test_surat_parser(text, expected):
    assert extract_surat(text) == expected


def test_html_card_dom_boundaries(fixtures):
    from playwright.sync_api import sync_playwright
    from scraper import get_browser_path
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("BIMA_TEST_BROWSER") or get_browser_path())
        try:
            page = browser.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content((fixtures / "bima.html").read_text())
            card = BimaScraper()._collect_cards(page)[0]
            assert extract_surat(card["full_text"]) == "256/DST/C2/PP.01.07/2026"
            assert card["date"] == "16 April 2026"
            assert card["documents"][0]["name"] == "Panduan.pdf"
        finally:
            browser.close()


def test_migration_80_records_idempotent_without_replay(tmp_path, config, monitors, notifier, telegram, announcement, monkeypatch):
    records = [dict(announcement, surat=f"{i}/DST/C/202616", title=f"Pengumuman {i}") for i in range(80)]
    records += [dict(announcement, surat=f"{i}/DST/C/2026", title=f"Pengumuman {i}") for i in range(80, 89)]
    path = tmp_path / "announcements_cache.json"
    atomic_json(path, records)
    original = path.read_bytes()
    assert migrate(path) == 80 and path.read_bytes() == original
    assert migrate(path, apply=True) == 80
    assert next(tmp_path.glob("*.bak")).read_bytes() == original
    repaired = json.loads(path.read_bytes())
    assert len(repaired) == 89
    assert [announcement_identity(i) for i in records] == [announcement_identity(i) for i in repaired]
    assert migrate(path, apply=True) == 0
    monkeypatch.setattr(monitors[0], "fetch_announcements", lambda: repaired)
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    telegram.assert_not_called()


@pytest.mark.parametrize("date,surat", [("17 April 2026", "1/A/202616"), ("99 April 2026", "1/A/202699"), ("16 Invalid 2026", "1/A/202616")])
def test_migration_leaves_ambiguous_values(date, surat):
    assert canonical_surat({"date": date, "surat": surat}) == surat


def test_rss_fixture(fixtures, monkeypatch):
    monitor = YouTubeMonitor()
    response = Mock(content=(fixtures / "youtube.xml").read_bytes())
    monkeypatch.setattr(monitor.session, "get", lambda *a, **k: response)
    videos = monitor.fetch_videos()
    assert videos[0]["video_id"] == "fixture1234"
    assert videos[0]["description"] == "Informasi penelitian"


def test_dry_run_no_send_no_state_write(tmp_path, config, monitors, notifier, telegram, announcement):
    atomic_json(tmp_path / "youtube_cache.json", [])
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = run_once(config, dry_run=True, state_dir=tmp_path,
                      scraper=monitors[0], notifier=notifier)
    assert result.pending == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    telegram.assert_not_called()


def test_dry_run_cli_never_loads_secrets(monkeypatch):
    import run_github
    config_loader = Mock(return_value={})
    from reliability import RunResult
    monkeypatch.setattr(run_github, "load_config", config_loader)
    monkeypatch.setattr(run_github, "run_once", Mock(return_value=RunResult()))
    assert run_github.main(["--dry-run"]) == 0
    config_loader.assert_called_once_with(load_env=False, include_secrets=False)


def test_corrupt_cache_does_not_replay(tmp_path, config, monitors, notifier, telegram):
    path = tmp_path / "announcements_cache.json"
    path.write_text("broken")
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    assert path.read_text() == "broken"
    telegram.assert_not_called()


@pytest.mark.parametrize("failed_command", ["commit", "push"])
def test_workflow_persistence_failure_propagates_and_retains_artifact(tmp_path, failed_command):
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/main.yml").read_text())
    job = workflow["jobs"]["check-announcements"]
    assert job["if"] == "github.ref == 'refs/heads/master'"
    steps = job["steps"]
    commit = next(s for s in steps if s.get("id") == "persist")
    assert "continue-on-error" not in commit
    assert "always()" in commit["if"]
    assert "delivery_state.json" in commit["run"]
    assert any(s.get("uses") == "actions/upload-artifact@v4" and "failure()" in s["if"] for s in steps)
    # Execute the actual persistence script with a stub git: commit succeeds, push fails.
    import shutil
    bash = shutil.which("bash")
    if Path("C:/Program Files/Git/bin/bash.exe").exists():
        bash = "C:/Program Files/Git/bin/bash.exe"
    assert bash, "bash required to test the Actions shell script"
    script = 'git() { if [ "$1" = diff ]; then return 1; fi; if [ "$1" = ' + failed_command + ' ]; then return 42; fi; return 0; }\n' + commit["run"]
    completed = subprocess.run([bash, "--noprofile", "--norc", "-e", "-c", script], cwd=tmp_path, capture_output=True)
    assert completed.returncode == 42


def test_attachment_failure_retries_without_resending_summary(tmp_path, config, monitors, notifier, telegram, monkeypatch, announcement):
    config["telegram_send_files"] = True
    announcement["documents"] = [{"name": "Panduan.pdf", "download_url": "https://example.invalid/signed?expires=1"}]
    downloaded = tmp_path / "mock-download.pdf"
    def download(*args):
        downloaded.write_bytes(b"offline fixture document")
        return str(downloaded)
    monkeypatch.setattr(notifier, "_download_file", download)
    ok = Mock(status_code=200, json=lambda: {"ok": True})
    failed = Mock(status_code=200, json=lambda: {"ok": False})
    telegram.side_effect = [ok, failed]
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    state = read_json(tmp_path / "delivery_state.json", {})
    assert state["batches"][0]["sent_chunks"] == [1]
    assert "expires=1" not in (tmp_path / "delivery_state.json").read_text(encoding="utf-8")
    assert "expires=1" not in (tmp_path / "announcements_cache.json").read_text(encoding="utf-8")
    assert not downloaded.exists()
    telegram.reset_mock()
    telegram.side_effect = [ok]
    # Preserve the batch's attachment policy even if the global flag changes.
    config["telegram_send_files"] = False
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    assert telegram.call_count == 1
    assert telegram.call_args.args[0].endswith("/sendDocument")


def test_missing_attachment_url_remains_pending(tmp_path, config, monitors, notifier, telegram, announcement):
    config["telegram_send_files"] = True
    announcement["documents"] = [{"name": "Panduan.pdf"}]
    result = run(tmp_path, config, monitors, notifier)
    assert result.exit_code == 1 and result.pending == 1
    assert telegram.call_count == 1


def test_checkpoint_failure_then_retry(tmp_path, config, monitors, notifier, telegram, monkeypatch):
    import monitor_runner
    original_write = monitor_runner.atomic_json
    def fail_ack(path, state):
        if state["batches"] and state["batches"][0]["sent_chunks"]:
            raise OSError("cannot persist confirmation")
        original_write(path, state)
    monkeypatch.setattr(monitor_runner, "atomic_json", fail_ack)
    assert run(tmp_path, config, monitors, notifier).exit_code == 1
    assert not read_json(tmp_path / "delivery_state.json", {})["batches"][0]["sent_chunks"]
    monkeypatch.setattr(monitor_runner, "atomic_json", original_write)
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    # The first send was confirmed remotely but not persisted: duplicate is possible.
    assert telegram.call_count == 2


def test_atomic_cache_replace_failure_preserves_history(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    atomic_json(path, [{"old": True}])
    before = path.read_bytes()
    monkeypatch.setattr("reliability.os.replace", Mock(side_effect=OSError("replace failed")))
    with pytest.raises(OSError):
        atomic_json(path, [{"new": True}])
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_real_cache_migration_in_temporary_copy_only(tmp_path, fixtures):
    source = fixtures / "bima_history_89.json"
    original = source.read_bytes()
    path = tmp_path / "cache.json"
    path.write_bytes(original)
    before = json.loads(original)
    repairs = migrate(path, apply=True)
    after = json.loads(path.read_bytes())
    assert len(before) == len(after)
    assert [announcement_identity(i) for i in before] == [announcement_identity(i) for i in after]
    assert migrate(path, apply=True) == 0
    assert source.read_bytes() == original
    assert repairs > 0


def test_production_cron_unchanged():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/main.yml").read_text()
    assert "cron: '0 */3 * * *'" in workflow


def test_legacy_surat_identity_no_replay_before_migration(tmp_path, config, monitors, notifier, telegram, announcement):
    atomic_json(tmp_path / "announcements_cache.json", [dict(announcement, surat=announcement["surat"] + "16")])
    assert run(tmp_path, config, monitors, notifier).exit_code == 0
    telegram.assert_not_called()
