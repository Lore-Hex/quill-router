#!/usr/bin/env bash
# Phase 4: parallel Cloud Run rollout across every TR_REGIONS entry, then
# attach a Serverless NEG per region to the global LB backend service so
# trustedrouter.com routes to the nearest healthy region. Finally ensures
# the HTTP -> HTTPS redirect on :80.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/deploy/_lib.sh
source "${SCRIPT_DIR}/_lib.sh"

TRUST_SOURCE_COMMIT=""
TRUST_IMAGE_REFERENCE=""
TRUST_IMAGE_DIGEST=""
TRUST_JSON=""
if [ -f "$TRUST_FILE" ]; then
  TRUST_JSON="$(cat "$TRUST_FILE")"
elif [ -n "${TRUST_FILE_URL:-}" ]; then
  TRUST_JSON="$(curl -fsSL --max-time 10 "$TRUST_FILE_URL" 2>/dev/null || true)"
fi
if [ -n "$TRUST_JSON" ]; then
  TRUST_SOURCE_COMMIT="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("source_commit", ""))' <<<"$TRUST_JSON")"
  TRUST_IMAGE_REFERENCE="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("image_reference", ""))' <<<"$TRUST_JSON")"
  TRUST_IMAGE_DIGEST="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("image_digest", ""))' <<<"$TRUST_JSON")"
fi

SECRET_ENVS=(
  "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
  "TR_STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:latest"
  "TR_STRIPE_WEBHOOK_SECRET=trustedrouter-stripe-webhook-secret:latest"
  "TR_INTERNAL_GATEWAY_TOKEN=trustedrouter-internal-gateway-token:latest"
)
add_secret_env_if_exists() {
  local env_name="$1"
  local secret_name="$2"
  if gc secrets describe "$secret_name" >/dev/null 2>&1; then
    SECRET_ENVS+=("${env_name}=${secret_name}:latest")
  fi
}
add_secret_env_if_exists "ANTHROPIC_API_KEY" "trustedrouter-anthropic-api-key"
add_secret_env_if_exists "OPENAI_API_KEY" "trustedrouter-openai-api-key"
add_secret_env_if_exists "GEMINI_API_KEY" "trustedrouter-gemini-api-key"
add_secret_env_if_exists "CEREBRAS_API_KEY" "trustedrouter-cerebras-api-key"
add_secret_env_if_exists "DEEPSEEK_API_KEY" "trustedrouter-deepseek-api-key"
add_secret_env_if_exists "MISTRAL_API_KEY" "trustedrouter-mistral-api-key"
add_secret_env_if_exists "KIMI_API_KEY" "trustedrouter-kimi-api-key"
add_secret_env_if_exists "ZAI_API_KEY" "trustedrouter-zai-api-key"
add_secret_env_if_exists "TOGETHER_API_KEY" "trustedrouter-together-api-key"
add_secret_env_if_exists "FIREWORKS_API_KEY" "trustedrouter-fireworks-api-key"
add_secret_env_if_exists "DEEPINFRA_API_KEY" "trustedrouter-deepinfra-api-key"
# 2026-05 — six new backends.
add_secret_env_if_exists "GROK_API_KEY" "trustedrouter-grok-api-key"
add_secret_env_if_exists "NOVITA_API_KEY" "trustedrouter-novita-api-key"
add_secret_env_if_exists "PHALA_API_KEY" "trustedrouter-phala-api-key"
add_secret_env_if_exists "SILICON_FLOW_API_KEY" "trustedrouter-siliconflow-api-key"
add_secret_env_if_exists "TINFOIL_API_KEY" "trustedrouter-tinfoil-api-key"
add_secret_env_if_exists "VENICE_API_KEY" "trustedrouter-venice-api-key"
add_secret_env_if_exists "NEBIUS_API_KEY" "trustedrouter-nebius-api-key"
add_secret_env_if_exists "MINIMAX_API_KEY" "trustedrouter-minimax-api-key"
add_secret_env_if_exists "BASETEN_API_KEY" "trustedrouter-baseten-api-key"
add_secret_env_if_exists "WAFER_API_KEY" "trustedrouter-wafer-api-key"
add_secret_env_if_exists "CRUSOE_API_KEY" "trustedrouter-crusoe-api-key"
add_secret_env_if_exists "MAKORA_API_KEY" "trustedrouter-makora-api-key"
add_secret_env_if_exists "ALIBABA_API_KEY" "trustedrouter-alibaba-api-key"
add_secret_env_if_exists "TR_SYNTHETIC_MONITOR_API_KEY" "trustedrouter-synthetic-monitor-api-key"
add_secret_env_if_exists "TR_GOOGLE_CLIENT_ID" "trustedrouter-google-client-id"
add_secret_env_if_exists "TR_GOOGLE_CLIENT_SECRET" "trustedrouter-google-client-secret"
add_secret_env_if_exists "TR_GITHUB_CLIENT_ID" "trustedrouter-github-client-id"
add_secret_env_if_exists "TR_GITHUB_CLIENT_SECRET" "trustedrouter-github-client-secret"
# SES email credentials only; not used for AWS hosting or failover.
add_secret_env_if_exists "TR_AWS_ACCESS_KEY_ID" "trustedrouter-aws-access-key-id"
add_secret_env_if_exists "TR_AWS_SECRET_ACCESS_KEY" "trustedrouter-aws-secret-access-key"
add_secret_env_if_exists "TR_PAYPAL_CLIENT_ID" "trustedrouter-paypal-client-id"
add_secret_env_if_exists "TR_PAYPAL_CLIENT_SECRET" "trustedrouter-paypal-client-secret"
add_secret_env_if_exists "TR_PAYPAL_WEBHOOK_ID" "trustedrouter-paypal-webhook-id"
add_secret_env_if_exists "AXIOM_API_TOKEN" "trustedrouter-axiom-api-token"
add_secret_env_if_exists "TR_ATHENA_WORKER_PROMPT" "trustedrouter-athena-worker-prompt-v1"
UPDATE_SECRETS="$(IFS=,; echo "${SECRET_ENVS[*]}")"

