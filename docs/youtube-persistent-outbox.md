# YouTube persistent observations and outbox

This feature adds an optional tabs-only delivery path. It is deliberately
separate from `youtube_cache.json`, the historical 133-ID baseline, and
`delivery_state.json`. Its SQLite file is `youtube_delivery.sqlite3` under the
runner's state directory and is ignored by Git. No historical JSON is migrated
or rewritten.

All three controls default to `false`: `youtube_observation_enabled`,
`youtube_delivery_enabled`, and `youtube_delivery_send_enabled`. Observation
and reservation require an explicit activation timestamp, policy approval,
and current policy evidence. Sending requires all three controls and a
non-dry-run invocation. The repository workflow remains disabled; these flags
do not enable or dispatch it.

The first healthy two-tab capture at or after the activation fence is recorded
as initial inventory. Its IDs receive persistent `first_seen_at` values marked
as inventory and are never promoted to new arrivals automatically. A later ID
first observed after inventory is recorded with its first-seen timestamp.
Eligibility still requires two consecutive healthy captures, both tabs,
category evidence, release/readiness evidence, freshness policy, and all
classifier checks. A timestamp is an observation fact, not `published_at`.
No RSS, Shorts, player pages, backfill, or synthetic approval evidence is used.

Eligible decisions may be reserved in the separate SQLite outbox. Unique event
keys and `BEGIN IMMEDIATE` make reservation atomic across concurrent runner
instances. The sender claims the reservation before its single attempt.
Confirmed Telegram `sendMessage` responses record the message ID and become
`delivered`. Network errors, incomplete responses, and process recovery after
a claimed send become `ambiguous`; they are never resent automatically.
An expired `sending` lease is recovered as ambiguous, not retried. Operator
review is required before any manually authorized follow-up. A legacy pending
or ambiguous historical journal entry blocks eligibility as before.

The specialized notifier accepts only a valid outbox ticket whose event key,
video ID, payload hash, canonical URL, tabs-only format, and single-message
shape match. Generic notifier calls continue to reject unverified YouTube
payloads. BIMA retains its existing independent scraper, cache, journal, and
notification path.

## Remaining requirements for one Experiment Channel smoke test

This implementation is not approval to send. Before a separately authorized
single test, operators must establish a reviewed activation fence and policy,
provide current non-synthetic category/readiness evidence for a real eligible
video, and verify the experiment destination and bot's message permission
against the existing allowlist. The three flags must remain off until that
preflight is reviewed. If no real candidate satisfies the classifier, the
smoke test is blocked; do not manufacture evidence or promote a held item.
No production destination, workflow, cutover, or real Telegram request is
enabled by this change.
