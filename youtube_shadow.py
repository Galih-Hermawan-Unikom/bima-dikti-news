"""Offline shadow state only. No network, notifier or production persistence."""
from copy import deepcopy
import hashlib
import json
import re

from reliability import SourceError
from youtube_history import parse_timestamp as publication_time
from youtube_videos_parser import VideosPage

MIN_STABLE_SECONDS = 60
FAILURE_CATEGORIES = {"PARSER_FAILURE", "HTTP_FAILURE", "TIMEOUT", "NETWORK_ERROR"}
VERIFICATION = {"BASELINE_KNOWN", "AWAITING_REVIEW", "NEW_CANDIDATE", "APPROVED"}


class ShadowError(SourceError):
    pass


def _require(condition, category):
    if not condition:
        raise ShadowError("YouTube shadow: " + category)


def _valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is not None


def _time(value):
    parsed = publication_time(value)
    _require(parsed is not None, "INVALID_OBSERVATION_TIME")
    return parsed.isoformat()


def _label(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value), "INVALID_REFERENCE")
    return value


def new_shadow(known_ids):
    ids = list(known_ids)
    _require(all(_valid_id(i) for i in ids), "INVALID_BASELINE")
    return {"version": 1, "mode": "shadow-only", "baseline_ids": sorted(set(ids)),
            "last_activity_at": None, "snapshots": [], "videos": {}, "reviews": []}


def validate_shadow(state):
    _require(isinstance(state, dict) and state.get("version") == 1
             and state.get("mode") == "shadow-only", "INVALID_STATE")
    baseline = state.get("baseline_ids")
    _require(isinstance(baseline, list) and all(_valid_id(i) for i in baseline)
             and len(set(baseline)) == len(baseline), "INVALID_BASELINE")
    _require(isinstance(state.get("videos"), dict) and isinstance(state.get("snapshots"), list)
             and isinstance(state.get("reviews"), list), "INVALID_STATE")
    activity = state.get("last_activity_at")
    _require(activity is not None or not (state["snapshots"] or state["videos"] or state["reviews"]),
             "INVALID_STATE")
    if activity is not None:
        _time(activity)
    snapshot_ids = set()
    for snapshot in state["snapshots"]:
        _require(isinstance(snapshot, dict), "INVALID_STATE")
        sid = _label(snapshot.get("snapshot_id"))
        _require(sid not in snapshot_ids and snapshot.get("health") in {"HEALTHY", "FAILED"}, "INVALID_STATE")
        _require(publication_time(_time(snapshot.get("observed_at"))) <= publication_time(activity),
                 "INVALID_STATE")
        _require(isinstance(snapshot.get("fingerprint"), str)
                 and re.fullmatch(r"[a-f0-9]{64}", snapshot["fingerprint"]), "INVALID_STATE")
        ids = snapshot.get("video_ids")
        _require(isinstance(ids, list) and all(_valid_id(i) for i in ids)
                 and len(ids) == len(set(ids)), "INVALID_STATE")
        _require((snapshot["health"] == "HEALTHY" and snapshot.get("error_category") is None)
                 or (snapshot["health"] == "FAILED" and not ids
                     and snapshot.get("error_category") in FAILURE_CATEGORIES), "INVALID_STATE")
        snapshot_ids.add(sid)
    for key, video in state["videos"].items():
        _require(_valid_id(key) and isinstance(video, dict) and video.get("video_id") == key, "INVALID_STATE")
        _require(isinstance(video.get("title"), str) and video["title"].strip(), "INVALID_STATE")
        _require(video.get("knowledge") == ("KNOWN" if key in baseline else "UNKNOWN"), "INVALID_STATE")
        _require(video.get("verification_status") in VERIFICATION, "INVALID_STATE")
        _require((key in baseline) == (video["verification_status"] == "BASELINE_KNOWN"), "INVALID_STATE")
        _require(type(video.get("observation_count")) is int and video["observation_count"] >= 1, "INVALID_STATE")
        _require(type(video.get("stable_observation_count")) is int
                 and 0 <= video["stable_observation_count"] <= video["observation_count"], "INVALID_STATE")
        _require(type(video.get("present_in_last_snapshot")) is bool, "INVALID_STATE")
        first, last = publication_time(_time(video.get("first_seen_at"))), publication_time(_time(video.get("last_seen_at")))
        _require(first <= last <= publication_time(activity), "INVALID_STATE")
        _require(isinstance(video.get("title_history"), list) and video["title_history"], "INVALID_STATE")
        sightings = [s for s in state["snapshots"] if s["health"] == "HEALTHY" and key in s["video_ids"]]
        _require(len(sightings) == video["observation_count"] and sightings
                 and video["first_seen_at"] == sightings[0]["observed_at"]
                 and video["last_seen_at"] == sightings[-1]["observed_at"], "INVALID_STATE")
        healthy = [s for s in state["snapshots"] if s["health"] == "HEALTHY"]
        _require(video["present_in_last_snapshot"] == (key in healthy[-1]["video_ids"]), "INVALID_STATE")
        if video["stable_observation_count"]:
            _require(video["present_in_last_snapshot"] and video.get("stable_since_at") is not None
                     and first <= publication_time(_time(video["stable_since_at"])) <= last, "INVALID_STATE")
        else:
            _require(not video["present_in_last_snapshot"] and video.get("stable_since_at") is None, "INVALID_STATE")
        _require(not {"published", "status", "delivered", "delivery_format", "event_keys"} & video.keys(),
                 "DELIVERY_FIELDS_FORBIDDEN")
    review_ids = set()
    for review in state["reviews"]:
        _require(isinstance(review, dict), "INVALID_STATE")
        rid = _label(review.get("review_id"))
        _require(rid not in review_ids and review.get("video_id") in state["videos"], "INVALID_STATE")
        _require(review.get("decision") in {"NEW_CANDIDATE", "APPROVED"}, "INVALID_STATE")
        _label(review.get("reviewer"))
        _label(review.get("history_review_reference"))
        _require(publication_time(_time(review.get("reviewed_at"))) <= publication_time(activity), "INVALID_STATE")
        review_ids.add(rid)
    for video in state["videos"].values():
        if video["verification_status"] in {"NEW_CANDIDATE", "APPROVED"}:
            _require(any(r["video_id"] == video["video_id"] and r.get("title") == video["title"]
                         and r["decision"] == video["verification_status"] for r in state["reviews"]),
                     "REVIEW_EVIDENCE_MISSING")
    return state


