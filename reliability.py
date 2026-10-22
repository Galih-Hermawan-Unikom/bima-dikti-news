"""Local persistence and explicit outcomes; no network or secret loading."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class SourceError(RuntimeError):
    pass


def awaiting_youtube_verification(item):
    # Legacy player payloads must not silently enter RSS delivery/retry.
    return item.get("source") == "youtube" and not (
        item.get("delivery_format") == "youtube-rss-v1" and item.get("video_id")
        and item.get("title") and item.get("published") and item.get("url")
        and "status" not in item and not item.get("is_status_update"))


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    # Corrupt history must never silently become an empty cache.
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class SourceResult:
    source: str
    status: str
    items: list = field(default_factory=list)
    error: str = ""


@dataclass
class RunResult:
    sources: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    pending: int = 0
    awaiting_verification: int = 0
    youtube_eligibility: dict = field(default_factory=dict)

    @property
    def exit_code(self):
        return int(bool(self.errors or any(s.status in {"FAILED", "DEGRADED"} for s in self.sources)))
