#!/usr/bin/env bash
# Cloudflare Load Balancer for api.quillrouter.com — Stage 4f / AWS
# Phase 5 of the multi-region expansion plan.
#
# Wires a Cloudflare LB with two pools so 99% of inference traffic
# stays on GCP and 1% trickles to the AWS NLB. The 1% trickle keeps
# the AWS path warmed under real production load: when GCP fails
# health checks, Cloudflare drops it from rotation and 100% goes to
# AWS within 30-60s. Steady-state cost: ~$15/mo (Cloudflare LB +
# DNS plan).
#
# Auth: API token + account ID read from ~/.quill_cloud_keys.private
# via the same read_key_file_var helper the rest of the deploy uses.
# Token must have:
#   - Zone:Read + Zone:DNS:Edit on quillrouter.com
#   - Account:Load Balancers:Edit on the target account
#
# Idempotent: every Cloudflare resource is checked-then-created. Pool
# weights, monitor settings, and DNS records get refreshed on each
# apply. Re-running is safe.
#
# Usage:
#   bash scripts/deploy/cloudflare_lb.sh                   # dry-run all
#   bash scripts/deploy/cloudflare_lb.sh --apply           # apply
#
# Phases (run in order, all-or-nothing):
#   1. Verify the API token has the right scopes.
#   2. Resolve the zone ID for ZONE_DOMAIN.
#   3. Create/update the HTTPS health-check monitor on /healthz.
#   4. Create/update the GCP origin pool (weight 99 in the LB).
#   5. Create/update the AWS origin pool (weight 1 in the LB).
#   6. Create/update the LB at api.${ZONE_DOMAIN} with both pools.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

ZONE_DOMAIN="${ZONE_DOMAIN:-quillrouter.com}"
LB_HOSTNAME="${LB_HOSTNAME:-api.${ZONE_DOMAIN}}"

# Per-pool origins. The plan calls for "GCP global LB IP" but the
# enclave layer uses regional forwarding rules (no global anycast
# yet); we use the us-central1 enclave LB IP as the canonical GCP
# origin until a global enclave NEG-fronted backend exists. Once
# that lands, swap GCP_ORIGIN_IP for the global IP and Cloudflare
# stops needing to know about regions.
GCP_ORIGIN_IP="${GCP_ORIGIN_IP:-34.61.11.3}"           # us-central1 enclave LB
GCP_ORIGIN_LABEL="${GCP_ORIGIN_LABEL:-gcp-us-central1}"
AWS_ORIGIN_HOST="${AWS_ORIGIN_HOST:-quill-enclave-nlb-df6a5999caabf334.elb.us-west-2.amazonaws.com}"
AWS_ORIGIN_LABEL="${AWS_ORIGIN_LABEL:-aws-us-west-2}"

GCP_POOL_NAME="${GCP_POOL_NAME:-quill-gcp-pool}"
AWS_POOL_NAME="${AWS_POOL_NAME:-quill-aws-pool}"
MONITOR_NAME="${MONITOR_NAME:-quill-https-healthz}"

# Health check probes /healthz expecting 200. Both GCP enclave and
# AWS NLB → enclave terminate TLS in the enclave, so we hit them
# over HTTPS. expectedCodes=200 catches "TLS handshakes but doesn't
# serve real HTTP" — that's exactly the failure mode AWS Phase 5 is
# guarding against.
# Cloudflare LB plan tiers gate the minimum monitor interval. Free
# Standard LB allows 60s+; lower-tier accounts surface this as a
# cryptic 'interval is not in range [1, 1]: validation failed'.
MONITOR_INTERVAL_SECS="${MONITOR_INTERVAL_SECS:-60}"
MONITOR_TIMEOUT_SECS="${MONITOR_TIMEOUT_SECS:-10}"
MONITOR_RETRIES="${MONITOR_RETRIES:-2}"
MONITOR_PATH="${MONITOR_PATH:-/healthz}"

# Pool weights inside the LB. Plan: 99/1 to keep AWS continuously
# warmed under ~1% of real load.
GCP_WEIGHT="${GCP_WEIGHT:-0.99}"
AWS_WEIGHT="${AWS_WEIGHT:-0.01}"

