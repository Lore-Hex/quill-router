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
# exits), and TR_FASTPATH_ENABLER_MAX_RANK=0 enables nobody off the include
# list. In apply mode the job writes nothing until an operator runs
# `fastpathctl enabler arm`, and never writes the hold table. The lists come
# from fastpath_lists.sh, shared with rollout.sh;
# tests/test_fastpath_lists.py holds them there.
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
  # 0: nobody joins off the include list. WS4 sets 10 and WS5 100.
  "TR_FASTPATH_ENABLER_MAX_RANK=0"
  # Section 4(b): the trust tier to join, 1 to 3. Never below the nodes'
  # required tier in effect (infra/fastpath.tf fastpath_required_tier), or
  # the enabler turns on workspaces every grant refuses: lowered only after
  # the nodes' is, and the nodes' raised only after this is
  # (tests/test_fastpath_enabler_deploy.py holds the two in order). 1 since
  # the nodes took -required-tier 1 with the lower tiers' allowances
  # (tr-fastpath-15 and 16, 2026-11-13; fastpath-deploy.md, "Lowering the
  # required tier"). With MAX_RANK 0 only the include list can join, and
  # every member was on or held when it was lowered.
  "TR_FASTPATH_ENABLER_REQUIRED_TIER=1"
  # Section 4(b): unspent credit of at least $100 to join, under $40 to leave.
  "TR_FASTPATH_ENABLER_ENABLE_BALANCE_MICRO=100000000"
  "TR_FASTPATH_ENABLER_DISABLE_BALANCE_MICRO=40000000"
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
