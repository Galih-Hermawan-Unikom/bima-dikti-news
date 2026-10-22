"""Historical identity utilities, with no discovery transport or RSS dependency."""
from datetime import datetime, timezone
import re

DEFAULT_CHANNEL = "UCGo_6l_6kp8H8OHcKcSIeDw"


def parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, AttributeError, TypeError):
        return None


def youtube_identity(item):
    if not isinstance(item, dict):
        return ""
    value = item.get("video_id", "")
    return "video:" + value.strip() if isinstance(value, str) and value.strip() else ""


def known_video_ids(cache, journal):
    ids = {i["video_id"] for i in cache if youtube_identity(i)}
    for batch in journal.get("batches", []):
        ids.update(i["video_id"] for i in batch["items"] if i.get("source") == "youtube" and youtube_identity(i))
        for key in batch["event_keys"]:
            match = re.fullmatch(r"youtube:video:([^:]+):[^:]+", key)
            if match:
                ids.add(match.group(1))
    return ids


def validate_baseline_marker(value):
    """Read the legacy marker unchanged; its name/schema are historical data."""
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get("version") != 1
            or parse_timestamp(value.get("cutover_at")) is None
            or not isinstance(value.get("baseline_ids"), list)
            or any(not isinstance(i, str) or not i.strip() for i in value["baseline_ids"])
            or len(set(value["baseline_ids"])) != len(value["baseline_ids"])):
        raise ValueError("Invalid historical YouTube baseline; operator review required")
    return value
