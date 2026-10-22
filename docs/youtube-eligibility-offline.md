# Offline YouTube eligibility classifier

`youtube_eligibility.py` implements Phase 4.4C decisions as an isolated pure function. Nothing imports it from the runner, notifier, RSS monitor or shadow adapter. It does not fetch, read configuration/state files, inspect secrets, mutate evidence, reserve an event, enqueue a batch, or call Telegram.

## Typed API and trust boundary

`classify(EligibilityInput) -> Decision` accepts frozen dataclasses and immutable collections: `RssEvidence`, `TabCapture`, `HistoricalIdentity`, `JournalEvidence`, `CategoryEvidence`, `ReleaseEvidence`, `TimeEvidence`, `AdmissionEvidence`, and `Policy`. Knowledge (`KNOWN`/`UNKNOWN`, or null when state cannot be trusted), eligibility, and delivery state are independent. Pending and partial records are never inferred delivered.

`Proof.verified`, historical integrity/authority, acknowledgement and per-ID admission approval are assertions supplied by a trusted future caller. This module checks binding, scope, freshness, policy version and state fingerprint; it does **not** authenticate a manifest or establish that a reviewer actually approved evidence. Checksums, RSS provenance and shadow APPROVED are not admission by themselves. An upstream authenticated evidence adapter is outside this PR.

Policy approval and per-ID admission approval are separate and default false. Reviewer/history reference, decision expiry and state fingerprint are required. W (`window`), TTL (`ttl`), `skew`, and `capture_interval` are configurable; none has an active design default. `activation_at` is required for new admission, and the original historical cutover belongs to `HistoricalIdentity`. All clock values are supplied explicitly and must be timezone aware. The classifier never calls the wall clock.

Every result has `enqueue=False` and `delivery_authorized=False`, including ELIGIBLE and pinned recovery. ELIGIBLE for a new event reports `requires_atomic_reservation=True`; that is a requirement on a future caller, not an implemented lock/CAS or an acquired reservation. Repeated calls are deterministic and cannot guarantee exclusive ownership. D33 tests distinguish an unreserved event from externally reported ownership by another worker.

## Policy behavior

- Hybrid evidence; RSS-only holds unless both policy and per-ID exception are explicitly approved with the remaining evidence. Partial tab failures never become an RSS-only fallback.
- Unknown category holds; verified Shorts reject, including RSS-only. No Shorts page/content is accessed. Approved tab surrogate policy is explicit and off by default; tab membership is not inherently a category certificate.
- Streams are replay-only with verified public completed replay and a valid past end time. Upcoming, LIVE and scheduled premieres hold. Relative metadata is not parsed into publication or playback time.
- Delivered initial events remain terminal under a trusted historical acknowledgement, even if current source evidence is absent. Baseline/cache identities suppress new initial events without claiming delivery.
- Pending/partial records require the exact event, a pinned eligible decision and a current approved admission lease; they can recover after RSS leaves the window. New identity/scope conflicts, exclusion, expiry or uncertainty hold recovery without mutating progress. No recovery is actually performed.
- Changed metadata without `earliest_published` holds rather than resetting age. Published drift uses `earliest_published`; pre-cutover publication rejects new initial events, pre-activation observations/publication hold backlog, and old candidates exceed the configurable age window. No timestamp, identity or first-seen is rewritten.
- Missing visibility never declares deletion. A previously eligible decision can remain ELIGIBLE only while its explicit admission lease, category/release and all other evidence remain valid; a previous label alone cannot auto-approve absence. Historical/delivered outcomes retain precedence.

Existing `youtube:video:<id>:initial` identity remains independent of policy version. There is no cache/observation ledger writer and no migration. Historical cache 129, baseline 133 and message-271 event are unchanged. The six audit records are sanitized facts, not approved delivery payloads or empty production identity sets.

## Rules and fixtures

The following synthetic scenarios cover every design rule D01–D35. Their explicit test approvals exist only as in-memory evidence. No synthetic delivered acknowledgement is written to the application journal.

| Rule | Scenario | Expected offline result |
|---|---|---|
| D01 | duplicate_delivery | ALREADY_DELIVERED |
| D02 | pinned_partial_recovery | ELIGIBLE |
| D03 | historical_baseline | REJECTED |
| D04 | historical_cache | REJECTED |
| D05 | journal_identity_without_confirmation | HOLD_FOR_REVIEW |
| D06 | invalid_state_authority | HOLD_FOR_REVIEW |
| D07 | shorts_excluded | REJECTED |
| D08 | wrong_channel | REJECTED |
| D09 | conflicting_sources | HOLD_FOR_REVIEW |
| D10 | regular_video_valid | ELIGIBLE |
| D11 | tab_membership_not_certificate | HOLD_FOR_REVIEW |
| D12 | completed_replay | ELIGIBLE |
| D13 | upcoming_stream | HOLD_FOR_REVIEW |
| D14 | stream_badge_without_readiness | HOLD_FOR_REVIEW |
| D15 | live_stream | HOLD_FOR_REVIEW |
| D16 | rss_only | HOLD_FOR_REVIEW |
| D17 | rss_only_explicit_exception | ELIGIBLE |
| D18 | multitab_only | HOLD_FOR_REVIEW |
| D19 | same_id_both_tabs | ELIGIBLE |
| D20 | pre_cutover | REJECTED |
| D21 | post_cutover_insufficient | HOLD_FOR_REVIEW |
| D22 | invalid_publication | HOLD_FOR_REVIEW |
| D23 | future_publication | HOLD_FOR_REVIEW |
| D24 | activation_backlog | HOLD_FOR_REVIEW |
| D25 | age_window_exceeded | REJECTED |
| D26 | published_changed_known_id | REJECTED |
| D27 | metadata_changed_unknown | HOLD_FOR_REVIEW |
| D28 | not_visible_in_capture | HOLD_FOR_REVIEW |
| D29 | partial_snapshot | HOLD_FOR_REVIEW |
| D30 | invalid_provenance | HOLD_FOR_REVIEW |
| D31 | shadow_approved_only | HOLD_FOR_REVIEW |
| D32 | eligible_never_authorizes_delivery | ELIGIBLE |
| D33 | reservation_owned_by_other | HOLD_FOR_REVIEW |
| D34 | pending_recovery_ambiguity | HOLD_FOR_REVIEW |
| D35 | hold_reviewed_with_new_evidence | ELIGIBLE |

Fixtures: `tests/fixtures/youtube_eligibility_rules.json` (35 rules plus stale-evidence case), and `tests/fixtures/youtube_eligibility_audit_candidates.json` (six historical records). Tests also cover typed malformed input, proof binding, expiry, scope, clock/offset boundaries, immutable inputs, forbidden I/O, pending ambiguities, default no approval and no enqueue.

All six historical candidates remain UNKNOWN / HOLD_FOR_REVIEW. Tests exercise the unapproved policy and a separately named synthetic activation-fence policy, with the real baseline/cache loaded read-only for identity membership. These tests do not certify category or live readiness.

## Validation and limits

Run the entire regression suite, not only the new file: `python -B -m pytest`. In this PR all real HTTP/secret-file loading remains blocked by existing test fixtures. Local full regression: **790 passed, 26 subtests passed**, including **146 new classifier tests**.

No changes to runner/notifier/adapters/workflows, no delivery integration, no live Telegram, no dispatch and no production policy activation. This is suitable for offline policy review; production remains NO-GO until operator decisions, provenance authentication, atomic reservation and a separately reviewed integration exist. Rolling back this offline PR needs no application state restoration.
