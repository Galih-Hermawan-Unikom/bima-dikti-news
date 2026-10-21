# YouTube live shadow monitoring (Phase 3)

Status: implementation for review, not activated. Production remains disabled.
This workflow must not be dispatched until this implementation is reviewed,
its merge is separately approved, and the operator authorizes live collection.
No production delivery or cutover contract is introduced.

## Architecture and boundaries

```text
Manual master dispatch (no schedule)
  -> one YouTubeVideosReader.fetch() of @kemdiktisaintek/videos
  -> existing ytInitialData / lockupViewModel parser
  -> schema validation, field allowlist, KNOWN/UNKNOWN comparison
  -> RUNNER_TEMP/youtube-shadow-<run>-<attempt>/snapshot.json
  -> immutable GitHub artifact (14 days, unique run + attempt)

Operator explicitly selects and authenticates downloaded artifacts
  -> trusted selection manifest + schema/checksum/baseline validation
  -> sort by observation time, run ID, attempt; deduplicate snapshot identities
  -> youtube_shadow.observe_snapshot() (existing transition engine)
  -> new external directory: shadow_state.json + shadow_summary.json
```

Unlike the earlier preflight, the collector preserves individual observations
as artifacts, including safe failure reports. Unlike production monitoring,
it never calls a notifier, RSS fetch, player, scraper, scheduler or delivery
runner. It makes one page request, with the existing reader's 15-second timeout,
no redirect following and no retry. It does not fetch streams, Shorts,
continuations or thumbnail URLs. Importing identity/time helpers from the RSS
module does not instantiate or invoke the RSS monitor.

Baseline identities are the union of the historical cutover marker (currently
133 approved IDs), cache IDs and delivery journal identities. Duplicate cache
IDs collapse only in this in-memory identity set; source files stay untouched.
KNOWN means a recognized identity, not confirmed delivery. Message 271's
`youtube:video:K94OBen4CiU:initial` remains known, without editing its journal.

## Snapshot contract

UTF-8 JSON, `schema_version: 1`, `source: youtube-videos-shadow-v1`.
The complete, runnable synthetic examples are in
`tests/fixtures/youtube_shadow_live/snapshot-1.json` and `snapshot-2.json`.
They are test data, not evidence of real YouTube requests or Actions runs.

Top-level fields:

| Field | Meaning |
| --- | --- |
| schema_version, source | Strict versioned contract, not a delivery format |
| channel_id, source_url | Fixed verified channel and /videos URL |
| repository, commit_sha | Allowlisted repository and exact 40-character checkout SHA |
| github_run_id, github_run_attempt | Positive run identity and attempt number |
| snapshot_id | `gh-<run>-attempt-<attempt>`; unique including workflow re-runs |
| observed_at_utc | Monitor UTC time immediately after fetch; NOT publication time |
| http_status | HTTP status, or null if no response |
| parser_health | HEALTHY or FAILED |
| parsing_errors | Safe parser error count; HTTP failures need not be parse errors |
| detected_count | Number of accepted observations; zero on failure, never partial success |
| empty_verified | True only for a parser-verified explicit empty channel |
| error_category | Null on success; fixed safe category on failure |
| baseline | Sorted unique IDs, RSS cutover version/time and canonical SHA-256 |
| observations | Strict allowlisted objects; empty on failure |
| sha256 | SHA-256 of canonical JSON excluding this field |

Each observation contains only `video_id`, `title`, `channel_title`, canonical
`url`, `thumbnail`, and `knowledge` (KNOWN/UNKNOWN). Thumbnail URLs are limited
to public `i.ytimg.com` / `i9.ytimg.com` image paths, without query/fragment or
credentials. Other thumbnail URLs become an empty string. HTML, raw JSON,
cookies, authorization, environment dumps and signed parameters are not saved.
Relative publication labels are intentionally omitted. No `published_at`, VOD,
LIVE, UPCOMING, delivery eligibility or approval is inferred.

Unknown schema fields, invalid UTC/run metadata, inconsistent counts/knowledge,
duplicate observations, failed snapshots containing observations, checksum
errors and incompatible baselines are rejected. A baseline includes cutover
lineage, not just a count. Different sets or cutover markers require explicit
operator review; reconciliation never silently unions them.

## Workflow and artifact lifecycle

