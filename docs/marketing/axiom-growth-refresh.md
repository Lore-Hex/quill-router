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

The job uses 1 CPU and 512 MiB only while running. ClickHouse queries use at most
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

To pause: pause the `trusted-router-growth-sync` Cloud Scheduler job. To roll
back: restore its preceding image digest. Retain the checkpoint; failures are
replay-safe. Never delete the live state to make a failing job look healthy.

Verify two successive scheduled completions, fresh `growth.sync_completed`
rows, a real acquisition event, and updated daily usage before declaring the
feed live. Dashboard stale state and missing-data notices remain visible.

The acquisition sink uses exact event-name comparisons, not a regex. Reapply
its allowlist with `python -m scripts.axiom_growth.provision source-filter`.
During rollout an overescaped filter was caught by comparing an actual browser
event in the primary logs with the isolated bucket. HTTP success and a worker
heartbeat alone do not prove source delivery.

The optional operator tool `scripts.axiom_growth.alerts` configures a debounced
email monitor. It has not been enabled: notification delivery requires separate
approval. Freshness warnings on all four dashboards are active.