DRY_RUN=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) DRY_RUN=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Read API token + account ID from the keyfile. Try the common
# variable names so the operator can use whichever convention they
# already had. Both Cloudflare itself (CLOUDFLARE_*) and Terraform
# (CF_*) use these names interchangeably.
CF_API_TOKEN="$(read_key_file_var CLOUDFLARE_API_TOKEN CF_API_TOKEN)"
CF_ACCOUNT_ID="$(read_key_file_var CLOUDFLARE_ACCOUNT_ID CF_ACCOUNT_ID)"
if [ -z "$CF_API_TOKEN" ] || [ -z "$CF_ACCOUNT_ID" ]; then
  echo "FATAL: Cloudflare credentials missing from $KEY_FILE" >&2
  echo "       expected one of: CLOUDFLARE_API_TOKEN / CF_API_TOKEN" >&2
  echo "       expected one of: CLOUDFLARE_ACCOUNT_ID / CF_ACCOUNT_ID" >&2
  exit 1
fi

CF_API="https://api.cloudflare.com/client/v4"

cf() {
  # Wrapper that runs a Cloudflare API call. Reads from stdin when
  # the caller passes -d @-. All responses are JSON; the caller is
  # responsible for parsing what it needs.
  local method="$1"; shift
  local path="$1"; shift
  curl -sS -X "$method" "${CF_API}${path}" \
    -H "Authorization: Bearer ${CF_API_TOKEN}" \
    -H "Content-Type: application/json" \
    "$@"
}

cf_apply() {
  if [ $DRY_RUN -eq 1 ]; then
    log "  [dry-run] cf $*"
    echo '{"result":{"id":"DRY_RUN_FAKE_ID"},"success":true}'
    return 0
  fi
  cf "$@"
}

require_success() {
  local context="$1"
  local resp="$2"
  if echo "$resp" | python3 -c "import json,sys; r=json.load(sys.stdin); sys.exit(0 if r.get('success') else 1)" 2>/dev/null; then
    return 0
  fi
  echo "ERROR: $context — Cloudflare API returned failure" >&2
  echo "$resp" | python3 -c "import json,sys; r=json.load(sys.stdin); print(r.get('errors'))" >&2 || echo "$resp" >&2
  return 1
}

extract_field() {
  # Pull a JSON path out of stdin. Returns empty string on missing
  # key so callers can chain without explicit jq.
  python3 -c "import json,sys; r=json.load(sys.stdin); v=r$1; print(v if v is not None else '')"
}

log "Cloudflare deploy: zone=$ZONE_DOMAIN lb_host=$LB_HOSTNAME"
log "Mode: $([ $DRY_RUN -eq 1 ] && echo DRY-RUN || echo APPLY)"

# ─── Phase 1: verify token has the right scopes ─────────────────────────
log "=== verify API token ==="
# Cloudflare ships two token kinds today:
#   - User-owned API Tokens (no prefix, ~40 chars) — created under
#     My Profile → API Tokens. Verified at /user/tokens/verify.
#   - Account-owned API Tokens (cfat_ prefix, ~53 chars) — created
#     under Account → API Tokens. Verified at /accounts/{id}/tokens/verify.
# Try the account-owned path first (it's the newer, recommended kind),
# fall back to user-owned for legacy tokens.
verify=$(cf GET "/accounts/${CF_ACCOUNT_ID}/tokens/verify")
if ! echo "$verify" | python3 -c "import json,sys; r=json.load(sys.stdin); sys.exit(0 if r.get('success') else 1)" 2>/dev/null; then
  log "  not an account-owned token; trying user-owned token verify"
  verify=$(cf GET "/user/tokens/verify")
fi
require_success "token verify" "$verify"
token_status=$(echo "$verify" | extract_field "['result']['status']")
log "  token status: $token_status"
if [ "$token_status" != "active" ]; then
  log "  ERROR: token is not active"
  exit 1
fi

# ─── Phase 2: resolve zone ID ───────────────────────────────────────────
log "=== resolve zone ID for $ZONE_DOMAIN ==="
zone_resp=$(cf GET "/zones?name=${ZONE_DOMAIN}")
require_success "zone lookup" "$zone_resp"
ZONE_ID=$(echo "$zone_resp" | extract_field "['result'][0]['id']")
if [ -z "$ZONE_ID" ]; then
  log "  ERROR: zone $ZONE_DOMAIN not found in this account"
  log "  zones visible to this token:"
  cf GET "/zones?per_page=50" | python3 -c "
