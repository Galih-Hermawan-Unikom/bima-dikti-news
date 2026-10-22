# YouTube persistent observations and outbox

This feature adds an optional tabs-only delivery path. It is deliberately
separate from `youtube_cache.json`, the historical 133-ID baseline, and
`delivery_state.json`. GitHub Actions loads a versioned JSON snapshot from the
dedicated `youtube-state` branch and checkpoints every committed mutation to
that branch through one GitHub Contents API update guarded by the blob SHA. The
file lives at `.bot-state/youtube_delivery_state.json`; it is not stored on a disposable runner
or mixed into historical cache/journal files. No historical JSON is migrated
or rewritten. The state branch must be explicitly created before observation
is enabled; a missing branch fails closed.

The JSON document stores `first_seen_at`, initial inventory, captures, event
identities, outbox payloads, and send status under one schema version. Each
durable checkpoint replaces that one complete document; there is no companion
manifest or second file to reconcile. A conflicting SHA fails closed without
retrying the write.

All three controls default to `false`: `youtube_observation_enabled`,
`youtube_delivery_enabled`, and `youtube_delivery_send_enabled`. Observation
and reservation require an explicit activation timestamp, policy approval,
and current policy evidence. In Actions, a file-SHA compare-and-swap prevents
stale writers from overwriting the branch. The existing monitoring workflow
concurrency group serializes its own runs; an external conflicting writer
causes a fail-closed error. A durable `sending` checkpoint must succeed before
the notifier is called. Sending requires all three controls, a non-dry-run
invocation, and the Experiment Channel allowlist. All three controls default to
false in both application config and workflow variables; adding the wiring
does not enable the workflow.

The first healthy two-tab capture at or after the activation fence is recorded
as initial inventory. Its IDs receive persistent `first_seen_at` values marked
as inventory and are never promoted to new arrivals automatically. A later ID
first observed after inventory is recorded with its first-seen timestamp.
Eligibility still requires two consecutive healthy captures, both tabs,
category evidence, release/readiness evidence, freshness policy, and all
classifier checks. Signed review envelopes are loaded from the protected
`YOUTUBE_REVIEW_EVIDENCE_JSON` secret and verified against the separate
`YOUTUBE_REVIEW_HMAC_KEY`, configured key ID, and source-host allowlist. Missing
or invalid evidence leaves the item held. A timestamp is an observation fact,
not `published_at`. No RSS, Shorts, player pages, backfill, or synthetic
approval evidence is used for real operation. A no-send inspection can load only
the review secrets with `--youtube-review-preview`; Telegram and state-write
credentials remain unavailable to that dry-run.

Eligible decisions may be reserved in the JSON outbox. Unique event keys and
the remote compare-and-swap make reservation atomic across Actions runs. The
sender claims the reservation and persists that claim before its single attempt.
Confirmed Telegram `sendMessage` responses record the message ID and become
`delivered`. Network errors, incomplete responses, and process recovery after
a claimed send become `ambiguous`; they are never resent automatically.
An expired `sending` lease is recovered as ambiguous, not retried. Operator
review is required before any manually authorized follow-up. A legacy pending
or ambiguous historical journal entry blocks eligibility as before.

YouTube uses a dedicated `YOUTUBE_EXPERIMENT_CHAT_ID`, never the generic
production `TELEGRAM_CHAT_ID`. Sending also requires its separately configured
SHA-256 allowlist value; Actions rejects a target equal to the production chat
ID. The explicit preflight checks the allowlist, confirms the destination is a
channel, and verifies the bot has post permission. It uses read-only Telegram
API methods and sends no message.

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
against the allowlist. The state branch must first be created without copying
cache, baseline, or journal data. The three flags must remain off until that
preflight is reviewed. If no real candidate satisfies the classifier, the
smoke test is blocked; do not manufacture evidence or promote a held item.
No production destination, workflow, cutover, or real Telegram request is
enabled by this change.

After authorization, initialize the state branch as an empty orphan branch so
it does not inherit cache or journal files:

```powershell
$stateRepo = Join-Path $env:TEMP ("bima-youtube-state-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $stateRepo | Out-Null
git -C $stateRepo init
git -C $stateRepo checkout --orphan youtube-state
Set-Content -LiteralPath (Join-Path $stateRepo "README.md") -Value "Durable YouTube observation and outbox state only."
git -C $stateRepo add README.md
git -C $stateRepo -c user.name="State Bootstrap" -c user.email="state-bootstrap@users.noreply.github.com" commit -m "Initialize isolated YouTube state branch"
git -C $stateRepo remote add origin https://github.com/Galih-Hermawan-Unikom/bima-dikti-news.git
git -C $stateRepo push --set-upstream origin youtube-state
```

Initialize the empty JSON state separately, with all observation/delivery
controls off. The command writes the empty versioned document; it does not
fetch BIMA or YouTube and does not contact Telegram.
Use an activation timestamp and policy version approved by the operator:

```powershell
$env:GITHUB_REPOSITORY = "Galih-Hermawan-Unikom/bima-dikti-news"
$env:YOUTUBE_STATE_TOKEN = gh auth token
$env:YOUTUBE_STATE_INITIALIZE = "true"
$env:YOUTUBE_ACTIVATION_AT = "<approved-UTC-activation-time>"
$env:YOUTUBE_POLICY_VERSION = "<approved-policy-version>"
python run_github.py --initialize-youtube-state --config <preflight-config.json> --state-dir <isolated-state-dir>
Remove-Item Env:YOUTUBE_STATE_TOKEN, Env:YOUTUBE_STATE_INITIALIZE
```

For a local test, copy the historical references into a new isolated directory;
the application uses copies and leaves the originals untouched:

```powershell
$stateDir = Join-Path $env:TEMP "bima-youtube-experiment-state"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
Copy-Item announcements_cache.json, youtube_cache.json, delivery_state.json -Destination $stateDir
```

After separate operator authorization, prepare a local config with observation
enabled and both delivery flags disabled, then run two dry-run captures using
the same isolated state directory:

```powershell
python run_github.py --dry-run --youtube-eligibility --youtube-review-preview --config <preflight-config.json> --state-dir <isolated-state-dir>
```

Verify the Experiment Channel destination and bot permissions without sending:

```powershell
python run_github.py --youtube-experiment-preflight --config <experiment-config.json>
```

Only after that check and a separate one-send authorization, use the dedicated
single-item command. It skips BIMA delivery, requires all three explicit
YouTube flags, performs the read-only Telegram preflight again, and sends at
most one eligible summary:

```powershell
python run_github.py --youtube-experiment-smoke-once --confirm-experiment-destination --config <experiment-config.json> --state-dir <isolated-state-dir>
```

For Actions, create the `youtube-state` branch before setting
`YOUTUBE_OBSERVATION_ENABLED=true`; use the provided `GITHUB_TOKEN` with
contents write permission. Never set delivery variables or enable the
workflow as part of this PR.