ENV_VARS=(
  "TR_ENVIRONMENT=production"
  "TR_RELEASE=$(git rev-parse --short HEAD 2>/dev/null || echo local)"
  "TR_ENABLE_LIVE_PROVIDERS=false"
  "TR_API_BASE_URL=https://api.trustedrouter.com/v1"
  "TR_TRUSTED_DOMAIN=trustedrouter.com"
  "TR_STORAGE_BACKEND=spanner-bigtable"
  "TR_GCP_PROJECT_ID=${PROJECT_ID}"
  "TR_SPANNER_INSTANCE_ID=${SPANNER_INSTANCE_ID}"
  "TR_SPANNER_DATABASE_ID=${SPANNER_DATABASE_ID}"
  "TR_BIGTABLE_INSTANCE_ID=${BIGTABLE_INSTANCE_ID}"
  "TR_BIGTABLE_GENERATION_TABLE=${BIGTABLE_GENERATION_TABLE}"
  "TR_BYOK_KMS_KEY_NAME=${BYOK_KMS_KEY_NAME}"
  "TR_REGIONS=${TR_REGIONS}"
  "TR_PRIMARY_REGION=${TR_PRIMARY_REGION}"
  "VERTEX_PROJECT_ID=${PROJECT_ID}"
  "VERTEX_LOCATION=${REGION}"
  "TR_TRUST_GCP_SOURCE_COMMIT=${TRUST_SOURCE_COMMIT}"
  "TR_TRUST_GCP_IMAGE_REFERENCE=${TRUST_IMAGE_REFERENCE}"
  "TR_TRUST_GCP_IMAGE_DIGEST=${TRUST_IMAGE_DIGEST}"
  "TR_GOOGLE_OAUTH_REDIRECT_URL=https://trustedrouter.com/google_oauth_callback"
  "TR_GITHUB_OAUTH_REDIRECT_URL=https://trustedrouter.com/github_oauth_callback"
  "TR_SIWE_DOMAIN=trustedrouter.com"
  "TR_AWS_REGION=us-east-1" # SES region only; hosted compute is GCP-only.
  "TR_SES_FROM_EMAIL=noreply@trustedrouter.com"
  "TR_SES_FROM_NAME=TrustedRouter"
  # Axiom log shipping. Token comes from Secret Manager via the
  # add_secret_env_if_exists block above; dataset name is plain config.
  # Empty AXIOM_API_TOKEN at runtime → handler is not registered (graceful no-op).
  "TR_AXIOM_DATASET=trusted-router-logs"
  "TR_AXIOM_URL=https://eu-central-1.aws.edge.axiom.co"
  # Billing typed-counter migration cutover, step 2 (docs/design/
  # billing-typed-counters.md): dual-write the hot credit/api_key counters to
  # the typed Spanner tables. Enforcement stays on the JSON path; this only
  # turns on the exact-mirror so backfill + the drift comparator can verify the
  # typed tables before the enforcement cohort flag. The typed tables already
  # exist (migrate_typed_counters.sh). Remove to revert the dual-write.
  "TR_TYPED_COUNTER_MIRROR=1"
  # Billing typed-counter migration cutover — ENFORCEMENT. A workspace's gateway
  # authorize/settle goes through the typed conditional-DML path (the deadlock
  # fix) when this gate passes. Settle/refund route by reservation ORIGIN, so
  # straddling requests stay matched. The denylist env
  # (TR_TYPED_BILLING_WORKSPACE_DENYLIST) ALWAYS wins → per-workspace emergency
  # kill switch (revert one workspace to legacy without a code change).
  #
  # 2026-06-25: REVERTED from the universal "*" default back to the validated
  # cohort. The "*" flip exposed a latent bug — the one-way JSON->typed exact
  # mirror copied reserved/total_usage, which the typed DML owns; any JSON write
  # on a typed workspace clobbered the in-flight hold -> "typed finalize failed:
  # release row-count != 1". The mirror itself is fixed by the ownership-split PR
  # (#79: mirror only JSON-owned columns). BUT THE MIRROR FIX ALONE DOES NOT MAKE
  # "*" SAFE. After the split, backfill() no longer seeds typed reserved/usage,
  # so flipping a not-yet-typed workspace would leave typed.reserved blind to its
  # open holds -> SILENT OVERSPEND (no row-count error). Re-enabling "*" requires
  # the separate ledger-derived flip reconciliation + fail-closed seed
  # verification (the "Step 6" safe-cutover effort).
  #
  # 2026-06-26: Step 6 LANDED + prod-validated — ownership-split mirror (#79),
  # reconcile_for_flip seed (#80), typed-aware reads (#81), workspace billing-pause
  # (#82), invariant auditor (#83/#84), reserved-clobber repair (#86). The live
  # cohort was repaired; the CANARY (cohort + 5 seeded never-typed) deployed,
  # converged across all regions, and the auditor stayed CLEAN through
  # pause→flip→unpause with the typed path healthy (synthetic reserving normally,
  # bounded open holds, zero "release row-count" errors).
  #
  # UNIVERSAL FLIP: every remaining workspace was reconcile_for_flip-seeded while
  # paused (typed total_usage=JSON, reserved=0; auditor CLEAN) before this "*" went
  # in; they stay paused through the full cross-region rollout, then unpause. The
  # latent bug that broke the FIRST "*" is fixed at the root (ownership-split mirror
  # no longer clobbers typed-owned columns) and every workspace is seeded, so
  # typed.reserved is never blind to its open holds. Denylist still wins for an
  # emergency per-workspace revert.
  "TR_TYPED_BILLING_WORKSPACE_IDS=*"
)
SET_ENV_VARS="$(IFS='|'; echo "^|^${ENV_VARS[*]}")"

