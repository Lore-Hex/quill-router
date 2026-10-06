#!/usr/bin/env bash
# Executable contract for scripts/deploy/_active_revision.sh: the resolver that
# rollout.sh and both split surfaces use to read sticky operator pins from the
# one revision serving 100% of primary traffic, and the image-digest pin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/deploy/_active_revision.sh
source "${ROOT}/scripts/deploy/_active_revision.sh"

SERVICE="trusted-router"
SCENARIO=""
IMAGE=""
CALL_LOG="$(mktemp "${TMPDIR:-/tmp}/tr-active-revision.XXXXXX")"
trap 'rm -f "$CALL_LOG"' EXIT

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

revision_json() {
  local release="$1"
  printf '{"spec":{"containers":[{"env":[{"name":"TR_RELEASE","value":"%s"},{"name":"TR_STORAGE_BACKEND","value":"spanner-clickhouse"}]}]}}\n' \
    "$release"
}

gc() {
  printf '%s\n' "$*" >>"$CALL_LOG"
  if [ "$1 $2 $3" = "run services describe" ]; then
    case "$SCENARIO" in
      rollback)
        printf '%s\n' '{"status":{"latestCreatedRevisionName":"rev-rejected-candidate","latestReadyRevisionName":"rev-rejected-candidate","traffic":[{"revisionName":"rev-rollback","percent":100}]}}'
        ;;
      ambiguous)
        printf '%s\n' '{"status":{"latestCreatedRevisionName":"rev-new","traffic":[{"revisionName":"rev-old","percent":50},{"revisionName":"rev-new","percent":50}]}}'
        ;;
      read_error)
        printf '%s\n' 'ERROR: (gcloud.run.services.describe) PERMISSION_DENIED: caller cannot read service' >&2
        return 1
        ;;
      exact_not_found)
        printf '%s\n' 'ERROR: (gcloud.run.services.describe) NOT_FOUND: Service [trusted-router] was not found' >&2
        return 1
        ;;
      *)
        printf '%s\n' '{"status":{"latestCreatedRevisionName":"rev-live","latestReadyRevisionName":"rev-live","traffic":[{"revisionName":"rev-live","percent":100}]}}'
        ;;
    esac
    return 0
  fi

  if [ "$1 $2 $3" = "run revisions describe" ]; then
    local revision="$4"
    case "$SCENARIO" in
      rollback)
        [ "$revision" = "rev-rollback" ] || fail "described latest candidate instead of rollback"
        revision_json rollback-release
        ;;
      *) revision_json live-release ;;
    esac
    return 0
  fi

  if [ "$1 $2 $3 $4" = "artifacts docker images describe" ]; then
    case "$SCENARIO" in
      digest) printf 'sha256:%064d\n' 7 ;;
      no_digest) printf '%s\n' 'not-a-digest' ;;
      *) fail "unexpected image describe in scenario ${SCENARIO}" ;;
    esac
    return 0
  fi

  fail "unexpected gc call: $*"
}

test_rollback_reads_the_traffic_revision_not_latest_candidate() {
  SCENARIO=rollback
  : >"$CALL_LOG"
  local json
  json="$(active_revision_json us-central1 false)"
  [ "$(revision_env "$json" TR_RELEASE missing)" = "rollback-release" ] ||
    fail "rollback revision's pin was not read"
  grep -q 'run revisions describe rev-rollback ' "$CALL_LOG" ||
    fail "100%-traffic rollback revision was not described"
  if grep -q 'run revisions describe rev-rejected-candidate ' "$CALL_LOG"; then
    fail "latest rejected candidate was described"
  fi
}

test_ambiguous_traffic_fails_closed() {
  SCENARIO=ambiguous
  if active_revision_json us-central1 false >/dev/null 2>&1; then
    fail "50/50 traffic was accepted as one active revision"
  fi
}

test_read_errors_are_not_fresh_environments() {
  SCENARIO=read_error
  local status=0
  active_revision_json us-central1 true >/dev/null 2>&1 || status=$?
  [ "$status" -eq 1 ] || fail "read error returned ${status}, expected fail-closed status 1"
}

