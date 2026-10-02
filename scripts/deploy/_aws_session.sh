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
  if [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
    aws_session_fail "ACTIONS_ID_TOKEN_REQUEST_TOKEN is missing; the job needs id-token: write"
    return 1
  fi
  if [[ ! "${AWS_DEPLOY_ROLE_ARN:-}" =~ ^arn:aws:iam::([0-9]{12}):role/.+$ ]]; then
    aws_session_fail "AWS_DEPLOY_ROLE_ARN must name the deploy role to renew the AWS session"
    return 1
  fi
  local account="${BASH_REMATCH[1]}" token_file credentials key secret session
  # The request token reaches curl on stdin (-H @-), and the ID token reaches
  # aws through a file (file://), so neither is in any command's argv. The
  # STS call is unsigned (--no-sign-request): the job's first session may
  # already have expired, and this call needs no credentials.
  if ! token_file="$(mktemp "${TMPDIR:-/tmp}/tr-aws-oidc.XXXXXX")"; then
    aws_session_fail "cannot create a private file for the GitHub OIDC token"
    return 1
  fi
  if ! printf 'Authorization: bearer %s\n' "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
      | curl -fsS -H @- "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=sts.amazonaws.com" \
      | python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin)["value"])' \
      > "$token_file" || [ ! -s "$token_file" ]; then
    rm -f "$token_file"
    aws_session_fail "could not fetch a fresh GitHub OIDC token to renew the AWS session"
    return 1
  fi
  if ! credentials="$(aws sts assume-role-with-web-identity --role-arn "$AWS_DEPLOY_ROLE_ARN" \
      --role-session-name "tr-deploy-${GITHUB_RUN_ID:-renewal}" \
      --web-identity-token "file://${token_file}" --duration-seconds 3600 \
      --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text \
      --no-sign-request)"; then
    rm -f "$token_file"
    aws_session_fail "renewing the AWS session after the deploy mutex wait failed"
    return 1
  fi
  rm -f "$token_file"
  read -r key secret session <<<"$credentials"
  if [ -z "$key" ] || [ -z "$secret" ] || [ -z "$session" ]; then
    aws_session_fail "AWS returned an incomplete session"
    return 1
  fi
  export AWS_ACCESS_KEY_ID="$key" AWS_SECRET_ACCESS_KEY="$secret" AWS_SESSION_TOKEN="$session"
  if [ "$(aws sts get-caller-identity --query Account --output text)" != "$account" ]; then
    aws_session_fail "the renewed AWS session is not in account ${account}"
    return 1
  fi
}
