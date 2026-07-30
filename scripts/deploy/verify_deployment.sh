#!/usr/bin/env bash
# Self-test a TrustedRouter deployment on any cloud.
#
#   bash scripts/deploy/verify_deployment.sh [--expect-monitor] https://tr-canary.....azurecontainerapps.io
#
# Cloud-agnostic on purpose: the Azure canary and the AWS EU region run the same
# checks, so "it works on Azure" and "it works on AWS" mean the same thing. A
# per-cloud bespoke smoke test would let the two drift and would not be evidence
# of anything.
#
# COSTS NOTHING. Every check below is a free endpoint. Inference probes spend
# real provider money and are deliberately excluded — a deploy check that bills
# per run will get switched off.
#
# Exits non-zero on the first hard failure so it can gate a rollout.
set -uo pipefail

BASE=""
EXPECT_MONITOR=0
for arg in "$@"; do
  case "$arg" in
    --expect-monitor) EXPECT_MONITOR=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *)
      [ -z "$BASE" ] || {
        echo "usage: $0 [--expect-monitor] <base-url>" >&2
        exit 2
      }
      BASE="$arg"
      ;;
  esac
done
[ -n "$BASE" ] || {
  echo "usage: $0 [--expect-monitor] <base-url>" >&2
  exit 2
}
BASE="${BASE%/}"

pass=0; fail=0; warn=0
ok()   { printf '  PASS  %s\n' "$*"; pass=$((pass+1)); }
bad()  { printf '  FAIL  %s\n' "$*"; fail=$((fail+1)); }
soft() { printf '  WARN  %s\n' "$*"; warn=$((warn+1)); }

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$1" 2>/dev/null; }
body() { curl -s --max-time 20 "$1" 2>/dev/null; }

printf '\n=== verifying %s\n\n' "$BASE"

# 1. Liveness. If this fails nothing else is meaningful.
if [ "$(code "$BASE/health")" = "200" ]; then
  ok "/health responds 200"
else
  bad "/health did not return 200 — the container is not serving"
  echo; echo "  $pass passed, $fail failed, $warn warnings"; exit 1
fi

# 2. The app renders. Catches a booted process that cannot template or read
#    static assets — a class of failure /health cannot see.
[ "$(code "$BASE/")" = "200" ] && ok "/ renders" || bad "/ did not return 200"

# 3. Database round-trip. This is the check that actually proves the deployment
#    reached ITS OWN database: /status.json reads synthetic probe samples, so a
#    200 here means the store is wired, reachable and queryable. A deployment
#    whose DSN is wrong passes checks 1 and 2 and fails this one.
status_code="$(code "$BASE/status.json")"
if [ "$status_code" = "200" ]; then
  ok "/status.json responds 200 (database read path works)"
  status_payload="$(body "$BASE/status.json")"
  if printf '%s' "$status_payload" | grep -q '"overall_status"'; then
    ok "status payload is well-formed"
  else
    bad "status payload missing overall_status — served, but wrong shape"
  fi
  if [ "$EXPECT_MONITOR" -eq 1 ]; then
    monitor_result="$(
      printf '%s' "$status_payload" | python3 -c '
import datetime as dt
import json
import sys

MAX_AGE_SECONDS = 30 * 60


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError) as exc:
    print(f"invalid status JSON: {exc}")
    raise SystemExit(1)

data = payload.get("data", payload)
if not isinstance(data, dict):
    print("status payload has no data object")
    raise SystemExit(1)

now = dt.datetime.now(dt.timezone.utc)
candidates = []
freshness = data.get("monitor_freshness")
if isinstance(freshness, dict):
    latest = parse_time(freshness.get("latest_sample_at"))
    if latest is not None:
        candidates.append(latest)
    else:
        age = freshness.get("latest_sample_age_seconds")
        if isinstance(age, (int, float)) and not isinstance(age, bool) and age >= 0:
            candidates.append(now - dt.timedelta(seconds=age))

current = data.get("current")
checks = current.get("checks") if isinstance(current, dict) else None
if isinstance(checks, list):
    for check in checks:
        if isinstance(check, dict):
            created_at = parse_time(check.get("created_at"))
            if created_at is not None:
                candidates.append(created_at)

if not candidates:
    print("no synthetic sample timestamp found")
    raise SystemExit(1)

latest = max(candidates)
age_seconds = max((now - latest).total_seconds(), 0)
if age_seconds > MAX_AGE_SECONDS:
    print(f"newest sample is {int(age_seconds)}s old (limit {MAX_AGE_SECONDS}s)")
    raise SystemExit(1)

print(f"newest sample is {int(age_seconds)}s old")
'
    )"
    if [ "$?" -eq 0 ]; then
      ok "synthetic monitor is fresh ($monitor_result)"
    else
      bad "synthetic monitor is stale or missing — $monitor_result"
    fi
  fi
else
  bad "/status.json returned $status_code — store not reachable or not implemented"
fi

# 4. Auth is actually enforced. A deployment that serves inference endpoints
#    unauthenticated is worse than one that is down, so an unauthenticated call
#    MUST be rejected. 404 is acceptable: it means the route is not mounted here
#    at all, which is true for a control-plane-only deployment.
auth_code="$(code "$BASE/v1/chat/completions")"
case "$auth_code" in
  401|403) ok "unauthenticated /v1/chat/completions rejected ($auth_code)" ;;
  404|405) soft "/v1/chat/completions not served here ($auth_code) — control-plane-only deployment" ;;
  200)     bad "unauthenticated inference returned 200 — AUTH IS NOT ENFORCED" ;;
  *)       soft "unauthenticated /v1/chat/completions returned $auth_code" ;;
esac

# 5. TLS. Cheap to check, and a silent downgrade is the kind of thing nobody
#    notices until it is in a compliance questionnaire.
case "$BASE" in
  https://*) ok "served over TLS" ;;
  *)         bad "not HTTPS" ;;
esac

printf '\n  %s passed, %s failed, %s warnings\n\n' "$pass" "$fail" "$warn"
[ "$fail" -eq 0 ] || exit 1
