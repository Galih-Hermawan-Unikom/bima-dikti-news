# Kemdiktisaintek Production Integration (Gated)

This change adds the Kemdiktisaintek source as an isolated production integration. It does not change the BIMA or YouTube runtime, cache, workflow, credentials, destination, or production state.

## Isolation and state

- Source identity and event keys use the Kemdiktisaintek UUID. No BIMA cross-source match is required.
- The dedicated file is `.bot-state/kemdiktisaintek_production.json` on the existing `dikti-production-state` branch. It is separate from the BIMA and YouTube state files and from the experiment state branch.
- The schema retains the activation fence, bounded initial inventory, first-seen evidence, revisions, admissions, and durable outbox. The inventory covers only the requested recent API pages; it does not claim the 928-record archive is complete.
- Remote writes use the GitHub Contents API with the expected blob SHA. A conflict or unknown result stops the operation. A claimed Telegram attempt is never retried automatically; an uncertain result is `AMBIGUOUS` and requires reconciliation.
- BIMA, YouTube, and Kemdiktisaintek share the existing `dikti-production-state` concurrency group when they write the production state branch.

## Eligibility and delivery

The reviewed Stage 3 parser, classifier, monitor, and durable delivery rules are isolated under `kemdiktisaintek_prod_*.py`. Initial inventory is not sent; publication and first-seen windows, trusted detail evidence, healthy unfiltered capture, UUID identity, relevant classification, and a fresh admission are required. Revision candidates remain review-only.

Delivery requires both exact-string repository Variables:

- `KEMDIKTISAINTEK_PRODUCTION_ENABLED=true`
- `KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND=true`

Both default to OFF when unset. The workflow also requires the independent `KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA` Variable to match the dispatch SHA on `master`. The production Telegram secrets and allowlist are passed to the runtime only for `POLL` when both delivery switches are true. The runtime uses only `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `PRODUCTION_TELEGRAM_CHAT_ID_SHA256`, then runs the existing official-channel `getMe`/`getChat`/`getChatMember` preflight before any reservation. It does not fall back to experiment credentials.

## Workflow modes

`.github/workflows/kemdiktisaintek-production.yml` is manual-only; this PR adds no schedule. `COLLECT` is read-only and uses `contents:read`. `INITIALIZE` requires an exact confirmation plus the approved master SHA and only creates the source-specific file if it is absent. `POLL` requires the source switch, persists observations via CAS, and can deliver only with both delivery switches enabled. All operations run from `master`, check the reviewed SHA, disable checkout credential persistence, and serialize on `dikti-production-state`.

After review and merge, an operator must separately approve the master SHA, create the one-time source baseline, and authorize monitoring/delivery Variables. No repository Variables or Secrets are changed by this PR. Automatic scheduling requires a separate reviewed activation; it is not enabled here.

## Validation

The new offline tests use a memory CAS backend, fixture HTTP responses, and a mock Telegram gateway. They cover single delivery across restart, switch-off behavior, CAS blocking before POST, ambiguous timeout handling, and strict production credential mapping. No real Telegram or GitHub state calls are made by tests.