prune_failed_revisions() {
  # `gcloud run deploy --no-traffic` waits for the LATEST revision on the
  # service to be Ready before returning success. If a previous deploy
  # left a NotReady revision (container failed to start, OOM during
  # startup probe, missing env, etc.) AND the latest revision is that
  # NotReady one, the new deploy gets misreported as failed even when
  # it successfully created a fresh revision behind the latest tag.
  #
  # Caught the hard way during the 2026-05-10 cutover: paypal.py was
  # uncommitted, an earlier deploy created revision 00131-zkk (Failed),
  # and every subsequent deploy returned "Revision 00131-zkk is not
  # ready" instead of failing-clean — leaving the operator to
  # manually `update-traffic` to the actually-healthy fresh revision.
  #
  # Fix: before deploying, find revisions whose Ready condition is
  # neither True nor pending and which currently have no traffic
  # routed (so they're safe to delete) and remove them. Idempotent;
  # no-op when everything is healthy.
  local target="$1"
  local serving
  serving=$(gc run services describe "$SERVICE" --region "$target" \
    --format='value(status.traffic[].revisionName)' 2>/dev/null \
    | tr ';' ' ')
  local failed_revs
  failed_revs=$(gc run revisions list --service "$SERVICE" --region "$target" \
    --format='value(metadata.name,status.conditions[0].status)' 2>/dev/null \
    | awk '$2 == "False" { print $1 }')
  for rev in $failed_revs; do
    # Skip if this NotReady revision is somehow still in the traffic
    # split — better to leave it and let the operator decide than risk
    # hitting a revision we deleted while live.
    case " $serving " in
      *" $rev "*) continue ;;
    esac
    log "  pruning failed revision ${rev} in ${target}"
    gc run revisions delete "$rev" --region "$target" --quiet >/dev/null 2>&1 \
      || log "  WARN: failed to prune ${rev}; will let gcloud's deploy step error if it cares"
  done
}

