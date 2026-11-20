#!/usr/bin/env bash
# Deploy the fast path's enabler Cloud Run job and its 15-minute schedule
# (docs/design/fast-admission-widening.md sections 4(d), 5 and 5.7).
# Modelled on trust_tier_job.sh: job in us-east4, scheduler in the primary
# region at 12,27,42,57, five minutes after the tier job's 7,22,37,52. It runs
# on every control-plane deploy, not behind TR_TRUST_JOBS_DEPLOY, so the
# job's settings move with the control plane's.
#
# Its literals: TR_FASTPATH_ENABLER_MODE=apply (WS2) writes the allow-list
# (dry-run reads and reports but writes nothing; off logs one line and
# exits), and TR_FASTPATH_ENABLER_MAX_RANK=100 (WS5) lets the hundred largest
# eligible workspaces join; the rank counts the include list's own
# workspaces, which join at any rank. In apply mode the job
# writes nothing until an operator runs `fastpathctl enabler arm`, and never
# writes the hold table. The lists come from fastpath_lists.sh, shared with
# rollout.sh; tests/test_fastpath_lists.py holds them there.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"
# The fast path's workspace lists (TR_FASTPATH_PILOT_EXCLUDE and
# TR_FASTPATH_ENABLER_INCLUDE), shared with rollout.sh.
# shellcheck source=scripts/deploy/fastpath_lists.sh
source "${SCRIPT_DIR}/fastpath_lists.sh"

JOB_REGION="${TR_FASTPATH_ENABLER_JOB_REGION:-us-east4}"
SCHEDULER_REGION="${TR_FASTPATH_ENABLER_SCHEDULER_REGION:-${TR_PRIMARY_REGION}}"
JOB_NAME="trusted-router-fastpath-enabler"
SCHEDULER_NAME="trusted-router-fastpath-enabler-15m"

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  log "refusing fast path enabler deploy: image ${IMAGE} does not exist"
  exit 1
fi

env_vars=(
  "TR_ENVIRONMENT=worker"
  "TR_SERVICE_SURFACE=control"
  "TR_STORAGE_BACKEND=spanner-clickhouse"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  # One snapshot, then one transaction at a time.
  "TR_SPANNER_POOL_SIZE=2"
  # The watermark's maximum age, the tier job's own (section 4(a)).
  "TR_TRUST_RECONCILE_MAX_AGE_SECONDS=3600"
  # off, dry-run or apply. Literals, so no deploy inherits a mode; off is
  # the way back, and `fastpathctl enabler pause` the way to stop it
  # without a deploy. WS1 set dry-run and WS2 sets apply (section 8.2):
  # apply since WS2's entry, after hold-backfill ran again (held 0, holds
  # exit 0), a dry-run report after it read unheld_disabled 0 with nothing
  # to enable or disable, and `fastpathctl enabler arm` (2026-11-09).
  "TR_FASTPATH_ENABLER_MODE=apply"
  # Raised on 2026-11-20 with the join balance (below), to above the count
  # that could then be eligible, so the rank leaves none of them out. The
  # rank is by lifetime usage among the eligible, largest first: the
  # enabler's report that day counted 39 eligible and 159 more out for
  # their balance alone, 198 at most, and at the 100 of WS5 (section 8.2)
  # the smaller of the newly eligible would have stayed out while, past the
  # margin of a tenth, workspaces already on left for their rank as larger
  # ones joined. A workspace leaves for its rank only past this figure and
  # a tenth more. WS4 ran at 10 (all nine then eligible joined, mostly
  # dormant); WS2 and WS3 ran at 0. Lowering it is a way back (section 8.3).
  "TR_FASTPATH_ENABLER_MAX_RANK=250"
  # Section 4(b): the trust tier to join, 1 to 3. Never below the nodes'
  # required tier in effect (infra/fastpath.tf fastpath_required_tier), or
  # the enabler turns on workspaces every grant refuses: lowered only after
  # the nodes' is, and the nodes' raised only after this is
  # (tests/test_fastpath_enabler_deploy.py holds the two in order). 1 since
  # the nodes took -required-tier 1 with the lower tiers' allowances
  # (tr-fastpath-15 and 16, 2026-11-13; fastpath-deploy.md, "Lowering the
  # required tier"). Every include-list member was on or held when it was
  # lowered; a dry run at tier 1 and rank 10 (2026-11-13) would enable seven
  # workspaces by rank and disable none.
  "TR_FASTPATH_ENABLER_REQUIRED_TIER=1"
  # Section 4(b), amended 2026-11-20: unspent credit of at least $3 to join,
  # under $2 to leave, over the nodes' $2 floor (infra/fastpath.tf
  # fastpath_floor). Until then $30 and $5 over a $20 floor (2026-11-13),
  # and $100 and $40 before, which admitted only nine workspaces.
  # The order: these come down only after both nodes run the lower floor,
  # and go back up before the floor does. tests/test_fastpath_enabler_deploy.py
  # holds the join balance at least a dollar above the nodes' floor and the
  # leave balance no lower than it, so a change that moves one side alone
  # fails.
  # The join is the floor and a dollar. A grant needs headroom of the floor
  # plus the lease asked, and an ask is about a minute of spend, at least
  # $0.01, so a dollar is room for a first lease and some leased spend.
  # The leave is the floor itself. Under the floor no lease is granted, and
  # a workspace left on there pays a front door hop for each request while
  # its owner asks for a lease once a second and is refused (the $5 to $20
  # band the old lines left). $2 is below the console's least auto-refill
  # threshold ($10): auto-refill fires only under its threshold, so a
  # refilling workspace never crosses the leave line. A workspace that
  # spends its credit down and buys more leaves and joins once a cycle, at
  # the schedule's pace. The 503 for headroom in leases is on (rollout.sh,
  # required below $100 by the same tests), so a synchronous hold that meets
  # a lease holding the rest is asked to retry, never refused for credit.
  # The leave threshold must stay below the join threshold (config.py).
  # The way back: `fastpathctl enabler pause` first, then both lines back
  # up, the leave line in steps. A run that finds more workspaces to
  # disable for a changed fact, their balance here, than the larger of 20
  # and a tenth of the enabled set disables none of them and alerts
  # fastpath_enabler_mass_disable, so each step leaves at most that many
  # under the line. Only then the nodes' floor (fastpath-deploy.md,
  # "Lowering the floor").
  "TR_FASTPATH_ENABLER_ENABLE_BALANCE_MICRO=3000000"
  "TR_FASTPATH_ENABLER_DISABLE_BALANCE_MICRO=2000000"
  "TR_FASTPATH_ENABLER_INCLUDE=${TR_FASTPATH_ENABLER_INCLUDE}"
  "TR_FASTPATH_PILOT_EXCLUDE=${TR_FASTPATH_PILOT_EXCLUDE}"
  # The async shadow's workspaces, the same literal as rollout.sh's (a test
  # holds the two equal): the enabler leaves them out (section 4(c)).
  "TR_ASYNC_SETTLE_SHADOW_WORKSPACES="
)
set_env_vars="$(IFS='|'; echo "^|^${env_vars[*]}")"

