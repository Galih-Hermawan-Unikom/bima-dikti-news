# YouTube channel-tabs eligibility

`youtube_eligibility.py` is a pure, offline classifier used by the tabs-only
dry-run. Its discovery evidence comes from the channel `/videos` and `/streams`
tabs. It has no RSS input, network access, filesystem access, clock reads,
notifier calls, enqueue operation, or delivery authorization.

## Evidence model

`EligibilityInput` keeps separate identity knowledge, eligibility and delivery
state. It receives typed channel-tab captures, trusted historical identities,
journal state, category and release evidence, observation time, policy and an
optional operator admission record. A source timestamp such as `published_at`
does not establish eligibility or replace observation time.

`first_seen_at` is the earliest captured observation for an ID after the
activation fence. It is not a publication date. The first healthy scan is an
inventory and all of its IDs remain held. Partial or failed scans also establish
an inventory quarantine; a later healthy scan re-inventories visible IDs rather
than backfilling a gap. Losing in-memory observations on restart causes a new
inventory quarantine. No observation ledger or application state is written.

## Decisions

| Condition | Knowledge | Eligibility | Delivery state / reason |
|---|---|---|---|
| ID in verified baseline or cache | KNOWN | REJECTED | `HISTORICAL_INITIAL_SUPPRESSED` |
| Confirmed delivered journal event | KNOWN | REJECTED | ALREADY_DELIVERED, terminal |
| Journal says pending, partial, or outcome unclear | KNOWN | HOLD_FOR_REVIEW | Operator reconciliation required |
| ID absent from all trusted identity stores | UNKNOWN | Evaluate evidence | Unknown is not proof of a new publication |
| Startup inventory, pre-fence `first_seen_at`, stale/partial capture | UNKNOWN | HOLD_FOR_REVIEW | No automatic backfill |
| Category unknown or inferred only from tab membership | UNKNOWN | HOLD_FOR_REVIEW | Category proof required |
| Verified Shorts category | UNKNOWN | REJECTED | No Shorts page or player is read |
| Upcoming or LIVE stream | UNKNOWN | HOLD_FOR_REVIEW | Not a released replay |
| Completed replay without verified end/readiness evidence | UNKNOWN | HOLD_FOR_REVIEW | Replay readiness incomplete |
| Verified regular public video with all policy evidence | UNKNOWN | ELIGIBLE | Still report-only; no delivery authorization |
| Verified completed public replay with all policy evidence | UNKNOWN | ELIGIBLE | Still report-only; no delivery authorization |
| ID not present in a later capture | Preserve prior knowledge | HOLD_FOR_REVIEW | Absence does not mean deletion |

`ELIGIBLE` means only that the supplied evidence satisfies the offline policy.
Every decision sets `enqueue=False` and `delivery_authorized=False`; an atomic
reservation would be required by any future delivery design.

## Policy and review

The default policy is unapproved and has no production window, TTL, clock skew,
or capture interval. These values must be explicitly supplied in a future
reviewed configuration. New IDs require an activation time and a first-seen
strictly after that fence. A reviewed record must bind the video ID, channel,
scope, policy version, state fingerprint, reviewer and the observed first-seen
value. Review evidence does not change the observation timestamp or establish
YouTube category/readiness by itself.

Historical baseline, cache and delivery journal remain distinct inputs. The
existing baseline has 133 IDs, including the four historical Shorts identities;
none is removed or rewritten. The 129-entry YouTube cache and prior journal
events remain read-only. Existing pending or ambiguous YouTube events are held;
this classifier does not resume them. There is no automatic backfill or state
migration.

## Validation boundary

`tests/test_youtube_tabs_policy.py` covers policy gates, malformed evidence,
activation and observation-time boundaries, historical suppression, category
and replay readiness, pending/ambiguous journal states, metadata changes,
absence, and the no-enqueue contract. Synthetic approved proofs in tests are
fixtures only. The live runner reports tab health and candidate decisions in
dry-run mode and keeps YouTube items out of the BIMA delivery path.
