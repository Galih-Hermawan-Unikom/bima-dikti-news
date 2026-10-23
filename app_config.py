import json
import os


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key and value and key not in os.environ:
                os.environ[key] = value


def _load_file_config(config_path):
    if not os.path.exists(config_path):
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_bool(value, default):
    if isinstance(value, bool):
        return value
    if value is None:
        return default

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_config(config_path="config.json", load_env=True, include_secrets=True,
                include_review_secrets=False, include_state_secrets=False):
    if load_env:
        load_env_file()

    file_config = _load_file_config(config_path)

    config = {
        "notification_method": file_config.get("notification_method", "console"),
        "check_interval_minutes": _read_int(
            file_config.get("check_interval_minutes", 30), 30
        ),
        "youtube_monitor_enabled": _read_bool(
            file_config.get("youtube_monitor_enabled", True), True
        ),
        # New observation/outbox path is opt-in; no production value is set.
        "youtube_observation_enabled": _read_bool(
            file_config.get("youtube_observation_enabled", False), False
        ),
        "youtube_delivery_enabled": _read_bool(
            file_config.get("youtube_delivery_enabled", False), False
        ),
        "youtube_delivery_send_enabled": _read_bool(
            file_config.get("youtube_delivery_send_enabled", False), False
        ),
        "youtube_automatic_tabs_verification_enabled": _read_bool(
            file_config.get("youtube_automatic_tabs_verification_enabled", False), False
        ),
        "youtube_activation_at": file_config.get("youtube_activation_at"),
        "youtube_policy_version": file_config.get("youtube_policy_version", "tabs-only-v1"),
        "youtube_policy_approved": _read_bool(file_config.get("youtube_policy_approved", False), False),
        "youtube_window_seconds": _read_int(file_config.get("youtube_window_seconds"), None),
        "youtube_ttl_seconds": _read_int(file_config.get("youtube_ttl_seconds"), None),
        "youtube_skew_seconds": _read_int(file_config.get("youtube_skew_seconds"), None),
        "youtube_capture_interval_seconds": _read_int(file_config.get("youtube_capture_interval_seconds"), None),
        "youtube_state_branch": file_config.get("youtube_state_branch", "youtube-state"),
        "youtube_state_initialize": _read_bool(file_config.get("youtube_state_initialize", False), False),
        "youtube_review_source_hosts": file_config.get("youtube_review_source_hosts", []),
        "youtube_review_key_id": file_config.get("youtube_review_key_id", "operator"),
        "youtube_channel_url": file_config.get(
            "youtube_channel_url", "https://www.youtube.com/@kemdiktisaintek"
        ),
        "youtube_channel_id": file_config.get(
            "youtube_channel_id", "UCGo_6l_6kp8H8OHcKcSIeDw"
        ),
        "telegram_send_files": _read_bool(
            file_config.get("telegram_send_files", False), False
        ),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", "") if include_secrets else "",
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "") if include_secrets else "",
        # YouTube experiment delivery has a separate credential and never falls
        # back to the BIMA/production Telegram token.
        "youtube_experiment_bot_token": os.environ.get("YOUTUBE_EXPERIMENT_BOT_TOKEN", "") if include_secrets else "",
        "youtube_state_token": os.environ.get("YOUTUBE_STATE_TOKEN", "")
            if include_secrets or include_state_secrets else "",
        "youtube_review_hmac_key": os.environ.get("YOUTUBE_REVIEW_HMAC_KEY", "")
            if include_secrets or include_review_secrets else "",
        "youtube_review_evidence_json": os.environ.get("YOUTUBE_REVIEW_EVIDENCE_JSON", "")
            if include_secrets or include_review_secrets else "",
        "youtube_experiment_chat_id": os.environ.get("YOUTUBE_EXPERIMENT_CHAT_ID", "") if include_secrets else "",
        "youtube_experiment_chat_id_sha256": os.environ.get("YOUTUBE_EXPERIMENT_CHAT_ID_SHA256", "") if include_secrets else "",
    }

    if os.environ.get("NOTIFICATION_METHOD"):
        config["notification_method"] = os.environ["NOTIFICATION_METHOD"]

    if os.environ.get("CHECK_INTERVAL_MINUTES"):
        config["check_interval_minutes"] = _read_int(
            os.environ["CHECK_INTERVAL_MINUTES"], config["check_interval_minutes"]
        )

    if os.environ.get("YOUTUBE_MONITOR_ENABLED"):
        config["youtube_monitor_enabled"] = _read_bool(
            os.environ["YOUTUBE_MONITOR_ENABLED"], config["youtube_monitor_enabled"]
        )

    if os.environ.get("YOUTUBE_CHANNEL_URL"):
        config["youtube_channel_url"] = os.environ["YOUTUBE_CHANNEL_URL"]

    if os.environ.get("YOUTUBE_CHANNEL_ID"):
        config["youtube_channel_id"] = os.environ["YOUTUBE_CHANNEL_ID"]

    for key, env_key in (
        ("youtube_observation_enabled", "YOUTUBE_OBSERVATION_ENABLED"),
        ("youtube_delivery_enabled", "YOUTUBE_DELIVERY_ENABLED"),
        ("youtube_delivery_send_enabled", "YOUTUBE_DELIVERY_SEND_ENABLED"),
        ("youtube_automatic_tabs_verification_enabled", "YOUTUBE_AUTOMATIC_TABS_VERIFICATION_ENABLED"),
        ("youtube_state_initialize", "YOUTUBE_STATE_INITIALIZE"),
        ("youtube_policy_approved", "YOUTUBE_POLICY_APPROVED"),
    ):
        if os.environ.get(env_key) is not None:
            config[key] = _read_bool(os.environ[env_key], config[key])

    for key, env_key in (
        ("youtube_window_seconds", "YOUTUBE_WINDOW_SECONDS"),
        ("youtube_ttl_seconds", "YOUTUBE_TTL_SECONDS"),
        ("youtube_skew_seconds", "YOUTUBE_SKEW_SECONDS"),
        ("youtube_capture_interval_seconds", "YOUTUBE_CAPTURE_INTERVAL_SECONDS"),
    ):
        if os.environ.get(env_key) is not None:
            config[key] = _read_int(os.environ[env_key], config[key])

    for key, env_key in (
        ("youtube_activation_at", "YOUTUBE_ACTIVATION_AT"),
        ("youtube_policy_version", "YOUTUBE_POLICY_VERSION"),
        ("youtube_state_branch", "YOUTUBE_STATE_BRANCH"),
        ("youtube_review_key_id", "YOUTUBE_REVIEW_KEY_ID"),
    ):
        if os.environ.get(env_key):
            config[key] = os.environ[env_key]

    if os.environ.get("YOUTUBE_REVIEW_SOURCE_HOSTS"):
        config["youtube_review_source_hosts"] = [
            host.strip().lower() for host in os.environ["YOUTUBE_REVIEW_SOURCE_HOSTS"].split(",") if host.strip()
        ]

    if os.environ.get("TELEGRAM_SEND_FILES"):
        config["telegram_send_files"] = _read_bool(
            os.environ["TELEGRAM_SEND_FILES"], config["telegram_send_files"]
        )

    return config
