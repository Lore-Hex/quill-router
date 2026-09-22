# Marketing attribution follow-through

## Contracts

The original dataset retains observed history. These changes do not manufacture
missing visits, retroactively assign experiments, or turn HTTP 200 into a
completed model response. No customer data is sent to advertising platforms.

### Billing accounts and people

`account_fingerprint` retains its existing observed-identity meaning. The new
`billing_account_fingerprint` identifies the **current workspace owner**, read
by the existing 30-minute workspace-directory job and hashed before export.
`billing_identity_basis=current_workspace_owner` and
`billing_owner_observed_at` make the timing and attribution model explicit.
This answers which commercial accounts consume tokens even without a browser
visit. It does not assert who called the API, who owned the workspace months
ago, or that every team member came from the owner's acquisition source.

If the directory is unavailable, the last verified hash-only snapshot is retained
with its original observation time and a `stale` heartbeat. Without a previous
snapshot, ownership is unresolved. Signup and payment export continue either way.

`billing_source` is assigned only when the owner's retained account journeys
agree on a source. Shared/ambiguous sources remain unattributed. Deleted and
federated shadow workspaces receive no local owner. Historic `account_fingerprint`
coverage and current commercial-owner coverage are reported separately.

The source already reads workspace records; there are no new inference-path
lookups, no user-table scans, and no new scheduled Spanner job. Its existing
kind-scoped directory read now has a 50,000-row cap, low priority and a 30-second
timeout. The growth worker has SELECT permission only on a hash-only view.

### Gateway attempts

Existing GCP enclave audits supply real starts and ends for Chat, Responses,
Messages, embeddings, image generation and speech requests. Local leases,
validation failures and provider fallbacks all pass through this outer request
audit. The collector does not log new content or touch settlement.

`growth.gateway_attempt` is a sanitized matched-attempt snapshot, deduplicated
by `event_id`. It exposes only hashed identity, the fixed route, timestamps,
HTTP status and pairing state. It never exports the original operational log.
HTTP response, HTTP error, connection closed, missing start, pending end and
conflicting records remain distinct. Provider finish reasons are unavailable in
this source and are **not inferred** from HTTP status.

A missing/invalid credential cannot honestly be assigned to an account. A missing
start is not reconstructed from latency. HTTP 200 may precede a streaming error;
settled activation remains the successful-inference measurement. Attempts outside
GCP are not included until their audit sources have independent collectors.

The collector runs off-path with a ten-minute overlap, 20-page query budget and
a 20,000-row audit buffer cap within the existing 512 MiB worker.
Completed pairs leave the buffer after 30 minutes; unmatched records have a
three-day limit. Only identifiable workspace attempts before their first retained
settled usage are exported. Existing established usage is not duplicated as
marketing telemetry. Compact first-attempt/error milestones survive that
buffer with account/workspace identity checks. Source failure leaves its cursor
unchanged and marks its heartbeat unavailable while other acquisition exports
continue. Ingest failure retains the entire previous checkpoint for replay.

### Two-arm welcome experiment

New untagged homepage visitors get a stable 50/50 assignment to
`onboarding_first_call_v1`: `run_request` or `get_answer`. Existing visitors and
explicit paid experiments keep their assignments. Privacy signals and crawlers
are excluded. The test changes the welcome heading/button only:

| Control | Treatment |
| --- | --- |
| Run my first API request | Get my first AI answer |

Both issue the same existing request with the same model, budget and response
handling. Agent-chat copy/paste, SDK examples and settings remain unchanged.
`acquisition.experiment_exposed` fires after the actionable, assigned welcome
page is rendered, not merely on assignment. The dashboard uses exposed accounts
and subsequent settled activation/purchase. Assignment balance in a test fixture
is not evidence of production lift or balanced customer cohorts. Wait for actual
traffic before comparing outcomes; do not manufacture signup or purchase events.

## Rollout

1. Run Ruff, mypy and the full Python suite; preserve the existing CI gates.
2. Apply `clickhouse/016_growth_billing_owners.sql` on the private cluster and
   update the directory refresher through its existing service. Verify only
   hashed owners leave the view and that deleted/shadow records are excluded.
3. Update the exact source sink with `scripts.axiom_growth.provision source-filter`.
   Verify real audit rows reach `tr-growth-source`; a heartbeat alone is insufficient.
4. Build the worker using its Dockerfile/cloudbuild.yaml. Preserve its service
   account, secrets, checkpoint, schedule, VPC and 512 MiB/1 CPU limit. Enable
   `GROWTH_GATEWAY_ATTEMPTS_ENABLED=true` and `GROWTH_BILLING_OWNERS_ENABLED=true`
   only after their sources are verified.
   `deploy-growth-sync.yml` provides a manual main-only release through the
   existing deployment identity, requiring green CI for the exact commit and
   `confirmation=APPLY`. Its two feature inputs default to disabled. It preserves
   all other job configuration and restores the old image/flags if execution
   fails. Feed-level degradation is visible in Axiom and requires operator
   verification; a successful Cloud Run execution alone is not acceptance.
5. Ship the welcome changes with the normal application workflow. Publish and
   execute all dashboard queries after source verification. Run
   `python -m scripts.axiom_growth.register_schema --apply` before query validation,
   with `GROWTH_AXIOM_TOKEN` loaded from the dedicated worker secret (never printed):
   Axiom needs the field types even when a new experiment has zero customers.
   This emits one deduplicated `schema_registration` record, never a customer,
   exposure, signup, payment, attempt or usage record.
6. Measure two scheduled executions, owner coverage, real paired attempts and
   production assignment cookies. Local callback tests use mocked OAuth; they
   are not represented as real customer signups.

Rollback the worker image and unset the two new flags; preserve the checkpoint.
The directory migration is additive. Existing reports never reinterpret lost
browser history as direct traffic or zero activity.
