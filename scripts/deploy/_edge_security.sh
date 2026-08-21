# shellcheck shell=bash
# Cloud Armor and trusted edge-identity reconciliation for global external
# Application Load Balancer backends. This file defines functions only; the
# caller owns authentication, PROJECT_ID, gc(), and log().

_TR_EDGE_ALLOWED_HOST_REGEX="^(trustedrouter[.]com|www[.]trustedrouter[.]com|status[.]trustedrouter[.]com|trust[.]trustedrouter[.]com|eu[.]trustedrouter[.]com|status-us[.]trustedrouter[.]com|status-eu[.]trustedrouter[.]com|allyrouter[.]com|www[.]allyrouter[.]com|status[.]allyrouter[.]com|trust[.]allyrouter[.]com|uptimerouter[.]com|www[.]uptimerouter[.]com|status[.]uptimerouter[.]com|trust[.]uptimerouter[.]com)(:[0-9]+)?$"

# Pure, read-only postcondition verifier shared by reconciliation, staging, and
# promotion/rollback.  Passing JSON as an argument keeps the function usable by
# callers that already captured a provider response without making another
# cloud request.
verify_cloud_armor_policy_contract_json() {
  [ "$#" -eq 1 ] || {
    echo "ERROR: verify_cloud_armor_policy_contract_json expects POLICY_JSON" >&2
    return 2
  }
  python3 - "$_TR_EDGE_ALLOWED_HOST_REGEX" "$1" <<'PY'
import json
import sys

allowed, raw = sys.argv[1:]
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit("Cloud Armor policy response is not valid JSON") from None
if not isinstance(data, dict):
    raise SystemExit("Cloud Armor policy response is malformed")
if data.get("type") != "CLOUD_ARMOR":
    raise SystemExit("Cloud Armor policy type drifted")
if data.get("description") != (
    "TrustedRouter exact edge controls; host and all-path gates enforced, "
    "route-shape rules previewed"
):
    raise SystemExit("Cloud Armor policy description drifted")
for field in ("recaptchaOptionsConfig", "userIpRequestHeaders"):
    if data.get(field) not in (None, {}, []):
        raise SystemExit(f"Cloud Armor policy retains forbidden {field}")

raw_rules = data.get("rules")
if not isinstance(raw_rules, list) or any(not isinstance(item, dict) for item in raw_rules):
    raise SystemExit("Cloud Armor rule inventory is malformed")
try:
    rules = {int(item.get("priority")): item for item in raw_rules}
except (TypeError, ValueError):
    raise SystemExit("Cloud Armor rule priority is malformed") from None
host_expression = (
    "!has(request.headers['host']) || "
    f"!request.headers['host'].lower().matches('{allowed}')"
)
expected = {
    900: (
        "deny(403)", False, host_expression, None, None,
        "Reject hosts outside canonical and marketing aliases",
    ),
    1000: (
        "throttle", True, "request.path.startsWith('/chat-proxy/')", 120, None,
        "Browser inference proxy per-client throttle",
    ),
    1100: (
        "throttle",
        True,
        "request.method != 'GET' && request.method != 'HEAD' && request.method != 'OPTIONS'",
        300,
        None,
        "State-changing request per-client throttle",
    ),
    1200: (
        "throttle", False, None, 2400, ["*"],
        "All-path per-source safety ceiling",
    ),
    2147483647: (
        "allow", False, None, None, ["*"],
        "Default allow; bounded route classes are evaluated first",
    ),
}
if set(rules) != set(expected) or len(raw_rules) != len(expected):
    raise SystemExit("unexpected Cloud Armor priority set")
allowed_rule_fields = {
    "action",
    "description",
    "kind",
    "match",
    "preview",
    "priority",
    "rateLimitOptions",
}
for priority, (action, preview, expression, count, ranges, description) in expected.items():
    rule = rules[priority]
    if set(rule) - allowed_rule_fields:
        raise SystemExit(f"rule {priority} retains forbidden fields")
    if rule.get("action") != action or rule.get("preview") is not preview:
        raise SystemExit(f"rule {priority} action/preview differs")
    if rule.get("description") != description:
        raise SystemExit(f"rule {priority} description differs")
    match = rule.get("match") or {}
    if not isinstance(match, dict):
        raise SystemExit(f"rule {priority} match is malformed")
    if expression is not None:
        if set(match) != {"expr"}:
            raise SystemExit(f"rule {priority} retains match extras")
        expr = match.get("expr") or {}
        if not isinstance(expr, dict) or set(expr) != {"expression"} or expr.get("expression") != expression:
            raise SystemExit(f"rule {priority} expression differs")
    if ranges is not None:
        if set(match) != {"config", "versionedExpr"}:
            raise SystemExit(f"rule {priority} retains source-match extras")
        config = match.get("config") or {}
        if not isinstance(config, dict) or set(config) != {"srcIpRanges"} or config.get("srcIpRanges") != ranges:
            raise SystemExit(f"rule {priority} source ranges differ")
        if match.get("versionedExpr") != "SRC_IPS_V1":
            raise SystemExit(f"rule {priority} versioned source expression differs")
    options = rule.get("rateLimitOptions") or {}
    if count is None:
        if "rateLimitOptions" in rule:
            raise SystemExit(f"rule {priority} retains rate-limit options")
        continue
    if not isinstance(options, dict):
        raise SystemExit(f"rule {priority} rate-limit contract differs")
    threshold = options.get("rateLimitThreshold") or {}
    if not isinstance(threshold, dict) or (
        set(options) != {
            "conformAction",
            "enforceOnKey",
            "exceedAction",
            "rateLimitThreshold",
        }
        or set(threshold) != {"count", "intervalSec"}
        or int(threshold.get("count", -1)) != count
        or int(threshold.get("intervalSec", -1)) != 60
        or options.get("conformAction") != "allow"
        or options.get("exceedAction") != "deny(429)"
        or options.get("enforceOnKey") != "IP"
    ):
        raise SystemExit(f"rule {priority} rate-limit contract differs")
PY
}

