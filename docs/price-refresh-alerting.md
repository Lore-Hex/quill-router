# Price refresh alerting

`price-refresh-alerts.yml` observes completed `refresh-prices.yml` runs on main
and checks freshness independently every hour. It runs trusted main code with
Python's standard library, without the refresh job's dependencies, GCP login,
provider keys, or artifacts. Existing price, coverage and exact-SHA CI gates are
unchanged.

## Signals

* **Refresh failed:** installation, authentication, parser, coverage, publication,
  timeout, and active cancellation failures send an error. Superseded queued
  cancellations are not incidents. The observer scans recent completions too,
  so coalesced completion notifications do not silently hide a recent failure.
* **Catalog stale:** no validated refresh in six hours sends a separate error.
  Only the `Catalog refresh validated` step counts. A successful run that only
  schedules recomputation because main moved does not count. A no-change run
  counts because it validated the current catalog. This measures publication,
  not deployment health, which remains covered by the release checks.
* **Watchdog missing:** the observer sends Sentry Cron check-ins after completing
  all reads, delivery and state writes. Sentry monitors this hourly heartbeat
  with a 60-minute grace period, so even a GitHub-wide scheduling outage or a
  broken observer can be detected outside GitHub. The first successful observer
  run provisions `price-refresh-watchdog`; verify it exists when installing.

The first observed failure sends immediately. Each error class sends at most
once per six hours across runs. Recovery closes the GitHub monitoring issue
but preserves cooldown timestamps, preventing flapping from spending the error
budget. Historical Sentry error issues are not automatically resolved: verify
the cause and recovery before resolving them. Cron recovery is automatic.

## Configuration and verification

Set the repository Actions secret `PRICE_REFRESH_SENTRY_DSN` to the existing
Sentry project's ingestion DSN. It is an ingestion credential only, not a
Sentry management token or any production provider key. No IAM grants are
needed. Keep it synchronized if the production DSN rotates.

Run the workflow manually after merging. Confirm:

1. The observer run succeeds, including Sentry acceptance and durable ledger.
2. `[bot] Price refresh monitoring` exists, created by `github-actions[bot]`.
3. The error events appear in Sentry and the `price-refresh-watchdog` Cron
   monitor is present with notification routing to the operator.
4. A second run does not emit duplicate errors inside the cooldown.

For a read-only preview, run `python3 scripts/price_refresh_alerts.py --dry-run`
with `GH_TOKEN` and `GITHUB_REPOSITORY=Lore-Hex/quill-router`. No Sentry or GitHub
writes occur. On initial installation, old successful runs have no publication
marker; freshness is conservatively unknown until a marked refresh completes.

The durable cooldown ledger is stored in the bot-owned issue, including when
closed. Do not delete it or manually edit its hidden state. Malformed state,
API errors, a missing DSN, and Sentry delivery failures fail the observer, not
catalog publication. A failed delivery never advances that event's cooldown.
Check-ins detect observer silence after initial provisioning. Neither this
code nor a successful HTTP ingestion response can restore exhausted Sentry
quota or prove email delivery; verify the project quota and alert routing too.

Only bounded run ID, attempt, commit, run URL, a fixed stage classification,
step number and timestamps leave GitHub. No logs, provider responses, prompts,
outputs, customer identifiers or credentials are sent to Sentry.
