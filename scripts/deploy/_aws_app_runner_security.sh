# shellcheck shell=bash
# App Runner capacity and health reconciliation. Functions only; callers
# provide aws(), log(), and REGION.

reconcile_app_runner_capacity() {
  local account="$1"
  local name="$2"
  local max_concurrency="$3"
  local min_size="$4"
  local max_size="$5"
  local region="${REGION:?REGION is required}"
  local value_name=""
  local value=""
  local base_arn=""
  local current=""
  local current_concurrency=""
  local current_min=""
  local current_max=""
  local current_arn=""
  local current_extra=""
  local result_arn=""
  local describe_error=""
  local describe_missing=0

  for value_name in max_concurrency min_size max_size; do
    case "$value_name" in
      max_concurrency) value="$max_concurrency" ;;
      min_size) value="$min_size" ;;
      max_size) value="$max_size" ;;
    esac
    case "$value" in
      ''|*[!0-9]*|0)
        echo "ERROR: App Runner ${value_name} must be a positive integer" >&2
        return 2
        ;;
    esac
  done
  [ "$max_concurrency" -le 200 ] \
    || { echo "ERROR: App Runner max concurrency must be <= 200" >&2; return 2; }
  [ "$min_size" -le 25 ] \
    || { echo "ERROR: App Runner min size must be <= 25" >&2; return 2; }
  [ "$max_size" -le 25 ] && [ "$max_size" -ge "$min_size" ] \
    || { echo "ERROR: App Runner max size must be between min size and 25" >&2; return 2; }
  case "$account" in
    *[!0-9]*|'') echo "ERROR: invalid AWS account id" >&2; return 2 ;;
  esac
  [ "${#account}" -eq 12 ] || { echo "ERROR: invalid AWS account id" >&2; return 2; }
  case "$name" in
    ''|*[!a-zA-Z0-9_-]*)
      echo "ERROR: invalid App Runner auto-scaling configuration name" >&2
      return 2
      ;;
  esac
  case "$name" in
    [a-zA-Z0-9]*) ;;
    *) echo "ERROR: invalid App Runner auto-scaling configuration name" >&2; return 2 ;;
  esac
  case "$name" in
    *[a-zA-Z0-9]) ;;
    *) echo "ERROR: invalid App Runner auto-scaling configuration name" >&2; return 2 ;;
  esac
  [ "${#name}" -ge 4 ] && [ "${#name}" -le 32 ] \
    || { echo "ERROR: App Runner auto-scaling name must be 4-32 characters" >&2; return 2; }

  base_arn="arn:aws:apprunner:${region}:${account}:autoscalingconfiguration/${name}"
  describe_error="$(mktemp "${TMPDIR:-/tmp}/tr-app-runner-autoscaling.XXXXXX")"
  if ! current="$(aws apprunner describe-auto-scaling-configuration \
      --region "$region" \
      --auto-scaling-configuration-arn "$base_arn" \
      --query '[AutoScalingConfiguration.MaxConcurrency,AutoScalingConfiguration.MinSize,AutoScalingConfiguration.MaxSize,AutoScalingConfiguration.AutoScalingConfigurationArn]' \
      --output text 2>"$describe_error")"; then
    if grep -q 'ResourceNotFoundException' "$describe_error"; then
      current=""
      describe_missing=1
    else
      cat "$describe_error" >&2
      rm -f "$describe_error"
      echo "ERROR: could not inspect App Runner auto-scaling configuration" >&2
      return 1
    fi
  fi
  rm -f "$describe_error"
  if [ "$describe_missing" = "0" ]; then
    if [ -z "$current" ] || [ "$current" = "None" ]; then
      echo "ERROR: App Runner auto-scaling inspection returned no configuration" >&2
      return 1
    fi
    IFS=$'\t' read -r current_concurrency current_min current_max current_arn current_extra \
      <<<"$current" || true
    case "$current_concurrency:$current_min:$current_max" in
      *[!0-9:]*|:*|*::*|*:)
        echo "ERROR: App Runner auto-scaling inspection returned malformed capacity" >&2
        return 1
        ;;
    esac
    [[ "$current_arn" == "${base_arn}/"* ]] || {
      echo "ERROR: App Runner auto-scaling inspection returned an unexpected ARN" >&2
      return 1
    }
    [ -z "$current_extra" ] || {
      echo "ERROR: App Runner auto-scaling inspection returned extra fields" >&2
      return 1
    }
  fi
  if [ "$current_concurrency" = "$max_concurrency" ] \
      && [ "$current_min" = "$min_size" ] \
      && [ "$current_max" = "$max_size" ] \
      && [[ "$current_arn" == arn:aws:apprunner:* ]]; then
    log "reusing bounded App Runner auto-scaling revision ${current_arn}"
    printf '%s\n' "$current_arn"
    return 0
  fi

  result_arn="$(aws apprunner create-auto-scaling-configuration \
    --region "$region" \
    --auto-scaling-configuration-name "$name" \
    --max-concurrency "$max_concurrency" \
    --min-size "$min_size" \
    --max-size "$max_size" \
    --query 'AutoScalingConfiguration.AutoScalingConfigurationArn' \
    --output text)"
  [[ "$result_arn" == "${base_arn}/"* ]] || {
    echo "ERROR: failed to create bounded App Runner auto-scaling configuration" >&2
    return 1
  }
  printf '%s\n' "$result_arn"
}

verify_app_runner_capacity_and_health() {
  local service_arn="$1"
  local expected_scaling_arn="$2"
  local region="${REGION:?REGION is required}"
  local actual_scaling_arn=""
  local health_protocol=""

  actual_scaling_arn="$(aws apprunner describe-service \
    --region "$region" \
    --service-arn "$service_arn" \
    --query 'Service.AutoScalingConfigurationSummary.AutoScalingConfigurationArn' \
    --output text)"
  health_protocol="$(aws apprunner describe-service \
    --region "$region" \
    --service-arn "$service_arn" \
    --query 'Service.HealthCheckConfiguration.Protocol' \
    --output text)"
  if [ "$actual_scaling_arn" != "$expected_scaling_arn" ]; then
    echo "ERROR: App Runner uses ${actual_scaling_arn:-<none>}, expected ${expected_scaling_arn}" >&2
    return 1
  fi
  if [ "$health_protocol" != "TCP" ]; then
    echo "ERROR: App Runner health protocol is ${health_protocol:-<none>}, expected TCP" >&2
    return 1
  fi
}
