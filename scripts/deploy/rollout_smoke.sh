#!/usr/bin/env bash
# Repository-owned, side-effect-free API and Firefox smoke callback.

set -euo pipefail

usage() {
  echo "Usage: rollout_smoke.sh MANIFEST PHASE PERCENT" >&2
  exit 2
}

[ "$#" -eq 3 ] || usage
MANIFEST="$1"
PHASE="$2"
PERCENT="$3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
STATE_TOOL="${SCRIPT_DIR}/rollout_state.py"
AUTH_HEADER_FILE="${TR_ROLLOUT_SMOKE_AUTH_HEADER_FILE:-}"
STORAGE_STATE="${TR_ROLLOUT_SMOKE_PLAYWRIGHT_STORAGE_STATE:-}"

[ "${TR_ROLLOUT_SMOKE_PRODUCTION_APPROVED:-false}" = true ] || {
  echo "ERROR: production rollout smoke requires explicit approval" >&2
  exit 1
}
case "$PHASE" in
  preflight|initial-companions|initial-map|initial-console|primary|secondary) ;;
  *) echo "ERROR: rollout smoke phase is invalid" >&2; exit 2 ;;
esac
case "$PERCENT" in 0|10|50|100) ;;
  *) echo "ERROR: rollout smoke percent is invalid" >&2; exit 2 ;;
esac

python3 "$STATE_TOOL" validate-manifest "$MANIFEST"
python3 - "$AUTH_HEADER_FILE" "$STORAGE_STATE" <<'PY'
import json
import re
import stat
import sys
from pathlib import Path

header_path, storage_path = (Path(value) for value in sys.argv[1:])
for path in (header_path, storage_path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(f"smoke credential file must be a regular mode-0600 file: {path}")
header = header_path.read_text(encoding="utf-8")
if not re.fullmatch(r"Authorization: Bearer [^\x00-\x20\x7f]{16,8192}\n?", header):
    raise SystemExit("smoke authorization header file is malformed")
state = json.loads(storage_path.read_text(encoding="utf-8"))
if not isinstance(state, dict) or set(state) != {"cookies", "origins"}:
    raise SystemExit("Playwright storage state fields differ")
if not isinstance(state["cookies"], list) or not isinstance(state["origins"], list):
    raise SystemExit("Playwright storage state is malformed")
PY

DOMAINS="$(python3 - "$MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

domains = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("domains")
if not isinstance(domains, list) or not all(isinstance(item, str) for item in domains):
    raise SystemExit("manifest smoke domains are invalid")
print(",".join(domains))
PY
)" || exit 1
[ "$DOMAINS" = "trustedrouter.com,allyrouter.com,uptimerouter.com" ] || {
  echo "ERROR: rollout smoke domain inventory differs" >&2
  exit 1
}

SMOKE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/tr-rollout-smoke.XXXXXX")"
trap 'rm -rf "$SMOKE_TMP"' EXIT
PLAYWRIGHT_OUTPUT="${SMOKE_TMP}/playwright-results"
mkdir -m 700 "$PLAYWRIGHT_OUTPUT"

check_code() {
  local name="$1" expected="$2" url="$3"
  shift 3
  local code
  code="$(curl --disable --silent --show-error --max-time 20 --output "${SMOKE_TMP}/${name}" \
    --write-out '%{http_code}' "$@" "$url")" || {
    echo "ERROR: smoke request failed: ${name}" >&2
    return 1
  }
  [ "$code" = "$expected" ] || {
    echo "ERROR: ${name} returned ${code}, expected ${expected}" >&2
    return 1
  }
}