is_warm_region() {
  # Returns 0 if $1 is in TR_WARM_REGIONS, 1 otherwise. Cold regions
  # (not in TR_WARM_REGIONS) deploy with --min-instances=0 so they don't
  # pay for always-on capacity at idle. They serve local users with a
  # ~5-10s cold-start tax on the first request, but ~$0/mo when idle.
  local r="$1"
  case ",${TR_WARM_REGIONS}," in
    *",${r},"*) return 0 ;;
    *) return 1 ;;
  esac
}

deploy_one_region() {
  local target="$1"
  local logfile="${2:-/dev/null}"
  # When TR_DEPLOY_NO_TRAFFIC=1 is set (the staged-traffic flow in the
  # GHA workflow), the new revision is created with 0% traffic. The
  # workflow then ramps it up via `gcloud run services update-traffic`
  # in 10% / 50% / 100% stages with synthetic checks between, so a bug
  # that breaks the new revision under real load is caught while most
  # traffic is still on the old revision.
  local traffic_arg=""
  if [ "${TR_DEPLOY_NO_TRAFFIC:-0}" = "1" ]; then
    traffic_arg="--no-traffic"
    log "deploying Cloud Run service ${SERVICE} to ${target} with --no-traffic (staged shift to follow)"
  else
    log "deploying Cloud Run service ${SERVICE} to ${target}"
  fi
  prune_failed_revisions "$target" >>"$logfile" 2>&1 || true
  # Cold regions (not in TR_WARM_REGIONS) scale to zero. The first request
  # pays a ~5-10s cold-start tax; subsequent requests within the
  # keep-warm window are fast. Explicit override via
  # TR_CLOUD_RUN_MIN_INSTANCES wins for either kind.
  local min_instances="${TR_CLOUD_RUN_MIN_INSTANCES:-}"
  if [ -z "$min_instances" ]; then
    if is_warm_region "$target"; then
      min_instances=1
    else
      min_instances=0
    fi
  fi
  # /chat and /synth stream through /chat-proxy/v1 for browser CORS.
  # Synth can legitimately take several model calls before final output, so
  # match the proxy's 300s upstream read timeout unless explicitly overridden.
  if gc run deploy "$SERVICE" \
      --region "$target" \
      --image "$IMAGE" \
      --allow-unauthenticated \
      --port 8080 \
      --memory "${TR_CLOUD_RUN_MEMORY:-1Gi}" \
      --concurrency "${TR_CLOUD_RUN_CONCURRENCY:-2}" \
      --min-instances "$min_instances" \
      --timeout "${TR_CLOUD_RUN_TIMEOUT_SECONDS:-300}" \
      --set-env-vars "$SET_ENV_VARS" \
      --update-secrets "$UPDATE_SECRETS" \
      ${traffic_arg} \
      --quiet >>"$logfile" 2>&1; then
    log "deploy succeeded: ${target}"
    return 0
  fi
  log "deploy FAILED: ${target} (see ${logfile})"
  return 1
}

