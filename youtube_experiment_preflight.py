"""Read-only runtime verification of the dedicated YouTube experiment target."""
from __future__ import annotations

import hashlib
import hmac
import os
import re

import requests


PRODUCTION_USERNAME = "dikti_news"


class PreflightError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _api(get, token, method, params=None):
    try:
        response = get(f"https://api.telegram.org/bot{token}/{method}", params=params,
                       timeout=15, allow_redirects=False)
        body = response.json()
    except Exception:
        raise PreflightError("TELEGRAM_API_UNAVAILABLE") from None
    if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        raise PreflightError("TELEGRAM_API_REJECTED")
    return body.get("result")


def verify_experiment_destination(environ=None, *, get=requests.get):
    """Check presence, hash allowlist, bot, experiment channel and production separation.

    Returns only a non-reversible short destination alias. Secret values, IDs,
    API responses, and token-bearing URLs are never included in errors/results.
    """
    env = os.environ if environ is None else environ
    token = env.get("YOUTUBE_EXPERIMENT_BOT_TOKEN", "")
    chat_id = env.get("YOUTUBE_EXPERIMENT_CHAT_ID", "")
    allowed_hash = env.get("YOUTUBE_EXPERIMENT_CHAT_ID_SHA256", "")
    if not token or not chat_id or not allowed_hash:
        raise PreflightError("EXPERIMENT_SECRET_MISSING")
    if not re.fullmatch(r"-?[0-9]{5,20}", chat_id):
        raise PreflightError("EXPERIMENT_CHAT_ID_INVALID")
    if not re.fullmatch(r"[a-f0-9]{64}", allowed_hash):
        raise PreflightError("ALLOWLIST_HASH_INVALID")
    actual_hash = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, allowed_hash):
        raise PreflightError("ALLOWLIST_MISMATCH")

    experiment = _api(get, token, "getChat", {"chat_id": chat_id})
    if (not isinstance(experiment, dict) or str(experiment.get("id")) != chat_id
            or experiment.get("type") != "channel"):
        raise PreflightError("EXPERIMENT_CHANNEL_IDENTITY_INVALID")
    bot = _api(get, token, "getMe")
    if (not isinstance(bot, dict) or bot.get("is_bot") is not True
            or type(bot.get("id")) is not int or bot["id"] <= 0):
        raise PreflightError("BOT_IDENTITY_INVALID")
    member = _api(get, token, "getChatMember", {"chat_id": chat_id, "user_id": bot["id"]})
    if not isinstance(member, dict):
        raise PreflightError("EXPERIMENT_POST_PERMISSION_MISSING")
    user = member.get("user")
    status = member.get("status")
    can_post = status == "creator" or (status == "administrator" and member.get("can_post_messages") is True)
    if not isinstance(user, dict) or user.get("id") != bot["id"] or not can_post:
        raise PreflightError("EXPERIMENT_POST_PERMISSION_MISSING")

    production = _api(get, token, "getChat", {"chat_id": "@" + PRODUCTION_USERNAME})
    if (not isinstance(production, dict) or production.get("type") != "channel"
            or production.get("username") != PRODUCTION_USERNAME
            or str(production.get("id")) == chat_id):
        raise PreflightError("PRODUCTION_SEPARATION_UNVERIFIED")
    return {"destination_alias": "EXPERIMENT-" + actual_hash[:12],
            "bot_verified": True, "experiment_channel_verified": True,
            "can_post_messages": True, "production_separation_verified": True}


def main():
    try:
        result = verify_experiment_destination()
    except PreflightError as exc:
        print("YOUTUBE_EXPERIMENT_PREFLIGHT=BLOCKED code=" + exc.code)
        return 1
    print("YOUTUBE_EXPERIMENT_PREFLIGHT=PASS destination=" + result["destination_alias"]
          + " bot=verified channel=verified can_post=true production_separation=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
