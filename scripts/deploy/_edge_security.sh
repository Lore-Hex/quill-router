# shellcheck shell=bash
# Cloud Armor and trusted edge-identity reconciliation for global external
# Application Load Balancer backends. This file defines functions only; the
# caller owns authentication, PROJECT_ID, gc(), and log().

_edge_require_positive_integer() {
  local name="$1"
  local value="$2"
  case "$value" in
    ''|*[!0-9]*|0)
      echo "ERROR: ${name} must be a positive integer; got ${value:-<empty>}" >&2
      return 2
      ;;
  esac
}

_edge_require_log_sample_rate() {
  local value="$1"
  local fraction=""

  case "$value" in
    0|1|1.0) return 0 ;;
    0.*)
      fraction="${value#0.}"
      ;;
    *)
      echo "ERROR: TR_CLOUD_ARMOR_LOG_SAMPLE_RATE must be between 0 and 1" >&2
      return 2
      ;;
  esac
  case "$fraction" in
    ''|*[!0-9]*)
      echo "ERROR: TR_CLOUD_ARMOR_LOG_SAMPLE_RATE must be between 0 and 1" >&2
      return 2
      ;;
  esac
}

_edge_upsert_rule() {
  local policy="$1"
  local priority="$2"
  shift 2
  local preview="${TR_CLOUD_ARMOR_PREVIEW:-1}"

  case "$preview" in
    1|0) ;;
    *)
      echo "ERROR: TR_CLOUD_ARMOR_PREVIEW must be 0 or 1" >&2
      return 2
      ;;
  esac

  if gc compute security-policies rules describe "$priority" \
      --security-policy="$policy" >/dev/null 2>&1; then
    # Create treats absence of --preview as enforcement. Update needs the
    # explicit inverse or a previously-previewed rule would remain previewed.
    if [ "$preview" = "0" ]; then
      gc compute security-policies rules update "$priority" \
        --security-policy="$policy" \
        "$@" \
        --no-preview \
        --quiet >/dev/null
    else
      gc compute security-policies rules update "$priority" \
        --security-policy="$policy" \
        "$@" \
        --preview \
        --quiet >/dev/null
    fi
  else
    if [ "$preview" = "0" ]; then
      gc compute security-policies rules create "$priority" \
        --security-policy="$policy" \
        "$@" \
        --quiet >/dev/null
    else
      gc compute security-policies rules create "$priority" \
        --security-policy="$policy" \
        "$@" \
        --preview \
        --quiet >/dev/null
    fi
  fi
}

_reconcile_cloud_armor_policy() {
  local policy="$1"
  local interval="${TR_CLOUD_ARMOR_RATE_INTERVAL_SECONDS:-60}"
  local browser_count="${TR_CLOUD_ARMOR_BROWSER_RATE_COUNT:-120}"
  local write_count="${TR_CLOUD_ARMOR_WRITE_RATE_COUNT:-300}"
  local global_count="${TR_CLOUD_ARMOR_GLOBAL_RATE_COUNT:-2400}"
  local allowed_host_regex="${TR_EDGE_ALLOWED_HOST_REGEX:-^(trustedrouter[.]com|www[.]trustedrouter[.]com|status[.]trustedrouter[.]com|trust[.]trustedrouter[.]com|eu[.]trustedrouter[.]com|status-us[.]trustedrouter[.]com|status-eu[.]trustedrouter[.]com|allyrouter[.]com|www[.]allyrouter[.]com|status[.]allyrouter[.]com|trust[.]allyrouter[.]com|uptimerouter[.]com|www[.]uptimerouter[.]com|status[.]uptimerouter[.]com|trust[.]uptimerouter[.]com)(:[0-9]+)?$}"

  _edge_require_positive_integer TR_CLOUD_ARMOR_RATE_INTERVAL_SECONDS "$interval"
  _edge_require_positive_integer TR_CLOUD_ARMOR_BROWSER_RATE_COUNT "$browser_count"
  _edge_require_positive_integer TR_CLOUD_ARMOR_WRITE_RATE_COUNT "$write_count"
  _edge_require_positive_integer TR_CLOUD_ARMOR_GLOBAL_RATE_COUNT "$global_count"

  if ! gc compute security-policies describe "$policy" --global >/dev/null 2>&1; then
    log "creating Cloud Armor backend policy ${policy}"
    gc compute security-policies create "$policy" \
      --global \
      --type=CLOUD_ARMOR \
      --description="TrustedRouter edge controls; per-source ceiling enforced, tighter rules previewed" \
      --quiet >/dev/null
  fi

  # Repair the immutable catch-all contract on every deploy. Custom rules all
  # have a lower priority; a typo must never turn policy creation into an
  # accidental default deny.
  gc compute security-policies rules update 2147483647 \
    --security-policy="$policy" \
    --action=allow \
    --src-ip-ranges='*' \
    --no-preview \
    --description="Default allow; bounded route classes are evaluated first" \
    --quiet >/dev/null

  # Host ownership is a routing boundary, not a tuning signal.  Keep it
  # enforced even while the narrower traffic-shape rules are in preview: an
  # allowed TLS SNI with an attacker-chosen Host must never fall through to an
  # unrelated/legacy URL-map default.
  TR_CLOUD_ARMOR_PREVIEW=0 _edge_upsert_rule "$policy" 900 \
    --action=deny-403 \
    --expression="!has(request.headers['host']) || !request.headers['host'].lower().matches('${allowed_host_regex}')" \
    --description="Reject hosts outside canonical and marketing aliases"

  _edge_upsert_rule "$policy" 1000 \
    --action=throttle \
    --expression="request.path.startsWith('/chat-proxy/')" \
    --description="Browser inference proxy per-client throttle" \
    --rate-limit-threshold-count="$browser_count" \
    --rate-limit-threshold-interval-sec="$interval" \
    --conform-action=allow \
    --exceed-action=deny-429 \
    --enforce-on-key=IP

  _edge_upsert_rule "$policy" 1100 \
    --action=throttle \
    --expression="request.method != 'GET' && request.method != 'HEAD' && request.method != 'OPTIONS'" \
    --description="State-changing request per-client throttle" \
    --rate-limit-threshold-count="$write_count" \
    --rate-limit-threshold-interval-sec="$interval" \
    --conform-action=allow \
    --exceed-action=deny-429 \
    --enforce-on-key=IP

  # Keep one generous all-path per-source ceiling enforced from the first
  # attach. The independent Cloud Run max-instance caps, not this IP-keyed
  # rule, bound a distributed botnet and the fleet-wide serverless bill. The
  # tighter browser/write rules can spend a canary period in preview, but
  # preview-only policy would leave health and every other cheap path entirely
  # unbounded at the edge during the exact launch window this policy protects.
  TR_CLOUD_ARMOR_PREVIEW=0 _edge_upsert_rule "$policy" 1200 \
    --action=throttle \
    --src-ip-ranges='*' \
    --description="All-path per-source safety ceiling" \
    --rate-limit-threshold-count="$global_count" \
    --rate-limit-threshold-interval-sec="$interval" \
    --conform-action=allow \
    --exceed-action=deny-429 \
    --enforce-on-key=IP
}