# Fan deploys out in parallel across every TR_REGIONS entry. Each
# region's gcloud invocation runs in its own subshell so a slow image
# pull in one cold region doesn't block the warm regions. Cloud Run scales to zero in
# unused regions so the bill stays the same as a single-region deploy
# at idle.
log_dir="$(mktemp -d "${TMPDIR:-/tmp}/tr-deploy-XXXXXX")"
log "parallel deploy logs in ${log_dir}"

if ! gc artifacts docker images describe "$IMAGE" >/dev/null 2>&1; then
  echo "ERROR: image ${IMAGE} does not exist. Run scripts/deploy/image.sh before rollout." >&2
  exit 1
fi

DEPLOY_TARGET_REGIONS="${TR_DEPLOY_TARGET_REGIONS:-$TR_REGIONS}"
IFS=',' read -ra _REGION_LIST <<<"$DEPLOY_TARGET_REGIONS"
TARGETS=()
for r in "${_REGION_LIST[@]}"; do
  [ -n "$r" ] && TARGETS+=("$r")
done
if [ "${TR_DEPLOY_ALL_REGIONS:-1}" != "1" ]; then
  TARGETS=("$REGION")
fi

# Full set of control-plane regions that SHOULD be in the LB (independent of
# what this deploy run targets). The detach-stale-NEG step below compares
# attached regions against this — NOT against TARGETS — so a narrow-target
# deploy (e.g. TR_DEPLOY_TARGET_REGIONS=us-central1) doesn't accidentally rip
# cold public-site regions out of the LB.
#
# Lost ~30s of trustedrouter.com 504s on 2026-05-10 from exactly this:
# a cold-region-only deploy detached all three warm-region NEGs from
# trusted-router-control-backend because the original loop compared
# against TARGETS (the cold subset) instead of the full control-plane region
# set. TR_REGIONS remains the attested API region set exposed to SDK callers.
IFS=',' read -ra _ALL_REGION_LIST <<<"$TR_CONTROL_PLANE_REGIONS"
ALL_REGIONS=()
for r in "${_ALL_REGION_LIST[@]}"; do
  [ -n "$r" ] && ALL_REGIONS+=("$r")
done

REGION_PIDS=()
REGION_LOGS=()
for fanout_region in "${TARGETS[@]}"; do
  region_log="${log_dir}/${fanout_region}.log"
  REGION_LOGS+=("$region_log")
  deploy_one_region "$fanout_region" "$region_log" &
  REGION_PIDS+=("$!")
done

deploy_failed=0
for idx in "${!TARGETS[@]}"; do
  fanout_region="${TARGETS[$idx]}"
  pid="${REGION_PIDS[$idx]}"
  if ! wait "$pid"; then
    deploy_failed=1
    log "deploy log for failed region ${fanout_region}:"
    tail -20 "${REGION_LOGS[$idx]}" >&2 || true
  fi
done

if [ "$deploy_failed" -ne 0 ]; then
  echo "ERROR: at least one region's deploy failed; see logs in ${log_dir}" >&2
  exit 1
fi

latest_ready_revision_for_region() {
  local target="$1"
  # `status.latestReadyRevisionName` can lag behind when a deploy reuses a
  # mutable tag or when we immediately force a nonce-only redeploy. Prefer the
  # newest revision from the revision list whose Ready condition is True.
  gc run revisions list --service "$SERVICE" --region "$target" \
    --limit=10 \
    --sort-by='~metadata.creationTimestamp' \
    --format='value(metadata.name,status.conditions[0].status)' 2>/dev/null \
    | awk '$2 == "True" { print $1; exit }'
}

