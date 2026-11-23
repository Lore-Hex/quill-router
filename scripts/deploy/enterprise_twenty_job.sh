#!/usr/bin/env bash
# Run only after the private queues, scoped role and secret version exist.
set -euo pipefail
project=quill-cloud-proxy
region=us-central1
job=tr-enterprise-twenty
image=${1:?supply the approved control-plane GAR image digest}
version=${2:?supply a numeric, verified People-only key version}
[[ "$image" =~ ^us-central1-docker\.pkg\.dev/quill-cloud-proxy/[a-z0-9_/-]+@sha256:[a-f0-9]{64}$ ]]
[[ "$version" =~ ^[1-9][0-9]*$ ]]
worker="$job@$project.iam.gserviceaccount.com"
timer="$job-timer@$project.iam.gserviceaccount.com"
gcloud run jobs deploy "$job" --project="$project" --region="$region" --image="$image" \
  --service-account="$worker" --command=/app/.venv/bin/python \
  --args=-m,trusted_router.services.twenty_enterprise --tasks=1 --parallelism=1 \
  --max-retries=0 --task-timeout=8m --cpu=1 --memory=512Mi \
  --set-secrets="TR_TWENTY_ENTERPRISE_KEY=tr-enterprise-twenty-key:$version"
gcloud run jobs add-iam-policy-binding "$job" --project="$project" --region="$region" \
  --member="serviceAccount:$timer" --role=roles/run.invoker
# Install the timer after the manual canary, not merely after job deployment.
if [[ ${3:-} == --verified-canary ]]; then
  action=create
  if gcloud scheduler jobs describe "$job" --location="$region" --project="$project" >/dev/null 2>&1; then
    action=update
  fi
  gcloud scheduler jobs "$action" http "$job" --project="$project" --location="$region" \
    --schedule='*/5 * * * *' --time-zone=Etc/UTC --http-method=POST --message-body='{}' \
    --uri="https://run.googleapis.com/v2/projects/$project/locations/$region/jobs/$job:run" \
    --oauth-service-account-email="$timer"
fi
