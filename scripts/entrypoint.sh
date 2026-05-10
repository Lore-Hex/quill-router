#!/usr/bin/env bash
# Container entrypoint for trusted-router.
#
# On AWS ECS Fargate (Stage 4D control plane), the GCP service-account key
# we need for cross-cloud Spanner / Bigtable reads is stored
# AWS-KMS-wrapped in Secrets Manager so the same secret can be unwrapped by
# the Nitro enclave under attestation. ECS isn't an enclave, so this
# entrypoint does the unwrap on container boot:
#
#   1. If GCP_SA_KEY_KMS_WRAPPED is set, decode-base64 it and call AWS KMS
#      Decrypt to recover the SA-key JSON.
#   2. Write the JSON to /tmp/sa.json (in-memory tmpfs on Fargate).
#   3. Set GOOGLE_APPLICATION_CREDENTIALS to that path so the GCP SDK's
#      default-ADC chain finds it.
#   4. Unset the wrapped env var so it doesn't leak into the app's logs
#      or worker subprocesses.
#
# On GCP Cloud Run / GCE the env is unset and we just exec uvicorn — the
# default-ADC chain finds the runtime SA from the metadata server. Same
# image, no behavior change.
#
# Errors decrypting fail-closed: we exit before starting the app. A 503
# from the LB beats a 200 from a service that silently has the wrong
# credentials.

set -euo pipefail

if [ -n "${GCP_SA_KEY_KMS_WRAPPED:-}" ]; then
  echo "[entrypoint] GCP_SA_KEY_KMS_WRAPPED present; KMS-decrypting" >&2
  if ! command -v aws >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: aws CLI not in PATH" >&2
    exit 1
  fi

  # base64-decode the wrapped value, pipe ciphertext to KMS Decrypt,
  # base64-decode the response (aws CLI returns plaintext as base64).
  PLAINTEXT_JSON=$(
    echo "$GCP_SA_KEY_KMS_WRAPPED" \
      | base64 -d \
      | aws kms decrypt \
          --region "${TR_AWS_REGION:-us-west-2}" \
          --ciphertext-blob fileb:///dev/stdin \
          --query Plaintext --output text \
      | base64 -d
  ) || {
    echo "[entrypoint] FATAL: KMS decrypt failed" >&2
    exit 1
  }

  if ! echo "$PLAINTEXT_JSON" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
    echo "[entrypoint] FATAL: KMS decrypt returned non-JSON output" >&2
    exit 1
  fi

  CRED_PATH="${GCP_SA_KEY_PATH:-/tmp/sa.json}"
  printf '%s' "$PLAINTEXT_JSON" > "$CRED_PATH"
  chmod 600 "$CRED_PATH"
  export GOOGLE_APPLICATION_CREDENTIALS="$CRED_PATH"
  unset GCP_SA_KEY_KMS_WRAPPED
  echo "[entrypoint] wrote credentials to $CRED_PATH" >&2
fi

# Exec uvicorn (or whatever CMD the image specifies). Using exec replaces
# this shell so signals (SIGTERM from ECS during rolling deploy) reach
# uvicorn directly.
exec "$@"