reconcile_edge_backend() {
  local backend="$1"
  local policy="$2"
  # Full request logging can turn the attack itself into a Cloud Logging bill.
  # Ten percent is enough for preview sizing; operators can temporarily raise
  # it during a bounded investigation.
  local log_sample_rate="${TR_CLOUD_ARMOR_LOG_SAMPLE_RATE:-0.1}"
  local backend_json=""
  local preserved_headers=""
  local final_json=""
  local attached_policy=""
  local header=""
  local header_args=()

  case "$backend" in
    ''|*[!a-zA-Z0-9_-]*)
      echo "ERROR: invalid load-balancer backend name: ${backend:-<empty>}" >&2
      return 2
      ;;
  esac
  case "$policy" in
    ''|*[!a-zA-Z0-9_-]*)
      echo "ERROR: invalid Cloud Armor policy name: ${policy:-<empty>}" >&2
      return 2
      ;;
  esac
  _edge_require_log_sample_rate "$log_sample_rate"

  if ! gc compute backend-services describe "$backend" --global >/dev/null 2>&1; then
    echo "ERROR: required public load-balancer backend ${backend} does not exist" >&2
    return 1
  fi

  _reconcile_cloud_armor_policy "$policy"
  backend_json="$(gc compute backend-services describe "$backend" \
    --global --format=json)"
  preserved_headers="$(printf '%s' "$backend_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
for header in data.get("customRequestHeaders", []) or []:
    name = str(header).split(":", 1)[0].strip().casefold()
    if name != "x-trustedrouter-client-ip":
        print(header)
')"
  while IFS= read -r header; do
    [ -n "$header" ] || continue
    header_args+=("--custom-request-header=${header}")
  done <<<"$preserved_headers"
  # Google adds this after receiving the client request and overwrites any
  # same-name client header case-insensitively. Never derive limiter identity
  # from X-Forwarded-For or a header that survives client input unchanged.
  header_args+=("--custom-request-header=X-TrustedRouter-Client-IP:{client_ip_address}")

  log "attaching ${policy} and trusted client identity to ${backend}"
  gc compute backend-services update "$backend" \
    --global \
    --security-policy="$policy" \
    --enable-logging \
    --logging-sample-rate="$log_sample_rate" \
    "${header_args[@]}" \
    --quiet >/dev/null

  attached_policy="$(gc compute backend-services describe "$backend" --global \
    --format='value(securityPolicy.basename())')"
  if [ "$attached_policy" != "$policy" ]; then
    echo "ERROR: ${backend} has Cloud Armor policy ${attached_policy:-<none>}, expected ${policy}" >&2
    return 1
  fi
  final_json="$(gc compute backend-services describe "$backend" --global \
    --format='json(customRequestHeaders,securityPolicy)')"
  if ! printf '%s' "$final_json" | python3 -c '
import json
import sys

data = json.load(sys.stdin)
matches = []
for header in data.get("customRequestHeaders", []) or []:
    name, separator, value = str(header).partition(":")
    if separator and name.strip().casefold() == "x-trustedrouter-client-ip":
        matches.append(value.strip())
if matches != ["{client_ip_address}"]:
    raise SystemExit(f"trusted client header drift: {matches!r}")
'; then
    echo "ERROR: ${backend} did not retain the trusted client-IP overwrite" >&2
    return 1
  fi
}

reconcile_edge_backend_mappings() {
  local mappings="$1"
  local pair=""
  local backend=""
  local policy=""
  local pairs=()

  [ -n "$mappings" ] || {
    echo "ERROR: TR_CLOUD_ARMOR_BACKEND_POLICIES cannot be empty" >&2
    return 2
  }
  IFS=',' read -ra pairs <<<"$mappings"
  for pair in "${pairs[@]}"; do
    case "$pair" in
      *=*) ;;
      *)
        echo "ERROR: edge mapping must be backend=policy: ${pair}" >&2
        return 2
        ;;
    esac
    backend="${pair%%=*}"
    policy="${pair#*=}"
    [ -n "$backend" ] && [ -n "$policy" ] || {
      echo "ERROR: edge mapping must have non-empty backend and policy: ${pair}" >&2
      return 2
    }
    reconcile_edge_backend "$backend" "$policy"
  done
}
