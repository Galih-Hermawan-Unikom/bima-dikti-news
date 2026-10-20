import hashlib
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from notifier import Notifier
from scraper import BimaScraper
from youtube_monitor import YouTubeMonitor


@pytest.fixture(autouse=True)
def forbid_network_and_secret_file(monkeypatch):
    original_connect = socket.socket.connect
    def forbidden(*args, **kwargs):
        pytest.fail("Real network/secret file access forbidden in tests")
    def connect_loopback_only(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        forbidden()
    monkeypatch.setattr(socket.socket, "connect", connect_loopback_only)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr("app_config.load_env_file", forbidden)


@pytest.fixture(scope="session", autouse=True)
def production_cache_integrity():
    root = Path(__file__).resolve().parents[1]
    paths = [root / name for name in ("announcements_cache.json", "youtube_cache.json")]
    original = root.with_name("BIMA_DIKTI_NEWS")
    if original != root:
        paths += [original / name for name in ("announcements_cache.json", "youtube_cache.json")]
    paths = [p for p in paths if p.exists()]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    yield
    assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == before
    print("Production cache SHA256 unchanged (both worktrees)")


@pytest.fixture
def fixtures():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def config():
    return {"notification_method": "telegram", "telegram_bot_token": "TEST_ONLY_TOKEN",
            "telegram_chat_id": "TEST_ONLY_CHAT", "telegram_send_files": False,
            "youtube_monitor_enabled": True}


@pytest.fixture
def announcement():
    return {"title": "Pengumuman Pendanaan Penelitian Tahun 2026",
            "surat": "256/DST/C2/PP.01.07/2026", "date": "16 April 2026",
            "documents": [], "url": "https://example.invalid/bima"}


@pytest.fixture
def monitors(monkeypatch, announcement):
    from copy import deepcopy
    bima, youtube = BimaScraper(), YouTubeMonitor()
    monkeypatch.setattr(bima, "fetch_announcements", lambda: [deepcopy(announcement)])
    monkeypatch.setattr(youtube, "fetch_videos", lambda: [])
    return bima, youtube


@pytest.fixture
def telegram(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True}
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)
    return post


@pytest.fixture
def notifier(config):
    return Notifier(config)
