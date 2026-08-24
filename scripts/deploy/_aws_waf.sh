# shellcheck shell=bash
# AWS WAF reconciliation for App Runner. Functions only; callers provide aws,
# log(), REGION, and the App Runner service ARN after it reaches RUNNING.

_aws_waf_require_integer() {
  local name="$1"
  local value="$2"
  case "$value" in
    ''|*[!0-9]*|0)
      echo "ERROR: ${name} must be a positive integer; got ${value:-<empty>}" >&2
      return 2
      ;;
  esac
}

_aws_waf_require_rate_limit() {
  local name="$1"
  local value="$2"

  _aws_waf_require_integer "$name" "$value"
  if [ "$value" -lt 10 ] || [ "$value" -gt 2000000000 ]; then
    echo "ERROR: ${name} must be between 10 and 2000000000; got ${value}" >&2
    return 2
  fi
}

_aws_waf_rules_json() {
  local service_host="$1"
  local preview="${TR_AWS_WAF_PREVIEW:-1}"
  local high_rate_limit="${TR_AWS_WAF_BLOCK_RATE_LIMIT:-6000}"
  local write_rate_limit="${TR_AWS_WAF_WRITE_RATE_LIMIT:-600}"
  local window="${TR_AWS_WAF_EVALUATION_WINDOW_SECONDS:-300}"
  local allowed_hosts="${TR_AWS_WAF_ALLOWED_HOSTS:-aws.trustedrouter.com,${service_host}}"

  case "$preview" in
    0|1) ;;
    *)
      echo "ERROR: TR_AWS_WAF_PREVIEW must be 0 or 1" >&2
      return 2
      ;;
  esac
  _aws_waf_require_rate_limit TR_AWS_WAF_BLOCK_RATE_LIMIT "$high_rate_limit"
  _aws_waf_require_rate_limit TR_AWS_WAF_WRITE_RATE_LIMIT "$write_rate_limit"
  case "$window" in
    60|120|300|600) ;;
    *)
      echo "ERROR: TR_AWS_WAF_EVALUATION_WINDOW_SECONDS must be 60, 120, 300, or 600" >&2
      return 2
      ;;
  esac

  TR_AWS_WAF_PREVIEW="$preview" \
  TR_AWS_WAF_BLOCK_RATE_LIMIT="$high_rate_limit" \
  TR_AWS_WAF_WRITE_RATE_LIMIT="$write_rate_limit" \
  TR_AWS_WAF_EVALUATION_WINDOW_SECONDS="$window" \
  TR_AWS_WAF_ALLOWED_HOSTS="$allowed_hosts" \
    python3 - <<'PY'
import json
import os


preview = os.environ["TR_AWS_WAF_PREVIEW"] == "1"
window = int(os.environ["TR_AWS_WAF_EVALUATION_WINDOW_SECONDS"])


def visibility(metric: str) -> dict[str, object]:
    return {
        "SampledRequestsEnabled": True,
        "CloudWatchMetricsEnabled": True,
        "MetricName": metric,
    }


def byte_match(field: dict[str, object], value: str) -> dict[str, object]:
    return {
        "ByteMatchStatement": {
            "SearchString": value,
            "FieldToMatch": field,
            "TextTransformations": [{"Priority": 0, "Type": "LOWERCASE"}],
            "PositionalConstraint": "EXACTLY",
        }
    }


hosts = sorted(
    {
        item.strip().casefold()
        for item in os.environ["TR_AWS_WAF_ALLOWED_HOSTS"].split(",")
        if item.strip()
    }
)
if not hosts:
    raise SystemExit("TR_AWS_WAF_ALLOWED_HOSTS cannot be empty")
host_values = sorted({value for host in hosts for value in (host, f"{host}:443")})
host_matches = [byte_match({"SingleHeader": {"Name": "host"}}, host) for host in host_values]
host_statement = host_matches[0] if len(host_matches) == 1 else {"OrStatement": {"Statements": host_matches}}

methods = [
    byte_match({"Method": {}}, method)
    for method in ("post", "put", "patch", "delete")
]

rules = [
    {
        "Name": "AllowedHosts",
        "Priority": 0,
        "Statement": {"NotStatement": {"Statement": host_statement}},
        "Action": {"Count": {}} if preview else {"Block": {}},
        "VisibilityConfig": visibility("TrustedRouterAllowedHosts"),
    },
    {
        # This is deliberately enforced on first deploy. 20 requests/second
        # sustained from one address is far above a human/browser control-plane
        # workload, while still leaving a generous emergency ceiling.
        "Name": "HighRatePerIpBlock",
        "Priority": 10,
        "Statement": {
            "RateBasedStatement": {
                "Limit": int(os.environ["TR_AWS_WAF_BLOCK_RATE_LIMIT"]),
                "EvaluationWindowSec": window,
                "AggregateKeyType": "IP",
            }
        },
        "Action": {"Block": {}},
        "VisibilityConfig": visibility("TrustedRouterHighRatePerIpBlock"),
    },
    {
        "Name": "StateChangingRate",
        "Priority": 20,
        "Statement": {
            "RateBasedStatement": {
                "Limit": int(os.environ["TR_AWS_WAF_WRITE_RATE_LIMIT"]),
                "EvaluationWindowSec": window,
                "AggregateKeyType": "IP",
                "ScopeDownStatement": {"OrStatement": {"Statements": methods}},
            }
        },
        "Action": {"Count": {}} if preview else {"Block": {}},
        "VisibilityConfig": visibility("TrustedRouterStateChangingRate"),
    },
    {
        "Name": "AwsManagedCommon",
        "Priority": 100,
        "Statement": {
            "ManagedRuleGroupStatement": {
                "VendorName": "AWS",
                "Name": "AWSManagedRulesCommonRuleSet",
            }
        },
        "OverrideAction": {"Count": {}} if preview else {"None": {}},
        "VisibilityConfig": visibility("TrustedRouterAwsManagedCommon"),
    },
]
json.dump(rules, fp=os.sys.stdout, separators=(",", ":"))
PY
}