import json, sys
r = json.load(sys.stdin)
for z in r.get('result', []):
    print(f\"    - {z['name']} (id={z['id']})\")"
  exit 1
fi
log "  zone id: $ZONE_ID"

# ─── Phase 3: create-or-update health-check monitor ─────────────────────
log "=== monitor: $MONITOR_NAME ==="
monitor_body=$(cat <<EOF
{
  "type": "https",
  "description": "$MONITOR_NAME",
  "method": "GET",
  "path": "$MONITOR_PATH",
  "interval": $MONITOR_INTERVAL_SECS,
  "timeout": $MONITOR_TIMEOUT_SECS,
  "retries": $MONITOR_RETRIES,
  "expected_codes": "200,401",
  "follow_redirects": false,
  "allow_insecure": true,
  "header": {
    "Host": ["$LB_HOSTNAME"],
    "User-Agent": ["Cloudflare-LB-HealthCheck/quill"]
  }
}
EOF
)

# allow_insecure=true: the GCP-side enclave LBs and the AWS NLB both
# present enclave-issued self-signed certs; without insecure, the
# health check fails with cert-verify errors. The Host header pins
# the SNI so the enclave returns the right cert variant.
#
# expected_codes "200,401": every route on the enclave gateway except
# /attestation requires an API key, so /healthz returns 401 with
# {"error":{"message":"Invalid API key","status":401}} when probed
# anonymously. That 401 still proves TLS terminated successfully and
# the gateway request handler ran — the same "401-with-Invalid-API-key
# = healthy" treatment we use in synthetic_monitor's tls_health_probe
# (src/trusted_router/synthetic/probes.py:118). Marking 401 as healthy
# means we don't have to wire a separate Cloudflare-only API key
# through Secret Manager just for the LB monitor.

existing_monitors=$(cf GET "/accounts/${CF_ACCOUNT_ID}/load_balancers/monitors")
require_success "list monitors" "$existing_monitors"
MONITOR_ID=$(echo "$existing_monitors" | python3 -c "
import json, sys
r = json.load(sys.stdin)
for m in r.get('result', []):
    if m.get('description') == '$MONITOR_NAME':
        print(m['id'])
        break
")

if [ -n "$MONITOR_ID" ]; then
  log "  monitor exists ($MONITOR_ID); updating"
  resp=$(cf_apply PUT "/accounts/${CF_ACCOUNT_ID}/load_balancers/monitors/${MONITOR_ID}" -d "$monitor_body")
else
  log "  creating monitor"
  resp=$(cf_apply POST "/accounts/${CF_ACCOUNT_ID}/load_balancers/monitors" -d "$monitor_body")
  MONITOR_ID=$(echo "$resp" | extract_field "['result']['id']")
fi
require_success "monitor upsert" "$resp"
log "  monitor id: $MONITOR_ID"

# ─── Phase 4 + 5: pools (GCP + AWS) ─────────────────────────────────────
upsert_pool() {
  local pool_name="$1"
  local origin_label="$2"
  local origin_address="$3"
  local pool_description="$4"

  log "=== pool: $pool_name ==="
  local pool_body
  pool_body=$(cat <<EOF
{
  "name": "$pool_name",
  "description": "$pool_description",
  "enabled": true,
  "minimum_origins": 1,
  "monitor": "$MONITOR_ID",
  "origins": [
    {"name": "$origin_label", "address": "$origin_address", "enabled": true, "weight": 1}
  ],
  "notification_email": ""
}
EOF
)

  local existing pool_id
  existing=$(cf GET "/accounts/${CF_ACCOUNT_ID}/load_balancers/pools")
  require_success "list pools" "$existing"
  pool_id=$(echo "$existing" | python3 -c "
import json, sys
r = json.load(sys.stdin)
for p in r.get('result', []):
    if p.get('name') == '$pool_name':
        print(p['id'])
        break
")

  if [ -n "$pool_id" ]; then
    log "  pool exists ($pool_id); updating"
    resp=$(cf_apply PUT "/accounts/${CF_ACCOUNT_ID}/load_balancers/pools/${pool_id}" -d "$pool_body")
  else
    log "  creating pool"
    resp=$(cf_apply POST "/accounts/${CF_ACCOUNT_ID}/load_balancers/pools" -d "$pool_body")
    pool_id=$(echo "$resp" | extract_field "['result']['id']")
  fi
  require_success "pool upsert ($pool_name)" "$resp"
  echo "$pool_id"
}

GCP_POOL_ID=$(upsert_pool "$GCP_POOL_NAME" "$GCP_ORIGIN_LABEL" "$GCP_ORIGIN_IP" \
  "Quill GCP enclaves (Cloud Run + Confidential Space VMs)")
log "  GCP pool id: $GCP_POOL_ID"

AWS_POOL_ID=$(upsert_pool "$AWS_POOL_NAME" "$AWS_ORIGIN_LABEL" "$AWS_ORIGIN_HOST" \
  "Quill AWS enclaves (Nitro on m5.xlarge in us-west-2)")
log "  AWS pool id: $AWS_POOL_ID"

# ─── Phase 6: load balancer at api.quillrouter.com ──────────────────────
log "=== load balancer: $LB_HOSTNAME ==="
lb_body=$(cat <<EOF
{
  "name": "$LB_HOSTNAME",
  "description": "Quill multi-cloud failover (99% GCP / 1% AWS warm-trickle)",
  "ttl": 60,
  "default_pools": ["$GCP_POOL_ID", "$AWS_POOL_ID"],
  "fallback_pool": "$GCP_POOL_ID",
  "proxied": false,
  "steering_policy": "random",
  "random_steering": {
    "default_weight": $GCP_WEIGHT,
    "pool_weights": {
      "$GCP_POOL_ID": $GCP_WEIGHT,
      "$AWS_POOL_ID": $AWS_WEIGHT
    }
  }
}
EOF
)

# steering_policy=random with random_steering: per the Cloudflare
# docs, this yields a weighted distribution across pools. Each
# request rolls a die against the weights — over thousands of
# requests, ~99% land on GCP and ~1% on AWS. Combined with the
# health-check, an unhealthy GCP pool drops to 0 weight and 100%
# goes to AWS automatically.
#
# proxied=false: this is a passthrough record so the enclave's TLS
# terminator handles the handshake. Cloudflare's edge can't proxy
# traffic when TLS terminates in the origin (the cert wouldn't
# match Cloudflare's edge SNI).
#
# ttl=60: low so DNS-vendor failover (the secondary Cloud DNS
# story) propagates within ~1 minute.

existing_lbs=$(cf GET "/zones/${ZONE_ID}/load_balancers")
require_success "list LBs" "$existing_lbs"
LB_ID=$(echo "$existing_lbs" | python3 -c "
import json, sys
r = json.load(sys.stdin)
for lb in r.get('result', []):
    if lb.get('name') == '$LB_HOSTNAME':
        print(lb['id'])
        break
")

if [ -n "$LB_ID" ]; then
  log "  LB exists ($LB_ID); updating"
  resp=$(cf_apply PUT "/zones/${ZONE_ID}/load_balancers/${LB_ID}" -d "$lb_body")
else
  log "  creating LB"
  resp=$(cf_apply POST "/zones/${ZONE_ID}/load_balancers" -d "$lb_body")
  LB_ID=$(echo "$resp" | extract_field "['result']['id']")
fi
require_success "LB upsert" "$resp"
log "  LB id: $LB_ID"

# ─── Summary ────────────────────────────────────────────────────────────
log ""
log "Cloudflare LB ready."
log "  api.quillrouter.com → 99% $GCP_ORIGIN_IP (GCP enclave us-central1)"
log "                       → 1%  $AWS_ORIGIN_HOST (AWS NLB us-west-2)"
log "  Health check: HTTPS GET $MONITOR_PATH every ${MONITOR_INTERVAL_SECS}s"
log ""
log "Verify resolution propagated (60s TTL):"
log "  dig +short api.quillrouter.com @1.1.1.1"
log ""
log "Watch real-time pool health:"
log "  curl -sS -H \"Authorization: Bearer \$CF_API_TOKEN\" \\"
log "    \"$CF_API/accounts/${CF_ACCOUNT_ID}/load_balancers/pools/${GCP_POOL_ID}/health\""
log ""
log "Next: Cloud DNS as secondary authoritative (DNS-vendor redundancy)"
log "  — separate phase, see plan section 4f."
