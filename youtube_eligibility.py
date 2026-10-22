"""Pure tabs-only eligibility. Observation time is never a publication time.

The caller authenticates evidence and history. This function cannot approve,
reserve, enqueue, recover a journal or authorize delivery.
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
class VideoEvidence:
    proof: Proof
    title: str
    url: str


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
    baseline_at: datetime
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
    completion_verified: bool = False


@dataclass(frozen=True)
class TimeEvidence:
    now: datetime
    first_seen_at: datetime
    clock_verified: bool = False
    initial_inventory: bool = True


@dataclass(frozen=True)
class AdmissionEvidence:
    proof: Proof | None = None
    policy_version: str = ""
    history_review_reference: str = ""
    state_fingerprint: str = ""
    expires_at: datetime | None = None
    reviewer: str = ""
    approved: bool = False


@dataclass(frozen=True)
class Policy:
    channel_id: str
    version: str
    scope: str = "offline"
    activation_at: datetime | None = None
    window: timedelta | None = None  # Time since first observation, never content age.
    ttl: timedelta | None = None
    skew: timedelta | None = None
    capture_interval: timedelta | None = None
    approved: bool = False
    automatic_tabs_verification: bool = False


@dataclass(frozen=True)
class EligibilityInput:
    video_id: str
    history: HistoricalIdentity
    time: TimeEvidence
    policy: Policy
    video: VideoEvidence | None = None
    captures: tuple[TabCapture, ...] = ()
    journal: JournalEvidence = JournalEvidence()
    category: CategoryEvidence = CategoryEvidence()
    release: ReleaseEvidence = ReleaseEvidence()
    admission: AdmissionEvidence = AdmissionEvidence()
    reservation: Reservation = Reservation.UNRESERVED


@dataclass(frozen=True)
class Decision:
    video_id: str
    event_key: str
    knowledge: Knowledge | None
    eligibility: Eligibility
    delivery_state: DeliveryState
    reason_codes: tuple[str, ...]
    recovery_only: bool = False
    requires_atomic_reservation: bool = False
    enqueue: Literal[False] = False
    delivery_authorized: Literal[False] = False


def _well_typed(value, annotation):
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


def classify(item: EligibilityInput) -> Decision:
    if not _well_typed(item, EligibilityInput):
        return Decision("", "", None, Eligibility.HOLD_FOR_REVIEW,
                        DeliveryState.UNVERIFIED, ("INVALID_TYPED_INPUT",))
    try:
        return _classify(item)
    except (ValueError, TypeError, OverflowError):
        return Decision(item.video_id, f"youtube:video:{item.video_id}:initial", None,
                        Eligibility.HOLD_FOR_REVIEW, item.journal.state, ("INVALID_TIME_OR_EVIDENCE",))


def _classify(item):
    h, p, t, j = item.history, item.policy, item.time, item.journal
    key = f"youtube:video:{item.video_id}:initial"
    known = item.video_id in h.baseline_ids | h.cache_ids | h.journal_ids or j.state is not DeliveryState.NONE

    def result(outcome, reason, unverified=False):
        return Decision(item.video_id, key, None if unverified else Knowledge.KNOWN if known else Knowledge.UNKNOWN,
            outcome, j.state, (reason,), j.state in {DeliveryState.PENDING, DeliveryState.PARTIAL},
            outcome is Eligibility.ELIGIBLE)

    def hold(reason, unverified=False):
        return result(Eligibility.HOLD_FOR_REVIEW, reason, unverified)

    if (not re.fullmatch(r"[A-Za-z0-9_-]{11}", item.video_id) or not h.authority_verified
            or not h.integrity_verified or h.schema_version != 1 or h.scope != p.scope
            or not re.fullmatch(r"[a-f0-9]{64}", h.fingerprint) or not _aware(h.baseline_at)
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{11}", v) for v in h.baseline_ids | h.cache_ids | h.journal_ids)):
        return hold("HISTORICAL_STATE_UNVERIFIED", True)
    if j.scope != p.scope or (j.event_key is not None and j.event_key != key):
        return hold("JOURNAL_EVENT_OR_SCOPE_CONFLICT")
    if j.state is DeliveryState.DELIVERED:
        return result(Eligibility.ALREADY_DELIVERED, "CONFIRMED_INITIAL_EVENT") if j.event_key == key and j.acknowledgement_verified else hold("JOURNAL_ACKNOWLEDGEMENT_UNVERIFIED")
    if j.ambiguous_outcome:
        return hold("RECOVERY_OUTCOME_AMBIGUOUS")
    if j.state in {DeliveryState.PENDING, DeliveryState.PARTIAL}:
        return hold("RECOVERY_REQUIRES_OPERATOR_RECONCILIATION")
    if (j.state is not DeliveryState.NONE or j.event_key is not None or j.acknowledgement_verified
            or item.video_id in h.journal_ids):
        return hold("JOURNAL_RECONCILIATION_REQUIRED")
    if item.video_id in h.baseline_ids | h.cache_ids:
        return result(Eligibility.REJECTED, "HISTORICAL_INITIAL_SUPPRESSED")
    if (not p.approved or not p.version.strip() or not p.scope.strip()
            or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", p.channel_id)
            or not t.clock_verified or not _aware(t.now) or not _aware(t.first_seen_at)
            or not _aware(p.activation_at) or p.activation_at < h.baseline_at
            or any(type(v) is not timedelta or v <= timedelta(0) for v in (p.window, p.ttl, p.capture_interval))
            or type(p.skew) is not timedelta or p.skew < timedelta(0)):
        return hold("POLICY_PARAMETERS_OR_CLOCK_UNVERIFIED")
    if t.first_seen_at <= p.activation_at:
        return hold("PRE_ACTIVATION_OBSERVATION_NO_BACKFILL")
    if t.initial_inventory:
        return hold("STARTUP_INVENTORY_NO_AUTOMATIC_BACKFILL")
    if t.first_seen_at > t.now + p.skew:
        return hold("FUTURE_OBSERVATION_OR_CLOCK_ERROR")
    if t.now - t.first_seen_at > p.window:
        return hold("OBSERVATION_WINDOW_EXPIRED")
    if item.reservation is Reservation.OWNED_BY_OTHER:
        return hold("EVENT_RESERVED_BY_OTHER_WRITER")

    def proof_error(proof):
        if proof is None or not proof.verified or not proof.reference.strip():
            return "UNVERIFIED_PROVENANCE"
        if (proof.video_id != item.video_id or proof.channel_id != p.channel_id or proof.scope != p.scope):
            return "EVIDENCE_IDENTITY_OR_SCOPE_CONFLICT"
        if not _aware(proof.observed_at) or not -p.skew <= t.now - proof.observed_at <= p.ttl:
            return "STALE_OR_FUTURE_EVIDENCE"
        return None

    if item.category.category is Category.SHORTS:
        error = proof_error(item.category.proof)
        return hold(error) if error else result(Eligibility.REJECTED, "EXCLUDED_SHORT")
    if item.video is None or not item.captures:
        return hold("CHANNEL_TAB_EVIDENCE_MISSING")
    if (not item.video.title.strip() or item.video.url != f"https://www.youtube.com/watch?v={item.video_id}"):
        return hold("INVALID_VIDEO_IDENTITY_OR_TITLE")
    error = proof_error(item.video.proof)
    if error:
        return hold(error)
    times, references = [], set()
    for c in item.captures:
        if c.partial or not c.videos_healthy or not c.streams_healthy or not c.tabs <= {"videos", "streams"}:
            return hold("PARTIAL_OR_FAILED_SNAPSHOT")
        if c.title_conflict:
            return hold("TAB_TITLE_CONFLICT")
        error = proof_error(c.proof)
        if error:
            return hold(error)
        if c.proof.reference in references or times and c.proof.observed_at <= times[-1]:
            return hold("DUPLICATE_OR_UNORDERED_CAPTURE")
        references.add(c.proof.reference)
        times.append(c.proof.observed_at)
    if not item.captures[-1].tabs:
        return hold("NOT_VISIBLE_IN_CAPTURE_NOT_DELETED")
    if len(times) < 2 or not all(c.tabs for c in item.captures) or times[-1] - times[0] < p.capture_interval:
        return hold("INSUFFICIENT_STABLE_CAPTURES")
    automatic = (p.automatic_tabs_verification
                 and item.category.method is CategoryMethod.TAB_SURROGATE)
    if item.category.category is Category.UNKNOWN or (item.category.method is CategoryMethod.TAB_SURROGATE and not automatic):
        return hold("CATEGORY_UNVERIFIED")
    proofs = (item.category.proof, item.release.proof)
    if not automatic:
        proofs += (item.admission.proof,)
    for proof in proofs:
        error = proof_error(proof)
        if error:
            return hold(error)
    if item.release.readiness is Readiness.UPCOMING:
        return hold("SCHEDULED_NOT_RELEASED")
    if item.release.readiness is Readiness.LIVE:
        return hold("LIVE_NOT_ALLOWED_REPLAY_ONLY")
    stream = item.category.category is Category.STREAM or "streams" in item.captures[-1].tabs
    if stream:
        if (item.category.category is not Category.STREAM
                or item.release.readiness is not Readiness.COMPLETED_REPLAY
                or not (item.release.completion_verified
                        or (_aware(item.release.ended_at) and item.release.ended_at <= t.now))):
            return hold("COMPLETED_PUBLIC_REPLAY_UNVERIFIED")
    elif item.release.readiness is not Readiness.PUBLIC_RELEASED:
        return hold("PUBLIC_RELEASE_UNVERIFIED")
    if automatic:
        return result(Eligibility.ELIGIBLE, "AUTOMATIC_TABS_POLICY_ELIGIBLE_NOT_AUTHORIZED")
    a = item.admission
    if (not a.approved or not a.reviewer.strip() or not a.history_review_reference.strip()
            or a.policy_version != p.version or a.state_fingerprint != h.fingerprint):
        return hold("ADMISSION_HISTORY_OR_FINGERPRINT_UNVERIFIED")
    if not _aware(a.expires_at) or a.expires_at <= t.now:
        return hold("ADMISSION_EXPIRED")
    return result(Eligibility.ELIGIBLE, "TABS_POLICY_ELIGIBLE_NOT_AUTHORIZED")
