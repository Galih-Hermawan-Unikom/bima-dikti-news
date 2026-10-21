# Multi-tab shadow evaluation (Phase 4.3B)

This is manual evaluation only, not a delivery contract or production cutover.
No live collection is authorized by this implementation PR. Merge and dispatch
require separate operator approval. The existing Phase 3 workflow is unchanged.

## Data flow

```text
manual master dispatch -> exact commit checkout (read-only)
  -> YouTubeChannelTabsReader -> /videos (one GET), /streams (one GET)
  -> read_channel_tabs / combine_pages (existing adapter, no new parser)
  -> youtube_multitab_snapshot.py -> RUNNER_TEMP/snapshot.json
  -> immutable run/attempt artifact (14 days)
  -> explicit operator selection + authenticated GitHub API verification
  -> downloaded ZIPs + independent trust manifest
  -> youtube_multitab_reconcile.py -> external reconciliation.json
  -> human review only (never Telegram authorization)
```

No RSS, player inspection, API key, login, browser automation, continuation fetch,
Shorts request, Telegram, repository writes or automatic previous-artifact fetch.
The adapter uses the existing fixed User-Agent and two requests with 15-second
timeouts, redirects disabled and no retry. The job timeout is five minutes.

## Snapshot version 1

Source is `youtube-videos-streams-shadow-v1`; Phase 3 artifacts are incompatible.
Root fields are strict and include:

| Field | Meaning |
|---|---|
| schema_version / source | Schema 1 and multi-tab namespace |
| repository / workflow_path / commit_sha | Fixed repository/workflow, exact checked-out SHA |
| github_run_id / github_run_attempt | Run identity, including explicit reruns |
| snapshot_id | `gh-multitab-<run>-attempt-<attempt>` |
| observed_at_utc | Monitor observation time after both requests, never publication time |
| tab_timings | Per-tab started_at_utc and finished_at_utc, including failed fetch attempts |
| channel_id / requested_tabs | Expected channel and exactly videos, streams |
| pages | Independent health, HTTP status, counts, parsing errors, continuation, empty proof, error category |
| overall_health / is_partial | Truth table below |
| baseline | Historical marker IDs, known-ID union and cutover lineage with canonical checksums |
| source_state_fingerprint | SHA-256 of exact bytes of the three input state files |
| observations | Unique video IDs, channel/title/URL/thumbnail, source_tabs, titles_by_tab, metadata_by_tab, title_conflict, knowledge |
| sha256 | Canonical JSON checksum excluding this field |

`baseline.historical_ids` preserves every approved marker ID, including the four
historic Shorts IDs. It is not filtered by current tab coverage. `baseline.known`
uses the existing cache/journal/marker union and original cutover version/time.
No baseline identity is a claim of delivery. Baseline equality is required across
reconciled snapshots; different lineage or known sets require operator review.
Raw state-file fingerprints are provenance evidence, not a substitute for baseline
identity: line endings or unrelated BIMA updates need not invalidate that identity.

Canonical JSON hashing uses sorted keys, ASCII escapes, compact separators and
UTF-8, exactly the existing `digest()` helper. This hash is not the SHA-256 of
the pretty-printed JSON bytes, and neither is the ZIP artifact SHA-256.

Each attempted tab records its interval around the adapter fetch, including HTTP
and parsing. The finished timestamp is recorded in finally even if the fetch
raises. All timestamps are timezone-aware UTC. Validation requires
videos.start <= videos.finish <= streams.start <= streams.finish <= observed_at_utc;
equal timestamps are allowed for clock resolution. Invalid/missing timing or a
backwards wall clock fails closed, not by fabricating or clamping timestamps.
These are monitor execution times, never video publication/schedule times. A global
timestamp alone cannot expose the gap between tabs or locate a failed attempt.
If setup fails before fetching, failure.json remains a diagnostic, not a snapshot.
This mandatory field completes the unreleased Phase 4.3B schema: pre-review snapshots
without it are rejected, not upgraded by inference. No Phase 3 schema changes.

