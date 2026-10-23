import hashlib
from unittest.mock import Mock

import pytest
import requests

from youtube_experiment_preflight import PreflightError, verify_experiment_destination


TARGET = "-1001234567890"
TOKEN = "EXPERIMENT_BOT_TOKEN_TEST_ONLY"


def _response(result, *, status=200, ok=True):
    response = Mock(status_code=status)
    response.json.return_value = {"ok": ok, "result": result}
    return response


def _env():
    return {"YOUTUBE_EXPERIMENT_BOT_TOKEN": TOKEN,
            "YOUTUBE_EXPERIMENT_CHAT_ID": TARGET,
            "YOUTUBE_EXPERIMENT_CHAT_ID_SHA256": hashlib.sha256(TARGET.encode()).hexdigest()}


def test_preflight_verifies_bot_experiment_target_rights_and_production_separation_without_send(monkeypatch):
    replies = [
        _response({"id": int(TARGET), "type": "channel", "title": "Experiment"}),
        _response({"id": 73, "is_bot": True, "username": "test_bot"}),
        _response({"user": {"id": 73}, "status": "administrator", "can_post_messages": True}),
        _response({"id": -1009999999999, "type": "channel", "username": "dikti_news", "title": "DIKTI News"}),
    ]
    get = Mock(side_effect=replies)
    post = Mock(side_effect=AssertionError("preflight must never send"))
    monkeypatch.setattr(requests, "post", post)
    result = verify_experiment_destination(_env(), get=get)
    assert result == {"destination_alias": "EXPERIMENT-" + hashlib.sha256(TARGET.encode()).hexdigest()[:12],
        "bot_verified": True, "experiment_channel_verified": True,
        "can_post_messages": True, "production_separation_verified": True}
    assert [call.args[0].rsplit("/", 1)[-1] for call in get.call_args_list] == [
        "getChat", "getMe", "getChatMember", "getChat"]
    assert post.call_count == 0


@pytest.mark.parametrize("field", ["YOUTUBE_EXPERIMENT_BOT_TOKEN", "YOUTUBE_EXPERIMENT_CHAT_ID",
                                    "YOUTUBE_EXPERIMENT_CHAT_ID_SHA256"])
def test_preflight_fails_closed_when_any_required_secret_is_missing(field):
    env = _env()
    env[field] = ""
    get = Mock(side_effect=AssertionError("missing secrets must stop before Telegram API"))
    with pytest.raises(PreflightError, match="EXPERIMENT_SECRET_MISSING"):
        verify_experiment_destination(env, get=get)
    assert get.call_count == 0


def test_preflight_rejects_allowlist_mismatch_before_network():
    env = _env()
    env["YOUTUBE_EXPERIMENT_CHAT_ID_SHA256"] = "0" * 64
    get = Mock(side_effect=AssertionError("bad allowlist must stop before Telegram API"))
    with pytest.raises(PreflightError, match="ALLOWLIST_MISMATCH"):
        verify_experiment_destination(env, get=get)
    assert get.call_count == 0


def test_preflight_rejects_production_target_even_with_valid_hash():
    env = _env()
    replies = [
        _response({"id": int(TARGET), "type": "channel"}),
        _response({"id": 73, "is_bot": True}),
        _response({"user": {"id": 73}, "status": "creator"}),
        _response({"id": int(TARGET), "type": "channel", "username": "dikti_news", "title": "DIKTI News"}),
    ]
    with pytest.raises(PreflightError, match="PRODUCTION_SEPARATION_UNVERIFIED"):
        verify_experiment_destination(env, get=Mock(side_effect=replies))


def test_experiment_token_is_separate_and_never_loaded_from_production_token(monkeypatch):
    from app_config import load_config
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "PRODUCTION_TOKEN")
    monkeypatch.delenv("YOUTUBE_EXPERIMENT_BOT_TOKEN", raising=False)
    cfg = load_config(config_path="missing-experiment-config.json", load_env=False, include_secrets=True)
    assert cfg["telegram_bot_token"] == "PRODUCTION_TOKEN"
    assert cfg["youtube_experiment_bot_token"] == ""
