"""Pure classifier tests: synthetic approvals are never runtime delivery state."""
import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import hashlib
from pathlib import Path

import pytest

import youtube_eligibility as e


HERE = Path(__file__).parent
CASES = json.loads((HERE / "fixtures/youtube_eligibility_rules.json").read_text(encoding="utf-8"))
VID = "video000001"
CHANNEL = "UC" + "x" * 22
NOW = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)


@pytest.fixture
def valid_input():
    def proof(ref, minutes=2):
        return e.Proof(VID, CHANNEL, "synthetic-" + ref, NOW - timedelta(minutes=minutes), "offline", True)
    return e.EligibilityInput(
        video_id=VID,
        history=e.HistoricalIdentity(frozenset({"history0001"}), frozenset(), frozenset(),
                                     datetime(2026, 10, 21, 1, 32, tzinfo=timezone.utc),
                                     "offline", "a" * 64, True, True),
        time=e.TimeEvidence(NOW, NOW - timedelta(minutes=55), clock_verified=True),
        policy=e.Policy(CHANNEL, "synthetic-test-v1", activation_at=NOW - timedelta(hours=2),
                        window=timedelta(hours=24), ttl=timedelta(minutes=30), skew=timedelta(minutes=5),
                        capture_interval=timedelta(minutes=10), approved=True),
        rss=e.RssEvidence(proof("rss", 1), "Synthetic regular video",
                          "https://www.youtube.com/watch?v=" + VID, "2026-10-22T01:00:00+00:00"),
        captures=(e.TabCapture(proof("capture-1", 15), frozenset({"videos"}), True, True),
                  e.TabCapture(proof("capture-2"), frozenset({"videos"}), True, True)),
        category=e.CategoryEvidence(e.Category.REGULAR, e.CategoryMethod.PUBLISHER, proof("category")),
        release=e.ReleaseEvidence(e.Readiness.PUBLIC_RELEASED, proof("release")),
        admission=e.AdmissionEvidence(proof("admission", 1), "synthetic-test-v1",
                                      "synthetic-history-review", "a" * 64, NOW + timedelta(minutes=10),
                                      reviewer="synthetic-offline-reviewer", approved=True),
    )


def change(obj, path, raw):
    """Fixture-only dataclass patcher; never loads evidence or approvals in production."""
    head, *tail = path.split(".")
    if isinstance(obj, tuple):
        values = list(obj)
        values[int(head)] = change(values[int(head)], ".".join(tail), raw)
        return tuple(values)
    current = getattr(obj, head)
    if tail:
        value = change(current, ".".join(tail), raw)
    elif raw is None:
        value = None
    elif isinstance(current, Enum):
        value = type(current)(raw)
    elif isinstance(current, datetime) or head in {"ended_at", "activation_at", "expires_at"}:
        value = datetime.fromisoformat(raw)
    elif isinstance(current, timedelta):
        value = timedelta(seconds=raw)
    elif isinstance(current, frozenset):
        value = frozenset(raw)
    elif isinstance(current, tuple):
        value = tuple(raw)
    elif head in {"pinned_eligibility", "previous_eligibility"}:
        value = e.Eligibility(raw)
    else:
        value = raw
    return replace(obj, **{head: value})


def scenario(valid_input, changes):
    for path, value in changes.items():
        valid_input = change(valid_input, path, value)
    return valid_input


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["rule"] + "-" + c["scenario"])
def test_all_design_rules(valid_input, case):
    item = scenario(valid_input, case["changes"])
    before = deepcopy(item)
    decision = e.classify(item)
    assert decision.eligibility.value == case["expected"]
    assert case["rule"] in decision.rule_ids
    assert decision.event_key == f"youtube:video:{VID}:initial"
    assert decision.delivery_state is item.journal.state
    assert decision.enqueue is False and decision.delivery_authorized is False
    assert item == before


def test_fixture_rule_coverage_is_complete():
    assert {case["rule"] for case in CASES} == {f"D{i:02d}" for i in range(1, 36)}
    required = {"regular_video_valid", "shorts_excluded", "rss_only", "upcoming_stream", "live_stream",
                "completed_replay", "published_changed_known_id", "historical_baseline", "duplicate_delivery",
                "partial_snapshot", "stale_evidence", "invalid_provenance", "activation_backlog",
                "pending_recovery_ambiguity"}
    assert required <= {case["scenario"] for case in CASES}


