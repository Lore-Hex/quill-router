# Price refresh alerting

`price-refresh-alerts.yml` observes completed `refresh-prices.yml` runs on main
and checks freshness independently every hour. It runs trusted main code with
Python's standard library, without the refresh job's dependencies, GCP login,
provider keys, or artifacts. Existing price, coverage and exact-SHA CI gates are
unchanged.

## Signals

* **Refresh failed:** installation, authentication, parser, coverage, publication,
  deploy-check, timeout, and active cancellation failures send an error. The
  deploy check is the job's last step (stage `deploy_dispatch`): it fails when
  it cannot tell whether a waiting snapshot needs a rollout, when its dispatch
  of `deploy.yml` fails, and when a snapshot is past the limit but main's head
  already had a deploy run that delivered nothing. Superseded queued
  cancellations are not incidents. The observer scans recent completions too,
  so coalesced completion notifications do not silently hide a recent failure.
* **Catalog stale:** no validated refresh in six hours sends a separate error.
  Only the `Catalog refresh validated` step counts. A successful run that only
  schedules recomputation because main moved does not count. A no-change run
  counts because it validated the current catalog. This measures publication,
  not deployment health, which remains covered by the release checks.
* **Dropped schedule, self-healed:** GitHub drops scheduled runs (the hourly
  `:37` cron started only twice on 2026-11-18, and the last validated refresh
  that day was 15:52Z until a manual dispatch). When no `refresh-prices.yml`
  run on main was created or started in the last 90 minutes and none is
  queued, waiting or in progress, the workflow observer (`--dispatch-stalled`)
  dispatches `refresh-prices.yml` on main itself, so a dropped cron recovers
  within about an hour instead of paging after six. It never dispatches while
  a run is active: the refresh keeps one pending run per concurrency group, so
  a dispatch then could cancel the queued run. At most one dispatch per 90
  minutes, recorded as `refresh_dispatch` in the durable ledger; a crash
  before the ledger write is still bounded because the dispatched run itself
  counts as a recent start. `--dry-run` prints the decision without
  dispatching. A failed dispatch sends a separate `refresh_dispatch_failed`
  error (same six-hour cooldown) and never stops the failure and freshness
  checks: the stale alert still fires when dispatching does not restore
  publication. The job needs `actions: write` for this. The independent host
  observer reads GitHub anonymously, has no GitHub token, and does not
  dispatch; giving it one would be a new credential.
* **Watchdog missing:** the observer sends paired Sentry Cron check-ins on start
  and completion, reporting OK only after all reads, delivery and state writes.
  An observer error or ten-minute timeout counts as a failed check-in.
  Sentry monitors this hourly heartbeat
  with a 15-minute grace period (the timer starts within about 40 s of
  :23) and opens the Cron issue after two consecutive failed or missed
  check-ins. A GitHub-wide scheduling outage or a silent host is detected
  outside GitHub about 2 h 15 min after the last check-in, and an observer
  that keeps erroring pages on its second failed run, while one transient
  failure does not page. The host observer reads GitHub anonymously and
  retries a timeout, connection reset, body shorter than its Content-Length,
  truncated chunked body or 5xx twice (2 s, then 8 s), and honors a
  `Retry-After` of up to 30 s. A longer rate limit or another 4xx fails the
  run at once. All retries in a run share one 180-second window. No read
  attempt starts unless one slow attempt (two 15-second socket timeouts) still
  fits before 420 s into the run, so a slow run, with or without retries,
  fails with a logged reason and leaves the unit's nine minutes room to report.
  A normal run takes about 20 seconds. The socket timeout bounds each blocking
  operation, not a whole response, so a server that trickles bytes can still
  hold an attempt longer; systemd's `TimeoutStartSec` and the monitor's
  ten-minute `max_runtime` remain the hard bound, and either reports a failed
  check-in. A failed read logs only a fixed reason, such as
  `(HTTP 403, rate limited)`, `(timeout)` or `(run deadline reached)`, never a
  URL or response body: read it with `gcloud logging read` on the host's
  syslog (`price-refresh-observer.service`). The first successful observer
  run provisions `price-refresh-watchdog`; verify it exists when installing.
  The external observer runs on `tr-clickhouse-3`
  (`scripts/ops/install_price_refresh_observer.sh`); moving or rebuilding that
  host means reinstalling it first.

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
