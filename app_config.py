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


def load_config(config_path="config.json", load_env=True):
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
        "youtube_channel_url": file_config.get(
            "youtube_channel_url", "https://www.youtube.com/@kemdiktisaintek"
        ),
        "youtube_channel_id": file_config.get(
            "youtube_channel_id", "UCGo_6l_6kp8H8OHcKcSIeDw"
        ),
        "telegram_send_files": _read_bool(
            file_config.get("telegram_send_files", False), False
        ),
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
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

    if os.environ.get("TELEGRAM_SEND_FILES"):
        config["telegram_send_files"] = _read_bool(
            os.environ["TELEGRAM_SEND_FILES"], config["telegram_send_files"]
        )

    return config