def classification(video):
    if video["knowledge"] == "KNOWN":
        return "KNOWN"
    return video["verification_status"] if video["verification_status"] in {"NEW_CANDIDATE", "APPROVED"} else "UNKNOWN"


def _items(page):
    _require(isinstance(page, VideosPage) and page.http_status == 200 and page.parsing_errors == 0,
             "INVALID_PARSER_RESULT")
    _require(isinstance(page.items, list) and bool(page.items) != bool(page.empty_verified),
             "UNVERIFIED_EMPTY_OR_CONTRADICTORY_RESULT")
    seen = {}
    for item in page.items:
        _require(isinstance(item, dict) and item.get("source") == "youtube"
                 and item.get("observation_format") == "youtube-videos-observation-v1"
                 and not {"published", "status", "delivery_format"} & item.keys(), "INVALID_OBSERVATION_CONTRACT")
        video_id, title = item.get("video_id"), item.get("title")
        _require(_valid_id(video_id) and isinstance(title, str) and title.strip(), "INVALID_OBSERVATION")
        title = " ".join(title.split())
        _require(video_id not in seen or seen[video_id]["title"] == title, "CONFLICTING_DUPLICATE")
        seen[video_id] = {"video_id": video_id, "title": title}
    return list(seen.values())


def observe_snapshot(state, *, snapshot_id, observed_at, page=None, error_category=None):
    """Pure transition; a failed parse records health but does not invent absences."""
    validate_shadow(state)
    snapshot_id, when = _label(snapshot_id), _time(observed_at)
    if error_category is not None:
        _require(isinstance(error_category, str) and error_category in FAILURE_CATEGORIES
                 and page is None, "INVALID_FAILURE")
        items = []
    else:
        items = _items(page)
    payload = {"observed_at": when, "error_category": error_category,
               "items": sorted(items, key=lambda i: i["video_id"])}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    for old in state["snapshots"]:
        if old["snapshot_id"] == snapshot_id:
            _require(old["fingerprint"] == fingerprint, "SNAPSHOT_ID_REUSED")
            return deepcopy(state)
    _require(state["last_activity_at"] is None
             or publication_time(when) >= publication_time(state["last_activity_at"]), "CLOCK_MOVED_BACKWARDS")
    result = deepcopy(state)
    result["snapshots"].append({"snapshot_id": snapshot_id, "observed_at": when,
                                "fingerprint": fingerprint, "health": "FAILED" if error_category else "HEALTHY",
                                "error_category": error_category, "video_ids": [i["video_id"] for i in items]})
    result["last_activity_at"] = when
    if error_category:
        return validate_shadow(result)
    visible = {i["video_id"] for i in items}
    for video_id, video in result["videos"].items():
        if video_id not in visible:
            video["present_in_last_snapshot"] = False
            video["stable_observation_count"], video["stable_since_at"] = 0, None
            if video["knowledge"] == "UNKNOWN":
                video["verification_status"] = "AWAITING_REVIEW"
    for item in items:
        video_id, title = item["video_id"], item["title"]
        video = result["videos"].get(video_id)
        if video is None:
            known = video_id in result["baseline_ids"]
            video = {"source": "youtube", "observation_format": "youtube-shadow-v1",
                     "video_id": video_id, "title": title, "first_seen_at": when,
                     "last_seen_at": when, "observation_count": 0,
                     "knowledge": "KNOWN" if known else "UNKNOWN",
                     "verification_status": "BASELINE_KNOWN" if known else "AWAITING_REVIEW",
                     "title_history": [{"title": title, "observed_at": when}],
                     "stable_observation_count": 0, "stable_since_at": when}
            result["videos"][video_id] = video
        changed = video["title"] != title
        if changed:
            video["title_history"].append({"title": title, "observed_at": when})
            if video["knowledge"] == "UNKNOWN":
                video["verification_status"] = "AWAITING_REVIEW"
        if changed or not video["stable_observation_count"]:
            video["stable_observation_count"], video["stable_since_at"] = 1, when
        else:
            video["stable_observation_count"] += 1
        video.update(title=title, last_seen_at=when, observation_count=video["observation_count"] + 1,
                     present_in_last_snapshot=True, last_seen_snapshot_id=snapshot_id)
    return validate_shadow(result)