@pytest.mark.parametrize("path,value", [
    ("policy.approved", False), ("policy.version", ""), ("policy.activation_at", None),
    ("policy.window", None), ("policy.ttl", None), ("policy.skew", None), ("policy.capture_interval", None),
    ("policy.window", 0), ("policy.ttl", -1), ("policy.skew", -1), ("policy.capture_interval", 0),
    ("time.clock_verified", False), ("history.integrity_verified", False),
    ("history.schema_version", 2), ("history.fingerprint", "invalid"), ("history.scope", "production"),
    ("rss.proof.verified", False), ("rss.proof.reference", ""), ("rss.proof.video_id", "other000001"),
    ("rss.proof.scope", "production"), ("category.proof.verified", False),
    ("release.proof.verified", False), ("admission.proof.verified", False),
    ("admission.history_review_reference", ""), ("admission.policy_version", "different-version"),
    ("admission.approved", False), ("admission.reviewer", ""),
    ("admission.state_fingerprint", "b" * 64), ("admission.expires_at", "2026-10-22T02:00:00+00:00"),
    ("captures.0.videos_healthy", False), ("captures.1.streams_healthy", False),
    ("captures.1.title_conflict", True), ("captures.1.proof.reference", "synthetic-capture-1"),
    ("captures.1.proof.observed_at", "2026-10-22T01:45:00+00:00"),
    ("captures.1.tabs", ["shorts"]), ("policy.capture_interval", 1200),
    ("rss.published", "2026-10-22T01:00:00"), ("rss.published", "2026-02-30T01:00:00Z"),
    ("time.earliest_published", "invalid"), ("time.earliest_published", "2026-10-22T01:01:00Z"),
])
def test_fail_closed_when_any_required_gate_is_missing(valid_input, path, value):
    decision = e.classify(change(valid_input, path, value))
    assert decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert not decision.enqueue and not decision.delivery_authorized


@pytest.mark.parametrize("path", ["time.now", "time.first_seen_at", "history.cutover_at", "policy.activation_at",
                                  "rss.proof.observed_at", "category.proof.observed_at",
                                  "release.proof.observed_at", "admission.proof.observed_at",
                                  "captures.0.proof.observed_at"])
def test_naive_evidence_times_fail_closed(valid_input, path):
    decision = e.classify(change(valid_input, path, "2026-10-22T01:00:00"))
    assert decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize("mutate", [
    lambda v: None, lambda v: {}, lambda v: replace(v, policy=None),
    lambda v: replace(v, captures=[v.captures[0]]),
    lambda v: replace(v, history=replace(v.history, authority_verified="true")),
    lambda v: replace(v, category=replace(v.category, category="REGULAR")),
    lambda v: replace(v, reservation="UNRESERVED"),
    lambda v: replace(v, history=replace(v.history, baseline_ids={"history0001"})),
])
def test_runtime_type_errors_return_hold_instead_of_silently_coercing(valid_input, mutate):
    decision = e.classify(mutate(valid_input))
    assert decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW and decision.knowledge is None


@pytest.mark.parametrize("value", ["2026-10-21T01:32:00+00:00", "2026-10-20T23:59:59-01:00"])
def test_earliest_publication_prevents_mutable_rss_backfill(valid_input, value):
    item = replace(valid_input, metadata_changed=True, time=replace(valid_input.time, earliest_published=value))
    decision = e.classify(item)
    assert decision.eligibility is e.Eligibility.REJECTED and "D20" in decision.rule_ids
    assert item.rss.published == valid_input.rss.published


def test_known_metadata_cannot_create_a_new_event(valid_input):
    item = replace(valid_input, metadata_changed=True,
                   history=replace(valid_input.history, cache_ids=frozenset({VID})))
    decision = e.classify(item)
    assert decision.knowledge is e.Knowledge.KNOWN and decision.eligibility is e.Eligibility.REJECTED
    assert {"D04", "D26"} <= set(decision.rule_ids)
    assert decision.event_key == e.classify(valid_input).event_key


@pytest.mark.parametrize("shadow", ["", "NEW_CANDIDATE", "APPROVED"])
def test_observation_and_shadow_review_never_auto_approve(valid_input, shadow):
    item = replace(valid_input, shadow_review=shadow, admission=e.AdmissionEvidence())
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_policy_defaults_have_no_active_parameters_or_approval(valid_input):
    item = replace(valid_input, policy=e.Policy(CHANNEL, "unapproved-design"))
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert item.policy.window is None and item.policy.ttl is None and not item.policy.approved


def test_rss_only_exception_needs_both_policy_and_per_id_review(valid_input):
    item = replace(valid_input, captures=(), policy=replace(valid_input.policy, allow_rss_only_exception=True))
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    item = replace(item, admission=replace(item.admission, rss_only_exception=True))
    assert e.classify(item).eligibility is e.Eligibility.ELIGIBLE
    assert not e.classify(item).enqueue


