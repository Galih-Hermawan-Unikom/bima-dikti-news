"""RSS + allowed channel tabs -> eligibility comparison, never delivery/state I/O."""
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re

from reliability import SourceError
from youtube_channel_tabs import YouTubeChannelTabsReader, read_channel_tabs
from youtube_monitor import YouTubeMonitor, DEFAULT_CHANNEL, known_video_ids, publication_time, validate_cutover
from youtube_review_evidence import ReviewedEvidence, ReviewVerificationError
import youtube_eligibility as e


def history_fingerprint(cache, journal):
    raw = json.dumps({"cache": cache, "journal": journal}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


class ChannelBoundRSS(YouTubeMonitor):
    def _read_feed(self):
        root = super()._read_feed()
        name = "{http://www.youtube.com/xml/schemas/2015}channelId"
        if root.findtext(name) != self.channel_id:
            raise SourceError("Hybrid RSS: CHANNEL_BINDING_INVALID")
        atom = "{http://www.w3.org/2005/Atom}entry"
        if any(entry.findtext(name) != self.channel_id for entry in root.findall(atom)):
            raise SourceError("Hybrid RSS: ENTRY_CHANNEL_BINDING_INVALID")
        return root


@dataclass(frozen=True)
class HybridEvaluation:
    decisions: tuple[e.Decision, ...]
    preview_items: tuple[dict, ...]
    health: dict
    errors: tuple[str, ...] = ()
    evidence: tuple[dict, ...] = ()

    def report(self):
        return {"mode": "dry-run-only", "delivery_authorized": False, "enqueue": False,
                "health": deepcopy(self.health), "errors": list(self.errors),
                "evidence": deepcopy(self.evidence),
                "decisions": [asdict(d) for d in self.decisions]}


class HybridYouTubeAdapter:
    """One instance retains bounded captures/earliest observations in memory only.

    No default category verifier, review, admission or approved policy. Direct
    source reads use fixed RSS channel + /videos and /streams, never /shorts.
    A new process starts without a capture lease. It cannot auto-admit backlog.
    """
    def __init__(self, *, channel_id=DEFAULT_CHANNEL, rss=None, tabs_reader=None, policy=None, reviews=None,
                 review_verifier=None, clock=None):
        self.rss = rss or ChannelBoundRSS(channel_id=channel_id)
        self._owns_rss = rss is None
        self.tabs_reader = tabs_reader
        self.policy = policy or e.Policy(channel_id, "hybrid-dry-run-v1")
        self.reviews = deepcopy(reviews or {})
        self.review_verifier = review_verifier
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cycles = []
        self._first_seen = {}
        self._earliest = {}

    def close(self):
        if self._owns_rss:
            self.rss.session.close()

    def _history(self, cache, journal):
        marker = validate_cutover(journal.get("youtube_rss_cutover"))
        if (marker is None or journal.get("version") != 1 or not isinstance(journal.get("batches"), list)
                or not isinstance(cache, list) or any(not isinstance(i, dict)
                or not re.fullmatch(r"[A-Za-z0-9_-]{11}", i.get("video_id", "")) for i in cache)):
            raise ValueError("Invalid historical identity")
        # Standalone callers get the same strict journal validation as the runner.
        from monitor_runner import validate_delivery
        validate_delivery(journal)
        baseline = frozenset(marker["baseline_ids"])
        cached = frozenset(i["video_id"] for i in cache)
        journal_ids = frozenset(known_video_ids([], journal))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", i) for i in baseline | cached | journal_ids):
            raise ValueError("Invalid historical identity")
        return e.HistoricalIdentity(baseline, cached, journal_ids, publication_time(marker["cutover_at"]),
            self.policy.scope, history_fingerprint(cache, journal), True, True)

    def evaluate(self, cache, journal):
        """Read observations, evaluate all observed and journal identities; no writes."""
        try:
            h = self._history(cache, journal)
            if self.policy.channel_id != DEFAULT_CHANNEL or self.rss.channel_id != DEFAULT_CHANNEL:
                raise ValueError("Channel mismatch")
        except (ValueError, TypeError, KeyError, AttributeError):
            return HybridEvaluation((), (), {}, ("HISTORICAL_STATE_OR_CHANNEL_UNVERIFIED",))
        errors, rss_rows = [], []
        try:
            rss_rows = self.rss.fetch_videos()
            self._validate_rss(rss_rows)
            rss_ok = True
        except Exception:
            errors.append("RSS_SOURCE_FAILED")
            rss_rows, rss_ok = [], False
        reader = self.tabs_reader
        owned = reader is None
        try:
            reader = reader or YouTubeChannelTabsReader()
            tabs = read_channel_tabs(reader, h.baseline_ids | h.cache_ids | h.journal_ids)
        except Exception:
            tabs = {"observations": [], "pages": {}, "health": "FAILED"}
            errors.append("TABS_SOURCE_FAILED")
        finally:
            if owned and reader is not None:
                reader.close()
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            return HybridEvaluation((), (), {}, ("EVALUATION_CLOCK_UNVERIFIED",))
        pages = tabs["pages"]
        healthy = {tab: pages.get(tab, {}).get("health") == "HEALTHY" for tab in ("videos", "streams")}
        all_tabs_ok = all(healthy.values()) and tabs["health"] == "HEALTHY"
        health = {"rss": "HEALTHY" if rss_ok else "FAILED",
                  **{tab: "HEALTHY" if ok else "FAILED" for tab, ok in healthy.items()},
                  "coverage": "initial-page-only-no-pagination"}
        observations = {o["video_id"]: o for o in tabs["observations"]}
        rss_by_id = {r["video_id"]: r for r in rss_rows}
        # Use the reader timestamp, not report creation time, for capture freshness.
        captured_at = publication_time(tabs.get("observed_at_utc"))
        cycle = {"at": captured_at, "observations": observations, "health": healthy}
        self._cycles = (self._cycles + [cycle])[-2:]
        for row in rss_rows:
            self._remember(row["video_id"], now, row["published"])
        for vid in observations:
            self._remember(vid, captured_at or now)
        for row in cache:
            published = row.get("published", row.get("date"))
            if publication_time(published) is not None:
                self._remember(row["video_id"], now, published)
        ids = sorted(rss_by_id.keys() | observations.keys() | h.journal_ids)
        decisions, previews, evidence = [], [], []
        for vid in ids:
            row = rss_by_id.get(vid)
            review = ReviewedEvidence()
            review_error = False
            if vid in self.reviews:
                try:
                    if self.review_verifier is None:
                        raise ReviewVerificationError()
                    review = self.review_verifier.verify(self.reviews[vid], video_id=vid,
                        channel_id=self.policy.channel_id, policy=self.policy, fingerprint=h.fingerprint)
                    if review.first_seen_at is not None:
                        self._remember(vid, review.first_seen_at, review.earliest_published)
                except ReviewVerificationError:
                    review_error = True
            proof = lambda ref, at: e.Proof(vid, self.policy.channel_id, ref, at, self.policy.scope, True)
            caps = []
            for index, c in enumerate(self._cycles):
                obs = c["observations"].get(vid, {})
                caps.append(e.TabCapture(proof("direct-tab-capture:" + str(c["at"]) + ":" + str(index), c["at"] or now),
                    frozenset(obs.get("source_tabs", [])), c["health"]["videos"], c["health"]["streams"],
                    partial=c["at"] is None, title_conflict=obs.get("title_conflict", False)))
            # Healthy absence is RSS-only, not a failed tab. Failed cycles MUST
            # remain represented even when they contain no observations at all.
            if all(not c.tabs for c in caps) and all_tabs_ok:
                caps = []
            j = self._journal_evidence(vid, journal)
            first_seen = self._first_seen.get(vid, now)
            earliest = self._earliest.get(vid)
            raw_rss = e.RssEvidence(proof("direct-rss:" + row["scraped_at"], publication_time(row["scraped_at"])),
                row["title"], row["url"], row["published"]) if row else None
            changed = bool(row and earliest and publication_time(earliest) != publication_time(row["published"]))
            conflict = bool(row and vid in observations and row["title"] != observations[vid]["title"])
            item = e.EligibilityInput(vid, h, e.TimeEvidence(now, first_seen, earliest, True), self.policy,
                raw_rss, tuple(caps), j, review.category, review.release, review.admission,
                evidence_conflict=conflict, metadata_changed=changed)
            decision = e.classify(item)
            # Fail the entire source cycle closed, including RSS-only exceptions
            # and pinned recovery. Preserve terminal historical/delivered results.
            terminal = decision.eligibility in {e.Eligibility.REJECTED, e.Eligibility.ALREADY_DELIVERED}
            if not terminal and (not rss_ok or not all_tabs_ok or review_error or captured_at is None):
                reason = "REVIEW_EVIDENCE_UNVERIFIED" if review_error else "PARTIAL_OR_FAILED_SOURCE_CYCLE"
                decision = replace(decision, eligibility=e.Eligibility.HOLD_FOR_REVIEW,
                    reason_codes=decision.reason_codes + (reason,), rule_ids=tuple(dict.fromkeys(decision.rule_ids + ("D30" if review_error else "D29",))),
                    requires_atomic_reservation=False)
            decisions.append(decision)
            evidence.append({"video_id": vid, "category": review.category.category.value,
                "readiness": review.release.readiness.value,
                "review_verified": review.admission.approved and not review_error,
                "review_reference": review.admission.proof.reference if review.admission.proof else None,
                "tabs": list(observations.get(vid, {}).get("source_tabs", []))})
            if decision.eligibility is e.Eligibility.ELIGIBLE and not decision.recovery_only and row:
                previews.append(deepcopy(row))
        return HybridEvaluation(tuple(decisions), tuple(previews), health, tuple(errors), tuple(evidence))

    def _remember(self, vid, observed_at, published=None):
        self._first_seen[vid] = min(self._first_seen.get(vid, observed_at), observed_at)
        if publication_time(published) is not None:
            old = self._earliest.get(vid)
            if old is None or publication_time(published) < publication_time(old):
                self._earliest[vid] = published

    @staticmethod
    def _validate_rss(rows):
        if not isinstance(rows, list):
            raise ValueError()
        seen = set()
        for r in rows:
            vid = r.get("video_id", "")
            if (not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid) or vid in seen
                    or r.get("source") != "youtube" or r.get("delivery_format") != "youtube-rss-v1"
                    or not isinstance(r.get("title"), str) or not r["title"].strip()
                    or r.get("url") != "https://www.youtube.com/watch?v=" + vid
                    or publication_time(r.get("published")) is None
                    or publication_time(r.get("scraped_at")) is None
                    or "status" in r or r.get("is_status_update")):
                raise ValueError()
            seen.add(vid)

    def _journal_evidence(self, vid, journal):
        key = f"youtube:video:{vid}:initial"
        batches = [b for b in journal["batches"] if key in b["event_keys"]]
        if not batches:
            return e.JournalEvidence(scope=self.policy.scope)
        if len(batches) != 1:
            return e.JournalEvidence(e.DeliveryState.UNVERIFIED, key, scope=self.policy.scope)
        batch = batches[0]
        if batch["delivered"]:
            return e.JournalEvidence(e.DeliveryState.DELIVERED, key, True, scope=self.policy.scope)
        # Legacy records have no pinned classifier admission; never synthesize it.
        progress = bool(batch["sent_chunks"] or batch["sent_documents"])
        return e.JournalEvidence(e.DeliveryState.PARTIAL if progress else e.DeliveryState.PENDING,
                                 key, scope=self.policy.scope)