if [ "${TR_DEPLOY_NO_TRAFFIC:-0}" != "1" ]; then
  # Defense against a real 2026-06-05 rollout bug: Cloud Run created Ready
  # revisions in us-east4/europe-west4, but service traffic remained pinned
  # to older revisions, so prod served a mixed catalog. Make the intended
  # no-staging path explicit: after every successful regional deploy, route
  # 100% to the newest Ready revision in that region.
  for traffic_region in "${TARGETS[@]}"; do
    latest_rev="$(latest_ready_revision_for_region "$traffic_region")"
    if [ -z "$latest_rev" ]; then
      echo "ERROR: could not find newest Ready revision for ${traffic_region}" >&2
      exit 1
    fi
    log "routing ${traffic_region} traffic to newest Ready revision ${latest_rev}"
    gc run services update-traffic "$SERVICE" \
      --region="$traffic_region" \
      --to-revisions="${latest_rev}=100" \
      --quiet >/dev/null
  done
fi

log "Cloud Run URLs:"
for url_region in "${TARGETS[@]}"; do
  url="$(gc run services describe "$SERVICE" --region "$url_region" --format='value(status.url)' 2>/dev/null || true)"
  if [ -n "$url" ]; then
    printf '  %-28s %s\n' "$url_region" "$url"
  fi
done

# ---------------------------------------------------------------------------
# Attach a Serverless NEG per region to the global LB backend service so
# trustedrouter.com routes to the nearest healthy region instead of always
# us-central1. The backend service was created out-of-band when the LB
# was first set up; this block discovers + reuses its existing config so
# we don't have to know LB topology upfront.
#
# Idempotent: skip-if-exists on every step (NEG create, backend add).
# Safe to re-run on every deploy.
# ---------------------------------------------------------------------------
LB_BACKEND_SERVICE="${LB_BACKEND_SERVICE:-trusted-router-control-backend}"
LB_NEG_NAME="${LB_NEG_NAME:-trusted-router-control-neg}"

attach_region_to_lb() {
  local target="$1"
  if ! gc compute network-endpoint-groups describe "$LB_NEG_NAME" \
      --region "$target" >/dev/null 2>&1; then
    log "creating Serverless NEG ${LB_NEG_NAME} in ${target}"
    gc compute network-endpoint-groups create "$LB_NEG_NAME" \
      --region "$target" \
      --network-endpoint-type=serverless \
      --cloud-run-service="$SERVICE" \
      --quiet >/dev/null
  fi

  local already_attached
  already_attached="$(gc compute backend-services describe "$LB_BACKEND_SERVICE" \
    --global --format='value(backends[].group)' 2>/dev/null \
    | tr ';' '\n' \
    | grep -c "/regions/${target}/networkEndpointGroups/${LB_NEG_NAME}\$" || true)"
  if [ "$already_attached" = "0" ]; then
    log "attaching NEG ${LB_NEG_NAME} (${target}) to ${LB_BACKEND_SERVICE}"
    gc compute backend-services add-backend "$LB_BACKEND_SERVICE" \
      --global \
      --network-endpoint-group="$LB_NEG_NAME" \
      --network-endpoint-group-region="$target" \
      --quiet >/dev/null
  fi
}