reconcile_app_runner_waf() {
  local service_arn="$1"
  local service_host="$2"
  local region="${REGION:?REGION is required}"
  local name="${TR_AWS_WAF_WEB_ACL_NAME:-trusted-router-app-runner-edge}"
  local description="TrustedRouter App Runner edge: enforced high ceiling, preview managed rules"
  local rules_json=""
  local summary=""
  local web_acl_id=""
  local web_acl_arn=""
  local current=""
  local lock_token=""
  local attempt=0

  case "$service_arn" in
    arn:aws:apprunner:*) ;;
    *) echo "ERROR: invalid App Runner service ARN: ${service_arn}" >&2; return 2 ;;
  esac
  case "$service_host" in
    ''|*[!a-zA-Z0-9.-]*|.*|*.)
      echo "ERROR: invalid App Runner service hostname: ${service_host:-<empty>}" >&2
      return 2
      ;;
  esac
  case "$name" in
    ''|*[!a-zA-Z0-9_-]*)
      echo "ERROR: invalid AWS WAF Web ACL name: ${name:-<empty>}" >&2
      return 2
      ;;
  esac
  rules_json="$(_aws_waf_rules_json "$service_host")"
  summary="$(aws wafv2 list-web-acls \
    --scope REGIONAL \
    --region "$region" \
    --query "WebACLs[?Name=='${name}'] | [0].[Id,ARN]" \
    --output text)"
  if [ -z "$summary" ] || [ "$summary" = "None" ]; then
    log "creating AWS WAF Web ACL ${name} in ${region}"
    summary="$(aws wafv2 create-web-acl \
      --name "$name" \
      --scope REGIONAL \
      --region "$region" \
      --description "$description" \
      --default-action '{"Allow":{}}' \
      --rules "$rules_json" \
      --visibility-config 'SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=TrustedRouterAppRunnerEdge' \
      --query 'Summary.[Id,ARN]' \
      --output text)"
  else
    web_acl_id="${summary%%[[:space:]]*}"
    current="$(aws wafv2 get-web-acl \
      --name "$name" \
      --scope REGIONAL \
      --id "$web_acl_id" \
      --region "$region" \
      --output json)"
    lock_token="$(printf '%s' "$current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["LockToken"])')"
    log "updating AWS WAF Web ACL ${name} in ${region}"
    aws wafv2 update-web-acl \
      --name "$name" \
      --scope REGIONAL \
      --id "$web_acl_id" \
      --region "$region" \
      --lock-token "$lock_token" \
      --description "$description" \
      --default-action '{"Allow":{}}' \
      --rules "$rules_json" \
      --visibility-config 'SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=TrustedRouterAppRunnerEdge' \
      >/dev/null
  fi
  web_acl_id="${summary%%[[:space:]]*}"
  web_acl_arn="${summary##*[[:space:]]}"
  [ -n "$web_acl_id" ] && [ -n "$web_acl_arn" ] && [ "$web_acl_arn" != "None" ] || {
    echo "ERROR: could not resolve AWS WAF Web ACL ${name}" >&2
    return 1
  }

  # WAF resources can take seconds to propagate. Retry only the association;
  # policy creation/update errors above remain hard deploy failures.
  while ! aws wafv2 associate-web-acl \
      --web-acl-arn "$web_acl_arn" \
      --resource-arn "$service_arn" \
      --region "$region" >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 6 ]; then
      echo "ERROR: failed to associate ${name} with ${service_arn}" >&2
      return 1
    fi
    sleep 5
  done
  verify_app_runner_waf "$service_arn" "$name" "$web_acl_id" "$web_acl_arn"
}

verify_app_runner_waf() {
  local service_arn="$1"
  local name="$2"
  local web_acl_id="$3"
  local expected_arn="$4"
  local region="${REGION:?REGION is required}"
  local attached_arn=""
  local policy_json=""

  attached_arn="$(aws wafv2 get-web-acl-for-resource \
    --resource-arn "$service_arn" \
    --region "$region" \
    --query 'WebACL.ARN' \
    --output text)"
  if [ "$attached_arn" != "$expected_arn" ]; then
    echo "ERROR: App Runner service has WAF ${attached_arn:-<none>}, expected ${expected_arn}" >&2
    return 1
  fi
  policy_json="$(aws wafv2 get-web-acl \
    --name "$name" \
    --scope REGIONAL \
    --id "$web_acl_id" \
    --region "$region" \
    --output json)"
  printf '%s' "$policy_json" | python3 -c '
import json
import sys

rules = {item["Name"]: item for item in json.load(sys.stdin)["WebACL"]["Rules"]}
high = rules.get("HighRatePerIpBlock", {})
if "Block" not in high.get("Action", {}):
    raise SystemExit("HighRatePerIpBlock is not enforcing BLOCK")
if high.get("Statement", {}).get("RateBasedStatement", {}).get("AggregateKeyType") != "IP":
    raise SystemExit("HighRatePerIpBlock is not keyed by source IP")
managed = rules.get("AwsManagedCommon", {})
if not managed.get("Statement", {}).get("ManagedRuleGroupStatement"):
    raise SystemExit("AWS managed common rule group is missing")
' || {
    echo "ERROR: AWS WAF ${name} failed post-apply verification" >&2
    return 1
  }
}
