"""Read-only official-channel verification. Never reads experiment credentials."""
import hashlib
import hmac
import os
import re

import requests

from youtube_experiment_preflight import PreflightError, _api


def validate_destination(chat_id, digest):
    return (isinstance(chat_id, str) and re.fullmatch(r"-[0-9]{5,20}", chat_id) is not None
        and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None
        and hmac.compare_digest(hashlib.sha256(chat_id.encode()).hexdigest(), digest))


def verify_production(environ=None, *, get=requests.get):
    env = os.environ if environ is None else environ
    token, target = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    digest = env.get("PRODUCTION_TELEGRAM_CHAT_ID_SHA256", "")
    if not token or not (target == "@dikti_news" or validate_destination(target, digest)):
        raise PreflightError("PRODUCTION_CREDENTIALS_OR_ALLOWLIST_INVALID")
    bot = _api(get, token, "getMe")
    official = _api(get, token, "getChat", {"chat_id": "@dikti_news"})
    destination = _api(get, token, "getChat", {"chat_id": target})
    if (not isinstance(bot, dict) or bot.get("is_bot") is not True
            or type(bot.get("id")) is not int
            or not isinstance(official, dict) or official.get("username") != "dikti_news"
            or official.get("type") != "channel"
            or not validate_destination(str(official.get("id")), digest)
            or not isinstance(destination, dict) or destination.get("type") != "channel"
            or destination.get("id") != official.get("id")):
        raise PreflightError("OFFICIAL_CHANNEL_IDENTITY_MISMATCH")
    target = str(official["id"])  # pin sends to the verified numeric identity
    member = _api(get, token, "getChatMember", {"chat_id": target, "user_id": bot["id"]})
    if (not isinstance(member, dict) or not isinstance(member.get("user"), dict)
            or member["user"].get("id") != bot["id"]
            or not (member.get("status") == "creator" or
                (member.get("status") == "administrator" and member.get("can_post_messages") is True))):
        raise PreflightError("PRODUCTION_POST_PERMISSION_MISSING")
    return {"telegram_bot_token": token, "telegram_chat_id": target,
            "production_chat_id_sha256": digest, "notification_method": "telegram"}
