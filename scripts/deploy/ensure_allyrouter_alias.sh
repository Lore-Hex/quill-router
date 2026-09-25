#!/usr/bin/env bash
# Idempotently provision and attach the public control-plane certificate for
# AllyRouter. DNS must already point every listed hostname at the control-plane
# load balancer before Google can mark the managed certificate ACTIVE.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-quill-cloud-proxy}"
CERT_NAME="${CERT_NAME:-allyrouter-control-cert-v1}"
HTTPS_PROXY="${HTTPS_PROXY:-trusted-router-control-https-proxy}"
DOMAINS="${DOMAINS:-allyrouter.com,www.allyrouter.com,status.allyrouter.com,trust.allyrouter.com}"

# A proxy with a certificate map serves the map's certificates
# (infra/control_lb_certificate_map.tf) and ignores its classic ones. Then this
# script creates and attaches none: it succeeds when every hostname has an
# ACTIVE map entry with an ACTIVE certificate and fails otherwise.
certificate_map="$(gcloud compute target-https-proxies describe "${HTTPS_PROXY}" \
  --global \
  --project="${PROJECT_ID}" \
  --format='value(certificateMap)')"
if [ -n "${certificate_map}" ]; then
  map_entries="$(gcloud certificate-manager maps entries list \
    --map="${certificate_map##*/}" \
    --location=global \
    --project="${PROJECT_ID}" \
    --format=json)"
  map_certificates="$(gcloud certificate-manager certificates list \
    --location=global \
    --project="${PROJECT_ID}" \
    --format=json)"
  # Certificate Manager serves a host from the entry for that exact name, or
  # else from the entry for *.<its parent domain>. An entry is PENDING while it
  # propagates to the load balancer's frontends. The two lists reach python3
  # on stdin, separated by an ASCII record separator, not through the
  # environment: Linux limits one environment string to 128 KiB, and the
  # certificate list, which carries PEM chains, was 233 KiB on 2026-09-25.
  gaps="$(printf '%s\n\036\n%s' "${map_entries}" "${map_certificates}" | HOSTS="${DOMAINS}" python3 -c '
import json, os, sys
entries_json, certificates_json = sys.stdin.read().split("\n\x1e\n")
entries = {e["hostname"]: e for e in json.loads(entries_json) if e.get("hostname")}
active = {c["name"] for c in json.loads(certificates_json) if c.get("managed", {}).get("state") == "ACTIVE"}
for host in os.environ["HOSTS"].split(","):
    wildcard = "*." + host.split(".", 1)[1]
    entry = entries[host] if host in entries else entries.get(wildcard, {})
    if entry.get("state") != "ACTIVE" or not any(c in active for c in entry.get("certificates", [])):
        print(host)
')"
  if [ -n "${gaps}" ]; then
    echo "${HTTPS_PROXY} uses certificate map ${certificate_map##*/}, which has no ACTIVE entry with an ACTIVE certificate for: ${gaps//$'\n'/, }" >&2
    echo "Add the domain to the certificate map in infra/ instead of creating a classic certificate." >&2
    exit 1
  fi
  echo "${HTTPS_PROXY} uses certificate map ${certificate_map##*/}; every hostname has an ACTIVE entry and certificate there."
  exit 0
fi

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
