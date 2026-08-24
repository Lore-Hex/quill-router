#!/usr/bin/env bash
# Container entrypoint for trusted-router.
#
# On GCP the SDK's default ADC chain finds the runtime service account from the
# metadata server, and everything below is skipped.
#
# Off GCP, a container that needs to reach a Google API has no metadata server
# to ask. It proves its identity with Workload Identity Federation instead, and
# the important property is that a WIF configuration is *not a secret*: it names
# a pool, a provider and a service account to impersonate, and points the SDK at
# the **local** cloud's instance metadata for the proof. No key material is
# involved, so it travels as a plain environment variable and needs no secret
# store.
#
# Note the narrow scope. Each cloud runs its own database (see
# docs/storage-portability/multi-cloud-separation.md), so this is for
# operational access — shared image registry, bootstrap, ops tooling — not for
# application data. Same image, same entrypoint, no long-lived Google
# credentials on any cloud.

set -euo pipefail

if [ -n "${TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG:-}" ]; then
  # Guard the seam. This path exists to REMOVE long-lived keys, so refuse to
  # materialise a service-account key through it — otherwise the keyless
  # design quietly degrades back into a key file the first time someone is in
  # a hurry.
  case "$TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG" in
    *'"private_key"'*|*'"service_account"'*)
      echo "entrypoint: refusing a credential config that carries key material." >&2
      echo "entrypoint: this variable takes a keyless external_account (Workload" >&2
      echo "entrypoint: Identity Federation) config only." >&2
      exit 1
      ;;
  esac

  config_path="${TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG_PATH:-/tmp/gcp-external-credential.json}"
  (umask 077 && printf '%s' "$TR_GOOGLE_EXTERNAL_CREDENTIAL_CONFIG" > "$config_path")
  export GOOGLE_APPLICATION_CREDENTIALS="$config_path"
fi

# exec replaces this shell so signals reach uvicorn directly.
exec "$@"