def test_confirmed_shorts_rejected_even_when_rss_only(valid_input):
    item = replace(valid_input, captures=(), category=replace(valid_input.category, category=e.Category.SHORTS))
    assert e.classify(item).eligibility is e.Eligibility.REJECTED
    assert "D07" in e.classify(item).rule_ids


def test_approved_tab_surrogate_still_needs_separate_admission(valid_input):
    item = replace(valid_input, category=replace(valid_input.category, method=e.CategoryMethod.TAB_SURROGATE),
                   policy=replace(valid_input.policy, allow_tab_category_surrogate=True))
    assert e.classify(item).eligibility is e.Eligibility.ELIGIBLE
    assert e.classify(replace(item, admission=e.AdmissionEvidence())).eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize("case", [c for c in CASES if c["rule"] in {"D10", "D12", "D17", "D19"}],
                         ids=lambda c: c["rule"])
def test_eligible_is_not_a_reservation_or_delivery_permission(valid_input, case):
    item = scenario(valid_input, case["changes"])
    first, second = e.classify(item), e.classify(item)
    assert first == second and first.requires_atomic_reservation
    assert not first.enqueue and not first.delivery_authorized
    # Pure classification cannot implement CAS; a losing reservation is external evidence.
    loser = e.classify(replace(item, reservation=e.Reservation.OWNED_BY_OTHER))
    assert loser.eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_pending_recovery_never_becomes_a_new_initial(valid_input):
    item = scenario(valid_input, next(c["changes"] for c in CASES if c["rule"] == "D02"))
    item = replace(item, rss=None)  # An existing pinned batch can outlive RSS visibility.
    decision = e.classify(item)
    assert decision.knowledge is e.Knowledge.KNOWN and decision.recovery_only
    assert decision.eligibility is e.Eligibility.ELIGIBLE and not decision.requires_atomic_reservation
    expired = replace(item, admission=replace(item.admission, expires_at=NOW))
    assert e.classify(expired).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert e.classify(expired).delivery_state is e.DeliveryState.PARTIAL
    assert e.classify(replace(item, journal=replace(item.journal, acknowledgement_verified=True))).delivery_state is e.DeliveryState.PARTIAL


@pytest.mark.parametrize("change_kind", ["short", "upcoming", "live", "fingerprint", "event", "scope"])
def test_pending_revocation_or_mismatch_keeps_progress_held(valid_input, change_kind):
    item = scenario(valid_input, next(c["changes"] for c in CASES if c["rule"] == "D02"))
    changes = {
        "short": {"category.category": "SHORTS"}, "upcoming": {"release.readiness": "UPCOMING"},
        "live": {"release.readiness": "LIVE"}, "fingerprint": {"admission.state_fingerprint": "b" * 64},
        "event": {"journal.event_key": "youtube:video:other000001:initial"},
        "scope": {"journal.scope": "production"},
    }
    decision = e.classify(scenario(item, changes[change_kind]))
    assert decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert decision.delivery_state is e.DeliveryState.PARTIAL and not decision.enqueue


def test_old_delivered_event_remains_terminal_without_current_source_evidence(valid_input):
    item = scenario(valid_input, next(c["changes"] for c in CASES if c["rule"] == "D01"))
    item = replace(item, rss=None, captures=(), category=e.CategoryEvidence(),
                   admission=e.AdmissionEvidence(), policy=e.Policy(CHANNEL, "new-policy-not-approved"))
    assert e.classify(item).eligibility is e.Eligibility.ALREADY_DELIVERED
    assert e.classify(replace(item, journal=replace(item.journal, acknowledgement_verified=False))).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_configurable_boundaries_and_offsets(valid_input):
    one_hour = replace(valid_input, policy=replace(valid_input.policy, window=timedelta(hours=1)))
    assert e.classify(one_hour).eligibility is e.Eligibility.ELIGIBLE
    short_window = replace(one_hour, policy=replace(one_hour.policy, window=timedelta(minutes=59)))
    assert e.classify(short_window).eligibility is e.Eligibility.REJECTED
    different_offset = replace(one_hour, rss=replace(one_hour.rss, published="2026-10-22T08:00:00+07:00"))
    assert e.classify(different_offset) == e.classify(one_hour)
    strict_ttl = replace(valid_input, policy=replace(valid_input.policy, ttl=timedelta(minutes=10)))
    assert e.classify(strict_ttl).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_pure_deterministic_and_immutable_no_io(valid_input, monkeypatch):
    before = deepcopy(valid_input)
    def forbidden(*args, **kwargs):
        pytest.fail("Classifier must not perform I/O")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("os.open", forbidden)
    monkeypatch.setattr("os.replace", forbidden)
    monkeypatch.setattr("os.unlink", forbidden)
    assert e.classify(valid_input) == e.classify(valid_input)
    assert valid_input == before
    with pytest.raises(FrozenInstanceError):
        valid_input.policy.approved = False
    decision = e.classify(valid_input)
    assert asdict(decision)["enqueue"] is False


