# Telegram Production Preflight

Status: implementation for review only. The workflow has not been dispatched.
Production cutover remains **NO-GO**. This PR does not merge the reliability PR.

## Isolation and Scope

`.github/workflows/telegram-preflight.yml` is manual-only (`workflow_dispatch`).
The job and inline Python both reject any context other than this repository's
`master` branch and manual dispatch. There is no checkout, scheduler, scraper,
monitoring runner, Notifier, third-party action, package installation, cache,
artifact, or repository write permission. Runtime uses Python standard library
on a clean GitHub-hosted Ubuntu runner. Job timeout is two minutes; each network
operation has a ten-second timeout and responses are limited to one MiB.

Only `getMe`, `getChat` and `getChatMember` are permitted, using HTTPS GET to the
fixed Telegram API host, verified default TLS, no proxy or redirects, no retry.
All-success execution makes exactly three calls; any failed check stops early.
No message/document or any other Telegram API method is invoked.

Both the production monitoring workflow at base `f4353d1` and this workflow
reference repository/organization Secrets `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` directly, without a GitHub Environment. Consequently the
configured scope/precedence is the same; secret values have not been inspected.
Do not add an environment or alternate secret names to preflight independently
of production: doing so could validate a different credential scope.

| Effective setting | Workflow fallback | Interpretation |
|---|---|---|
| `NOTIFICATION_METHOD` | `telegram` | Same raw value as monitoring; only `telegram`/`both` pass, `console` means Telegram is disabled |
| `TELEGRAM_SEND_FILES` | `false` | Same accepted boolean strings and fallback as `app_config._read_bool` |
| `YOUTUBE_MONITOR_ENABLED` | `true` | Same boolean interpretation; displayed only, no YouTube access |

Expressions match `.github/workflows/main.yml`. An invalid boolean override
prints only its effective production fallback and fails preflight, rather than
silently accepting the typo. Arbitrary invalid configuration values are not
echoed. Tests compare these defaults with `config.json` and the production
boolean parser; if production rules/defaults change, review preflight again.

## Checks and Safe Output

The workflow validates internally that:

- `getMe` returns bot username `bima_dikti_bot`, a bot ID consistent with the token, and `is_bot=true`.
- `getChat` resolves the actual configured Secret target, not a replacement hardcoded target. The resolved numeric ID must match a numeric Secret; a public `@dikti_news` Secret is also supported. Type must be `channel`, username `dikti_news`, title exactly `DIKTI News`.
- `getChatMember` for that verified chat and bot returns the same bot, status `administrator`, and `can_post_messages=true`.

Missing/invalid Secrets, an experiment/private/group target, identity mismatch,
HTTP failure, `ok=false`, timeout, invalid/oversized JSON, redirects or missing
posting permission fail with a nonzero exit. Debug execution is rejected.

Output is limited to effective non-secret settings, API success labels,
validated public identity constants, posting permission, UTC check time and
PASS/FAIL. It does not emit numeric chat IDs, bot credentials, API URLs, raw
responses, environment dumps or HTTP headers. Unexpected errors are sanitized
without a traceback because network exceptions may include credential URLs.
GitHub's own secret masking is additional protection, not the application's
redaction mechanism.

## Run Only After Separate Approval and Merge

1. Review and separately approve this workflow-only PR. Do not merge or dispatch as part of the preparation task. A manual workflow must exist on the default branch before it can be dispatched normally.
2. Confirm the approved file and SHA are on `master`, expected public identities are unchanged, debug logging is off, and the same production Secret/Variable scope still applies.
3. An authorized operator may then select **Telegram Production Preflight (Read Only)** in Actions, choose branch **master**, and click **Run workflow**. CLI equivalent after approval: `gh workflow run telegram-preflight.yml --repo Galih-Hermawan-Unikom/bima-dikti-news --ref master`.
4. Inspect that preflight run only. Confirm all three API success labels, validated bot/channel, administrator posting permission, effective file/YouTube policy and PASS. Do not dispatch `main.yml` for this check.
5. Record the run URL, workflow SHA, timestamp and effective settings without copying credentials. A successful preflight does not approve cutover or prove an actual message/PDF delivery.

Wrong-ref dispatch skips the job; a skipped job is **not PASS**. This branch/PR
must not be used for a credentialed test. The workflow has no PR triggers and
never executes PR content. Do not use `pull_request_target` to bypass this.

## Limitations and Revalidation

Repository writers capable of editing/dispatching a credentialed workflow are
inside the trust boundary: the guard does not defeat a malicious writer who
changes the workflow itself. Review, branch protection and operator access
control remain necessary. Do not rely on masking if workflow code is changed.

This is a point-in-time access/permission check, not proof of future delivery,
document download availability, source health, persistence rights or exactly-once
delivery. Repeat after changes to Secrets, Variables, bot/channel identity,
posting rights, workflow/code, environment scope or production defaults, and
again near the approved cutover. A title rename intentionally causes failure
until the expected identity is reviewed. No new service or credential is needed.

## Offline Validation

Run `python -B -m unittest discover -s tests -p test_telegram_preflight.py -v`.
Tests execute the exact inline Python with synthetic environment/API responses,
block real network, and cover identity/scope guards, defaults, membership,
HTTP/JSON errors, timeouts, redirects, method allowlisting and output redaction.
No workflow, Bot API or production configuration is used by these tests.

Validation at preparation time: **20 offline tests passed**, YAML parsed and
manual trigger/guard/empty permissions/scope parity checked, inline Python AST
compiled, and the Bash heredoc passed `bash -n` without execution. Production
files and caches are unchanged from base `f4353d1`; PR #1 remains separate at
`c8d0837`. This is static/offline evidence, not a successful GitHub or Telegram
preflight run. No credentialed workflow dispatch has been performed.

References: [GitHub manual workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[GitHub secure use](https://docs.github.com/en/actions/reference/security/secure-use),
[Telegram Bot API](https://core.telegram.org/bots/api).
