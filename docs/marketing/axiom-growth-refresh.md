# Continuous Growth Analytics

The four `TR Growth` Axiom dashboards read `trustedrouter-marketing`. They are
separate from the continuously shipped operational `trusted-router-logs` dataset.

## September 16 Incident

The dashboards were backed by a September 11 manual snapshot. Their five-minute
panel refresh only reran queries; it did not import new events. Access permissions
were not the cause. The retained September 11-16 records were backfilled.

Historical browser journey coverage is incomplete from August 23 at 15:56 UTC
through September 4 at 18:52 UTC. Some application logs in this interval contain
only the event name, without a visitor fingerprint or campaign. These cannot be
turned into attributed visits. Usage and observed conversion records still exist
through this interval. Do not call missing browser history zero traffic, or
combine recovered `history.*` milestones with observed conversions.

## Worker

`scripts/axiom_growth/runtime.py` runs as a dedicated Cloud Run job every five
minutes. A one-minute delay, one-hour overlap and daily 24-hour replay allow for
late source delivery. Each backlog run advances at most one day. Outages beyond
source retention fail closed and require a reviewed backfill.

Only changed event, daily usage and journey snapshots are sent. Stable event IDs
and dashboard `arg_max(exported_at, ...)` make retries at-least-once and
query-deduplicated. A partial or failed ingest never advances the checkpoint.
A conditional-write GCS lease prevents concurrent executions. The private state
retains up to 365 days; explicit row and byte limits fail rather than truncate.

Sources are bounded Axiom acquisition queries, allowlisted acquisition and gateway
audit Cloud Logging, and SELECT-only ClickHouse views. The gateway projection
exports content-free first-attempt metadata for observed signup cohorts only.
Daily usage excludes synthetics and retains
the GCP source scope. Workspaces are not people; ambiguous visitor links remain
unattributed. Credit purchases are top-ups, not recognized revenue.

## Access And Cost

The worker identity is `tr-growth-sync`. It has no Spanner, billing or inference
permissions. Its Cloud Logging access is restricted to the `tr-growth-source`
bucket, fed only by the named acquisition-event/audit sink. Its dedicated ClickHouse
user can read only `tr.growth_daily_usage` and `tr.growth_billing_owners`, which hash
identities before returning them. It cannot read the underlying request or
directory tables. Current billing ownership is not historical caller identity;
see [the follow-through contract](attribution-completion.md).

The dataset-scoped Axiom token and dedicated ClickHouse password live in Secret
Manager with dedicated worker access and authorized operator access. No personal CLI credentials ship in the
image. Operator provisioning uses the existing local login; it is never scheduled.
The Axiom token expires in one year and must be rotated before expiry.

The job uses 1 CPU and 1 GiB only while running. ClickHouse queries use at most
two threads, 512 MiB and 45 seconds. Incremental export avoids reingesting every
historical journey each run. No data goes to Google Ads or another ad platform.

## Release And Recovery

Run repository Ruff, mypy and the full test suite before releasing. Build only
the worker directory with `cloudbuild.yaml`; the image contains four runtime
modules, not operator tools or local caches. Deploy the immutable image digest,
one task, no automatic retries, 240-second timeout, private VPC egress, and the
dedicated service account. The scheduler calls the authenticated Cloud Run job
using a job-scoped invoker identity. There is no public HTTP endpoint.

Bootstrap only once from the validated sanitized cache using
`python -m scripts.axiom_growth.provision bootstrap --cache <path>`. GCS writes
require generation zero and cannot silently replace an existing checkpoint.
Keep source transport and timestamps unchanged when replaying historical data.

The `deploy-growth-sync` workflow keeps the gateway-attempt and billing-owner
collector flags exactly as the running job has them unless the operator picks
`true` or `false` for that run; its summary records the before and after values.
The isolated export job has 1 GiB of memory because 512 MiB runs exhausted
memory while serializing the retained checkpoint after ingest. Its existing
row limits, timeout, CPU, network and identity remain unchanged. A failed
release execution restores the previous image, memory limit and collector
flags. Verify completed Cloud Run executions, not just pre-checkpoint Axiom
heartbeats, when checking the two scheduled cycles.

### Gateway Attempt Recovery

Gateway audits have their own cursor. A stopped audit collector does not stop
acquisition and usage export. The `tr-growth-source` Cloud Logging bucket retains
90 days; verify that setting before relying on historical recovery. A cursor
older than 89 days fails closed for explicit operator recovery, never jumps to
the present, and never fabricates missing starts.

Each scheduled run can replay up to twelve five-minute windows. A window that
ends within an hour of now also re-reads the ten minutes before it for late
deliveries; older history is settled and reads no overlap, because at peak
density the overlap alone held about 14,500 audits and no window fit the budget
(the cursor stalled at 2026-09-28 06:15Z for a month). All windows share the existing twenty-page / 20,000-source-record
budget and a sixty-second source deadline. Page requests use at most ten seconds.
This runs off the inference path, with no Spanner access or additional worker.
Dense or slow windows retry next run; incomplete pages never advance the cursor.
The 20,000-audit checkpoint cap still fails rather than truncating records.
The source filter and projector read the audit line from `jsonPayload.message`
or `jsonPayload.MESSAGE`: Confidential Space launcher images wrote `MESSAGE`
until VMs picked up a newer image on 2026-10-06. A changed source filter needs
the operator `source-filter` step before the deploy, as above.

Buffered evidence is projected before compaction. Completed pairs are retained
through the overlap, unmatched records keep the existing three-day horizon, and
first-attempt milestones survive in the compact checkpoint. Expiry follows the
historical cursor while recovering, so wall time cannot erase a historical start
before its matching end is read. Axiom ingest must succeed before checkpointing.

Watch `gateway_observed_through` separately from `observed_through` in
`growth.sync_completed`. `gateway_status=catching_up` means complete historical
windows advanced but the gateway feed is not current. `unavailable` means a source
or safety-bound failure. Only `current` means its cursor reached the cycle target.
Do not call catch-up complete from a fresh acquisition heartbeat alone.

To pause: pause the `trusted-router-growth-sync` Cloud Scheduler job. To roll
back: restore its preceding image digest. Retain the checkpoint; failures are
replay-safe. Never delete the live state to make a failing job look healthy.

Verify two successive scheduled completions, fresh `growth.sync_completed`
rows, a real acquisition event, and updated daily usage before declaring the
feed live. Dashboard stale state and missing-data notices remain visible.

The acquisition sink uses exact event-name comparisons, not a regex. Reapply
its allowlist with `python -m scripts.axiom_growth.provision source-filter`.
The service allowlist includes `trusted-router-control`, where
`/token-exchange/events` is routed, alongside the public and legacy combined
services. Keep the exact event allowlist in place on every service; including
the control plane does not authorize exporting other control-plane logs.
The sink and worker read query share `model.ACQUISITION_SERVICE_FILTER` so a
collected event cannot be excluded by a stale, independently maintained reader.
An exact match is a read-only no-op, so ordinary worker redeploys need only
`logging.sinks.get`. A changed filter still requires an authorized operator to
apply it; the deploy identity is not granted general sink-update access.
During rollout an overescaped filter was caught by comparing an actual browser
event in the primary logs with the isolated bucket. HTTP success and a worker
heartbeat alone do not prove source delivery.

The optional operator tool `scripts.axiom_growth.alerts` configures a debounced
email monitor. It has not been enabled: notification delivery requires separate
approval. Freshness warnings on all four dashboards are active.