def test_classifier_stays_pure_and_delivery_modules_do_not_import_it():
    root = HERE.parent
    tree = ast.parse((root / "youtube_eligibility.py").read_text(encoding="utf-8"))
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports <= {"dataclasses", "datetime", "enum", "re", "types", "typing"}
    for name in ("monitor_runner.py", "notifier.py", "youtube_monitor.py", "youtube_channel_tabs.py", "youtube_shadow.py"):
        # The runner now has an authorized opt-in dry-run comparison adapter.
        # Configuration/report field names are not imports or delivery wiring.
        runtime = ast.parse((root / name).read_text(encoding="utf-8"))
        dependencies = {n.module for n in ast.walk(runtime) if isinstance(n, ast.ImportFrom)}
        dependencies |= {a.name for n in ast.walk(runtime) if isinstance(n, ast.Import) for a in n.names}
        assert "youtube_eligibility" not in dependencies


AUDIT = json.loads((HERE / "fixtures/youtube_eligibility_audit_candidates.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("row", AUDIT["candidates"], ids=lambda row: row["video_id"])
@pytest.mark.parametrize("approved_test_policy", [False, True], ids=["unapproved", "synthetic-activation-fence"])
def test_all_six_historical_candidates_stay_held_without_changing_original_state(
        valid_input, row, approved_test_policy):
    root = HERE.parent
    paths = [root / name for name in ("youtube_cache.json", "delivery_state.json", "announcements_cache.json")]
    before = {p: p.read_bytes() for p in paths}
    cache = json.loads(before[paths[0]])
    journal = json.loads(before[paths[1]])
    baseline = frozenset(journal["youtube_rss_cutover"]["baseline_ids"])
    journal_ids = frozenset(i["video_id"] for batch in journal["batches"] for i in batch["items"]
                            if i.get("source") == "youtube")
    cache_ids = frozenset(i["video_id"] for i in cache)
    assert len(cache) == AUDIT["original_cache_count"] == 129
    assert len(baseline) == AUDIT["original_baseline_count"] == 133
    assert row["video_id"] not in baseline | cache_ids | journal_ids
    observed = datetime.fromisoformat(AUDIT["rss_observed_at"])
    tab_observed = datetime.fromisoformat(AUDIT["tab_observed_at"])
    rss_proof = replace(valid_input.rss.proof, video_id=row["video_id"], observed_at=observed,
                         reference="saved-local-audit-rss", verified=False)
    item = replace(
        valid_input, video_id=row["video_id"],
        history=replace(valid_input.history, baseline_ids=baseline, cache_ids=cache_ids,
                        journal_ids=journal_ids, cutover_at=datetime.fromisoformat(AUDIT["cutover_at"]),
                        fingerprint=hashlib.sha256(before[paths[1]]).hexdigest()),
        time=e.TimeEvidence(observed + timedelta(minutes=2), min(observed, tab_observed),
                            earliest_published=row["published_rss"], clock_verified=True),
        policy=replace(valid_input.policy, approved=approved_test_policy,
                       activation_at=observed + timedelta(minutes=1)),
        rss=e.RssEvidence(rss_proof, row["title"], row["url"], row["published_rss"]),
        captures=(), category=e.CategoryEvidence(), release=e.ReleaseEvidence(),
        admission=e.AdmissionEvidence(),
    )
    # Even granting a synthetic transport-provenance assertion cannot grant backlog admission.
    if approved_test_policy:
        item = replace(item, rss=replace(item.rss, proof=replace(item.rss.proof, verified=True)))
    decision = e.classify(item)
    assert decision.knowledge is e.Knowledge.UNKNOWN
    assert decision.eligibility.value == row["expected"] == "HOLD_FOR_REVIEW"
    assert not decision.enqueue and not decision.delivery_authorized
    assert {p: p.read_bytes() for p in paths} == before
    assert "youtube:video:K94OBen4CiU:initial" in journal["batches"][0]["event_keys"]
    assert journal["batches"][0]["delivered"] is True


def test_real_historical_baseline_is_preserved_when_cache_is_absent_in_input(valid_input):
    state = json.loads((HERE.parent / "delivery_state.json").read_text(encoding="utf-8"))
    baseline = frozenset(state["youtube_rss_cutover"]["baseline_ids"])
    # These historical exclusions are identities only; no Shorts page or content is accessed.
    for vid in ("4ZlcLMqtPYI", "SPzLQ-H4Fcw", "oC6JCsrKdoc", "vH4lgwut_wI"):
        assert vid in baseline
        item = replace(valid_input, video_id=vid,
                       history=replace(valid_input.history, baseline_ids=baseline, cache_ids=frozenset()),
                       rss=None, captures=())
        decision = e.classify(item)
        assert decision.knowledge is e.Knowledge.KNOWN
        assert decision.eligibility is e.Eligibility.REJECTED and "D03" in decision.rule_ids


def test_publication_and_activation_fences_do_not_reset_on_metadata_change(valid_input):
    item = replace(valid_input, metadata_changed=True,
                   time=replace(valid_input.time, earliest_published="2026-10-21T23:59:00Z"))
    decision = e.classify(item)
    assert decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW and "D24" in decision.rule_ids
    assert item.time.earliest_published == "2026-10-21T23:59:00Z"


def test_scheduled_premiere_on_videos_is_not_public_release(valid_input):
    item = replace(valid_input, release=replace(valid_input.release, readiness=e.Readiness.UPCOMING))
    assert "D13" in e.classify(item).rule_ids


@pytest.mark.parametrize("ended", [None, NOW + timedelta(seconds=1), NOW.replace(tzinfo=None)])
def test_replay_needs_a_valid_past_end_time(valid_input, ended):
    item = scenario(valid_input, next(c["changes"] for c in CASES if c["rule"] == "D12"))
    item = replace(item, release=replace(item.release, ended_at=ended))
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_pending_requires_a_real_pin_not_an_unconfirmed_identity(valid_input):
    item = scenario(valid_input, next(c["changes"] for c in CASES if c["rule"] == "D02"))
    item = replace(item, journal=replace(item.journal, pinned_eligibility=None))
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_recaptured_held_identity_does_not_become_known_or_approved_automatically(valid_input):
    held = replace(valid_input, admission=e.AdmissionEvidence(), previous_eligibility=e.Eligibility.HOLD_FOR_REVIEW)
    assert e.classify(held).knowledge is e.Knowledge.UNKNOWN
    assert e.classify(held).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    renewed = replace(held, admission=valid_input.admission)
    assert e.classify(renewed).eligibility is e.Eligibility.ELIGIBLE
    assert e.classify(renewed).knowledge is e.Knowledge.UNKNOWN


def test_verified_rss_provenance_is_not_an_admission_approval(valid_input):
    admission = e.AdmissionEvidence(proof=valid_input.rss.proof, policy_version=valid_input.policy.version,
                                    history_review_reference="not-an-approval", state_fingerprint="a" * 64,
                                    expires_at=NOW + timedelta(minutes=5))
    assert e.classify(replace(valid_input, admission=admission)).eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize("changes", [
    {"journal.event_key": "youtube:video:video000001:initial"},
    {"journal.acknowledgement_verified": True}, {"journal.pinned_eligibility": "ELIGIBLE"},
])
def test_inconsistent_empty_journal_record_is_not_eligible(valid_input, changes):
    decision = e.classify(scenario(valid_input, changes))
    assert decision.eligibility is e.Eligibility.HOLD_FOR_REVIEW and "D05" in decision.rule_ids


def test_recovery_pin_cannot_mask_a_new_identity_conflict(valid_input):
    item = scenario(valid_input, next(c["changes"] for c in CASES if c["rule"] == "D02"))
    item = replace(item, release=replace(item.release, proof=replace(item.release.proof, video_id="other000001")))
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_mutable_publication_without_earliest_history_holds_unknown_identity(valid_input):
    item = replace(valid_input, metadata_changed=True)
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert "D27" in e.classify(item).rule_ids


def test_missing_visibility_preserves_only_a_still_valid_explicit_lease(valid_input):
    item = change(valid_input, "captures.1.tabs", [])
    item = replace(item, previous_eligibility=e.Eligibility.ELIGIBLE)
    decision = e.classify(item)
    assert decision.eligibility is e.Eligibility.ELIGIBLE and "D28" in decision.rule_ids
    assert not decision.enqueue
    # A label alone cannot survive a missing approval or an expired lease.
    assert e.classify(replace(item, admission=e.AdmissionEvidence())).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert e.classify(replace(item, admission=replace(item.admission, expires_at=NOW))).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert e.classify(replace(item, category=e.CategoryEvidence())).eligibility is e.Eligibility.HOLD_FOR_REVIEW
