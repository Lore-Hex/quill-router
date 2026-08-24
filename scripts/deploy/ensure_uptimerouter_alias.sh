#!/usr/bin/env bash
# Provision and attach UptimeRouter's control-plane certificate. The shared
# helper is parameterized even though its historical filename mentions Ally.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_NAME="${CERT_NAME:-uptimerouter-control-cert-v1}" \
DOMAINS="${DOMAINS:-uptimerouter.com,www.uptimerouter.com,status.uptimerouter.com,trust.uptimerouter.com}" \
  exec "${SCRIPT_DIR}/ensure_allyrouter_alias.sh"
