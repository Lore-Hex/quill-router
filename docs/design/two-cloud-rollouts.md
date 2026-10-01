# Two-Cloud Rollouts

## Policy

Production deployments may mutate **two distinct clouds**, never all three.
Control-plane, public-site and gateway rollouts in the same cloud share one
exclusive reservation. A different cloud must remain healthy and unchanged.
Regional canaries, billing checks, attestation, rollback and final completeness
checks still apply. This replaces the default 24-hour AWS/Azure promotion delay;
explicit `promote` and `canary` bake modes remain available as stricter options.

The guard is deployment coordination, not a promise that independent outages
cannot happen. Cloud health is rechecked at admission and before regional
mutations. Existing per-region recovery remains responsible for rollback.

## One Coordinator

`scripts/deploy/cloud_rollout.py` owns the protocol. The shell compatibility API
and quill-cloud-proxy's commit-and-SHA256-pinned bootstrap use that implementation.
One generation-CAS journal in the existing private deployment-mutex bucket holds
all reservations. Two separate lock objects would permit a three-way race.

Each reservation records cloud, component, operation ID, owner workflow or local
process, and timestamps. The protected cloud records a verified cloud-local
control-plane release. Both its control-plane and gateway health must pass;
redirects, unknown releases and wrong-cloud API URLs fail closed.

Release removes only the matching operation's reservation after completion.
Failed or interrupted operations remain reserved. Expiry **does not** unlock
them. The bucket must have no lifecycle deletion rules. A cleared V2 journal
remains present, so legacy clients cannot acquire the old single-owner lock.
Legacy lock owners must finish before migration; never delete a live lock.

## Catch-Up

`reconcile-cloud-releases.yml` runs after successful GCP control-plane deployments
and every 15 minutes. It selects an exact merged commit whose regional rollout,
public companion, completeness and finalization jobs all passed. A superseded
run with skipped deploy jobs is not a candidate. It dispatches stale AWS/Azure
control planes, skips already queued/running jobs and never downgrades a newer
or divergent deployment. Actual admission happens inside those workflows.

App artifacts may be older than main; coordination scripts must be from the
reviewed workflow checkout (`ops`), not the selected historical artifact (`src`).
Gateway image promotions still require their cloud-specific attestation steps;
the control-plane catch-up job does not build or repin gateway images.

## Activation

1. Merge both repositories' guard changes, with quill-cloud-proxy pinned to the
   reviewed coordinator commit and content hash. Test the pinned bootstrap.
2. Confirm no legacy deployment is still running, including manual operators.
3. Remove the existing lock bucket's lifecycle rules. The coordinator refuses
   to start until bucket metadata proves automatic deletion is disabled.
4. Run the normal gated workflows; inspect journal ownership and regional health.
   No additional application database migration or provider-key rotation is needed.

## Recovery

Inspect `bash scripts/deploy/deploy_mutex.sh status`. Do not remove the object.
Identify the exact owner and operation. Stop and verify any remaining cloud-side
rollout (ECS refresh, MIG update or Azure deployment) and complete rollback or
regional health, billing and attestation checks. A completed GitHub run alone
does not prove that an asynchronous cloud operation finished.

Only then run `bash scripts/deploy/deploy_mutex.sh recover --cloud CLOUD
--operation OPERATION`. Recovery independently requires the owner workflow to
be completed (or a same-host local PID to be gone) and fresh cloud-local health.
It does not auto-recover by time or disable any fail-closed application behavior.

For phased manual tools, first `export TR_DEPLOY_OWNER_PID=$$` in the long-lived
operator shell (not an acquire subprocess). A missing owner PID cannot prove
that a manual operation stopped and therefore cannot be automatically recovered.
For phased gateway tools, acquire the outer reservation using
`python3 tools/cloud-rollout.py acquire --cloud CLOUD`, export its fence, retain
it across all regions and the bind/deploy/verify/narrow phases, then call
`release --cloud CLOUD --outcome success` only after all completion gates pass.
Use `--outcome failure` on interruption. Never release between individual phases.
