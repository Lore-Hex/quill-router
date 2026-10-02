#!/usr/bin/env bash
# Renew the job's AWS session from its GitHub OIDC identity.
#
# aws-actions/configure-aws-credentials mints a one-hour session when the job
# starts, and the deploy mutex can hold the job for up to an hour before its
# first production write. A session that expires mid-rollout fails the
# rollout, and the rollback that follows runs on the same expired session.
# Call aws_refresh_session once the lease is held. Outside GitHub Actions it
# leaves the operator's credentials alone.
#
# Requires AWS_DEPLOY_ROLE_ARN, the role configure-aws-credentials assumed.

aws_session_fail() { printf '%s\n' "[FAIL] $*" >&2; }

aws_refresh_session() {
  [ -n "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || return 0
  # Tracing would print the tokens and the new session. Turn it off for the
  # renewal and restore the caller's setting afterwards.
  local traced=0 rc=0
  case $- in *x*) traced=1; set +x ;; esac
  aws_refresh_session_untraced || rc=$?
  [ "$traced" -eq 0 ] || set -x
  return "$rc"
}

# Every step's status is checked on its own, so a failure is caught whether or
# not the caller set pipefail (GitHub's default step shell does not).
aws_refresh_session_untraced() {
  if [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
    aws_session_fail "ACTIONS_ID_TOKEN_REQUEST_TOKEN is missing; the job needs id-token: write"
    return 1
  fi
  if [[ ! "${AWS_DEPLOY_ROLE_ARN:-}" =~ ^arn:aws:iam::([0-9]{12}):role/.+$ ]]; then
    aws_session_fail "AWS_DEPLOY_ROLE_ARN must name the deploy role to renew the AWS session"
    return 1
  fi
  local account="${BASH_REMATCH[1]}" response id_token credentials key secret session caller
  # The request token reaches curl on stdin (-H @-), so it is not in argv.
  if ! response="$(printf 'Authorization: bearer %s\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
      | curl -fsS -H @- "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=sts.amazonaws.com")"; then
    aws_session_fail "could not fetch a fresh GitHub OIDC token to renew the AWS session"
    return 1
  fi
  if ! id_token="$(printf '%s' "$response" \
      | python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin)["value"])')" \
      || [ -z "$id_token" ]; then
    aws_session_fail "the GitHub OIDC token endpoint returned no token"
    return 1
  fi
  # The ID token reaches aws on stdin (file:///dev/stdin): never in argv, never
  # in a file. The call is unsigned, since the job's first session may already
  # have expired and this call needs no credentials. Its stderr is discarded:
  # on a response it cannot parse, the AWS CLI prints the raw response, which
  # can hold the new credentials before they are masked.
  local status=0
  credentials="$(printf '%s' "$id_token" \
    | aws sts assume-role-with-web-identity --role-arn "$AWS_DEPLOY_ROLE_ARN" \
      --role-session-name "tr-deploy-${GITHUB_RUN_ID:-renewal}" \
      --web-identity-token file:///dev/stdin --duration-seconds 3600 \
      --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text \
      --no-sign-request 2>/dev/null)" || status=$?
  if [ "$status" -ne 0 ]; then
    aws_session_fail "renewing the AWS session after the deploy mutex wait failed (aws exit ${status})"
    return 1
  fi
  read -r key secret session <<<"$credentials"
  if [ -z "$key" ] || [ -z "$secret" ] || [ -z "$session" ]; then
    aws_session_fail "AWS returned an incomplete session"
    return 1
  fi
  # configure-aws-credentials masked the first session; mask this one too.
  if [ "${GITHUB_ACTIONS:-}" = true ]; then
    printf '::add-mask::%s\n' "$key" "$secret" "$session"
  fi
  export AWS_ACCESS_KEY_ID="$key" AWS_SECRET_ACCESS_KEY="$secret" AWS_SESSION_TOKEN="$session"
  if ! caller="$(aws sts get-caller-identity --query Account --output text)" \
      || [ "$caller" != "$account" ]; then
    aws_session_fail "the renewed AWS session is not in account ${account}"
    return 1
  fi
}