# The synthetic monitor's and the Stage D probe's keys, from the control
# plane's own secrets, each where it exists: the job keeps only their lookup
# hashes, to leave their workspaces out (section 4(c)). Named outside the
# TR_ prefix, since the control surface's settings may not hold them. The
# async settle shadow's probe key stays confined to its own job; its
# workspace is left out by TR_FASTPATH_PILOT_EXCLUDE, which names it
# since WS2. The inventory lists names only, never a value; a failure to
# list stops the deploy.
if ! secret_names="$(gc secrets list --format='value(name)')"; then
  log "refusing fast path enabler deploy: cannot list the secret inventory"
  exit 1
fi
secrets=("TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest")
for pair in \
  "FASTPATH_ENABLER_MONITOR_KEY=trustedrouter-synthetic-monitor-api-key" \
  "FASTPATH_ENABLER_STAGE_D_PROBE_KEY=trustedrouter-stage-d-probe-api-key"; do
  if grep -Fxq -- "${pair#*=}" <<<"$secret_names"; then
    secrets+=("${pair}:latest")
  fi
done
# --set-secrets replaces every binding, so a secret removed since the last
# deploy leaves no stale binding that would fail the job's start.
set_secrets="$(IFS=,; echo "${secrets[*]}")"

if gc run jobs describe "$JOB_NAME" --region "$JOB_REGION" >/dev/null 2>&1; then
  mutation=update
else
  mutation=create
fi
gc run jobs "$mutation" "$JOB_NAME" \
  --region "$JOB_REGION" \
  --image "$IMAGE" \
  --command="/app/.venv/bin/python" \
  --args="-m,trusted_router.fastpath_enabler" \
  --service-account "$RUN_SERVICE_ACCOUNT" \
  --set-env-vars "$set_env_vars" \
  --set-secrets="$set_secrets" \
  --max-retries=0 \
  --task-timeout=10m \
  --cpu=1 \
  --memory=512Mi \
  --quiet >/dev/null

gc run jobs add-iam-policy-binding "$JOB_NAME" \
  --region="$JOB_REGION" \
  --member="serviceAccount:${RUN_SERVICE_ACCOUNT}" \
  --role=roles/run.invoker \
  --quiet >/dev/null

uri="https://${JOB_REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
common=(
  --location="$SCHEDULER_REGION"
  --schedule="12,27,42,57 * * * *"
  --time-zone=UTC
  --uri="$uri"
  --http-method=POST
  --oauth-service-account-email="$RUN_SERVICE_ACCOUNT"
  --attempt-deadline=30s
  --max-retry-attempts=0
  --quiet
)
if gc scheduler jobs describe "$SCHEDULER_NAME" \
    --location="$SCHEDULER_REGION" >/dev/null 2>&1; then
  gc scheduler jobs update http "$SCHEDULER_NAME" "${common[@]}" >/dev/null
else
  gc scheduler jobs create http "$SCHEDULER_NAME" "${common[@]}" >/dev/null
fi
log "fast path enabler job deployed, five minutes after the tier job"
