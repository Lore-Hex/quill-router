#!/usr/bin/env bash
# Change the password of the `tr` ClickHouse user on the GCP cluster to the
# current value of its Secret Manager secret, one node at a time.
#
#   scripts/deploy/clickhouse_rotate_password.sh            # report only
#   scripts/deploy/clickhouse_rotate_password.sh --apply    # change it
#
# Add the new secret version first; this script never writes the secret:
#
#   openssl rand -hex 32 | tr -d '\n' | gcloud secrets versions add \
#     trustedrouter-clickhouse-password --data-file=- --project quill-cloud-proxy
#
# Each node gets scripts/deploy/clickhouse-node-config/rotate_tr_password.sh,
# which installs the hashed user file, waits for the server to accept the new
# password without a restart, requires that the old one is refused, rewrites
# /etc/tr-clickhouse-ingest.env and restarts the running services that read it.
# Nodes go in the order tr-clickhouse-3, -2, then -1, the writer host last. The
# first failure stops the rollout, and running it again continues from there.
# Between the first node and the last, a job that signs in to another node with
# its own node's password is turned away; the timers run it again.
# NAME and ZONE together target one node.
#
# The password is read once, held in this shell and written to each node's
# standard input. It is never a command argument, here or on the node.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT="${PROJECT:-quill-cloud-proxy}"
SECRET="${SECRET:-trustedrouter-clickhouse-password}"
CONFIG_DIR="scripts/deploy/clickhouse-node-config"
# Root-only and on a memory file system: nothing of a run outlives the node.
STAGING="/run/tr-clickhouse-rotate"

for arg in "$@"; do
  case "$arg" in
    --apply) ;;
    *) echo "usage: $0 [--apply]" >&2; exit 2 ;;
  esac
done

if [ -n "${NAME:-}" ] || [ -n "${ZONE:-}" ]; then
  if [ -z "${NAME:-}" ] || [ -z "${ZONE:-}" ]; then
    echo "refusing: set NAME and ZONE together to target one node" >&2
    exit 1
  fi
  NODES=("${NAME}:${ZONE}")
else
  NODES=(
    "tr-clickhouse-3:us-central1-c"
    "tr-clickhouse-2:us-central1-b"
    "tr-clickhouse-1:us-central1-a"
  )
fi

# Ship committed files only, as the worker bundle does.
dirty="$(git -C "$ROOT" status --porcelain --untracked-files=all -- "$CONFIG_DIR")"
if [ -n "$dirty" ]; then
  echo "refusing: ${CONFIG_DIR} has uncommitted changes:" >&2
  echo "$dirty" >&2
  exit 1
fi
archive="$(mktemp "${TMPDIR:-/tmp}/tr-clickhouse-rotate.XXXXXX")"
trap 'rm -f "$archive"' EXIT
git -C "$ROOT" archive --format=tar.gz --output="$archive" HEAD "$CONFIG_DIR"

for node in "${NODES[@]}"; do
  name="${node%%:*}"
  zone="${node##*:}"
  external_ip="$(gcloud compute instances describe "$name" \
    --project="$PROJECT" \
    --zone="$zone" \
    --format='value(networkInterfaces[0].accessConfigs[0].natIP)')"
  if [ -n "$external_ip" ]; then
    echo "refusing rotation: $name has external IP $external_ip" >&2
    exit 1
  fi
done

# One read for every node: a version added halfway through must not leave the
# cluster on two passwords.
# The trailing x keeps the command substitution from dropping trailing
# newlines: a new node's startup script hashes the secret's exact bytes, so a
# value that only works once its newline is stripped must be refused here.
password="$(gcloud secrets versions access latest \
  --secret="$SECRET" \
  --project="$PROJECT" && printf x)"
password="${password%x}"
# The node script enforces the same rule. Checked here so that a value it would
# refuse stops the rollout before any node is contacted.
if ! [[ "$password" =~ ^[A-Za-z0-9_][A-Za-z0-9_-]{23,}$ ]]; then
  echo "refusing: the latest version of ${SECRET} must be 24 or more characters from A-Z a-z 0-9 _ -, must not start with - and must not end in a newline" >&2
  exit 1
fi

for node in "${NODES[@]}"; do
  name="${node%%:*}"
  zone="${node##*:}"
  echo "== ${name}"
  gcloud compute ssh "$name" \
    --project="$PROJECT" \
    --zone="$zone" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo bash -c 'set -eu; rm -rf ${STAGING}; install -d -m 0700 ${STAGING}; tar -xzf - -C ${STAGING}'" \
    <"$archive"
  # Standard input is the password now, which is why the script went ahead.
  printf '%s\n' "$password" | gcloud compute ssh "$name" \
    --project="$PROJECT" \
    --zone="$zone" \
    --tunnel-through-iap \
    --quiet \
    --command="sudo bash -c 'set -eu; trap \"rm -rf ${STAGING}\" EXIT; bash ${STAGING}/${CONFIG_DIR}/rotate_tr_password.sh $*'"
done
if [ "$#" -gt 0 ]; then
  echo "The tr password is current on: ${NODES[*]%%:*}"
fi