def review_observation(state, *, video_id, decision, review_id, reviewer, history_review_reference, reviewed_at):
    """Explicit human shadow review only; APPROVED is never delivery permission."""
    validate_shadow(state)
    _require(_valid_id(video_id) and video_id in state["videos"] and isinstance(decision, str)
             and decision in {"NEW_CANDIDATE", "APPROVED"}, "INVALID_REVIEW")
    review = {"video_id": video_id, "decision": decision, "review_id": _label(review_id),
              "reviewer": _label(reviewer), "history_review_reference": _label(history_review_reference),
              "reviewed_at": _time(reviewed_at), "title": state["videos"][video_id]["title"]}
    for old in state["reviews"]:
        if old["review_id"] == review["review_id"]:
            _require(old == review, "REVIEW_ID_REUSED")
            return deepcopy(state)
    video = state["videos"][video_id]
    _require(video["knowledge"] == "UNKNOWN", "HISTORICAL_ID_NOT_CANDIDATE")
    _require(state["snapshots"] and state["snapshots"][-1]["health"] == "HEALTHY"
             and video["present_in_last_snapshot"], "LATEST_OBSERVATION_NOT_HEALTHY_OR_PRESENT")
    _require(publication_time(review["reviewed_at"]) >= publication_time(state["last_activity_at"]),
             "CLOCK_MOVED_BACKWARDS")
    _require(video["stable_observation_count"] >= 2 and video["stable_since_at"] is not None
             and (publication_time(video["last_seen_at"]) - publication_time(video["stable_since_at"])).total_seconds()
             >= MIN_STABLE_SECONDS, "INSUFFICIENT_STABLE_OBSERVATIONS")
    expected = "AWAITING_REVIEW" if decision == "NEW_CANDIDATE" else "NEW_CANDIDATE"
    _require(video["verification_status"] == expected, "REVIEW_SEQUENCE_INVALID")
    result = deepcopy(state)
    result["videos"][video_id]["verification_status"] = decision
    result["reviews"].append(review)
    result["last_activity_at"] = review["reviewed_at"]
    return validate_shadow(result)
