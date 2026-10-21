# Phase 2: Offline Shadow Monitoring

## Scope and Existing State

Base master: `dbfedbe1d6f6f8419fed9c3370f059dc845af47d`.
PR #7's read-only /videos preflight succeeded in GitHub run 39567511797:
HTTP 200, 30 KNOWN, zero UNKNOWN, zero parsing errors. That observation is not
proof of future availability or a publication timestamp.

Audit at this base: BIMA 90 entries, YouTube 129 distinct IDs, approved historical
RSS baseline 133 IDs, one delivered journal batch and zero pending. Event
`youtube:video:K94OBen4CiU:initial` remains delivered (message 271).
Production workflow remains disabled; this phase does not run a live monitor.

Existing identity is video_id. `known_video_ids()` reads cache and all journal
item/event IDs; `load_known_ids()` additionally includes the 133-ID cutover
baseline. Even pending/detected IDs can be KNOWN: knowledge is NOT delivery
proof. Only the original delivery journal records confirmed sending.
Duplicate legacy cache rows collapse in the identity SET, never in the original
file. Nothing here edits/removes historical cache or journal rows.

## Components and Data Model

`youtube_shadow.py` is a pure state-transition engine: no network, scheduler,
notifier or persistence. Input may be a validated PR #7 VideosPage, or an explicit
safe parser-failure category. State remains separate from both production files.

| Field | Meaning |
|---|---|
| video_id | Stable case-sensitive YouTube identity |
| title | Latest normalized observed title |
| first_seen_at | First successful SHADOW observation in UTC, never YouTube publication |
| last_seen_at | Most recent successful observation containing the ID |
| observation_count | Number of distinct successful snapshots containing the ID |
| knowledge | KNOWN from fixed baseline union, otherwise UNKNOWN |
| verification_status | BASELINE_KNOWN, AWAITING_REVIEW, NEW_CANDIDATE or APPROVED |

Additional fields record title history, visibility in the last successful
snapshot, consecutive stable observations, and their start time.
No published/status/delivery_format/delivered fields are accepted.
Snapshot and review ledgers are retained for audit. Baseline identities are
frozen when a shadow evaluation is created; refreshing baseline needs review,
not an automatic rewrite of ongoing observation history.

Classification is derived separately from knowledge:

| Classification | Rule |
|---|---|
| KNOWN | ID appears in historical cache/journal/baseline union |
| UNKNOWN | Unmatched ID awaiting manual history review, even after repeated sightings |
| NEW_CANDIDATE | Explicit human shadow review after additional consistency checks |
| APPROVED | A second explicit human shadow decision following NEW_CANDIDATE |

An APPROVED unmatched observation still has knowledge=UNKNOWN: its identity was
not in the historical baseline. APPROVED is shadow-only, NOT permission to send.
All four classifications are rejected by the UNCHANGED production notifier
guard because these records never carry youtube-rss-v1 or trusted publication.

## Observation and Review Rules

Each caller supplies a unique snapshot_id and timezone-aware observation time.
Duplicate IDs count once per snapshot. Replaying the same snapshot_id and
canonical payload is a no-op; reusing it for different content fails closed.
The clock must not move backwards. Times are monitor timestamps, never inferred
from a relative label, scraped_at, RSS cutoff or an old cache entry.

Repeated sightings may demonstrate repeated presence, but never auto-promote an
UNKNOWN. Additional checks for an explicit candidate review are:

- ID not in the historical union.
- Latest parser snapshot healthy and video present in it.
- At least two stable-title sightings since any title change/reappearance,
  spanning at least 60 seconds (a conservative simulation policy, not publication proof).
- Explicit reviewer identity, unique review_id and nonempty history-review
  reference supplied by the operator.

APPROVED requires a separate explicit review decision from NEW_CANDIDATE with
the same health/presence/stability checks. The reference is an operator statement
and audit pointer, not authenticated evidence or automatic external verification.
There is no review UI, network verification, production delivery contract or CLI
auto-approval option in this phase. Only synthetic fixtures exercise approvals.

Title changes keep the same ID, append title history and reset unmatched review
status to AWAITING_REVIEW. Not appearing in the next successful visible-page
snapshot is NOT proof of removal/unavailability; preserve all timestamps/counts,
reset current confidence/review, and keep the record. Reappearance continues the
same first_seen history rather than creating a new video.