test_only_exact_service_not_found_is_a_fresh_environment() {
  SCENARIO=exact_not_found
  local status=0
  active_revision_json us-central1 true >/dev/null 2>&1 || status=$?
  [ "$status" -eq 3 ] || fail "exact service NOT_FOUND returned ${status}, expected 3"
  status=0
  active_revision_json us-central1 false >/dev/null 2>&1 || status=$?
  [ "$status" -eq 1 ] || fail "NOT_FOUND without the fresh-environment allowance returned ${status}, expected 1"
}

test_revision_env_accepts_cloud_run_empty_plain_values() {
  local name_only='{"spec":{"containers":[{"env":[{"name":"EMPTY"}]}]}}'
  [ "$(revision_env "$name_only" EMPTY fallback)" = "" ] ||
    fail "Cloud Run name-only empty value was not preserved"

  local explicit_empty='{"spec":{"containers":[{"env":[{"name":"EMPTY","value":""}]}]}}'
  [ "$(revision_env "$explicit_empty" EMPTY fallback)" = "" ] ||
    fail "explicit empty value was not preserved"

  [ "$(revision_env "$name_only" MISSING fallback)" = "fallback" ] ||
    fail "missing environment variable did not use its default"
}

test_revision_env_rejects_non_plain_values() {
  local secret_ref='{"spec":{"containers":[{"env":[{"name":"SECRET","valueFrom":{"secretKeyRef":{"name":"secret","key":"latest"}}}]}]}}'
  if revision_env "$secret_ref" SECRET fallback >/dev/null 2>&1; then
    fail "secret-backed environment variable was accepted as plain text"
  fi

  local non_string='{"spec":{"containers":[{"env":[{"name":"INVALID","value":false}]}]}}'
  if revision_env "$non_string" INVALID fallback >/dev/null 2>&1; then
    fail "non-string environment variable was accepted as plain text"
  fi

  local duplicate='{"spec":{"containers":[{"env":[{"name":"DUP","value":"a"},{"name":"DUP","value":"b"}]}]}}'
  if revision_env "$duplicate" DUP fallback >/dev/null 2>&1; then
    fail "duplicate environment variable was accepted"
  fi
}

test_resolve_image_digest_pins_the_digest_and_strips_only_the_tag() {
  SCENARIO=digest
  local expected
  expected="$(printf 'sha256:%064d' 7)"
  IMAGE="us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router:abc1234"
  resolve_image_digest || fail "digest resolution failed"
  [ "$IMAGE" = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router@${expected}" ] ||
    fail "tag was not replaced by the digest: ${IMAGE}"
  # A registry port is not a tag: only the final path component loses its tag.
  IMAGE="localhost:5000/repo/service:tag"
  resolve_image_digest || fail "digest resolution failed for a port-qualified registry"
  [ "$IMAGE" = "localhost:5000/repo/service@${expected}" ] ||
    fail "registry port was mistaken for a tag: ${IMAGE}"
}

test_resolve_image_digest_refuses_a_non_digest_answer() {
  SCENARIO=no_digest
  IMAGE="us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router:abc1234"
  if resolve_image_digest >/dev/null 2>&1; then
    fail "a non-digest answer was accepted as an immutable pin"
  fi
  [ "$IMAGE" = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router:abc1234" ] ||
    fail "IMAGE was rewritten despite the refused answer: ${IMAGE}"
}

test_rollback_reads_the_traffic_revision_not_latest_candidate
test_ambiguous_traffic_fails_closed
test_read_errors_are_not_fresh_environments
test_only_exact_service_not_found_is_a_fresh_environment
test_revision_env_accepts_cloud_run_empty_plain_values
test_revision_env_rejects_non_plain_values
test_resolve_image_digest_pins_the_digest_and_strips_only_the_tag
test_resolve_image_digest_refuses_a_non_digest_answer
printf '%s\n' 'active revision shell tests: 8 passed'
