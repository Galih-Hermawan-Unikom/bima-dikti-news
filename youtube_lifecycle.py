"""Fail-closed stream lifecycle; public watch evidence, never card-age inference."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import re

import requests

import youtube_eligibility as e
from youtube_channel_tabs import CHANNEL_ID
from youtube_history import parse_timestamp
from youtube_tabs_monitor import (ChannelTabsAdapter, TabsEvaluation, _LIVE_MARKERS,
                                  _UPCOMING_MARKERS, _REPLAY_MARKERS)

VERSION = 1
MAX_GAP = timedelta(hours=2)
FRESHNESS = timedelta(minutes=2)
WINDOW = timedelta(hours=24)
PHASES = {"UNKNOWN", "UPCOMING", "LIVE", "COMPLETED_REPLAY"}


def card_consistent(capture, phase):
    meta = (capture.get("metadata_by_tab") or {}).get("streams", {})
    values = {v.strip().casefold() for k in ("badges", "relative_text") for v in meta.get(k, [])}
    if values & {"short", "shorts"}:
        return False
    marked = {p for p, markers in (("LIVE", _LIVE_MARKERS), ("UPCOMING", _UPCOMING_MARKERS),
              ("COMPLETED_REPLAY", _REPLAY_MARKERS)) if values & markers}
    return not marked or marked == {phase}


def player_evidence(player, video_id, at):
    """Sanitize publisher evidence. No playback URLs or raw HTML enter state."""
    result = {"phase": "UNKNOWN", "observed_at": at.isoformat(),
              "actual_end_at": None, "public_playable": False,
              "reference": f"https://www.youtube.com/watch?v={video_id}"}
    try:
        details = player["videoDetails"]
        micro = player["microformat"]["playerMicroformatRenderer"]
        live = micro.get("liveBroadcastDetails", {})
        end = parse_timestamp(live.get("endTimestamp"))
        start = parse_timestamp(live.get("startTimestamp"))
        status = player["playabilityStatus"]["status"]
        if (details.get("videoId") != video_id or details.get("channelId") != CHANNEL_ID
                or details.get("isPrivate") is not False or micro.get("isUnlisted") is not False
                or details.get("isLiveContent") is not True):
            return result
        # Upcoming may be unplayable, but must have an explicit publisher marker.
        if details.get("isUpcoming") is True:
            if live.get("isLiveNow") is not True and end is None:
                result["phase"] = "UPCOMING"
            return result
        streaming = player.get("streamingData", {})
        playable = status == "OK" and bool(streaming.get("formats") or streaming.get("adaptiveFormats"))
        if not playable or start is None or start > at:
            return result
        result["public_playable"] = True
        if "isLive" in details and type(details["isLive"]) is not bool:
            return result
        if (live.get("isLiveNow") is True and end is None and details.get("isLive") is not False
                and live.get("endTimestamp") is None):
            result["phase"] = "LIVE"
        elif (live.get("isLiveNow") is False and details.get("isLive") is not True
                and (end is not None or details.get("isLive") is False)):
            # Completion without endTimestamp can only be admitted via continuity.
            if live.get("endTimestamp") is not None and (end is None or not start <= end <= at):
                return result
            result.update(phase="COMPLETED_REPLAY", actual_end_at=end.isoformat() if end else None)
        return result
    except (KeyError, TypeError, AttributeError, ValueError):
        return result


class PublicPlayerReader:
    """Anonymous GET, fixed watch host, no redirects, bounded request, no retries."""
    def fetch(self, video_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Invalid video identity")
        response = requests.get(f"https://www.youtube.com/watch?v={video_id}",
                                timeout=15, allow_redirects=False,
                                headers={"User-Agent": "Mozilla/5.0", "Cache-Control": "no-cache"})
        try:
            if response.status_code != 200 or len(response.content) > 8_000_000:
                raise ValueError("Public player unavailable")
            matches = list(re.finditer(r"(?:var\s+)?ytInitialPlayerResponse\s*=\s*", response.text))
            if len(matches) != 1:
                raise ValueError("Public player structure changed")
            player, _ = json.JSONDecoder().raw_decode(response.text[matches[0].end():])
            return player
        finally:
            response.close()


def validate_lifecycle(value):
    if (not isinstance(value, dict) or set(value) != {"version", "activation_at", "suppressed_ids", "stream_ids", "records"}
            or value["version"] != VERSION or parse_timestamp(value["activation_at"]) is None
            or not isinstance(value["suppressed_ids"], list)
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for v in value["suppressed_ids"])
            or not isinstance(value["stream_ids"], list)
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for v in value["stream_ids"])
            or not isinstance(value["records"], dict)):
        raise ValueError("Invalid lifecycle state")
    if (len(set(value["suppressed_ids"])) != len(value["suppressed_ids"])
            or len(set(value["stream_ids"])) != len(value["stream_ids"])
            or set(value["records"]) - set(value["stream_ids"])):
        raise ValueError("Invalid lifecycle identity index")
    for vid, row in value["records"].items():
        if (not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) or not isinstance(row, dict)
                or set(row) != {"latest", "origins", "replay_admitted", "first_verified_replay_at", "actual_end_at", "capture_seq"}
                or type(row["replay_admitted"]) is not bool
                or type(row["capture_seq"]) is not int or row["capture_seq"] < 1
                or not isinstance(row["origins"], dict)
                or any(p not in {"LIVE", "COMPLETED_REPLAY"} or parse_timestamp(t) is None for p, t in row["origins"].items())
                or (row["first_verified_replay_at"] is not None and parse_timestamp(row["first_verified_replay_at"]) is None)):
            raise ValueError("Invalid lifecycle record")
        if row["actual_end_at"] is not None and parse_timestamp(row["actual_end_at"]) is None:
            raise ValueError("Invalid publisher end")
        latest = row["latest"]
        if (not isinstance(latest, dict) or set(latest) != {"phase", "observed_at", "actual_end_at", "public_playable", "reference"}
                or latest["phase"] not in PHASES or parse_timestamp(latest["observed_at"]) is None
                or type(latest["public_playable"]) is not bool
                or latest["reference"] != f"https://www.youtube.com/watch?v={vid}"
                or (latest["actual_end_at"] is not None and parse_timestamp(latest["actual_end_at"]) is None)):
            raise ValueError("Invalid lifecycle evidence")
        observed = parse_timestamp(latest["observed_at"])
        end = parse_timestamp(latest["actual_end_at"])
        first_replay = parse_timestamp(row["first_verified_replay_at"])
        if ((end is not None and end > observed)
                or (row["actual_end_at"] is not None and (first_replay is None
                    or parse_timestamp(row["actual_end_at"]) > first_replay))
                or any(parse_timestamp(t) > observed for t in row["origins"].values())
                or (first_replay is not None and (first_replay > observed
                    or "COMPLETED_REPLAY" not in row["origins"]
                    or parse_timestamp(row["origins"]["COMPLETED_REPLAY"]) > first_replay))):
            raise ValueError("Invalid lifecycle chronology")


def migrate(store, fence, at):
    """Add one immutable epoch; preserve all legacy observation and delivery fields."""
    existing = store._state.get("lifecycle")
    if existing is not None:
        validate_lifecycle(existing)
        if parse_timestamp(existing["activation_at"]) != fence:
            raise ValueError("Lifecycle activation fence mismatch")
        return
    if fence is None or fence < parse_timestamp(store._state["activation_at"]) or at < fence:
        raise ValueError("Lifecycle activation fence invalid or not reached")
    def mutation(state):
        state["lifecycle"] = {"version": VERSION, "activation_at": fence.isoformat(),
            "suppressed_ids": sorted(state["observations"]), "records": {},
            "stream_ids": sorted({vid for c in state["captures"] for vid, item in c["observations"].items()
                                  if "streams" in item["tabs"]})}
    store._commit(mutation)


def advance(store, vid, evidence, *, healthy, interval):
    """Persist first phase times once. A replay discovered across a gap stays held."""
    state = store._state
    captures = state["captures"]
    at = parse_timestamp(evidence["observed_at"])
    previous = state["lifecycle"]["records"].get(vid)
    seq = state["capture_seq"]
    continuous = (healthy and len(captures) == 2 and all(c["healthy"] for c in captures)
        and interval <= at - parse_timestamp(captures[0]["observed_at"]) <= MAX_GAP)
    prior = previous["latest"] if previous and previous["capture_seq"] == seq - 1 else None
    phase = evidence["phase"]
    def mutation(current):
        if vid not in current["lifecycle"]["stream_ids"]:
            current["lifecycle"]["stream_ids"].append(vid)
        row = current["lifecycle"]["records"].setdefault(vid, {"latest": evidence,
            "origins": {}, "replay_admitted": False, "first_verified_replay_at": None,
            "actual_end_at": None, "capture_seq": seq})
        if phase == "LIVE":
            row["origins"].setdefault(phase, evidence["observed_at"])
        if phase == "COMPLETED_REPLAY" and row["first_verified_replay_at"] is None:
            row["first_verified_replay_at"] = evidence["observed_at"]
            end = parse_timestamp(evidence["actual_end_at"])
            fence = parse_timestamp(current["lifecycle"]["activation_at"])
            transition = prior and prior["phase"] in {"UPCOMING", "LIVE"}
            recent_end = (end is not None and (prior is None or prior["phase"] in {"UPCOMING", "LIVE"})
                and max(fence, parse_timestamp(captures[0]["observed_at"])) < end <= at)
            row["replay_admitted"] = bool(continuous and (recent_end or transition)
                and (end is None or end > fence))
            row["origins"][phase] = evidence["actual_end_at"] or evidence["observed_at"]
        if phase == "COMPLETED_REPLAY" and row["actual_end_at"] is None:
            row["actual_end_at"] = evidence["actual_end_at"]
        row.update(latest=deepcopy(evidence), capture_seq=seq)
    store._commit(mutation)
    return prior, continuous


class LifecycleTabsAdapter(ChannelTabsAdapter):
    """Reuse discovery/history; override stream admission with publisher lifecycle."""
    def __init__(self, *, lifecycle_fence=None, player_reader=None, **kwargs):
        super().__init__(**kwargs)
        self.fence = lifecycle_fence
        self.player_reader = player_reader or PublicPlayerReader()

    def _player(self, vid):
        try:
            player = self.player_reader.fetch(vid)
            return player_evidence(player, vid, self.clock())
        except Exception:
            return player_evidence({}, vid, self.clock())

    def evaluate(self, cache, journal):
        store = self.observation_store
        # Migration precedes collection so every pre-existing identity is fenced.
        if self.fence is not None:
            migrate(store, self.fence, self.clock())
        base = super().evaluate(cache, journal)
        if base.errors:
            return base
        history = self._history(cache, journal)
        decisions, previews, evidence_rows = list(base.decisions), list(base.preview_items), list(base.evidence)
        for index, decision in enumerate(decisions):
            vid = decision.video_id
            persistent = store.observation(vid)
            if persistent is None:
                continue
            captures = persistent["captures"]
            # A stream remains a stream if it disappears/moves to another tab.
            tracked = persistent["seen_on_streams"] or vid in store._state.get("lifecycle", {}).get("stream_ids", [])
            is_stream = tracked or any("streams" in c["tabs"] for c in captures)
            if not is_stream:
                continue
            previews = [p for p in previews if p["video_id"] != vid]
            key, reason = f"youtube:video:{vid}:initial", "LIFECYCLE_ACTIVATION_REQUIRED"
            d = replace(decision, eligibility=e.Eligibility.HOLD_FOR_REVIEW,
                        requires_atomic_reservation=False, reason_codes=(reason,))
            if self.fence is not None:
                lifecycle = store._state["lifecycle"]
                if (vid in lifecycle["suppressed_ids"] or vid in history.baseline_ids | history.cache_ids | history.journal_ids
                        or store.outbox_record(f"youtube:video:{vid}:initial") is not None):
                    decisions[index] = replace(d, reason_codes=("LEGACY_OR_CUTOVER_IDENTITY_NO_BACKFILL",))
                    continue  # no publisher requests or new per-video checkpoints for fenced history
                replay_key = f"youtube:video:{vid}:replay"
                replay_outbox = store.outbox_record(replay_key)
                if replay_outbox is not None:
                    delivered = replay_outbox["status"] == "delivered"
                    decisions[index] = replace(d, event_key=replay_key,
                        eligibility=e.Eligibility.ALREADY_DELIVERED if delivered else e.Eligibility.HOLD_FOR_REVIEW,
                        delivery_state=e.DeliveryState.DELIVERED if delivered else e.DeliveryState.PENDING,
                        reason_codes=("CONFIRMED_REPLAY_EVENT" if delivered else "REPLAY_REQUIRES_OPERATOR_RECONCILIATION",))
                    continue
                latest = self._player(vid) if captures[-1]["tabs"] == ["streams"] else player_evidence({}, vid, self.clock())
                previous_record = lifecycle["records"].get(vid, {})
                reversed_completion = (previous_record.get("first_verified_replay_at") is not None
                                       and latest["phase"] in {"LIVE", "UPCOMING"})
                recorded_end = previous_record.get("actual_end_at")
                changed_end = (latest["phase"] == "COMPLETED_REPLAY" and (
                    (recorded_end is not None and latest["actual_end_at"] != recorded_end)
                    or (latest["actual_end_at"] is not None and previous_record.get("first_verified_replay_at") is not None
                        and parse_timestamp(latest["actual_end_at"]) > parse_timestamp(previous_record["first_verified_replay_at"]))))
                if reversed_completion or changed_end or not card_consistent(captures[-1], latest["phase"]):
                    latest.update(phase="UNKNOWN", public_playable=False, actual_end_at=None)
                prior, continuous = advance(store, vid, latest, healthy=True, interval=self.policy.capture_interval)
                row = store._state["lifecycle"]["records"][vid]
                phase = latest["phase"]
                suffix = {"LIVE": "live", "COMPLETED_REPLAY": "replay"}.get(phase, "initial")
                key = f"youtube:video:{vid}:{suffix}"
                stable = (prior and prior["phase"] == phase and continuous
                    and all(c["tabs"] == ["streams"] and not c["title_conflict"] for c in captures)
                    and all(card_consistent(c, phase) for c in captures)
                    and captures[0]["title"] == captures[-1]["title"]
                    and captures[0]["url"] == captures[-1]["url"])
                reason = ("LIFECYCLE_PHASE_UNVERIFIED" if phase not in {"LIVE", "COMPLETED_REPLAY"} else
                    "LIFECYCLE_CHRONOLOGY_UNVERIFIED" if not stable else
                    "REPLAY_DISCOVERED_WITHOUT_CONTINUITY" if phase == "COMPLETED_REPLAY" and not row["replay_admitted"] else None)
                if reason is None:
                    at = parse_timestamp(latest["observed_at"])
                    proof = e.Proof(vid, self.policy.channel_id, latest["reference"], at, self.policy.scope, True)
                    tab_captures = tuple(e.TabCapture(e.Proof(vid, self.policy.channel_id,
                        f"persistent-tabs:{c['seq']}", parse_timestamp(c["observed_at"]), self.policy.scope, True),
                        frozenset(c["tabs"]), c["healthy"], c["healthy"],
                        title_conflict=c["title_conflict"]) for c in captures)
                    outbox = store.outbox_record(key)
                    j = e.JournalEvidence(scope=self.policy.scope)
                    if outbox:
                        j = e.JournalEvidence(e.DeliveryState.DELIVERED if outbox["status"] == "delivered" else e.DeliveryState.PENDING,
                            key, outbox["status"] == "delivered", outbox["status"] == "ambiguous", self.policy.scope)
                    inp = e.EligibilityInput(vid, history, e.TimeEvidence(self.clock(),
                        parse_timestamp(persistent["first_seen_at"]), True, persistent["initial_inventory"],
                        parse_timestamp(row["origins"].get(phase))),
                        replace(self.policy, lifecycle_activation_at=self.fence, window=WINDOW),
                        e.VideoEvidence(proof, persistent["video"]["title"], persistent["video"]["url"]),
                        tab_captures, j,
                        e.CategoryEvidence(e.Category.STREAM, e.CategoryMethod.TAB_SURROGATE, proof),
                        e.ReleaseEvidence(e.Readiness(phase), proof, parse_timestamp(latest["actual_end_at"]),
                            phase == "COMPLETED_REPLAY", latest["public_playable"]))
                    d = e.classify(inp)
                else:
                    d = replace(d, event_key=key, reason_codes=(reason,))
                evidence_rows[index] = {**evidence_rows[index], "readiness": phase,
                    "actual_end_at": latest["actual_end_at"], "first_verified_replay_at": row["first_verified_replay_at"]}
                if d.eligibility is e.Eligibility.ELIGIBLE:
                    previews.append({"source": "youtube", "kind": "video", "video_id": vid,
                        **persistent["video"], "first_seen_at": persistent["first_seen_at"],
                        "delivery_format": "youtube-tabs-preview-v1", "lifecycle_phase": suffix,
                        "lifecycle_activation_at": self.fence.isoformat()})
            decisions[index] = d
        return TabsEvaluation(tuple(decisions), tuple(previews), base.health, base.errors, tuple(evidence_rows))

    def revalidate(self, item):
        """Fresh anonymous playback check before reservation AND after durable claim."""
        latest = self._player(item["video_id"])
        expected = {"live": "LIVE", "replay": "COMPLETED_REPLAY"}.get(item.get("lifecycle_phase"))
        age = self.clock() - parse_timestamp(latest["observed_at"])
        recorded = self.observation_store._state.get("lifecycle", {}).get("records", {}).get(item["video_id"], {}).get("latest", {})
        end, previous_end = parse_timestamp(latest["actual_end_at"]), parse_timestamp(recorded.get("actual_end_at"))
        consistent_end = ((end is None and previous_end is None)
            or (end is not None and self.fence is not None and end > self.fence
                and (previous_end is None or previous_end == end)))
        return (self.fence is not None and latest["phase"] == expected and latest["public_playable"]
                and consistent_end
                and timedelta(0) <= age <= FRESHNESS)
