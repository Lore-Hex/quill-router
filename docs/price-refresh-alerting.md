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
  publication. The job needs `actions: write` for this. The workflow
  observer runs on GitHub's scheduler too, which is the one dropping the
  refresh cron, so the independent host observer (below) dispatches as well,
  with its own narrowly scoped token. Both apply the same guards to the same
  run list: nothing while a refresh run is queued, waiting or in progress,
  and nothing within 90 minutes of a run's creation or start, so the run one
  observer dispatched blocks the other. Their cooldown ledgers are separate
  (the host imports the bot ledger's cooldowns; the bot cannot read the
  host's), so the run list is the shared guard. The only overlap is two
  observers deciding within the same few seconds, both at `:23` when GitHub
  runs the workflow on time: then two dispatches can land, the second waits
  as the refresh concurrency group's one pending run (`cancel-in-progress:
  false`), and it is an extra no-change refresh, not a cancelled one.
* **Watchdog missing:** the observer sends paired Sentry Cron check-ins on start
  and completion, reporting OK only after all reads, delivery and state writes.
  An observer error or ten-minute timeout counts as a failed check-in.
  Sentry monitors this hourly heartbeat
  with a 15-minute grace period (the timer starts within about 40 s of
  :23) and opens the Cron issue after two consecutive failed or missed
  check-ins. A GitHub-wide scheduling outage or a silent host is detected
  outside GitHub about 2 h 15 min after the last check-in, and an observer
  that keeps erroring pages on its second failed run, while one transient
  failure does not page. The host observer reads GitHub directly
  (anonymously, or with its token: see Host observer token) and retries a
  timeout, connection reset, body shorter than its Content-Length,
  truncated chunked body or 5xx twice (2 s, then 8 s). A rate-limit reply (429,
  or 403 with rate-limit headers) with a `Retry-After` of up to 30 s waits the
  longer of that value and the same 2 s, then 8 s backoff; a longer
  `Retry-After`, any other 4xx, a TLS certificate verification failure and any
  other TLS error except a connection that ended mid-TLS fail the run at once.
  A 5xx `Retry-After` is not consulted. Each attempt builds a fresh request, so
  a proxy rewrite of one attempt cannot downgrade the next. The token's
  dispatch POST is attempted once and never retried: a dispatch that timed
  out may already have queued a run, and a second could cancel it. All retries in a run share one 180-second
  window. No read attempt starts unless one slow attempt (two 15-second socket
  timeouts) still fits before 420 s into the run, so a slow run, with or
  without retries, fails with a logged reason and leaves the unit's nine
  minutes room to report. A normal run takes about 20 seconds; a run whose 22
  reads each take 20 seconds now fails at about 400 s where it used to finish
  at 440 s, a deliberate trade for a clean failure. The socket timeout bounds
  each blocking operation, not a whole response, so a server that trickles
  bytes can still hold an attempt longer. systemd's `TimeoutStartSec` stops the
  process; the monitor's ten-minute `max_runtime` does not stop anything, it
  marks the open check-in failed. A failed read logs only a fixed reason, such as
  `(HTTP 403, rate limited)`, `(timeout)` or `(run deadline reached)`, never a
  URL or response body: read it with `gcloud logging read` on the host's
  syslog (`price-refresh-observer.service`). The first successful observer
  run provisions `price-refresh-watchdog`; verify it exists when installing.
  The external observer runs on `tr-clickhouse-3`
  (`scripts/ops/install_price_refresh_observer.sh`); moving or rebuilding that
  host means reinstalling it first. Run the installer from a detached
  worktree at the target SHA, so the installer matches the unit it installs:

  ```bash
  sha=<full SHA merged into origin/main>
  git fetch origin
  git worktree add --detach "/tmp/tr-observer-$sha" "$sha"
  bash "/tmp/tr-observer-$sha/scripts/ops/install_price_refresh_observer.sh" "$sha"
  git worktree remove "/tmp/tr-observer-$sha"
  ```

  The installer refuses to run, before any gcloud call, unless it is
  byte-identical to its own version at that SHA (on 2026-11-19 an older
  installer installed a newer unit without the credential it loads).

The first observed failure sends immediately. Each error class sends at most
once per six hours across runs. Recovery closes the GitHub monitoring issue
but preserves cooldown timestamps, preventing flapping from spending the error
budget. Historical Sentry error issues are not automatically resolved: verify
the cause and recovery before resolving them. Cron recovery is automatic.

## Publication when main keeps moving

The publication step pushes a snapshot to main only after a successful
`ci.yml` run of that exact SHA, whose parent must still be main's head. A
busy main (a merge every 20 to 40 minutes) against a 35 to 55 minute CI run
makes that a race, and the stale alert above is what reports losing it. Two
rules keep the race winnable without ever pushing an untested tree:

* **Carry instead of recompute.** When main moved but changed no refresh
  input (`scripts/pricing/refresh_inputs.py`: the refresh's import closure,
  catalog data, parsers, lock, workflow, and only the `static/` directories the
  social cards are drawn from and written to), the snapshot commit is
  replayed onto the new head and that new SHA gets its own CI run. A change
  to an input still queues a fresh recomputation. Until 2026-11-20 all of
  `static/` counted, so every site-page or CSS merge forced a recompute.
* **Restart at once.** The CI poll checks main about once a minute. When main
  moves, the running CI run can no longer be published: it is cancelled by id
  and the next round starts right away (carry and re-dispatch, or recompute).
  Before, the job waited for the doomed run to finish and then needed main to
  stay still for one more whole CI run. On 2026-11-19 (about 9 hours) and
  11-20 (runs 44782888255 and 44800370604) every round lost. Publication now
  needs one quiet CI-length window after any merge.

Rounds are bounded: at most six exact-SHA CI dispatches, and no new round
after 150 minutes of publication. A run that runs out of rounds queues a fresh
recomputation and ends green without validating, which the stale alert counts
as no publication.

## Host observer token

The host observer on `tr-clickhouse-3` reads GitHub's REST API directly
(`TR_PRICE_REFRESH_PUBLIC_API=1`: urllib, no gh CLI, local state required,
never writes the issue ledger). Without a token it reads anonymously, which
GitHub limits to 60 requests an hour per address, and refuses
`--dispatch-stalled`. That is still the behavior when no
`--github-token-file` is given.

With `--github-token-file`, every read is authenticated (5,000 an hour) and
the token's one permitted write is `POST
actions/workflows/refresh-prices.yml/dispatches` with `{"ref": "main"}`; any
other write is refused before a request is made. The token is read from the
file, stripped and checked to be one fine-grained token (`github_pat_...`);
it is held in memory only, sent only to `api.github.com` with redirects
disabled, and never placed in argv, the environment, logs, exception text or
Sentry payloads.

* **Token:** a fine-grained personal access token, resource owner Lore-Hex,
  repository `quill-router` only, permission Actions: read and write
  (metadata read is implied). Nothing else.
* **Storage:** Secret Manager `trustedrouter-price-refresh-dispatch-token`
  in `quill-cloud-proxy`, version 1 added 2026-11-19 21:54Z. Terraform
  manages only the secret container (`infra/price_refresh_observer.tf`,
  adopted by an import in `infra/imports.tf`); versions are added by hand,
  so no token is in Terraform state. There is no IAM binding for it.
* **Delivery:** `scripts/ops/install_price_refresh_observer.sh` reads the
  latest version with the operator's own gcloud identity (the owner login or
  `tr-deploy`, which already read every secret and are allowed readers in
  `infra/secret_reader_alert.tf`), exactly as it reads
  `trustedrouter-sentry-dsn`, copies it over IAP and installs it as
  `/etc/tr-price-refresh/github-dispatch.token` (root, 0600). The unit loads
  it with `LoadCredential=github-token:...` and passes `--github-token-file
  %d/github-token --dispatch-stalled`. The host has no Google identity for
  it. If the secret cannot be read, or is not one fine-grained token, the
  installer stops before copying anything to the host.
* **Renewal:** the token expires about one year after creation, around
  **2027-11-19** (check the exact date on the token's GitHub settings page).
  Before then, create a replacement with the same scope, add it as a new
  version (`gcloud secrets versions add
  trustedrouter-price-refresh-dispatch-token --data-file=FILE
  --project=quill-cloud-proxy`, then delete FILE), rerun the installer with
  the current main SHA, and disable the old version and revoke the old
  token. An expired token makes every read fail with 401: the observer
  errors, the `price-refresh-watchdog` Cron issue opens, and refreshes are
  no longer dispatched from the host.

After installing, check on the host (no token is printed):

```sh
systemctl show price-refresh-observer.service -p Result -p ExecMainStatus
journalctl -u price-refresh-observer.service -n 20 --no-pager
sudo stat -c '%a %U %n' /etc/tr-price-refresh/github-dispatch.token  # 600 root
cat /opt/tr-price-refresh/SOURCE_SHA
```

The journal shows `Price refresh monitoring: ...; durable local ledger`, and
`Dispatched refresh-prices.yml on main` on a run that kicked a stalled
refresh (that run then appears as a `workflow_dispatch` run of
`refresh-prices.yml` started by the token's owner). The Sentry
`price-refresh-watchdog` monitor keeps reporting OK hourly.

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
