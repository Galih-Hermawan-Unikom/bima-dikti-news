"""Offline, side-effect-free admission decisions. Never authorizes or enqueues delivery.

Proof.verified represents an upstream verifier's assertion, NOT authentication
performed by this module. A checksum, tab membership or shadow approval alone
must never be converted into a verified admission proof by a caller.
"""
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
import re
from types import UnionType
from typing import Literal, Union, get_args, get_origin, get_type_hints


class Knowledge(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


class Eligibility(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    HOLD_FOR_REVIEW = "HOLD_FOR_REVIEW"
    REJECTED = "REJECTED"
    ALREADY_DELIVERED = "ALREADY_DELIVERED"


class DeliveryState(str, Enum):
    NONE = "NONE"
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    DELIVERED = "DELIVERED"
    UNVERIFIED = "UNVERIFIED"


class Category(str, Enum):
    UNKNOWN = "UNKNOWN"
    REGULAR = "REGULAR"
    STREAM = "STREAM"
    SHORTS = "SHORTS"


class CategoryMethod(str, Enum):
    PUBLISHER = "PUBLISHER"
    MANUAL = "MANUAL"
    TAB_SURROGATE = "TAB_SURROGATE"


class Readiness(str, Enum):
    UNKNOWN = "UNKNOWN"
    PUBLIC_RELEASED = "PUBLIC_RELEASED"
    UPCOMING = "UPCOMING"
    LIVE = "LIVE"
    COMPLETED_REPLAY = "COMPLETED_REPLAY"


class Reservation(str, Enum):
    UNRESERVED = "UNRESERVED"
    OWNED_BY_SELF = "OWNED_BY_SELF"
    OWNED_BY_OTHER = "OWNED_BY_OTHER"


@dataclass(frozen=True)
class Proof:
    video_id: str
    channel_id: str
    reference: str
    observed_at: datetime
    scope: str
    verified: bool = False


@dataclass(frozen=True)
class RssEvidence:
    proof: Proof
    title: str
    url: str
    published: str


@dataclass(frozen=True)
class TabCapture:
    proof: Proof
    tabs: frozenset[str]
    videos_healthy: bool
    streams_healthy: bool
    partial: bool = False
    title_conflict: bool = False


@dataclass(frozen=True)
class HistoricalIdentity:
    baseline_ids: frozenset[str]
    cache_ids: frozenset[str]
    journal_ids: frozenset[str]
    cutover_at: datetime
    scope: str
    fingerprint: str
    authority_verified: bool = False
    integrity_verified: bool = False
    schema_version: int = 1


@dataclass(frozen=True)
class JournalEvidence:
    state: DeliveryState = DeliveryState.NONE
    event_key: str | None = None
    acknowledgement_verified: bool = False
    pinned_eligibility: Eligibility | None = None
    ambiguous_outcome: bool = False
    scope: str = "offline"


@dataclass(frozen=True)
class CategoryEvidence:
    category: Category = Category.UNKNOWN
    method: CategoryMethod = CategoryMethod.PUBLISHER
    proof: Proof | None = None


@dataclass(frozen=True)
class ReleaseEvidence:
    readiness: Readiness = Readiness.UNKNOWN
    proof: Proof | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True)
class TimeEvidence:
    now: datetime
    first_seen_at: datetime
    earliest_published: str | None = None
    clock_verified: bool = False


@dataclass(frozen=True)
class AdmissionEvidence:
    proof: Proof | None = None
    policy_version: str = ""
    history_review_reference: str = ""
    state_fingerprint: str = ""
    expires_at: datetime | None = None
    rss_only_exception: bool = False
    reviewer: str = ""
    approved: bool = False


@dataclass(frozen=True)
class Policy:
    channel_id: str
    version: str
    scope: str = "offline"
    activation_at: datetime | None = None
    window: timedelta | None = None
    ttl: timedelta | None = None
    skew: timedelta | None = None
    capture_interval: timedelta | None = None
    approved: bool = False
    allow_rss_only_exception: bool = False
    allow_tab_category_surrogate: bool = False


@dataclass(frozen=True)
class EligibilityInput:
    video_id: str
    history: HistoricalIdentity
    time: TimeEvidence
    policy: Policy
    rss: RssEvidence | None = None
    captures: tuple[TabCapture, ...] = ()
    journal: JournalEvidence = JournalEvidence()
    category: CategoryEvidence = CategoryEvidence()
    release: ReleaseEvidence = ReleaseEvidence()
    admission: AdmissionEvidence = AdmissionEvidence()
    reservation: Reservation = Reservation.UNRESERVED
    evidence_conflict: bool = False
    metadata_changed: bool = False
    previous_eligibility: Eligibility | None = None
    shadow_review: str = ""


@dataclass(frozen=True)
class Decision:
    video_id: str
    event_key: str
    knowledge: Knowledge | None
    eligibility: Eligibility
    delivery_state: DeliveryState
    reason_codes: tuple[str, ...]
    rule_ids: tuple[str, ...]
    recovery_only: bool = False
    requires_atomic_reservation: bool = False
    # Literal False is a structural boundary, independent of policy/admission.
    enqueue: Literal[False] = False
    delivery_authorized: Literal[False] = False


def _well_typed(value, annotation):
    """Reject malformed external objects, including truthy strings in bool fields."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        return any(_well_typed(value, arg) for arg in args)
    if origin in (tuple, frozenset):
        return type(value) is origin and all(_well_typed(item, args[0]) for item in value)
    if is_dataclass(annotation):
        if type(value) is not annotation:
            return False
        hints = get_type_hints(annotation)
        return all(_well_typed(getattr(value, f.name), hints[f.name]) for f in fields(annotation))
    return type(value) is annotation


def _aware(value):
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _published(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if _aware(parsed) else None
    except (ValueError, OverflowError):
        return None


def _proof_error(proof, item):
    if proof is None or not proof.verified or not proof.reference.strip():
        return "UNVERIFIED_PROVENANCE", "D30"
    if proof.video_id != item.video_id or proof.channel_id != item.policy.channel_id:
        return "EVIDENCE_IDENTITY_CONFLICT", "D09"
    if proof.scope != item.policy.scope:
        return "EVIDENCE_SCOPE_CONFLICT", "D09"
    if not _aware(proof.observed_at):
        return "INVALID_EVIDENCE_TIME", "D30"
    age = item.time.now - proof.observed_at
    if age < -item.policy.skew or age > item.policy.ttl:
        return "STALE_OR_FUTURE_EVIDENCE", "D30"
    return None


def _admission_error(item):
    a = item.admission
    error = _proof_error(a.proof, item)
    if error:
        if item.shadow_review in {"APPROVED", "NEW_CANDIDATE"}:
            return "SHADOW_APPROVAL_IS_NOT_ADMISSION", "D31"
        return error
    if (not a.approved or not a.reviewer.strip()
            or a.policy_version != item.policy.version or not a.history_review_reference.strip()
            or a.state_fingerprint != item.history.fingerprint):
        return "ADMISSION_HISTORY_OR_FINGERPRINT_UNVERIFIED", "D30"
    if not _aware(a.expires_at) or a.expires_at <= item.time.now:
        return "ADMISSION_EXPIRED", "D30"
    return None


def classify(item: EligibilityInput) -> Decision:
    """Evaluate supplied evidence only. No I/O, clock reads, approval or reservation.

    A typed invalid/naive input returns HOLD, not an empty historical identity set.
    Actual atomic reservation (D33) is deliberately outside this pure function.
    """
    if not _well_typed(item, EligibilityInput):
        return Decision("", "", None, Eligibility.HOLD_FOR_REVIEW,
                        DeliveryState.UNVERIFIED, ("INVALID_TYPED_INPUT",), ("D06",))
    try:
        return _classify(item)
    except (ValueError, TypeError, OverflowError):
        return Decision(item.video_id, f"youtube:video:{item.video_id}:initial", None,
                        Eligibility.HOLD_FOR_REVIEW, item.journal.state,
                        ("INVALID_TIME_OR_EVIDENCE",), ("D06",))


def _classify(item):
    p, h, t, j = item.policy, item.history, item.time, item.journal
    key = f"youtube:video:{item.video_id}:initial"
    known = item.video_id in h.baseline_ids | h.cache_ids | h.journal_ids
    known |= j.state is not DeliveryState.NONE or j.event_key == key
    knowledge = Knowledge.KNOWN if known else Knowledge.UNKNOWN
    pending = j.state in {DeliveryState.PENDING, DeliveryState.PARTIAL}

    def result(eligibility, reason, rule, *, unverified=False):
        rules = [rule]
        if item.metadata_changed:
            rules.append("D26" if known else "D27")
        if (not known and eligibility is Eligibility.HOLD_FOR_REVIEW and item.rss
                and _published(item.rss.published) and _aware(h.cutover_at)
                and _published(item.rss.published) > h.cutover_at):
            rules.append("D21")
        if eligibility is Eligibility.ELIGIBLE:
            rules.append("D32")
            if item.previous_eligibility is Eligibility.HOLD_FOR_REVIEW:
                rules.append("D35")
        return Decision(item.video_id, key, None if unverified else knowledge,
                        eligibility, j.state, (reason,), tuple(dict.fromkeys(rules)),
                        recovery_only=pending,
                        requires_atomic_reservation=eligibility is Eligibility.ELIGIBLE and not pending)

    def hold(reason, rule="D30", **kwargs):
        return result(Eligibility.HOLD_FOR_REVIEW, reason, rule, **kwargs)

    def reject(reason, rule):
        # Exclusions never discard the progress/ownership of a pending batch.
        return result(Eligibility.HOLD_FOR_REVIEW if pending else Eligibility.REJECTED, reason, rule)

    if (not re.fullmatch(r"[A-Za-z0-9_-]{11}", item.video_id)
            or not h.authority_verified or not h.integrity_verified or h.schema_version != 1
            or not re.fullmatch(r"[a-f0-9]{64}", h.fingerprint)
            or h.scope != p.scope or not _aware(h.cutover_at)
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid)
                   for vid in h.baseline_ids | h.cache_ids | h.journal_ids)):
        return hold("HISTORICAL_STATE_UNVERIFIED", "D06", unverified=True)
    if j.scope != p.scope or (j.event_key is not None and j.event_key != key):
        return hold("JOURNAL_EVENT_OR_SCOPE_CONFLICT", "D09")
    if j.state is DeliveryState.DELIVERED:
        if j.event_key == key and j.acknowledgement_verified:
            return result(Eligibility.ALREADY_DELIVERED, "CONFIRMED_INITIAL_EVENT", "D01")
        return hold("JOURNAL_ACKNOWLEDGEMENT_UNVERIFIED", "D05")
    if j.ambiguous_outcome:
        return hold("RECOVERY_OUTCOME_AMBIGUOUS", "D34")
    if j.state is DeliveryState.NONE and (
            j.event_key is not None or j.pinned_eligibility is not None or j.acknowledgement_verified):
        return hold("INCONSISTENT_JOURNAL_STATE", "D05")
    if j.state is DeliveryState.UNVERIFIED or (item.video_id in h.journal_ids and not pending):
        return hold("JOURNAL_RECONCILIATION_REQUIRED", "D05")
    if not pending and item.video_id in h.baseline_ids:
        return reject("HISTORICAL_INITIAL_SUPPRESSED", "D03")
    if not pending and item.video_id in h.cache_ids:
        return reject("CACHE_KNOWN_INITIAL_SUPPRESSED", "D04")
    if (not p.approved or not p.version.strip() or not p.scope.strip()
            or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", p.channel_id)
            or not t.clock_verified or not _aware(t.now) or not _aware(t.first_seen_at)
            or not _aware(p.activation_at) or p.activation_at < h.cutover_at
            or any(type(v) is not timedelta or v <= timedelta(0)
                   for v in (p.window, p.ttl, p.capture_interval))
            or type(p.skew) is not timedelta or p.skew < timedelta(0)):
        return hold("POLICY_PARAMETERS_OR_CLOCK_UNVERIFIED")
    if item.evidence_conflict:
        return hold("SOURCE_EVIDENCE_CONFLICT", "D09")
    # A pinned recovery lease must not hide a newly supplied identity/scope conflict.
    proofs = ([item.rss.proof] if item.rss else []) + [c.proof for c in item.captures]
    proofs += [proof for proof in (item.category.proof, item.release.proof, item.admission.proof) if proof]
    for proof in proofs:
        if proof.video_id != item.video_id or proof.scope != p.scope:
            return hold("EVIDENCE_IDENTITY_OR_SCOPE_CONFLICT", "D09")
        if proof.channel_id != p.channel_id:
            if item.rss is not None and proof is item.rss.proof and not pending:
                return reject("WRONG_CHANNEL_OR_CANONICAL_URL", "D08")
            return hold("EVIDENCE_CHANNEL_CONFLICT", "D09")
    if item.reservation is Reservation.OWNED_BY_OTHER:
        return hold("EVENT_RESERVED_BY_OTHER_WRITER", "D33")
    if item.category.category is Category.SHORTS:
        error = _proof_error(item.category.proof, item)
        if error:
            return hold(*error)
        return reject("EXCLUDED_SHORT", "D07")
    if pending:
        if j.event_key != key or j.pinned_eligibility is not Eligibility.ELIGIBLE:
            return hold("PENDING_ADMISSION_NOT_PINNED", "D02")
        error = _admission_error(item)
        if error:
            return hold(error[0], "D02")
        # A valid lease can recover an existing batch even after RSS visibility ends.
        if item.category.category is Category.SHORTS:
            return hold("PENDING_CATEGORY_REVOKED", "D07")
        if item.release.readiness in {Readiness.UPCOMING, Readiness.LIVE}:
            return hold("PENDING_READINESS_CONFLICT", "D02")
        return result(Eligibility.ELIGIBLE, "PINNED_RECOVERY_ONLY_NO_NEW_BATCH", "D02")
    if item.rss is None:
        return hold("RSS_CONTRACT_MISSING", "D18")
    if (item.rss.proof.channel_id != p.channel_id
            or item.rss.url != f"https://www.youtube.com/watch?v={item.video_id}"):
        return reject("WRONG_CHANNEL_OR_CANONICAL_URL", "D08")
    error = _proof_error(item.rss.proof, item)
    if error:
        return hold(*error)
    if not item.rss.title.strip():
        return hold("RSS_TITLE_MISSING", "D22")
    publication = _published(item.rss.published)
    if item.metadata_changed and t.earliest_published is None:
        return hold("EARLIEST_PUBLICATION_REQUIRED_FOR_CHANGED_METADATA", "D27")
    earliest = _published(t.earliest_published) if t.earliest_published is not None else publication
    if publication is None or earliest is None or earliest > publication:
        return hold("INVALID_OR_CONFLICTING_PUBLICATION", "D22")
    if earliest <= h.cutover_at:
        return reject("PRE_CUTOVER_INITIAL_EXCLUDED", "D20")
    if publication > t.now + p.skew or t.first_seen_at > t.now + p.skew:
        return hold("FUTURE_PUBLICATION_OR_CLOCK_ERROR", "D23")
    if min(earliest, t.first_seen_at) < p.activation_at:
        return hold("PRE_ACTIVATION_BACKLOG", "D24")
    if t.now - earliest > p.window:
        return reject("AGE_WINDOW_EXCEEDED", "D25")
    tabs = frozenset()
    previous_time = None
    references = set()
    for capture in item.captures:
        if (capture.partial or not capture.videos_healthy or not capture.streams_healthy
                or not capture.tabs <= {"videos", "streams"}):
            return hold("PARTIAL_OR_FAILED_SNAPSHOT", "D29")
        if capture.title_conflict:
            return hold("TAB_TITLE_CONFLICT", "D09")
        error = _proof_error(capture.proof, item)
        if error:
            return hold(*error)
        if previous_time is not None and capture.proof.observed_at <= previous_time:
            return hold("CAPTURE_TIMES_NOT_INCREASING")
        previous_time = capture.proof.observed_at
        if capture.proof.reference in references:
            return hold("DUPLICATE_CAPTURE_PROVENANCE")
        references.add(capture.proof.reference)
    preserving_lease = False
    if item.captures:
        tabs = item.captures[-1].tabs
        if not tabs:
            previous_visible = [capture for capture in item.captures[:-1] if capture.tabs]
            if item.previous_eligibility is not Eligibility.ELIGIBLE or not previous_visible:
                return hold("NOT_VISIBLE_IN_CAPTURE_NOT_DELETED", "D28")
            # Absence does not revoke an existing lease, but all remaining gates
            # (including its explicit approved admission and expiry) still apply.
            tabs, preserving_lease = previous_visible[-1].tabs, True
        if not preserving_lease and (len(item.captures) < 2 or not all(capture.tabs for capture in item.captures)
                or item.captures[-1].proof.observed_at - item.captures[0].proof.observed_at < p.capture_interval):
            return hold("INSUFFICIENT_STABLE_CAPTURES")
    else:
        if not (p.allow_rss_only_exception and item.admission.rss_only_exception):
            return hold("RSS_ONLY_DEFAULT_HOLD", "D16")
    if item.category.category is Category.UNKNOWN:
        return hold("CATEGORY_UNVERIFIED", "D11" if "videos" in tabs else "D16")
    error = _proof_error(item.category.proof, item)
    if error:
        return hold(*error)
    if item.category.method is CategoryMethod.TAB_SURROGATE:
        if not p.allow_tab_category_surrogate or not tabs:
            return hold("TAB_MEMBERSHIP_NOT_CATEGORY_CERTIFICATE", "D11")
    stream = item.category.category is Category.STREAM or "streams" in tabs
    if stream and item.category.category is not Category.STREAM:
        return hold("CATEGORY_AND_TAB_CONFLICT", "D09")
    error = _proof_error(item.release.proof, item)
    if error:
        return hold("RELEASE_PROOF_UNVERIFIED", "D14" if stream else "D11")
    if item.release.readiness is Readiness.UPCOMING:
        return hold("SCHEDULED_NOT_RELEASED", "D13")
    if item.release.readiness is Readiness.LIVE:
        return hold("LIVE_NOT_ALLOWED_REPLAY_ONLY", "D15")
    if stream:
        if (item.release.readiness is not Readiness.COMPLETED_REPLAY
                or not _aware(item.release.ended_at) or item.release.ended_at > t.now):
            return hold("COMPLETED_PUBLIC_REPLAY_UNVERIFIED", "D14")
    elif item.release.readiness is not Readiness.PUBLIC_RELEASED:
        return hold("PUBLIC_NON_PREMIERE_RELEASE_UNVERIFIED", "D11")
    error = _admission_error(item)
    if error:
        return hold(*error)
    rule = "D28" if preserving_lease else "D17" if not tabs else "D19" if len(tabs) == 2 else "D12" if stream else "D10"
    return result(Eligibility.ELIGIBLE, "OFFLINE_POLICY_ELIGIBLE_NOT_AUTHORIZED", rule)