# Exercise the two high-risk split routes only through their pre-body auth
# gates. The deliberately non-actionable JSON remains invalid for the handler
# behind each gate, so even an unexpected routing/auth regression cannot turn
# this callback into inference, a credit reservation, or payment work.
check_auth_rejection() {
  local name="$1" url="$2" expected_message="$3"
  local request_id="tr-rollout-${name}"
  local code
  code="$(curl --disable --silent --show-error --max-time 20 \
    --request POST \
    --header 'authorization:' \
    --header 'cookie:' \
    --header 'content-type: application/json' \
    --header "x-request-id: ${request_id}" \
    --data '{"smoke":"auth-gate-only"}' \
    --dump-header "${SMOKE_TMP}/${name}.headers" \
    --output "${SMOKE_TMP}/${name}" \
    --write-out '%{http_code}' \
    "$url")" || {
    echo "ERROR: smoke auth-rejection request failed: ${name}" >&2
    return 1
  }
  [ "$code" = 401 ] || {
    echo "ERROR: ${name} returned ${code}, expected 401" >&2
    return 1
  }
  python3 - \
    "${SMOKE_TMP}/${name}" \
    "${SMOKE_TMP}/${name}.headers" \
    "$expected_message" \
    "$request_id" <<'PY'
import json
import sys
from pathlib import Path

body_path, headers_path = (Path(value) for value in sys.argv[1:3])
expected_message, expected_request_id = sys.argv[3:]

payload = json.loads(body_path.read_text(encoding="utf-8"))
error = payload.get("error") if isinstance(payload, dict) else None
expected_error = {
    "code": 401,
    "message": expected_message,
    "type": "unauthorized",
    "source": "router",
}
if error != expected_error:
    raise SystemExit(f"smoke auth-rejection body differs: {error!r}")

# curl can record an informational response before the final one. Reset the
# map on each HTTP status line and verify only the final response block.
headers: dict[str, str] = {}
for raw_line in headers_path.read_text(encoding="iso-8859-1").splitlines():
    line = raw_line.rstrip("\r")
    if line.startswith("HTTP/"):
        headers = {}
    elif ":" in line:
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()

if not headers.get("content-type", "").lower().startswith("application/json"):
    raise SystemExit("smoke auth-rejection response is not JSON")
if headers.get("x-trustedrouter-request-id") != expected_request_id:
    raise SystemExit("smoke auth-rejection request id was not echoed by the application")
if headers.get("strict-transport-security") != "max-age=63072000; includeSubDomains":
    raise SystemExit("smoke auth-rejection response lacks the reviewed HSTS contract")
for forbidden in ("x-trustedrouter-provider", "x-trustedrouter-served-model"):
    if forbidden in headers:
        raise SystemExit(f"smoke auth-rejection unexpectedly returned {forbidden}")
PY
}

IFS=',' read -r -a domain_list <<<"$DOMAINS"
for domain in "${domain_list[@]}"; do
  safe_domain="${domain//./-}"
  check_code "${safe_domain}-home" 200 "https://${domain}/"
  check_code "${safe_domain}-health" 200 "https://${domain}/health"
  check_code "${safe_domain}-ready" 200 "https://${domain}/ready"
  check_code "${safe_domain}-models" 200 "https://${domain}/v1/models"
  check_code "${safe_domain}-session" 200 "https://${domain}/auth/session" \
    --header "@${AUTH_HEADER_FILE}"
  python3 - "${SMOKE_TMP}/${safe_domain}-session" <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("data") or {}
if data.get("authenticated") is not True or data.get("management") is not True:
    raise SystemExit("authenticated management smoke session is not active")
PY
  check_code "${safe_domain}-invalid-action" 422 \
    "https://${domain}/support/inquiry" \
    --header 'content-type: application/json' \
    --data '{"name":"","email":"invalid","category":"api","subject":"","message":"","website":""}'
  check_code "${safe_domain}-invalid-webhook" 400 \
    "https://${domain}/internal/stripe/webhook" \
    --header 'content-type: application/json' \
    --header 'stripe-signature: t=0,v1=invalid' \
    --data '{"id":"evt_rollout_invalid","type":"payment_intent.succeeded","data":{"object":{"id":"pi_invalid"}}}'
  check_auth_rejection \
    "chat-auth-${safe_domain}" \
    "https://${domain}/chat-proxy/v1/chat/completions" \
    "Missing Authentication header"
  check_auth_rejection \
    "internal-auth-${safe_domain}" \
    "https://${domain}/v1/internal/gateway/authorize" \
    "Invalid internal service token"
done

export TR_ROLLOUT_SMOKE_DOMAINS="$DOMAINS"
export TR_ROLLOUT_SMOKE_AUTH_HEADER_FILE="$AUTH_HEADER_FILE"
export TR_ROLLOUT_SMOKE_PLAYWRIGHT_STORAGE_STATE="$STORAGE_STATE"
export TR_ROLLOUT_SMOKE_PHASE="$PHASE"
export TR_ROLLOUT_SMOKE_PERCENT="$PERCENT"
export TR_ROLLOUT_SMOKE_PLAYWRIGHT_OUTPUT_DIR="$PLAYWRIGHT_OUTPUT"
cd "$ROOT_DIR"
npx playwright test \
  --config=playwright.rollout.config.js \
  --project=firefox \
  tests/browser/production_rollout_smoke.spec.js

echo "rollout smoke passed phase=${PHASE} percent=${PERCENT}" >&2