A parser failure adds a FAILED snapshot without changing video counts,
timestamps, titles or presence flags. New reviews are blocked while the latest
snapshot is FAILED. Prior reviews remain historical audit facts, not current
availability or delivery permission. An unverified empty parser result is
rejected; only the existing parser's verified-empty result may mark not-seen.

## Offline Simulation

`youtube_shadow_simulation.py` consumes synthetic scenario frames, optionally
passing fixture HTML through the actual PR #7 parser. It never fetches a page,
calls Telegram, reads .env or executes the monitoring runner.
It reads the selected baseline directory but only writes two NEW files in an
explicit external directory:

- youtube_shadow_state.json: independent scenario histories.
- shadow_summary.json: health, counts, classification and review counts.

Output under ANY Git worktree ancestor is rejected, including symlink-resolved
paths. Existing output directories are refused, never overwritten or cleaned.
Atomic persistence uses the existing helper but cannot target production file
names. There is no schedule, workflow, automatic continuation or live collector.
This is a single-writer offline simulation, not a concurrent shadow service.

Example with an operator-selected fresh directory outside all repositories:

```powershell
python -B youtube_shadow_simulation.py --baseline-dir . --output-dir <new-external-directory>
```

Fixtures cover new/unmatched videos, historical reappearance, title changes after
review, disappear/reappear, multiple unknown IDs, actual parser failure, repeated
historical IDs and explicit shadow-only review. Unit tests additionally use a
temporary duplicate legacy cache and verify its bytes are unchanged.
Simulation exit 0 means the harness handled scenarios correctly; the intentional
parser-failure scenario still reports health=FAILED. It is NOT a live health PASS.
Fixture observation times are synthetic and must never become production timestamps.

## Proposed Transition to Production (Not Implemented)

1. Review offline behavior and obtain separate approval for a manual live shadow
   collector and an isolated persistence location. Keep production delivery
   disabled for these observations. Require one writer, protected artifacts and
   clear FAILED health, retention and recovery policy before scheduling anything.
2. Observe over an operator-selected period; reconcile every UNKNOWN against
   history. Finite /videos windows, sorting, localization, delayed visibility and
   unobserved older uploads mean first_seen after a baseline is not proof of new publication.
3. Review candidate evidence independently. Define authenticated human approval,
   freshness and invalidation policy before using any approval beyond simulation.
4. Propose a separately versioned delivery contract and explicit per-ID approval
   reference, or a verified absolute publication source. Do not relabel shadow
   data as youtube-rss-v1 or weaken current guards. No delivery changes in this PR.
5. Under separate cutover authorization, freeze writers, back up final remote
   state, preserve initial event keys and existing journal progress, and test
   dedup/retry/checkpoint against that final snapshot. APPROVED shadow is not delivered.
6. Only after approval, perform a separately authorized controlled production run.
   Never synthesize delivered batches, replay message 271, reset caches or
   infer delivery from KNOWN. Exactly-once remains unguaranteed after checkpoint/
   commit/push failure; preserve recovery evidence and reconcile rather than retry blindly.

Rollback in this phase: stop/ignore the simulator; retain its external artifacts.
There is no production switch or data migration to undo. Any future delivery
rollback needs a separately reviewed plan preserving the real delivery journal.
Unbounded snapshot/review history will need an explicit retention/export design
before long-running operation; this phase deliberately does not prune history.

## Validation

All automated network is mocked/forbidden and .env reads are blocked. Tests cover
the eight fixture scenarios, snapshot/review replay, clock errors, duplicate IDs,
title/reappearance review invalidation, invalid/corrupt state, empty/parser
failures, 133-ID compatibility, output-path/overwrite protection, persistence
failure, and direct notifier rejection of every classification including APPROVED.
The complete BIMA/RSS/reliability/parser regression suite is run unchanged.

Local result: **372 passed, 0 failed** (323 existing + 49 shadow tests).
The eight offline scenarios were also executed against the unchanged 133-ID
baseline from the base checkout, with only external shadow artifacts written.
Repeated unmatched video remains UNKNOWN; historical video remains KNOWN;
revised title resets an explicitly reviewed observation; parser failure leaves
counts/timestamps unchanged and remains FAILED in the summary.
Only the fixture with two explicit synthetic reviews reaches APPROVED; no
network or Telegram is used. This is evidence for offline shadow evaluation,
not approval for live collection, scheduling or production delivery.