if gc compute backend-services describe "$LB_BACKEND_SERVICE" --global >/dev/null 2>&1; then
  log "wiring Serverless NEGs to ${LB_BACKEND_SERVICE}"
  # Attach every control-plane region, not just this deploy's TARGETS, so the
  # LB always reflects the full intended public-site region set. Idempotent:
  # attach_region_to_lb no-ops on regions that are already attached. Without
  # this, a narrow-target deploy could leave a Cloud Run region outside LB
  # rotation.
  for fanout_region in "${ALL_REGIONS[@]}"; do
    attach_region_to_lb "$fanout_region" || log "WARN: NEG attach failed for ${fanout_region}"
  done
  existing_backend_regions="$(gc compute backend-services describe "$LB_BACKEND_SERVICE" \
    --global --format='value(backends[].group)' 2>/dev/null \
    | tr ';' '\n' \
    | sed -n 's#.*regions/\([^/]*\)/networkEndpointGroups/.*#\1#p' \
    | sort -u)"
  for attached_region in $existing_backend_regions; do
    # Compare against ALL_REGIONS (= TR_CONTROL_PLANE_REGIONS), not TARGETS.
    # TARGETS is just this deploy run's subset; detaching anything outside of
    # it would rip cold regions out of the LB when running a warm-only or
    # narrow-target deploy. We only want to detach regions that fell out of
    # TR_CONTROL_PLANE_REGIONS entirely.
    keep_region=0
    for full_region in "${ALL_REGIONS[@]}"; do
      if [ "$attached_region" = "$full_region" ]; then
        keep_region=1
        break
      fi
    done
    if [ "$keep_region" = "0" ]; then
      log "detaching stale NEG ${LB_NEG_NAME} (${attached_region}) from ${LB_BACKEND_SERVICE}"
      gc compute backend-services remove-backend "$LB_BACKEND_SERVICE" \
        --global \
        --network-endpoint-group="$LB_NEG_NAME" \
        --network-endpoint-group-region="$attached_region" \
        --quiet >/dev/null || log "WARN: stale NEG detach failed for ${attached_region}"
    fi
  done
else
  log "WARN: ${LB_BACKEND_SERVICE} not found; skipping NEG wiring"
fi

# ---------------------------------------------------------------------------
# HTTP -> HTTPS redirect on the public load balancer
# ---------------------------------------------------------------------------
# The HTTPS forwarding rule was created out-of-band when the LB was first
# set up. We add a parallel :80 stack here that 301-redirects every HTTP
# request to HTTPS, so visitors who type `http://trustedrouter.com` don't
# get a connection-reset blank page. Three resources, all idempotent:
# skip-if-exists guards make it safe to re-run on every deploy.
LB_HTTP_URL_MAP="${LB_HTTP_URL_MAP:-trusted-router-control-http-redirect}"
LB_HTTP_PROXY="${LB_HTTP_PROXY:-trusted-router-control-http-proxy}"
LB_HTTP_FORWARDING_RULE="${LB_HTTP_FORWARDING_RULE:-trusted-router-control-http}"
LB_HTTPS_FORWARDING_RULE="${LB_HTTPS_FORWARDING_RULE:-trusted-router-control-https}"

ensure_http_redirect_lb() {
  if ! gc compute url-maps describe "$LB_HTTP_URL_MAP" --global >/dev/null 2>&1; then
    log "creating HTTP-redirect URL map ${LB_HTTP_URL_MAP}"
    gc compute url-maps import "$LB_HTTP_URL_MAP" --global \
      --source=/dev/stdin --quiet <<YAML
name: ${LB_HTTP_URL_MAP}
defaultUrlRedirect:
  httpsRedirect: true
  redirectResponseCode: MOVED_PERMANENTLY_DEFAULT
  stripQuery: false
YAML
  fi

  if ! gc compute target-http-proxies describe "$LB_HTTP_PROXY" --global >/dev/null 2>&1; then
    log "creating HTTP target proxy ${LB_HTTP_PROXY}"
    gc compute target-http-proxies create "$LB_HTTP_PROXY" \
      --url-map="$LB_HTTP_URL_MAP" --global --quiet
  fi

  if ! gc compute forwarding-rules describe "$LB_HTTP_FORWARDING_RULE" --global >/dev/null 2>&1; then
    local lb_ip
    lb_ip="$(gc compute forwarding-rules describe "$LB_HTTPS_FORWARDING_RULE" \
      --global --format='value(IPAddress)' 2>/dev/null || true)"
    if [ -z "$lb_ip" ]; then
      log "WARN: HTTPS forwarding rule ${LB_HTTPS_FORWARDING_RULE} not found; skipping HTTP rule"
      return 0
    fi
    log "creating HTTP forwarding rule ${LB_HTTP_FORWARDING_RULE} on ${lb_ip}:80"
    gc compute forwarding-rules create "$LB_HTTP_FORWARDING_RULE" \
      --address="$lb_ip" \
      --target-http-proxy="$LB_HTTP_PROXY" \
      --ports=80 --global --quiet
  fi
}

ensure_http_redirect_lb