```json
{"tab_timings": {
  "videos": {"started_at_utc": "2026-10-22T00:00:00+00:00", "finished_at_utc": "2026-10-22T00:00:01+00:00"},
  "streams": {"started_at_utc": "2026-10-22T00:00:02+00:00", "finished_at_utc": "2026-10-22T00:00:03+00:00"}
}, "observed_at_utc": "2026-10-22T00:00:04+00:00"}
```

Titles and raw relative schedule/badge text remain source text. Schedule timezone
stays null. No inferred published_at, playback status, WIB schedule or delivery
eligibility is added. A title conflict is evidence, not an automatic failure or
new video. Counts per tab must equal observation membership in that tab; duplicate
union IDs, unexpected fields, unsafe metadata or inconsistent health fail closed.

## Health and failure

| Healthy tabs | overall_health | is_partial | Evidence |
|---|---|---|---|
| 2 | HEALTHY | false | Complete initial-page observation |
| 1 | FAILED | true | Healthy tab retained as partial evidence only |
| 0 | FAILED | false | Failure metadata, no observations |

Only explicitly verified empty pages can be healthy and empty. Extraction failure
is not an empty feed. A failed snapshot records a failed shadow frame but never
updates video counters, first/last_seen, presence, tab history or review status.
It cannot create NEW_CANDIDATE or APPROVED. Later healthy frames resume observation.
All UNKNOWN identities remain awaiting human review; repeated sightings do not
prove publication time. Internal projection into the existing pure shadow engine
reuses transitions only and does not convert a multi-tab artifact into Phase 3.

The collector exits nonzero on either tab failure. Safe setup/validation/output
failure may produce `failure.json` instead of a valid snapshot. This diagnostic is
not reconcilable. Checkout, dependency installation, cancellation, disk or upload
failure can prevent an artifact entirely. Never represent that as an empty snapshot.
Upload uses always(), missing-file error and no continue-on-error; upload failure
therefore remains a workflow failure. Successful upload does not turn parser failure
into workflow success. No automatic rerun is configured.

## Manual collection (requires separate approval)

After reviewed merge, inspect master SHA, workflow contents and production state
hashes. Confirm the production monitoring workflow remains in its operator-selected
state; this workflow neither enables nor disables it. Then, only with approval:

```powershell
gh workflow run youtube-shadow-multitab.yml -R Galih-Hermawan-Unikom/bima-dikti-news --ref master
```

Identify the new manual run by workflow, SHA, actor, time and attempt. Watch it;
do not automatically rerun a failure. The concurrency group is multi-tab-specific,
with cancel-in-progress false: a new request must not cancel an active collector.
GitHub concurrency is not a durable queue; operators should avoid overlapping requests.
Artifact name is `youtube-shadow-multitab-<run>-<attempt>`, retention 14 days,
overwrite false. Artifacts are temporary evaluation evidence, not production state.
Download selected evidence before expiry, without committing snapshots to Git.

## Artifact authentication and offline reconciliation

Use an authenticated GitHub CLI session to read run, attempt/job and artifact API
metadata. Confirm repository, workflow ID/path, master branch, workflow_dispatch,
head SHA, actor/approval, run ID/attempt, completion and upload step success.
Compare the trusted commit with the reviewed workflow/code. For reruns inspect the
specific attempt, not merely the latest run summary. Match artifact ID/name and
its workflow_run metadata to the selected run. Do not trust assertions inside JSON.

Download the raw ZIP by the selected artifact ID using the authenticated GitHub
artifact download API to a directory outside Git. A normal `gh run download`
extracts files and is not sufficient when preserving/verifying the ZIP digest.
Compute SHA-256 on the downloaded ZIP and compare with the authenticated API
`digest` field (strip `sha256:`). If that digest is unavailable or mismatched,
stop; do not fabricate an authenticated digest from the ZIP alone.
Validate the internal snapshot schema and canonical checksum separately, then
record the approved checksum and metadata in a local trust manifest.

Trust manifest example (synthetic values; populate from verified API evidence):