_verify_edge_backend_security_contract_json() {
  [ "$#" -eq 2 ] || {
    echo "ERROR: internal edge backend verifier expects PUBLIC_FLAG BACKEND_JSON" >&2
    return 2
  }
  python3 - "$1" "$2" <<'PY'
import json
import sys

public_raw, raw = sys.argv[1:]
if public_raw not in {"0", "1"}:
    raise SystemExit("edge backend public flag is invalid")
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit("edge backend response is not valid JSON") from None
if not isinstance(data, dict):
    raise SystemExit("edge backend response is malformed")

expected_header = ["X-TrustedRouter-Client-IP:{client_ip_address}"]
if data.get("customRequestHeaders", []) != expected_header:
    raise SystemExit("backend request-header allowlist drifted")
if data.get("customResponseHeaders", []) not in (None, []):
    raise SystemExit("backend retains custom response headers")
if data.get("edgeSecurityPolicy") not in (None, ""):
    raise SystemExit("backend retains an unexpected edge security policy")
iap = data.get("iap") or {}
if not isinstance(iap, dict):
    raise SystemExit("backend IAP contract is malformed")
if iap:
    if iap.get("enabled") is not False:
        raise SystemExit("backend IAP contract is not disabled")
    if set(iap) - {"enabled", "oauth2ClientId"}:
        raise SystemExit("backend IAP retains an OAuth secret or unknown state")
    if iap.get("oauth2ClientId") not in (None, " "):
        raise SystemExit("backend IAP retains an OAuth client ID")

logging = data.get("logConfig") or {}
if not isinstance(logging, dict) or (
    set(logging) - {"enable", "sampleRate", "optionalMode", "optionalFields"}
    or logging.get("enable") is not True
    or float(logging.get("sampleRate", -1)) != 0.1
    or logging.get("optionalMode", "EXCLUDE_ALL_OPTIONAL") != "EXCLUDE_ALL_OPTIONAL"
    or logging.get("optionalFields", []) not in (None, [])
):
    raise SystemExit("backend logging contract drifted")

public = public_raw == "1"
if bool(data.get("enableCDN", False)) != public:
    raise SystemExit("backend CDN state drifted")
cdn_policy = data.get("cdnPolicy") or {}
if not isinstance(cdn_policy, dict):
    raise SystemExit("backend CDN policy is malformed")
if public:
    cache_key = cdn_policy.get("cacheKeyPolicy") or {}
    if not isinstance(cache_key, dict):
        raise SystemExit("public backend cache-key policy is malformed")
    allowed_cdn_fields = {
        "bypassCacheOnRequestHeaders",
        "cacheKeyPolicy",
        "cacheMode",
        "clientTtl",
        "defaultTtl",
        "maxTtl",
        "negativeCaching",
        "negativeCachingPolicy",
        "requestCoalescing",
        "serveWhileStale",
        "signedUrlCacheMaxAgeSec",
        "signedUrlKeyNames",
    }
    allowed_cache_key_fields = {
        "includeHost",
        "includeHttpHeaders",
        "includeNamedCookies",
        "includeProtocol",
        "includeQueryString",
        "queryStringBlacklist",
        "queryStringWhitelist",
    }
    if (
        set(cdn_policy) - allowed_cdn_fields
        or set(cache_key) - allowed_cache_key_fields
        or cdn_policy.get("cacheMode") != "USE_ORIGIN_HEADERS"
        or cdn_policy.get("negativeCaching", False) is not False
        or cdn_policy.get("negativeCachingPolicy", []) not in (None, [])
        or int(cdn_policy.get("serveWhileStale", -1)) != 600
        or cdn_policy.get("clientTtl") is not None
        or cdn_policy.get("defaultTtl") is not None
        or cdn_policy.get("maxTtl") is not None
        or cdn_policy.get("bypassCacheOnRequestHeaders", []) not in (None, [])
        or cdn_policy.get("requestCoalescing", True) is not True
        or cdn_policy.get("signedUrlCacheMaxAgeSec") is not None
        or cdn_policy.get("signedUrlKeyNames", []) not in (None, [])
        or cache_key.get("includeHost") is not True
        or cache_key.get("includeProtocol") is not True
        or cache_key.get("includeQueryString") is not True
        or cache_key.get("queryStringBlacklist", []) not in (None, [])
        or cache_key.get("queryStringWhitelist", []) not in (None, [])
        or cache_key.get("includeHttpHeaders", []) not in (None, [])
        or cache_key.get("includeNamedCookies", []) not in (None, [])
        or data.get("compressionMode") != "AUTOMATIC"
    ):
        raise SystemExit("public backend CDN/cache-key contract drifted")
elif cdn_policy:
    raise SystemExit("non-public backend retains a CDN policy")
PY
}

