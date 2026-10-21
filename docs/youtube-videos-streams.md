# Phase 4.1: read-only videos and streams

## Scope and operator decision

Shorts are excluded from future DIKTI News monitoring. Only `/videos` and
`/streams` are evaluated here. Existing historical Shorts identities remain
in the approved 133-ID baseline; the 44 unknown Shorts found during the earlier
coverage audit are NOT imported, approved, cached or queued.

This implementation is additive and read-only. It does not alter the Phase 3
parser, live workflow, snapshot/reconciliation engine, production RSS monitor,
BIMA scraper, notifier, delivery contract, workflow, cron, Secrets or state.
No new workflow or live request is executed as part of this implementation.

## Data flow

```text
Explicit evaluation command
  -> read existing cache + journal + baseline identities (no .env)
  -> one GET /videos, one GET /streams (no retry or redirect following)
  -> strict channel ID and selected-tab endpoint checks
  -> existing ytInitialData extraction and lockupViewModel helper
  -> read-only observations grouped by video_id
  -> JSON on stdout only; no cache/journal or delivery writes
```

`youtube_channel_tabs.py` reuses the Phase 3 Session/User-Agent configuration,
`extract_initial_data()`, `_parse_lockup()` and safe thumbnail projection.
For `/videos`, it additionally delegates to the unchanged `parse_videos_html()`.
The new adapter requires channel ID `UCGo_6l_6kp8H8OHcKcSIeDw` and an explicit
relative endpoint for the requested tab, accepting the handle or matching
`/channel/<id>/<tab>` alias. A missing/wrong channel, conflicting selected tabs,
missing endpoint, wrong host, query/fragment, or wrong tab fails closed.
The more permissive legacy parser interface itself is intentionally unchanged.

Transport is fixed to two allowlisted URLs, with timeout 15 seconds, redirects
disabled, trust_env disabled and no proxy/authentication changes. Invalid tab
requests, including `shorts`, fail before network. Continuations are reported
as a boolean; their tokens are not retained and pagination is not fetched.
No watch/player, YouTube API, login, browser or thumbnail request is made.

## Observation contract

The new marker is `youtube-channel-tabs-observation-v1`, NOT `youtube-rss-v1`
and NOT the Phase 3 snapshot schema. Each combined observation includes:

- `video_id`, canonical video URL, title, channel title and sanitized thumbnail.
- `source_tabs`, in deterministic videos-then-streams order.
- `titles_by_tab`, `metadata_by_tab` and `title_conflict`.
- `knowledge`, KNOWN or UNKNOWN against the supplied known-ID set.

Identity is always `video_id`, never title. A shared ID becomes one record,
even when the titles differ. The representative title comes from `/videos`
when present, not from an inferred "latest" timestamp; all titles remain
available for operator review. Exact same-page duplicates collapse; conflicting
same-page metadata fails closed rather than being silently overwritten.

Display metadata retains `relative_text` and `badges`, for example:

```json
{
  "relative_text": ["1 penonton menunggu", "Tayang 23/10/26, 02.00"],
  "badges": ["Mendatang"],
  "schedule_timezone": null
}
```

This is evidence of text on the page, not playback verification. No LIVE,
UPCOMING or VOD classification, absolute publication timestamp, or start-time
conversion is produced. A past schedule is not proof of VOD. Even an explicit
LIVE badge is retained as source text only. The observation timestamp in the
evaluation report is monitor time, not publication time.

The existing delivery safety guard still rejects these observations. KNOWN
does not mean delivered, UNKNOWN does not mean newly published, and a successful
evaluation cannot authorize Telegram. Message 271's historical initial event
is read as identity evidence, not replayed or rewritten.

## Failure and partial coverage

HTTP 404/429/5xx, redirect, timeout, network errors, missing initial data,
unknown structures, invalid cards and metadata conflicts remain explicit
failures. No cache is advanced. One tab failing does not prevent inspection of
the other; valid observations are retained with an overall FAILED result and
CLI exit 1. They must not be interpreted as complete channel coverage.

A bare empty grid, continuation-only grid or unknown empty message fails.
For streams, only explicit recognized empty-stream messages can verify an empty
page; these message strings are synthetic contract fixtures, not proof of every
localization used by YouTube. Failure must not be relaxed to "no new items".
Invalid optional metadata structures are rejected by the new adapter without
changing how the legacy Phase 3 parser behaves.

## Tests

`tests/fixtures/youtube_streams_initial.json` is synthetic: two stream cards,
one scheduled display label, one duration/relative-time display, and a dummy
continuation. It contains no captured HTML, real tracking token or credentials.
The dummy thumbnail signature is removed from observations.

New tests cover strict channel/tab checks, source metadata, no inferred status
or timezone, duplicate and cross-tab identities, conflicting titles, invalid
cards, explicit/unknown empty pages, HTTP/network errors, one request per tab,
independent tab failure, preserved historical Shorts IDs, message 271's known
identity, baseline stability, delivery guard, CLI read-only behavior and no
Shorts/player/Telegram requests. Network and .env access are blocked by the
existing test fixture; all HTTP responses in new tests are mocked.

The first focused run exposed one failing malformed-relative-metadata test;
the additive adapter was tightened rather than changing the Phase 3 helper.
Final local and CI results are recorded in the PR review report.

Final full local regression: **493 passed, 0 failed**, including all 423
Phase 1-3 tests and 70 new cases. Intermediate full runs passed 487 and 492
tests before the final explicit-badge and missing-marker cases were complete.
The final suite was rerun in full. Git blobs/normalized checkout contents for
all protected production/Phase 3 files, cache and journal were verified
unchanged; the baseline remains 133 IDs and message 271's initial event stays
delivered. CI must be verified on the committed PR head separately; local
success is not evidence of a live multi-tab GitHub collection.

## Next live evaluation (separate approval)

No real YouTube HTTP request was made for this implementation. After review and explicit
live-test approval, an operator can evaluate the new CLI from its trusted
checkout with the project interpreter:

```powershell
python -B youtube_channel_tabs.py --state-dir <trusted-state-directory>
```

It prints a sanitized JSON evaluation and performs exactly two GETs. Use a
verified copy of the current state if isolation is desired. Any output retained
for review should be stored outside Git. Do not overwrite cache/journal files
with stdout. There is no --send, --approve or --apply option, and the CLI does
not read .env or Telegram configuration.

The CLI requires the historical cutover marker and validates its identity
baseline before either GET. A missing marker fails closed; it does not reduce
the known-ID set to cache-only and accidentally forget baseline-only identities.

For a future GitHub evaluation, propose a separate manual-only, read-only
preflight referencing trusted code with two allowlisted source tabs and no
Secrets. Do not silently modify `youtube-shadow-live.yml` or feed this report
to its Phase 3 reconciler: extending artifact provenance, baseline lineage,
partial-tab failure handling and reconciliation schema needs separate review.
No new artifact or delivery contract is activated here.

## Limits and rollback

Coverage is limited to the first response per tab. Videos can fall outside the
initial window, disappear between observations or be affected by localization,
HTML changes and throttling. No continuation fetching means no full archive
claim. Shorts are deliberately excluded, even when they were previously
discovered through RSS. Historical Shorts baseline IDs must not be removed.

A future union must keep tab provenance and deduplicate by ID when content
moves between tabs. Relative schedule text cannot establish publication or
delivery eligibility. Newly visible IDs need historical review before any
future cutover; no mass backfill is justified by this adapter.

Rollback is to stop using this separate evaluator. Existing production and
Phase 3 code paths are untouched, so no cache reset, journal mutation,
baseline deletion or production workflow toggle is needed.
