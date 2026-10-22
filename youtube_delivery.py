"""Persistent observation and at-most-once YouTube outbox state.

This SQLite database is separate from historical cache and delivery_state.json.
Every mutation uses a SQLite write transaction; no automatic resend is allowed.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

import youtube_eligibility as e


SCHEMA_VERSION = 1
DEFAULT_SEND_LEASE = timedelta(minutes=3)
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


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
    """Atomic first-seen history, capture memberships, and one-shot outbox."""

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def set_commit_hook(self, hook):
        if hook is not None and not callable(hook):
            raise TypeError("Commit hook must be callable")
        self._commit_hook = hook

    def flush(self):
        """Durably checkpoint the initialized database before any observation."""
        if self._commit_hook is not None:
            self._commit_hook(self.path)

    def _checkpoint_after_commit(self):
        if self._commit_hook is not None:
            self._commit_hook(self.path)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("CREATE TABLE captures (seq INTEGER PRIMARY KEY, observed_at TEXT, healthy INTEGER NOT NULL CHECK(healthy IN (0,1)))")
                conn.execute("CREATE TABLE observations (video_id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, initial_inventory INTEGER NOT NULL CHECK(initial_inventory IN (0,1)))")
                conn.execute("CREATE TABLE memberships (seq INTEGER NOT NULL REFERENCES captures(seq), video_id TEXT NOT NULL, tabs_json TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY(seq, video_id))")
                conn.execute("CREATE INDEX memberships_video_seq ON memberships(video_id, seq DESC)")
                conn.execute("CREATE TABLE outbox (event_key TEXT PRIMARY KEY, video_id TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('reserved','sending','delivered','ambiguous')), attempts INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, owner_token TEXT, lease_until TEXT, message_id INTEGER, last_error TEXT)")
                values = {
                    "schema_version": str(SCHEMA_VERSION),
                    "activation_at": self.activation_at,
                    "policy_version": self.policy_version,
                    "capture_seq": "0",
                    "last_capture_at": "",
                    "initialized": "0",
                    "needs_inventory": "1",
                }
                conn.executemany("INSERT INTO meta(key,value) VALUES (?,?)", values.items())
            elif tables != {"meta", "captures", "observations", "memberships", "outbox"}:
                raise ValueError("Unrecognized YouTube state database; refusing migration")
            meta = dict(conn.execute("SELECT key,value FROM meta"))
            if meta.get("schema_version") != str(SCHEMA_VERSION):
                raise ValueError("Unsupported YouTube state schema; refusing migration")
            if meta.get("activation_at") != self.activation_at or meta.get("policy_version") != self.policy_version:
                # A policy/fence change is a new observation epoch. Keep original
                # first_seen values, quarantine all prior identities, and require
                # a fresh inventory before later arrivals can qualify.
                conn.execute("UPDATE observations SET initial_inventory=1")
                conn.execute("UPDATE meta SET value='1' WHERE key IN ('initialized','needs_inventory')")
                conn.execute("UPDATE meta SET value=? WHERE key='activation_at'", (self.activation_at,))
                conn.execute("UPDATE meta SET value=? WHERE key='policy_version'", (self.policy_version,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _meta(self, conn):
        return dict(conn.execute("SELECT key,value FROM meta"))

    def record_capture(self, observations, observed_at, *, healthy):
        """Persist one source cycle. Failed cycles break stability but add no IDs."""
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
                        or video_id in normalized or not isinstance(tabs, (list, tuple))
                        or not tabs or any(t not in {"videos", "streams"} for t in tabs)
                        or not isinstance(title, str) or not title.strip()
                        or url != "https://www.youtube.com/watch?v=" + video_id):
                    raise ValueError("Invalid channel-tabs observation")
                normalized[video_id] = (sorted(set(tabs)), title, url)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            meta = self._meta(conn)
            if (meta.get("schema_version") != str(SCHEMA_VERSION)
                    or meta.get("activation_at") != self.activation_at
                    or meta.get("policy_version") != self.policy_version):
                raise ValueError("YouTube state authority changed")
            last_at = _parse(meta.get("last_capture_at"))
            current_at = _parse(stamp)
            if last_at is not None and current_at <= last_at:
                raise ValueError("Non-monotonic capture time")
            seq = int(meta.get("capture_seq", "0")) + 1
            conn.execute("INSERT INTO captures(seq,observed_at,healthy) VALUES (?,?,?)",
                         (seq, stamp, int(healthy)))
            frontier = meta.get("initialized") != "1" or meta.get("needs_inventory") == "1"
            if healthy:
                for video_id, (tabs, title, url) in normalized.items():
                    exists = conn.execute("SELECT 1 FROM observations WHERE video_id=?", (video_id,)).fetchone()
                    if not exists:
                        conn.execute("INSERT INTO observations(video_id,first_seen_at,initial_inventory) VALUES (?,?,?)",
                                     (video_id, stamp, int(frontier)))
                    conn.execute("INSERT INTO memberships(seq,video_id,tabs_json,title,url) VALUES (?,?,?,?,?)",
                                 (seq, video_id, json.dumps(tabs, separators=(",", ":")), title, url))
                conn.execute("UPDATE meta SET value='1' WHERE key='initialized'")
                conn.execute("UPDATE meta SET value='0' WHERE key='needs_inventory'")
            else:
                conn.execute("UPDATE meta SET value='1' WHERE key='needs_inventory'")
            conn.execute("UPDATE meta SET value=? WHERE key='capture_seq'", (str(seq),))
            conn.execute("UPDATE meta SET value=? WHERE key='last_capture_at'", (stamp,))
            conn.commit()
            self._checkpoint_after_commit()
            return seq
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def observation(self, video_id):
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            return None
        conn = self._connect()
        try:
            row = conn.execute("SELECT first_seen_at,initial_inventory FROM observations WHERE video_id=?",
                               (video_id,)).fetchone()
            if row is None:
                return None
            captures = conn.execute("""
                SELECT c.seq,c.observed_at,c.healthy,m.tabs_json,m.title,m.url
                FROM captures c LEFT JOIN memberships m ON m.seq=c.seq AND m.video_id=?
                ORDER BY c.seq DESC LIMIT 2
            """, (video_id,)).fetchall()
            packed = []
            for capture in reversed(captures):
                packed.append({"seq": capture["seq"], "observed_at": capture["observed_at"],
                    "healthy": bool(capture["healthy"]),
                    "tabs": json.loads(capture["tabs_json"]) if capture["tabs_json"] else [],
                    "title": capture["title"], "url": capture["url"]})
            last_visible = next((c for c in reversed(packed) if c["tabs"]), None)
            return {"first_seen_at": row["first_seen_at"],
                    "initial_inventory": bool(row["initial_inventory"]),
                    "captures": packed,
                    "video": ({"title": last_visible["title"], "url": last_visible["url"]}
                              if last_visible else None)}
        finally:
            conn.close()

    def observation_ids(self):
        conn = self._connect()
        try:
            return tuple(r[0] for r in conn.execute("SELECT video_id FROM observations ORDER BY video_id"))
        finally:
            conn.close()

    def reserve(self, decision, item, *, now):
        """Insert one outbox event only for an ELIGIBLE classifier decision."""
        stamp = _iso(now)
        video_id = getattr(decision, "video_id", None)
        event_key = getattr(decision, "event_key", None)
        if (getattr(decision, "eligibility", None) is not e.Eligibility.ELIGIBLE
                or getattr(decision, "requires_atomic_reservation", False) is not True
                or not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id)
                or event_key != f"youtube:video:{video_id}:initial"
                or not isinstance(item, dict) or item.get("source") != "youtube"
                or item.get("video_id") != video_id or item.get("url") != "https://www.youtube.com/watch?v=" + video_id
                or item.get("documents") or item.get("delivery_format") != "youtube-tabs-preview-v1"):
            raise ValueError("Outbox reservation requires a valid ELIGIBLE tabs event")
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload_json,status FROM outbox WHERE event_key=?", (event_key,)).fetchone()
            if row is None:
                conn.execute("""INSERT INTO outbox(event_key,video_id,payload_json,status,attempts,created_at,updated_at)
                    VALUES (?,?,?,'reserved',0,?,?)""", (event_key, video_id, payload, stamp, stamp))
                result = {"created": True, "status": "reserved", "event_key": event_key}
            else:
                if row["payload_json"] != payload:
                    raise ValueError("Idempotency key payload conflict")
                result = {"created": False, "status": row["status"], "event_key": event_key}
            conn.commit()
            self._checkpoint_after_commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def outbox_record(self, event_key):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM outbox WHERE event_key=?", (event_key,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["item"] = json.loads(value.pop("payload_json"))
            return value
        finally:
            conn.close()

    def send_once(self, event_key, sender, *, enabled=False, now=None):
        """Call sender at most once. Unknown/negative outcomes are terminal ambiguous."""
        if enabled is not True:
            return "disabled"
        now = now or datetime.now(timezone.utc)
        stamp = _iso(now)
        token = uuid.uuid4().hex
        lease_until = _iso(now + self.send_lease)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,payload_json FROM outbox WHERE event_key=?", (event_key,)).fetchone()
            if row is None:
                conn.rollback()
                return "missing"
            if row["status"] != "reserved":
                status = row["status"]
                conn.rollback()
                return status
            changed = conn.execute("""UPDATE outbox SET status='sending',attempts=1,updated_at=?,
                owner_token=?,lease_until=? WHERE event_key=? AND status='reserved'""",
                (stamp, token, lease_until, event_key)).rowcount
            if changed != 1:
                conn.rollback()
                return "reserved"
            payload = json.loads(row["payload_json"])
            ticket = DeliveryTicket(event_key, payload["video_id"], token,
                hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest())
            conn.commit()
            self._checkpoint_after_commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        confirmation = None
        error = None
        try:
            confirmation = sender(payload, ticket)
        except Exception as exc:
            error = type(exc).__name__
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,owner_token FROM outbox WHERE event_key=?", (event_key,)).fetchone()
            if row is None or row["status"] != "sending" or row["owner_token"] != token:
                raise RuntimeError("Outbox send lease lost; result requires reconciliation")
            confirmed = type(confirmation) is SendConfirmation and confirmation.confirmed
            status = "delivered" if confirmed else "ambiguous"
            message_id = confirmation.message_id if confirmed else None
            conn.execute("""UPDATE outbox SET status=?,updated_at=?,owner_token=NULL,lease_until=NULL,
                message_id=?,last_error=? WHERE event_key=? AND owner_token=?""",
                (status, _iso(datetime.now(timezone.utc)), message_id, error or (None if confirmed else "UNCONFIRMED_SEND_RESULT"), event_key, token))
            conn.commit()
            self._checkpoint_after_commit()
            return status
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def recover_expired_sends(self, *, now=None):
        """Mark expired in-flight calls ambiguous. Never requeue or resend them."""
        stamp = _iso(now or datetime.now(timezone.utc))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""UPDATE outbox SET status='ambiguous',owner_token=NULL,
                last_error='SEND_OUTCOME_UNKNOWN_AFTER_RESTART',updated_at=?
                WHERE status='sending' AND lease_until<=?""", (stamp, stamp)).rowcount
            conn.commit()
            self._checkpoint_after_commit()
            return changed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