`.github/workflows/youtube-shadow-live.yml` supports `workflow_dispatch` only.
The job additionally requires `refs/heads/master`. It has `contents: read`,
checkout `persist-credentials: false`, a five-minute job limit, no Telegram
Secrets, no git writes and no production workflow changes. Only requests is
installed; Playwright remains available to the unchanged BIMA project elsewhere.

The dedicated repository-wide concurrency group does not cancel an active run.
GitHub concurrency may replace an older pending run when another is queued;
it is not a guarantee that every queued request executes. Operators should
dispatch one at a time. This does not interact with production concurrency.

Artifact name: `youtube-shadow-<run_id>-<run_attempt>`, retention 14 days,
overwrite disabled. The upload step uses `always()` so a failed fetch/parser
can still retain its report. The collector exits nonzero for failure. If setup,
baseline validation, runner loss or output/upload fails, a snapshot may not
exist: missing artifact is a failed execution, never an empty success. Upload
errors remain failures. Cancelled/lost runners cannot guarantee artifact upload.

Artifacts are temporary evaluation storage, NOT persistent production state.
Download needed evidence before expiration; no automatic prior-artifact lookup
or selection is implemented. GitHub's own artifact service/runtime token is
used by the upload action; it is never copied into snapshot data.

## Operator procedure after separate approval

After this PR is separately approved and merged, inspect the master workflow
and SHA, then (only with live-execution approval):

```powershell
$repo = 'Galih-Hermawan-Unikom/bima-dikti-news'
gh workflow run youtube-shadow-live.yml -R $repo --ref master
gh run list -R $repo --workflow youtube-shadow-live.yml --limit 5
gh run view <RUN_ID> -R $repo
```

Do not rerun automatically on failure. No part of the current implementation
task dispatches this workflow. Standard regression CI is separate and mocked.

### Authenticate and download explicitly selected artifacts

Use an authenticated `gh` session with read access to the intended repository.
For each chosen run and attempt, inspect GitHub's API independently of its
artifact payload:

```powershell
gh api repos/Galih-Hermawan-Unikom/bima-dikti-news/actions/runs/<RUN_ID>/attempts/<ATTEMPT> `
  --jq '{id,run_attempt,event,path,head_sha,head_branch,status,conclusion,workflow_id}'
gh run download <RUN_ID> -R $repo `
  -n youtube-shadow-<RUN_ID>-<ATTEMPT> -D "$env:TEMP/dikti-shadow-input/run-<RUN_ID>-<ATTEMPT>"
```

Require the intended repository, `workflow_dispatch`, master, the reviewed
`youtube-shadow-live.yml` workflow identity, exact trusted commit SHA, run ID,
attempt and artifact name. Inspect job/log outcomes: a failed parser run is
expected to contain a FAILED report, not a HEALTHY page. Check the artifact
digest against authenticated GitHub metadata when available. Extract only the
JSON file into a dedicated external directory; do not execute artifact content.
Do not trust identity fields merely because the downloaded JSON claims them.

Create a separate operator trust manifest *after* this verification. Schema:

```json
{
  "version": 1,
  "repository": "Galih-Hermawan-Unikom/bima-dikti-news",
  "snapshots": {
    "gh-100-attempt-1": {
      "sha256": "<verified canonical snapshot checksum>",
      "commit_sha": "<independently verified trusted checkout SHA>",
      "github_run_id": "100",
      "github_run_attempt": 1
    }
  }
}
```

`trust-synthetic.json` is an executable example, never authority for live data.
A payload checksum detects corruption, NOT authenticity. The manifest records
the operator's authenticated selection; it is not a cryptographic attestation.
Do not generate trusted selection automatically from untrusted snapshots.
The reconciler makes no network request and cannot independently authenticate
GitHub: incorrect operator trust evidence remains a risk.

### Reconcile offline

```powershell
python -B youtube_shadow_reconcile.py `
  --snapshots "$env:TEMP/dikti-shadow-input/run-A/snapshot.json" `
              "$env:TEMP/dikti-shadow-input/run-B/snapshot.json" `
  --trust-manifest "$env:TEMP/dikti-shadow-input/trust.json" `
  --output-dir "$env:TEMP/dikti-shadow-evaluation-unique"
```

