"""Single-source channel-tabs discovery. No publication inference or state writer."""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

from youtube_channel_tabs import YouTubeChannelTabsReader, read_channel_tabs
from youtube_history import DEFAULT_CHANNEL, known_video_ids, parse_timestamp, validate_baseline_marker
from youtube_review_evidence import ReviewedEvidence, ReviewVerificationError
import youtube_eligibility as e


def history_fingerprint(cache, journal):
    return hashlib.sha256(json.dumps({"cache": cache, "journal": journal}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True)
class TabsEvaluation:
    decisions: tuple[e.Decision, ...]
    preview_items: tuple[dict, ...]
    health: dict
    errors: tuple[str, ...] = ()
    evidence: tuple[dict, ...] = ()

    def report(self):
        return {"mode": "tabs-only-dry-run", "delivery_authorized": False, "enqueue": False,
            "health": deepcopy(self.health), "errors": list(self.errors),
            "evidence": deepcopy(self.evidence), "preview_items": deepcopy(self.preview_items),
            "decisions": [asdict(d) for d in self.decisions]}


class ChannelTabsAdapter:
    """Read-only: startup inventory is quarantined; later arrivals need admission.

    Restart, authority/policy change or an unhealthy gap requires a new healthy
    inventory. No inventory or first_seen is persisted into historical state.
    """
    def __init__(self, *, channel_id=DEFAULT_CHANNEL, tabs_reader=None, policy=None,
                 reviews=None, review_verifier=None, clock=None, observation_store=None):
        self.policy = policy or e.Policy(channel_id, "tabs-only-v1")
        self.tabs_reader = tabs_reader
        self.reviews = deepcopy(reviews or {})
        self.review_verifier = review_verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.observation_store = observation_store
        self._cycles, self._first_seen = [], {}
        self._inventory, self._primed, self._epoch = set(), False, None

    def _history(self, cache, journal):
        from monitor_runner import validate_delivery
        validate_delivery(journal)
        marker = validate_baseline_marker(journal.get("youtube_rss_cutover"))
        if (marker is None or not isinstance(cache, list) or any(not isinstance(i, dict)
                or not re.fullmatch(r"[A-Za-z0-9_-]{11}", i.get("video_id", "")) for i in cache)):
            raise ValueError()
        ids = (frozenset(marker["baseline_ids"]), frozenset(i["video_id"] for i in cache),
               frozenset(known_video_ids([], journal)))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for values in ids for v in values):
            raise ValueError()
        return e.HistoricalIdentity(*ids, parse_timestamp(marker["cutover_at"]), self.policy.scope,
                                    history_fingerprint(cache, journal), True, True)

    def evaluate(self, cache, journal):
        try:
            history = self._history(cache, journal)
            if self.policy.channel_id != DEFAULT_CHANNEL:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            return TabsEvaluation((), (), {}, ("HISTORICAL_STATE_OR_CHANNEL_UNVERIFIED",))
        epoch = (history.fingerprint, self.policy.version, self.policy.scope,
                 self.policy.activation_at, self.policy.approved)
        if self.observation_store is None and epoch != self._epoch:
            self._cycles, self._inventory, self._primed = [], set(), False
            self._epoch = epoch
        reader, owned = self.tabs_reader, self.tabs_reader is None
        try:
            reader = reader or YouTubeChannelTabsReader()
            snapshot = read_channel_tabs(reader, history.baseline_ids | history.cache_ids | history.journal_ids)
        except Exception:
            self._primed = False
            return TabsEvaluation((), (), {}, ("CHANNEL_TABS_SOURCE_FAILED",))
        finally:
            if owned and reader is not None:
                reader.close()
        pages = snapshot["pages"]
        health = {tab: deepcopy(pages.get(tab, {"health": "FAILED"})) for tab in ("videos", "streams")}
        healthy = all(health[t]["health"] == "HEALTHY" for t in health)
        at, now = parse_timestamp(snapshot.get("observed_at_utc")), self.clock()
        if (at is None or type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None):
            self._primed = False
            return TabsEvaluation((), (), health, ("CAPTURE_TIME_UNVERIFIED",))
        current = {o["video_id"]: o for o in snapshot["observations"]}
        if self.observation_store is None:
            if not healthy or not self._primed:
                self._inventory.update(current)
            for vid in current:
                self._first_seen[vid] = min(self._first_seen.get(vid, at), at)
            previous = self._cycles[-1]["observations"] if self._cycles else {}
            self._cycles = (self._cycles + [{"at": at, "observations": current, "healthy": healthy}])[-2:]
            activation = self.policy.activation_at
            timing_valid = (type(activation) is datetime and activation.tzinfo is not None
                and type(self.policy.ttl) is timedelta and type(self.policy.skew) is timedelta)
            self._primed = bool(healthy and self.policy.approved and timing_valid
                and at > activation and -self.policy.skew <= now - at <= self.policy.ttl)
            persistent_ids = set()
        else:
            try:
                self.observation_store.record_capture(list(current.values()), at, healthy=healthy)
                persistent_ids = set(self.observation_store.observation_ids())
            except Exception:
                return TabsEvaluation((), (), health, ("PERSISTENT_OBSERVATION_STATE_FAILED",))
            previous = {}
        decisions, previews, evidence = [], [], []
        for vid in sorted(current.keys() | previous.keys() | history.journal_ids | persistent_ids):
            observed = current.get(vid) or previous.get(vid)
            persistent = self.observation_store.observation(vid) if self.observation_store is not None else None
            first_seen = parse_timestamp(persistent["first_seen_at"]) if persistent else self._first_seen.get(vid, at)
            if first_seen is None:
                first_seen = at
            review, invalid_review = ReviewedEvidence(), False
            if vid in self.reviews:
                try:
                    if self.review_verifier is None:
                        raise ReviewVerificationError()
                    review = self.review_verifier.verify(self.reviews[vid], video_id=vid,
                        channel_id=self.policy.channel_id, policy=self.policy,
                        fingerprint=history.fingerprint, first_seen_at=first_seen)
                except ReviewVerificationError:
                    invalid_review = True
            proof = lambda ref, time: e.Proof(vid, self.policy.channel_id, ref, time, self.policy.scope, True)
            if persistent is not None:
                captures = tuple(e.TabCapture(proof("persistent-tabs:"+str(c["seq"]),
                    parse_timestamp(c["observed_at"]) or at), frozenset(c["tabs"]), c["healthy"], c["healthy"],
                    partial=not c["healthy"]) for c in persistent["captures"])
                observed = current.get(vid) or persistent["video"]
                initial_inventory = persistent["initial_inventory"]
            else:
                captures = tuple(e.TabCapture(proof("direct-tabs:"+c["at"].isoformat(),c["at"]),
                    frozenset(c["observations"].get(vid,{}).get("source_tabs",[])), c["healthy"], c["healthy"],
                    partial=not c["healthy"], title_conflict=c["observations"].get(vid,{}).get("title_conflict",False))
                    for c in self._cycles)
                initial_inventory = vid in self._inventory
            video = e.VideoEvidence(proof("direct-video:"+at.isoformat(),at),observed["title"],observed["url"]) if observed else None
            item = e.EligibilityInput(vid, history,
                e.TimeEvidence(now, first_seen, True, initial_inventory), self.policy,
                video, captures, self._journal(vid,journal), review.category, review.release, review.admission)
            d = e.classify(item)
            if d.eligibility not in {e.Eligibility.REJECTED,e.Eligibility.ALREADY_DELIVERED} and (not healthy or invalid_review):
                d = replace(d,eligibility=e.Eligibility.HOLD_FOR_REVIEW,requires_atomic_reservation=False,
                    reason_codes=d.reason_codes+("REVIEW_EVIDENCE_UNVERIFIED" if invalid_review else "PARTIAL_OR_FAILED_SOURCE_CYCLE",))
            decisions.append(d)
            evidence.append({"video_id":vid,"tabs":current.get(vid,{}).get("source_tabs",[]),
                "first_seen_at":first_seen.isoformat(),"initial_inventory":initial_inventory,
                "category":review.category.category.value,"readiness":review.release.readiness.value})
            if d.eligibility is e.Eligibility.ELIGIBLE and vid in current:
                previews.append({"source":"youtube","kind":"video","video_id":vid,"title":observed["title"],
                    "url":observed["url"],"first_seen_at":first_seen.isoformat(),"delivery_format":"youtube-tabs-preview-v1"})
        return TabsEvaluation(tuple(decisions),tuple(previews),health,
                              () if healthy else ("CHANNEL_TABS_SOURCE_FAILED",),tuple(evidence))

    def _journal(self, vid, journal):
        key = f"youtube:video:{vid}:initial"
        batches = [b for b in journal["batches"] if key in b["event_keys"]]
        if not batches:
            return e.JournalEvidence(scope=self.policy.scope)
        if len(batches) != 1:
            return e.JournalEvidence(e.DeliveryState.UNVERIFIED,key,scope=self.policy.scope)
        b = batches[0]
        state = e.DeliveryState.DELIVERED if b["delivered"] else e.DeliveryState.PARTIAL if b["sent_chunks"] or b["sent_documents"] else e.DeliveryState.PENDING
        return e.JournalEvidence(state,key,acknowledgement_verified=b["delivered"],scope=self.policy.scope)