```json
{
  "version": 1,
  "source": "youtube-videos-streams-shadow-v1",
  "repository": "Galih-Hermawan-Unikom/bima-dikti-news",
  "workflow_path": ".github/workflows/youtube-shadow-multitab.yml",
  "baseline_sha256": "<canonical baseline sha256>",
  "verified_by": "<operator>",
  "verified_at_utc": "2026-10-23T00:00:00+00:00",
  "artifacts": {
    "selected-run.zip": {
      "artifact_id": "<API artifact ID>",
      "artifact_name": "youtube-shadow-multitab-<run>-<attempt>",
      "zip_sha256": "<API digest verified against local ZIP>",
      "snapshot_sha256": "<validated canonical snapshot checksum>",
      "commit_sha": "<reviewed head_sha>",
      "github_run_id": "<run>",
      "github_run_attempt": 1,
      "workflow_id": "<verified workflow ID>",
      "event": "workflow_dispatch",
      "head_branch": "master",
      "run_status": "completed",
      "run_conclusion": "success",
      "artifact_upload_conclusion": "success",
      "authentication": "operator-verified-github-api"
    }
  }
}
```

The manifest is an explicit operator trust boundary, not cryptographic attestation.
It must be created independently of untrusted snapshot fields. Keep the supporting
API evidence and manifest outside Git. Failure snapshots require failure conclusion
and successful artifact upload; healthy snapshots from failed jobs require an
explanation of the unrelated job failure before inclusion. Cancelled runs and
upload failures are not accepted as authenticated snapshots by the manifest validator.

```powershell
python -B youtube_multitab_reconcile.py --trust-manifest <external-trust.json> --output-dir <new-external-directory> <selected-first.zip> <selected-second.zip>
```

ZIPs are size-limited and read without extraction; exactly one snapshot.json is
accepted. Corrupt ZIP/CRC, extra/path traversal members, invalid JSON/hash/schema,
identity mismatch or incompatible baseline aborts before output. Existing output
directories or paths inside a Git checkout are refused. Input ZIPs are not modified.
Failed setup artifacts with only failure.json remain diagnostic evidence and are
not observations. Do not manually rewrite them into valid snapshots.

The result contains the unchanged-schema pure shadow state, separate tab histories,
and comparisons ordered by UTC/run/attempt. Identical snapshots are idempotent;
reused snapshot ID with different content fails. A workflow rerun has a new attempt
and counts as a distinct observation, not a new publication. A video on both tabs
increments count once per healthy snapshot. Same-tab title changes are reported
separately from tab movement. Reappearance keeps the original first_seen. Missing
IDs mean only not_visible_in_capture, never deleted or no longer available.

## Tests, limitations and rollback

The synthetic fixture has two videos on each tab and three unique IDs overall:
one known ID on both tabs (with conflicting source titles), plus two UNKNOWN IDs.
Reconciliation of two healthy snapshots keeps all UNKNOWN identities unapproved;
the shared ID has observation_count 2, not 4. Inserting a failed partial snapshot
between them leaves all video counters/timestamps/reviews unchanged for that frame.
Example evaluation summary, not a production snapshot:

```json
{"healthy_snapshots": 2, "unique_ids": 3, "known": 1, "unknown": 2,
 "shared_id_observation_count": 2, "delivery_authorized": false}
```

Tests use synthetic fixtures, mock HTTP and temporary external state. Existing
Phase 1-3 and adapter tests remain in the full regression suite. Workflow tests
validate the upload failure policy statically; they do not claim to simulate the
GitHub artifact service. An operator-authenticated manifest mismatch/upload-failure
test rejects evidence; no actual workflow or Telegram call is made in unit tests.

Initial pages are finite and continuation is reported, never fetched. Videos can
appear/disappear between captures or beyond the initial window; no inference of
publication, removal or exhaustive channel history is safe. YouTube markup can
change. Titles differ by tab; relative times lack trusted absolute timezone.
Known IDs include historical Shorts but no Shorts page is accessed. False positives
are controlled by UNKNOWN review; false negatives remain possible due to capture
window, interval, pagination and parser failure. Artifacts expire after 14 days.

Rollback means stop manually invoking the new workflow, preserve selected evidence,
and discard/rebuild external evaluation outputs if needed. No production cache,
journal, marker, delivery event or Phase 3 state needs restoration. Any code/workflow
revert requires a separate reviewed PR; do not touch production monitoring settings.
GO for a separately approved manual live test requires passing regression and CI,
reviewed provenance procedure and unchanged protected state. This is never GO for
Telegram, automated collection, production cutover or merge without approval.