All inputs validate before creating output. Output must be a new directory
outside **all** Git worktrees (resolved paths). Neither source snapshots nor
production cache/journal are written. Reconciliation sorts UTC timestamps,
then numeric run ID and attempt. Exact duplicate snapshot identities count
once; conflicting reused identities fail closed. A new workflow attempt is a
separate observation, not publication evidence. Without a previous snapshot,
the first selected snapshot establishes observations only, never new releases.

FAILED frames go through the existing engine's failure transition: no invented
absence, last_seen_at change or observation_count increase. Healthy explicit
empty frames can mark absence, but absence is not proof of deletion. Returning
IDs preserve identity/first_seen_at; title changes are history, not new videos.
Repeated UNKNOWN observations remain UNKNOWN. The offline shadow review API
still requires explicit separate human NEW_CANDIDATE then APPROVED decisions;
this collector/reconciler performs neither. APPROVED remains shadow-only, never
Telegram authorization. Reconciliation rebuilds observations; it does not
import or overwrite an earlier review ledger. Preserve separately reviewed
shadow evidence rather than substituting it for delivery state.

Synthetic reconciliation example (`summary-synthetic.json`): two healthy
snapshots, one KNOWN, one UNKNOWN, zero NEW_CANDIDATE/APPROVED, no delivery
authorization; each visible video's observation_count is two. Supplying the
same artifact twice leaves counts unchanged. These examples are regression
tested and can be evaluated without any network.

## Risks, recovery and rollback

- /videos is a partial current listing: no pagination, streams or Shorts.
  Videos can appear/disappear between manual intervals, producing false negatives.
- Late reappearance, renamed titles, changed ordering, backfilled uploads or
  historical IDs outside the baseline can look novel. UNKNOWN is not proof of
  recent publication; repetition cannot resolve that false-positive risk.
- YouTube DOM changes, HTTP throttling, network errors and localization can
  invalidate the parser. Failure stays explicit and contributes no sightings.
- Runner clock accuracy bounds observation ordering. Backdated or overlapping
  evidence cannot prove publication timing; collisions/incompatible evidence
  require operator review rather than destructive repair.
- SHA-256 is an integrity check, not origin authentication. Trust requires the
  independently verified GitHub run, workflow and commit. Retention, artifact
  expiration and account compromise limit availability/authenticity guarantees.
- Future baseline changes must be reviewed and evaluated as a separate lineage.
  Do not silently edit old snapshots to make incompatible baselines match.
- On failure preserve the diagnostic artifact and existing downloads, then
  investigate. A later manual collection requires separate authorization;
  do not turn failed collection into automatic retry or production failover.
- Rollback is to stop dispatching the isolated workflow and retain evidence.
  No cache reset, journal rollback, delivered mutation or production toggle is
  required. Reverting this additive implementation needs normal PR approval.

## Transition toward production

Phase 3 establishes visibility evidence only. Before any delivery proposal,
review coverage across multiple authenticated observations, independently
resolve unknown historical IDs and publication uncertainty, define an explicit
operator baseline/cutover and a separately reviewed delivery contract. Existing
RSS-only eligibility checks are intentionally unchanged. Delivery retries,
BIMA PDFs, checkpoints and production artifacts remain outside this workflow.
No production GO follows from a successful collection or shadow approval.

## Implementation verification

Local regression suite: **423 passed, 0 failed**, including all previous 372
tests and 51 new cases. Tests block real network and .env access. New coverage
includes HTTP 404/429/500/503, redirect, timeout/network errors, broken DOM,
verified versus unverified empty results, 30 KNOWN observations, UNKNOWN
preservation, re-runs, duplicate/corrupt artifacts, incompatible schemas and
baseline lineage, failed frames, title changes, historical reappearance,
external-only outputs, no Telegram, state integrity and workflow isolation.

The synthetic offline CLI was also exercised with snapshots 2, 1, 1: two
unique healthy snapshots, one duplicate ignored, one KNOWN, one UNKNOWN,
zero NEW_CANDIDATE/APPROVED and no delivery authorization. Protected production
Git blobs remain identical to pre-Phase-3 master. No live collection or
Phase 3 workflow dispatch was performed during implementation. CI outcomes
must be verified separately on the eventual PR; these local results are not
proof of successful live access from GitHub.
