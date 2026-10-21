# YouTube /videos: Read-Only Parser Preflight

## Scope and Audit

Base master: `257df8223105d74b1478871b8248d6aa4647a4dd`.
Production monitoring remains disabled. This PR does not install the new reader
in `monitor_runner.py`, replace/delete RSS, change BIMA, delivery guards, cron,
configuration, caches, or journal. The user's dirty main worktree is untouched.

The ignored experiment `eksperimen/youtube_monitor_v2.py` provided the
ytInitialData -> selected tab -> richGridRenderer -> richItemRenderer ->
lockupViewModel path, contentId/watchEndpoint fallback, title and thumbnail
paths. Its /streams merge, status heuristics, default VOD, hard-coded consent
cookie and exception-to-empty fallback are deliberately NOT adopted.

Current contracts audited:

- `youtube_monitor.py`: RSS-only detection, stable `video_id`, known IDs from
  cache and journal; approved `youtube_rss_cutover` adds baseline IDs and an
  absolute publication cutoff. RSS retry code remains unchanged.
- `monitor_runner.py`: initial event `youtube:video:<id>:initial`, journal
  before detection cache, chunk/document checkpoints, retry and persistence.
- `reliability.py`/`notifier.py`: only complete `youtube-rss-v1` payloads
  without status are eligible. Cache/KNOWN are not proof of delivered.
- `app_config.py`: env/file configuration belongs to existing production
  runners. New preflight never calls load_config or reads .env/Secrets.
- State at base: BIMA 90, YouTube cache 129, approved baseline 133 IDs, journal
  one delivered / zero pending. K94OBen4CiU initial (message 271) is retained.
- Existing main.yml remains the sole scheduled writer. tests.yml remains
  unchanged. New workflow is manual only, contents:read, no credentials retained.

## New Components

`youtube_videos_parser.py` performs one requests GET to the fixed /videos URL,
15-second connect/read timeout, no redirects, proxy, authentication, browser,
pagination or retries. No /streams, /shorts, watch/player or Data API request.
Video URLs and thumbnails are returned metadata, never fetched.

HTML script extraction plus JSONDecoder.raw_decode handles multiline JSON and
braces/semicolons inside titles. Only the selected videos tab is traversed.
Required fields are video_id, title, actual channel_title and thumbnail URL.
Duplicate IDs are collapsed in source order; conflicting titles fail closed.
Missing/invalid metadata among valid cards fails the whole parse with a count;
partial extraction is not silently reported healthy.

Results use `observation_format=youtube-videos-observation-v1`. They have no
`delivery_format`, status, publication timestamp, scheduled time or update flag.
Relative metadata is retained as source text only; no conversion to dates.

`youtube_videos_preflight.py` reads cache/journal and validates IDs and any
existing RSS cutover marker before network access. It compares the ID union
from cache, all journal items/events and baseline, outputting KNOWN/UNKNOWN.
Neither label means delivered or new publication. Both remain ineligible for
delivery under the UNCHANGED production guard, including direct Notifier calls.

Failure categories distinguish HTTP failure, timeout, network error, redirect,
missing/invalid/ambiguous ytInitialData, tab/grid changes, invalid lockups and
unverified empty results. SourceError-compatible exceptions and exit 1 make
failures visible without body, cookie, signed URL or raw exception output.
Zero results only pass on a recognized explicit empty-channel message in the
selected videos tab; empty grids/consent/unrecognized layouts fail closed.
Supported empty-message shapes/languages are intentionally narrow and fixture
tested, not a claim of exhaustive YouTube localization support.

## Metadata and Source Limitations

Unlike RSS, this reader has NO trustworthy absolute publication time. Appearing
in /videos is not proof of VOD, availability, or publication after cutover.
Only the initial visible regular-video page is inspected, not a complete channel
inventory. Sorting, localized UI, delayed visibility, A/B tests and renderer
changes may hide/reorder historical items. Streams and Shorts are outside scope.