verify_edge_backend_contract_json() {
  [ "$#" -eq 8 ] || {
    echo "ERROR: verify_edge_backend_contract_json expects SURFACE BACKEND_JSON PROJECT REGIONS_CSV SERVICE NEG POLICY TIMEOUT" >&2
    return 2
  }
  local surface="$1"
  local backend_json="$2"
  local project="$3"
  local regions_csv="$4"
  local service="$5"
  local neg="$6"
  local policy="$7"
  local timeout="$8"
  local public_flag=0
  [ "$surface" = public ] && public_flag=1
  _verify_edge_backend_security_contract_json "$public_flag" "$backend_json" || return 1
  python3 - "$surface" "$backend_json" "$project" "$regions_csv" \
    "$service" "$neg" "$policy" "$timeout" <<'PY'
import json
import re
import sys

surface, raw, project, raw_regions, service, neg, policy, timeout = sys.argv[1:]
services = {
    "public": "trusted-router-public",
    "actions": "trusted-router-actions",
    "console": "trusted-router-console",
    "chat": "trusted-router-chat",
    "webhooks": "trusted-router-webhooks",
    "internal": "trusted-router-billing",
}
backends = {
    "public": "trusted-router-public-backend",
    "actions": "trusted-router-actions-backend",
    "console": "trusted-router-console-backend",
    "chat": "trusted-router-chat-backend",
    "webhooks": "trusted-router-webhooks-backend",
    "internal": "trusted-router-billing-backend",
}
if surface not in services or service != services[surface]:
    raise SystemExit("edge backend surface/service identity is noncanonical")
if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
    raise SystemExit("edge backend project identity is noncanonical")
if any(not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", item) for item in (neg, policy)):
    raise SystemExit("edge backend NEG or policy identity is noncanonical")
regions = raw_regions.split(",")
if not regions or len(regions) != len(set(regions)) or any(
    not re.fullmatch(r"[a-z]+-[a-z0-9]+[0-9]", region) for region in regions
):
    raise SystemExit("edge backend region inventory is noncanonical")
if not re.fullmatch(r"[1-9][0-9]{0,4}", timeout) or int(timeout) > 86400:
    raise SystemExit("edge backend timeout is noncanonical")
data = json.loads(raw)
name = str(data.get("name") or "").rstrip("/").rsplit("/", 1)[-1]
if name != backends[surface]:
    raise SystemExit("edge backend resource identity is noncanonical")

def resource_path(value: object) -> str:
    text = str(value or "").rstrip("/")
    match = re.search(
        r"(?:^|/)(projects/[^/]+/(?:regions/[^/]+/networkEndpointGroups|"
        r"global/securityPolicies)/[^/]+)$",
        text,
    )
    if not match:
        raise SystemExit(f"{name} has a noncanonical edge-resource reference: {text!r}")
    return match.group(1)

raw_backends = data.get("backends")
if not isinstance(raw_backends, list) or any(not isinstance(item, dict) for item in raw_backends):
    raise SystemExit(f"{name} has a malformed NEG inventory")
expected_groups = sorted(
    f"projects/{project}/regions/{region}/networkEndpointGroups/{neg}"
    for region in regions
)
actual_groups = sorted(resource_path(item.get("group")) for item in raw_backends)
if actual_groups != expected_groups or len(raw_backends) != len(expected_groups):
    raise SystemExit(f"{name} NEG membership is not the exact same-project regional inventory")
if data.get("loadBalancingScheme") != "EXTERNAL_MANAGED" or data.get("protocol") != "HTTP":
    raise SystemExit(f"{name} has the wrong load-balancer scheme/protocol")
if int(data.get("timeoutSec", 0)) != int(timeout):
    raise SystemExit(f"{name} timeout differs from the exact surface contract")
attached = resource_path(data.get("securityPolicy"))
if attached != f"projects/{project}/global/securityPolicies/{policy}":
    raise SystemExit(f"{name} has the wrong Cloud Armor policy")
PY
}

_reconcile_cloud_armor_policy() {
  local policy="$1"
  local allowed_host_regex="$_TR_EDGE_ALLOWED_HOST_REGEX"
  local policy_file=""
  local policy_json=""
  local setting=""

  # These values are a reviewed production contract, not tuning inputs. A
  # per-invocation override could silently weaken one backend while the other
  # five retained the audited policy.
  for setting in \
    TR_EDGE_ALLOWED_HOST_REGEX \
    TR_CLOUD_ARMOR_RATE_INTERVAL_SECONDS \
    TR_CLOUD_ARMOR_BROWSER_RATE_COUNT \
    TR_CLOUD_ARMOR_WRITE_RATE_COUNT \
    TR_CLOUD_ARMOR_GLOBAL_RATE_COUNT \
    TR_CLOUD_ARMOR_PREVIEW; do
    if [ -n "${!setting:-}" ]; then
      echo "ERROR: ${setting} is not an operator override; edit and review the exact edge contract" >&2
      return 2
    fi
  done

  if ! gc compute security-policies describe "$policy" --global >/dev/null 2>&1; then
    log "creating Cloud Armor backend policy ${policy}"
    gc compute security-policies create "$policy" \
      --global \
      --type=CLOUD_ARMOR \
      --description="TrustedRouter edge controls; per-source ceiling enforced, tighter rules previewed" \
      --quiet >/dev/null
  fi

  policy_file="$(mktemp "${TMPDIR:-/tmp}/tr-edge-policy-XXXXXX.json")"
  chmod 600 "$policy_file"
  if ! python3 - "$allowed_host_regex" "$policy_file" <<'PY'
import json
import sys
from pathlib import Path

allowed, destination = sys.argv[1:]
host_expression = (
    "!has(request.headers['host']) || "
    f"!request.headers['host'].lower().matches('{allowed}')"
)

def source_match():
    return {"config": {"srcIpRanges": ["*"]}, "versionedExpr": "SRC_IPS_V1"}

def rate_limit(count):
    return {
        "conformAction": "allow",
        "enforceOnKey": "IP",
        "exceedAction": "deny(429)",
        "rateLimitThreshold": {"count": count, "intervalSec": 60},
    }

policy = {
    "description": (
        "TrustedRouter exact edge controls; host and all-path gates enforced, "
        "route-shape rules previewed"
    ),
    "type": "CLOUD_ARMOR",
    "rules": [
        {
            "action": "deny(403)",
            "description": "Reject hosts outside canonical and marketing aliases",
            "match": {"expr": {"expression": host_expression}},
            "preview": False,
            "priority": 900,
        },
        {
            "action": "throttle",
            "description": "Browser inference proxy per-client throttle",
            "match": {"expr": {"expression": "request.path.startsWith('/chat-proxy/')"}},
            "preview": True,
            "priority": 1000,
            "rateLimitOptions": rate_limit(120),
        },
        {
            "action": "throttle",
            "description": "State-changing request per-client throttle",
            "match": {
                "expr": {
                    "expression": (
                        "request.method != 'GET' && request.method != 'HEAD' && "
                        "request.method != 'OPTIONS'"
                    )
                }
            },
            "preview": True,
            "priority": 1100,
            "rateLimitOptions": rate_limit(300),
        },
        {
            "action": "throttle",
            "description": "All-path per-source safety ceiling",
            "match": source_match(),
            "preview": False,
            "priority": 1200,
            "rateLimitOptions": rate_limit(2400),
        },
        {
            "action": "allow",
            "description": "Default allow; bounded route classes are evaluated first",
            "match": source_match(),
            "preview": False,
            "priority": 2147483647,
        },
    ],
}
Path(destination).write_text(json.dumps(policy, separators=(",", ":")) + "\n")
PY
  then
    rm -f "$policy_file"
    return 1
  fi

  # Import is a single policy replacement. It removes unknown priorities and
  # stale headerAction/redirect/WAF fields instead of trying to update an
  # attacker-controlled rule in place and accidentally retaining its extras.
  if ! gc compute security-policies import "$policy" \
      --file-name="$policy_file" \
      --file-format=json \
      --global \
      --quiet >/dev/null; then
    rm -f "$policy_file"
    return 1
  fi
  rm -f "$policy_file"

  policy_json="$(gc compute security-policies describe "$policy" \
    --global --format=json)" || return 1
  if ! verify_cloud_armor_policy_contract_json "$policy_json"; then
    echo "ERROR: ${policy} did not retain the exact Cloud Armor contract" >&2
    return 1
  fi
}

reconcile_edge_backend() {
  local backend="$1"
  local policy="$2"
  # Full request logging can turn the attack itself into a Cloud Logging bill.
  # The reviewed contract is always a ten-percent sample.
  local log_sample_rate="0.1"
  local final_json=""
  local attached_policy=""
  local public_backend="${TR_PUBLIC_BACKEND:-trusted-router-public-backend}"

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
  if [ -n "${TR_CLOUD_ARMOR_LOG_SAMPLE_RATE:-}" ]; then
    echo "ERROR: TR_CLOUD_ARMOR_LOG_SAMPLE_RATE is fixed at 0.1 for production reconciliation" >&2
    return 2
  fi

  if ! gc compute backend-services describe "$backend" --global >/dev/null 2>&1; then
    echo "ERROR: required public load-balancer backend ${backend} does not exist" >&2
    return 1
  fi

  _reconcile_cloud_armor_policy "$policy"

  log "clearing stale edge state and attaching ${policy} to ${backend}"
  # Clear response/request injection before installing the sole trusted header.
  # The IAP single-space values are the documented API sentinel for clearing a
  # retained OAuth client while leaving IAP disabled.
  gc compute backend-services update "$backend" \
    --global \
    --security-policy="$policy" \
    --edge-security-policy="" \
    '--iap=disabled,oauth2-client-id= ,oauth2-client-secret= ' \
    --no-custom-request-headers \
    --no-custom-response-headers \
    --enable-logging \
    --logging-sample-rate="$log_sample_rate" \
    --quiet >/dev/null

  # Google adds this value after receiving the client request. Replacing the
  # whole request-header list prevents an old Authorization/internal-token
  # injection from surviving a service split.
  if [ "$backend" = "$public_backend" ]; then
    gc compute backend-services update "$backend" \
      --global \
      --custom-request-header='X-TrustedRouter-Client-IP:{client_ip_address}' \
      --enable-cdn \
      --cache-mode=USE_ORIGIN_HEADERS \
      --cache-key-include-host \
      --cache-key-include-protocol \
      --cache-key-include-query-string \
      --cache-key-query-string-blacklist= \
      --cache-key-include-http-header= \
      --cache-key-include-named-cookie= \
      --no-bypass-cache-on-request-headers \
      --no-negative-caching-policies \
      --serve-while-stale=600 \
      --request-coalescing \
      --no-client-ttl \
      --no-default-ttl \
      --no-max-ttl \
      --compression-mode=AUTOMATIC \
      --quiet >/dev/null
    gc compute backend-services update "$backend" \
      --global \
      --no-negative-caching \
      --quiet >/dev/null
  else
    gc compute backend-services update "$backend" \
      --global \
      --custom-request-header='X-TrustedRouter-Client-IP:{client_ip_address}' \
      --no-enable-cdn \
      --quiet >/dev/null
  fi

  attached_policy="$(gc compute backend-services describe "$backend" --global \
    --format='value(securityPolicy.basename())')"
  if [ "$attached_policy" != "$policy" ]; then
    echo "ERROR: ${backend} has Cloud Armor policy ${attached_policy:-<none>}, expected ${policy}" >&2
    return 1
  fi
  final_json="$(gc compute backend-services describe "$backend" --global --format=json)"
  if ! _verify_edge_backend_security_contract_json \
      "$([ "$backend" = "$public_backend" ] && echo 1 || echo 0)" "$final_json"; then
    echo "ERROR: ${backend} did not retain the exact edge backend contract" >&2
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
