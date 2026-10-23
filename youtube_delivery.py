"""Versioned JSON observation state and at-most-once YouTube outbox."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid

import youtube_eligibility as e


SCHEMA_VERSION = 1
DEFAULT_SEND_LEASE = timedelta(minutes=3)
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MAX_STATE_BYTES = 900_000


def _iso(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc).isoformat()


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None and result.utcoffset() is not None else None


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SendConfirmation:
    http_status: int
    ok: bool
    message_id: int | None

    @property
    def confirmed(self):
        return (type(self.http_status) is int and self.http_status == 200
                and self.ok is True and type(self.message_id) is int and self.message_id > 0)


@dataclass(frozen=True)
class DeliveryTicket:
    event_key: str
    video_id: str
    attempt_token: str
    payload_sha256: str
    attempt: int = 1


class YouTubeStateStore:
    """Atomic local JSON state; an optional hook checkpoints one whole blob remotely."""

    def __init__(self, path, *, activation_at, policy_version, send_lease=DEFAULT_SEND_LEASE,
                 commit_hook=None):
        self.path = Path(path)
        self.activation_at = _iso(activation_at)
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise ValueError("Invalid YouTube policy version")
        if type(send_lease) is not timedelta or send_lease <= timedelta(0):
            raise ValueError("Invalid outbox lease")
        self.policy_version = policy_version
        self.send_lease = send_lease
        self._commit_hook = commit_hook
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._state = self._read_state()
            if (self._state["activation_at"] != self.activation_at
                    or self._state["policy_version"] != self.policy_version):
                self._commit(lambda state: self._quarantine_for_new_epoch(state))
        else:
            self._state = self._new_state()
            self._write_local()

    def _new_state(self):
        return {"schema_version": SCHEMA_VERSION, "activation_at": self.activation_at,
                "policy_version": self.policy_version, "capture_seq": 0,
                "last_capture_at": None, "initialized": False, "needs_inventory": True,
                "observations": {}, "captures": [], "outbox": {}}

    def _read_state(self):
        raw = self.path.read_bytes()
        if not raw or len(raw) > _MAX_STATE_BYTES:
            raise ValueError("YouTube JSON state size is invalid")
        state = json.loads(raw.decode("utf-8"))
        self._validate_state(state)
        return state

    @staticmethod
    def _validate_state(state):
        if (not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION
                or not isinstance(state.get("activation_at"), str)
                or _parse(state.get("activation_at")) is None
                or not isinstance(state.get("policy_version"), str)
                or type(state.get("capture_seq")) is not int or state["capture_seq"] < 0
                or type(state.get("initialized")) is not bool
                or type(state.get("needs_inventory")) is not bool
                or not isinstance(state.get("observations"), dict)
                or not isinstance(state.get("captures"), list)
                or len(state["captures"]) > 2
                or not isinstance(state.get("outbox"), dict)):
            raise ValueError("Invalid YouTube JSON state schema")
        evaluation = state.get("last_evaluation")
        if evaluation is not None:
            if (not isinstance(evaluation, dict) or evaluation.get("version") != 1
                    or _parse(evaluation.get("evaluated_at")) is None
                    or type(evaluation.get("capture_seq")) is not int
                    or not 0 <= evaluation["capture_seq"] <= state["capture_seq"]
                    or not isinstance(evaluation.get("health"), dict)
                    or set(evaluation["health"]) != {"videos", "streams"}
                    or any(value not in {"HEALTHY", "FAILED"} for value in evaluation["health"].values())
                    or not isinstance(evaluation.get("counts"), dict)
                    or set(evaluation["counts"]) != {"checked", "videos_tab", "streams_tab", "known",
                        "unknown", "unverified", "new_candidates", "new_eligible", "new_hold",
                        "eligible", "hold", "rejected", "already_delivered"}
                    or any(type(value) is not int or value < 0 for value in evaluation["counts"].values())
                    or not isinstance(evaluation.get("decisions"), list)
                    or len(evaluation["decisions"]) > 1000
                    or not isinstance(evaluation.get("errors"), list)
                    or any(not isinstance(error, str) for error in evaluation["errors"])):
                raise ValueError("Invalid YouTube last evaluation")
            for decision in evaluation["decisions"]:
                if (not isinstance(decision, dict)
                        or not _VIDEO_ID.fullmatch(decision.get("video_id", ""))
                        or decision.get("knowledge") not in {None, "KNOWN", "UNKNOWN"}
                        or decision.get("eligibility") not in {"ELIGIBLE", "HOLD_FOR_REVIEW", "REJECTED", "ALREADY_DELIVERED"}
                        or decision.get("delivery_state") not in {"NONE", "PENDING", "PARTIAL", "DELIVERED", "UNVERIFIED"}
                        or not isinstance(decision.get("reason_codes"), list)
                        or any(not isinstance(code, str) or not code for code in decision["reason_codes"])
                        or type(decision.get("initial_inventory")) is not bool
                        or _parse(decision.get("first_seen_at")) is None
                        or not isinstance(decision.get("tabs"), list)
                        or any(tab not in {"videos", "streams"} for tab in decision["tabs"])
                        or decision.get("category") not in {"UNKNOWN", "REGULAR", "STREAM", "SHORTS"}
                        or decision.get("readiness") not in {"UNKNOWN", "PUBLIC_RELEASED", "UPCOMING", "LIVE", "COMPLETED_REPLAY"}):
                    raise ValueError("Invalid YouTube evaluation decision")
        for video_id, record in state["observations"].items():
            if (not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id)
                    or not isinstance(record, dict) or _parse(record.get("first_seen_at")) is None
                    or type(record.get("initial_inventory")) is not bool):
                raise ValueError("Invalid YouTube observation state")
        last_seq, last_at = 0, None
        for capture in state["captures"]:
            at = _parse(capture.get("observed_at")) if isinstance(capture, dict) else None
            seq = capture.get("seq") if isinstance(capture, dict) else None
            if (type(seq) is not int or seq <= last_seq or _parse(capture.get("observed_at")) is None
                    or type(capture.get("healthy")) is not bool
                    or not isinstance(capture.get("observations"), dict)):
                raise ValueError("Invalid YouTube capture state")
            if last_at is not None and at <= last_at:
                raise ValueError("Non-monotonic YouTube capture state")
            last_seq, last_at = seq, at
            for video_id, item in capture["observations"].items():
                if (not _VIDEO_ID.fullmatch(video_id) or not isinstance(item, dict)
                        or not isinstance(item.get("tabs"), list)
                        or any(tab not in {"videos", "streams"} for tab in item["tabs"])
                        or not isinstance(item.get("title"), str)
                        or item.get("url") != "https://www.youtube.com/watch?v=" + video_id):
                    raise ValueError("Invalid YouTube capture observation")
                metadata = item.get("metadata_by_tab")
                if metadata is not None and (not isinstance(metadata, dict)
                        or any(tab not in item["tabs"] for tab in metadata)
                        or any(not isinstance(value, dict)
                            or set(value) != {"relative_text", "badges", "schedule_timezone"}
                            or value["schedule_timezone"] is not None
                            or any(not isinstance(value.get(key), list)
                                or any(not isinstance(text, str) or not text.strip() for text in value[key])
                                for key in ("relative_text", "badges")) for value in metadata.values())):
                    raise ValueError("Invalid YouTube source metadata")
                if "title_conflict" in item and type(item["title_conflict"]) is not bool:
                    raise ValueError("Invalid YouTube title conflict marker")
        if state["capture_seq"] < last_seq:
            raise ValueError("Invalid YouTube capture sequence")
        for event_key, record in state["outbox"].items():
            if (not isinstance(event_key, str) or not isinstance(record, dict)
                    or record.get("event_key") != event_key
                    or record.get("status") not in {"reserved", "sending", "delivered", "ambiguous"}
                    or not isinstance(record.get("video_id"), str)
                    or not _VIDEO_ID.fullmatch(record["video_id"])
                    or not isinstance(record.get("item"), dict)):
                raise ValueError("Invalid YouTube outbox state")

    def set_commit_hook(self, hook):
        if hook is not None and not callable(hook):
            raise TypeError("Commit hook must be callable")
        self._commit_hook = hook

    def _write_local(self):
        raw = _canonical(self._state).encode("utf-8")
        if len(raw) > _MAX_STATE_BYTES:
            raise ValueError("YouTube JSON state exceeds GitHub Contents API limit")
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _commit(self, mutation):
        with self._lock:
            previous = deepcopy(self._state)
            mutation(self._state)
            try:
                self._validate_state(self._state)
                self._write_local()
                if self._commit_hook is not None:
                    self._commit_hook(self.path)
            except Exception:
                self._state = previous
                self._write_local()
                raise

    @staticmethod
    def _quarantine_for_new_epoch(state):
        state["initialized"] = False
        state["needs_inventory"] = True
        for record in state["observations"].values():
            record["initial_inventory"] = True

    def flush(self):
        """Checkpoint the whole JSON state before the first source observation."""
        self._validate_state(self._state)
        self._write_local()
        if self._commit_hook is not None:
            self._commit_hook(self.path)

    def record_capture(self, observations, observed_at, *, healthy):
        stamp = _iso(observed_at)
        if type(healthy) is not bool or not isinstance(observations, (list, tuple)):
            raise ValueError("Invalid capture")
        normalized = {}
        if healthy:
            for item in observations:
                if not isinstance(item, dict):
                    raise ValueError("Invalid observation")
                video_id, tabs = item.get("video_id"), item.get("source_tabs")
                title, url = item.get("title"), item.get("url")
                if (not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id)
                        or video_id in normalized or not isinstance(tabs, (list, tuple)) or not tabs
                        or any(tab not in {"videos", "streams"} for tab in tabs)
                        or not isinstance(title, str) or not title.strip()
                        or url != "https://www.youtube.com/watch?v=" + video_id):
                    raise ValueError("Invalid channel-tabs observation")
                normalized[video_id] = {"tabs": sorted(set(tabs)), "title": title, "url": url,
                    "metadata_by_tab": deepcopy(item.get("metadata_by_tab")),
                    "title_conflict": item.get("title_conflict", False)}
        current_at = _parse(stamp)
        previous_at = _parse(self._state.get("last_capture_at"))
        if previous_at is not None and current_at <= previous_at:
            raise ValueError("Non-monotonic capture time")
        def mutation(state):
            seq = state["capture_seq"] + 1
            frontier = not state["initialized"] or state["needs_inventory"]
            state["captures"].append({"seq": seq, "observed_at": stamp, "healthy": healthy,
                                      "observations": normalized})
            state["captures"] = state["captures"][-2:]
            if healthy:
                for video_id in normalized:
                    state["observations"].setdefault(video_id,
                        {"first_seen_at": stamp, "initial_inventory": bool(frontier)})
                state["initialized"] = True
                state["needs_inventory"] = False
            else:
                state["needs_inventory"] = True
            state["capture_seq"] = seq
            state["last_capture_at"] = stamp
        self._commit(mutation)
        return self._state["capture_seq"]

    def record_evaluation(self, evaluation):
        """CAS-checkpoint a compact, non-delivery result for the latest capture."""
        if not isinstance(evaluation, dict):
            raise ValueError("Invalid YouTube evaluation")
        record = deepcopy(evaluation)
        def mutation(state):
            if record.get("capture_seq") != state["capture_seq"]:
                raise ValueError("Evaluation does not match the latest capture")
            state["last_evaluation"] = record
        self._commit(mutation)

    def observation(self, video_id):
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            return None
        record = self._state["observations"].get(video_id)
        if record is None:
            return None
        captures = []
        for capture in self._state["captures"]:
            item = capture["observations"].get(video_id)
            captures.append({"seq": capture["seq"], "observed_at": capture["observed_at"],
                "healthy": capture["healthy"], "tabs": item["tabs"] if item else [],
                "title": item["title"] if item else None, "url": item["url"] if item else None,
                "metadata_by_tab": deepcopy(item.get("metadata_by_tab")) if item else None,
                "title_conflict": item.get("title_conflict", False) if item else False})
        last_visible = next((capture for capture in reversed(captures) if capture["tabs"]), None)
        return {"first_seen_at": record["first_seen_at"],
                "initial_inventory": record["initial_inventory"], "captures": captures,
                "video": ({"title": last_visible["title"], "url": last_visible["url"]}
                          if last_visible else None)}

    def observation_ids(self):
        return tuple(sorted(self._state["observations"]))

    def reserve(self, decision, item, *, now):
        """Persist an idempotent outbox reservation for an ELIGIBLE decision."""
        stamp = _iso(now)
        video_id, event_key = getattr(decision, "video_id", None), getattr(decision, "event_key", None)
        if (getattr(decision, "eligibility", None) is not e.Eligibility.ELIGIBLE
                or getattr(decision, "requires_atomic_reservation", False) is not True
                or not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id)
                or event_key != f"youtube:video:{video_id}:initial"
                or not isinstance(item, dict) or item.get("source") != "youtube"
                or item.get("video_id") != video_id
                or item.get("url") != "https://www.youtube.com/watch?v=" + video_id
                or item.get("documents") or item.get("delivery_format") != "youtube-tabs-preview-v1"):
            raise ValueError("Outbox reservation requires a valid ELIGIBLE tabs event")
        payload = _canonical(item)
        record = {"event_key": event_key, "video_id": video_id, "item": deepcopy(item),
                  "payload_json": payload,
                  "payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                  "status": "reserved", "attempts": 0, "created_at": stamp,
                  "updated_at": stamp, "owner_token": None, "lease_until": None,
                  "message_id": None, "last_error": None}
        result = {}
        def mutation(state):
            existing = state["outbox"].get(event_key)
            if existing is not None:
                if existing["payload_json"] != payload:
                    raise ValueError("Idempotency key payload conflict")
                result.update(created=False, status=existing["status"], event_key=event_key)
                return
            state["outbox"][event_key] = record
            result.update(created=True, status="reserved", event_key=event_key)
        with self._lock:
            self._commit(mutation)
        return result

    def outbox_record(self, event_key):
        record = self._state["outbox"].get(event_key)
        return deepcopy(record) if record is not None else None

    def send_once(self, event_key, sender, *, enabled=False, now=None):
        """Checkpoint the send claim first. An unknown result is never resent."""
        if enabled is not True:
            return "disabled"
        now = now or datetime.now(timezone.utc)
        stamp, token = _iso(now), uuid.uuid4().hex
        def claim(state):
            current = state["outbox"].get(event_key)
            if current is None or current["status"] != "reserved":
                raise ValueError("Outbox reservation changed")
            current.update(status="sending", attempts=1, updated_at=stamp,
                           owner_token=token, lease_until=lease_until)
        with self._lock:
            record = self._state["outbox"].get(event_key)
            if record is None:
                return "missing"
            if record["status"] != "reserved":
                return record["status"]
            lease_until = _iso(now + self.send_lease)
            self._commit(claim)  # remote CAS/checkpoint failure stops before sender invocation
        claimed = self._state["outbox"][event_key]
        payload = deepcopy(claimed["item"])
        ticket = DeliveryTicket(event_key, claimed["video_id"], token,
                                 claimed["payload_sha256"])
        confirmation, error = None, None
        try:
            confirmation = sender(payload, ticket)
        except Exception as exc:
            error = type(exc).__name__
        confirmed = type(confirmation) is SendConfirmation and confirmation.confirmed
        status = "delivered" if confirmed else "ambiguous"
        finished_at = _iso(datetime.now(timezone.utc))
        def finish(state):
            current = state["outbox"].get(event_key)
            if current is None or current["status"] != "sending" or current["owner_token"] != token:
                raise RuntimeError("Outbox send lease lost; result requires reconciliation")
            current.update(status=status, updated_at=finished_at, owner_token=None,
                lease_until=None, message_id=confirmation.message_id if confirmed else None,
                last_error=error or (None if confirmed else "UNCONFIRMED_SEND_RESULT"))
        self._commit(finish)
        return status

    def recover_expired_sends(self, *, now=None):
        """Mark expired in-flight calls ambiguous. Never requeue or resend them."""
        stamp, count = _iso(now or datetime.now(timezone.utc)), 0
        boundary = _parse(stamp)
        for record in self._state["outbox"].values():
            expiry = _parse(record.get("lease_until"))
            if record["status"] == "sending" and expiry is not None and expiry <= boundary:
                count += 1
        if not count:
            return 0
        def mutation(state):
            for record in state["outbox"].values():
                expiry = _parse(record.get("lease_until"))
                if record["status"] == "sending" and expiry is not None and expiry <= boundary:
                    record.update(status="ambiguous", owner_token=None,
                        last_error="SEND_OUTCOME_UNKNOWN_AFTER_RESTART", updated_at=stamp)
        self._commit(mutation)
        return count
