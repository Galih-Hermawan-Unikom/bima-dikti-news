# YouTube channel-tabs dry-run

The runner's only YouTube discovery adapter reads the public channel `/videos`
and `/streams` tabs. RSS is not imported or consulted by the current runner,
adapter or eligibility classifier. `youtube_monitor.py` remains only as a legacy
module for historical compatibility; it is not a fallback source.

## Running a local evaluation

Use the reliability runner with dry-run enabled:

```powershell
python run_github.py --dry-run --youtube-eligibility
```

`--youtube-eligibility` requires `--dry-run`. When YouTube monitoring is enabled,
the runner selects the tabs adapter directly. Any non-dry-run request with
YouTube enabled stops before scraper, notifier, credential, or state-write work.
The obsolete `youtube_eligibility_dry_run` source selector returns an error;
there is no hybrid or RSS fallback.

The adapter uses the configured channel URL/ID and reads only `/videos` and
`/streams`. It does not read Shorts or a video player. Both tabs must be healthy
for a candidate evaluation. A failed/partial scan or restart causes visible IDs
to remain in startup/gap inventory quarantine. The adapter holds only in-memory
capture history; it does not migrate or persist observation state.

## Identity, timing, and readiness

The delivery journal's baseline marker is read for compatibility and integrity
checks, while the historical YouTube cache, 133-ID baseline, and journal remain
read-only. Cache/baseline identities suppress initial events. Delivered journal
events stay terminal; pending, partial, or ambiguous outcomes require operator
reconciliation. No synthetic receipt is created.

`first_seen_at` is taken from a real tab capture after the activation fence. It
is an observation time, never `published_at`. Initial inventory, IDs first seen
before activation, insufficiently fresh captures, unknown categories, upcoming
streams, LIVE streams, and replays without verified public-release/end evidence
are held. Shorts are rejected. Even an offline `ELIGIBLE` result is report-only:
the runner does not enqueue or call the notifier for YouTube.

The default policy is unapproved. Signed operator review evidence is optional
and disabled unless a verifier is explicitly supplied by a future caller. Such
review can bind an observation to a policy and state fingerprint; it cannot
replace category or readiness proof.

## Failure and rollback

If either tab fails, the runner reports that source health and does not switch
to RSS. The next healthy cycle re-inventories current visible IDs, preventing a
gap from becoming automatic backfill. Missing IDs are not marked deleted.
Because this integration is dry-run only and writes no YouTube state, rollback
consists of reverting the code change; no cache or journal restoration is
needed. Any later delivery activation requires a separate design for durable
first-seen observations, operator review, atomic reservation, recovery, and
explicit approval.
