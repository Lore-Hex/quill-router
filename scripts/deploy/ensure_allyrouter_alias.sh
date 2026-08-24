#!/usr/bin/env bash
# Idempotently provision and attach the public control-plane certificate for
# AllyRouter. DNS must already point every listed hostname at the control-plane
# load balancer before Google can mark the managed certificate ACTIVE.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
CERT_NAME="${CERT_NAME:-allyrouter-control-cert-v1}"
HTTPS_PROXY="${HTTPS_PROXY:-trusted-router-control-https-proxy}"
DOMAINS="${DOMAINS:-allyrouter.com,www.allyrouter.com,status.allyrouter.com,trust.allyrouter.com}"

if ! gcloud compute ssl-certificates describe "${CERT_NAME}" \
  --global --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute ssl-certificates create "${CERT_NAME}" \
    --global \
    --project="${PROJECT_ID}" \
    --domains="${DOMAINS}"
fi

current="$(gcloud compute target-https-proxies describe "${HTTPS_PROXY}" \
  --global \
  --project="${PROJECT_ID}" \
  --format='value(sslCertificates.basename())')"
current_csv="${current//;/,}"
case ",${current_csv}," in
  *",${CERT_NAME},"*) ;;
  *)
    gcloud compute target-https-proxies update "${HTTPS_PROXY}" \
      --global \
      --project="${PROJECT_ID}" \
      --ssl-certificates="${current_csv},${CERT_NAME}"
    ;;
esac

gcloud compute ssl-certificates describe "${CERT_NAME}" \
  --global \
  --project="${PROJECT_ID}" \
  --format='yaml(managed.status,managed.domainStatus,expireTime)'