One local read-only validation of this implementation returned HTTP 200,
30 observations, 30 KNOWN, zero UNKNOWN and zero parsing errors. The experiment
previously limited output to 10; the new parser validates the whole visible grid
without assuming a fixed count. This is local evidence, NOT a successful GitHub
preflight and not proof of long-term source stability.
Comparison uses the cache/journal snapshot checked out at the selected ref, not
an automatically refreshed remote state. Before any future preflight, review
that snapshot against current master and verify the run head SHA; a stale ref
can produce UNKNOWN without implying a newly published video.

## Manual GitHub Preflight

New file: `.github/workflows/youtube-videos-preflight.yml`.
It uses workflow_dispatch only, contents:read, a three-minute job timeout,
requests only and `persist-credentials: false`. It never runs the production
runner, scheduler or notifier, never sends Telegram, never pushes or writes
state. It prints HTTP status, count, parsing-error count and ID/title/knowledge.
UNKNOWN observations are only reported; they do not cause delivery or journaling.

After separate review/approval and once the workflow exists on the default
branch, an operator can run it manually on an explicitly reviewed ref:

```powershell
gh workflow run youtube-videos-preflight.yml -R Galih-Hermawan-Unikom/bima-dikti-news --ref <reviewed-ref>
```

A newly added workflow normally must exist on the default branch before manual
dispatch becomes available. This PR does not merge or dispatch it. No temporary
push-trigger diagnostic or production-workflow modification is used to bypass
that prerequisite. CI regression success is not evidence that GitHub can read
the live /videos page; report that separately after a real approved run.

## Future Delivery and Cutover Proposal (Not Implemented)

Do NOT mark observations as youtube-rss-v1 or weaken existing guards.
Do NOT reuse relative time, first_seen, or the existing RSS cutoff as publication
proof. A baseline cannot establish that every cached item was delivered.

Before a future delivery integration, freeze writers under separate approval,
backup final remote state, compare all visible IDs with the 133-ID/historical
union and inspect journal progress. Operator must reconcile every UNKNOWN,
including older unseen/reordered videos. Preserve rows, batches, event keys,
attempts and message 271 without synthetic delivered records.

A future contract (e.g. youtube-videos-v1) needs explicit review and delivery
approval evidence per unknown ID, or a separately verified absolute publication
source. A separately approved observation baseline could suppress known history,
but merely first seeing an ID after that baseline does not prove recent upload.
Keep such IDs UNKNOWN/held until approved. No schema, baseline, cutoff or
delivery event is created by this implementation.

Preserve the stable initial event key when a future contract is approved so
existing delivered events still suppress replay. Pending mixed/legacy batches
must remain unchanged and require explicit operator review.

## Rollback

At this stage there is no production switch to roll back: stop/ignore the manual
preflight. If this read-only code is later merged, RSS and production runner
continue unchanged; removal/revert of the new module/workflow requires review
but no cache/journal reset. Never revert historical state to hide an error.
Future delivery activation needs its own backup, rollback and duplicate-risk
procedure; exactly-once remains unguaranteed across checkpoint/push failures.

## Validation

Fixtures are synthetic HTML/JSON reflecting the experiment's lockup paths.
Automated tests block external network and .env reads. Coverage includes normal/
multiple cards, JSON boundaries, duplicate/invalid IDs, metadata/DOM changes,
HTTP errors, timeout/network failure, missing initial data, verified/unverified
empty feeds, KNOWN/UNKNOWN, unchanged temp state, stable initial keys and direct
delivery rejection. The full existing BIMA/RSS/reliability suite is also run.

Local final regression result: **323 passed, 0 failed** (274 existing cases +
49 parser/preflight cases). Production files, caches/journal, RSS implementation
and existing workflows have no diff. Network in automated tests is mocked;
the single HTTP 200 / 30 KNOWN observation above is a separate local read-only
check. The new manual GitHub preflight has NOT been run.
