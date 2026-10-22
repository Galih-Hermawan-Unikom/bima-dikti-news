# Hybrid YouTube eligibility dry-run

This adds an opt-in comparison path to the reliability runner. It does not
integrate delivery, activate a policy, migrate state or create an outbox.

```sh
python run_github.py --dry-run --youtube-eligibility --state-dir /isolated/state
```

This command reads current public RSS, `/videos` and `/streams` when run by an
operator. Development tests inject saved/synthetic sources instead; no live
acquisition or Telegram was performed for this PR. The flag requires `--dry-run`
before config loading, and dry-run excludes `.env` and Telegram secrets. No flag
is added to existing workflows/configuration. Normal runs retain the existing
reliability path. Selecting hybrid through the Python API also requires dry-run,
even when notification_method is console. There is no fallback to legacy RSS
delivery if hybrid evaluation fails.
The `main.py` entrypoint delegates the hybrid flag to the same guarded parser,
including rejection before secret loading when `--dry-run` is omitted.

## Flow and boundary

`ChannelBoundRSS` validates the feed owner and every entry's channel ID, retaining
the existing RSS payload contract. `read_channel_tabs` uses its existing fixed
two-tab allowlist and parser. Page failure, missing timestamp, empty observation
cycles and continuation limits are carried explicitly. Failure of either tab
holds new admission; it cannot turn into a permissive RSS-only exception.
Pagination remains unsupported: healthy means a valid initial page, not complete
channel coverage. No Shorts endpoint, per-ID player or external API is added.

`HybridYouTubeAdapter.evaluate(cache, journal)` validates the reliability journal
and existing cutover marker. Missing/corrupt history fails before acquisition;
it never substitutes an empty baseline or plans a cutover. It builds typed
classifier inputs and calls `youtube_eligibility.classify`. Knowledge, eligibility
and delivery state remain separate. Baseline identities suppress initial events;
confirmed delivered events remain terminal; old pending/partial records do not
gain synthetic pinned admission. No mutation or migration of the 129-entry cache,
133-ID baseline, or historical message-271 event is needed.

Default policy is unapproved, all timing parameters inactive and category,
readiness and admission unknown. Tab membership/badges/relative schedule text
never become category or release certificates. Streams require a verified
completed public replay; upcoming/LIVE hold. Four historical Shorts remain
baseline tombstones; existing mixed RSS evidence does not authorize Shorts.

An adapter instance retains only two captures and earliest observed publication
and first-seen values in memory. Inject the same instance into repeated runner
dry-runs to compare stable captures spaced according to an explicitly configured
policy. No process restart restores an admission lease. This PR does not add an
observation-ledger writer; durable history is a separate future integration.
Signed review history supplies a reviewed earliest publication/first-seen when
available; neither later metadata nor a restart is permission for backfill.

`RunResult.youtube_eligibility` and CLI JSON expose per-ID decisions, category,
readiness, review reference and source health. Only policy-ELIGIBLE *new* RSS
payloads enter the console preview. Every decision still has `enqueue=false` and
`delivery_authorized=false`. Existing pending YouTube batches (including mixed
batches) are withheld from preview/recovery and retain exact progress/chunks.
No `send_notification`, Telegram call, journal/cache save or synthetic delivered
acknowledgement occurs on this path. BIMA detection/console preview continues.

## Positive category/readiness evidence

The default does **not** claim real evidence where it is missing. For the six
historical candidates the available audit proves neither category nor completed
replay, so the real-data path remains HOLD. Positive tests use explicitly named
synthetic reviews, not claims about those candidates.

`SignedReviewVerifier` accepts an explicitly injected authority-key mapping
(`key_id -> bytes`, minimum 32 bytes), allowed HTTPS source hosts, and envelopes
`{key_id, payload, signature}`. It never reads a key, file, environment variable
or URL. HMAC-SHA256 authenticates an operator review; it does not independently
prove YouTube's playback state. The operator must actually inspect the referenced
evidence, and the configured authority must be trusted for that review. A checksum,
shadow APPROVED or caller-supplied verified boolean is not a signed review.

Payload schema `operator-youtube-review-v1` has exactly these fields:

`schema, video_id, channel_id, scope, policy_version, state_fingerprint, category,
readiness, source_url, reviewer, history_review_reference, observed_at, expires_at,
ended_at, first_seen_at, earliest_published, approved`.

Category/readiness are classifier enum values. All times are timezone aware;
`ended_at` may be null except completed replay must have a past end time to pass
the classifier. Source must be HTTPS on the explicit allowlist, without credentials
or a Shorts path; YouTube source URLs must be the exact canonical watch URL bound
to the reviewed ID. No source URL is fetched by this verifier. `approved` must be
the boolean true, with a named reviewer/history reference; a signature alone does
not satisfy admission. Fingerprint is `history_fingerprint(cache, journal)`.
Signature covers UTF-8 JSON of payload using `sort_keys=True`,
`separators=(',', ':')`, `ensure_ascii=True`. Keys are never printed or included
in the report. Rejected envelopes produce a fixed safe reason; changing any
signed field, target scope, state fingerprint, policy version or key invalidates
admission. Policy approval remains a separate explicit `Policy` argument.

Programmatic tests/use pass `HybridYouTubeAdapter(policy=..., reviews=...,
review_verifier=..., ...)` through `run_once(..., dry_run=True,
youtube_adapter=adapter)`. The CLI configures no authority, signing key or approval;
the CLI comparison therefore cannot silently admit unknown videos. Publisher
attestations/live evidence automation are not invented by this PR. Keep signing
keys outside Git and use the separate operator process to supply real reviews.

## Validation and remaining experiment blockers

Tests cover XML/HTML source parsing through the adapter, strict feed channel
binding, both tabs/failure with zero observations, signed-review tampering and
expiry, regular video/replay positive controls, Shorts/upcoming/LIVE exclusions,
RSS-only, activation backlog, earliest-publication drift, missing history,
delivered dedup, pending recovery withholding, runner console preview with a
notifier mock, zero real sends, CLI secret-free dry-run and no legacy fallback.
Existing network guards block all external HTTP. Full regression is required.

The mock experiment path is testable now. A real experiment using a new candidate
still needs actual ID-bound category/release evidence, accepted timing/activation
policy and explicit per-ID review. Real Telegram also requires a separately
authorized send path and current experiment-target preflight; this PR deliberately
rejects non-dry-run hybrid calls. Previous permission for one smoke batch does not
authorize another. Production CAS/outbox integration and durable observation
history remain future production concerns, not prerequisites to these mock tests.

No live Telegram, workflow activation/dispatch, automatic backfill, cutover or
merge is part of this change. Rollback of this comparison path is removal of the
opt-in flag; no application state restoration is required.
