#!/usr/bin/env bash
# Container entrypoint for trusted-router.
#
# Production hosting is GCP-only. The GCP SDK's default ADC chain finds the
# runtime service account from the metadata server, so there is no cross-cloud
# external credential unwrap path here.

set -euo pipefail

# Exec uvicorn (or whatever CMD the image specifies). Using exec replaces
# this shell so signals reach uvicorn directly.
exec "$@"
