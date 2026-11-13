#!/usr/bin/env bash
# Keep the ClickHouse server away from the instance metadata server.
#
# The metadata server gives the node's service-account token to any local
# process that asks, and a database that can be made to fetch a URL is such a
# process. ClickHouse itself never needs the token: the ingest workers run as
# their own user and root runs gcloud. So TCP from the clickhouse user to
# 169.254.169.254 is rejected, except port 53: that address is also the
# node's resolver.
#
# Runs as root. clickhouse-server runs it before every start
# (tr-metadata-block.conf), so the server never starts without the rule, and
# apply_node_config.sh runs it on a node whose server is already up.
set -euo pipefail

PATH="${PATH}:/usr/sbin:/sbin"

if ! command -v iptables >/dev/null 2>&1; then
  echo "refusing: iptables is not installed, so the ClickHouse server cannot be kept from the metadata server" >&2
  exit 1
fi

RULE=(
  -d 169.254.169.254/32 -p tcp ! --dport 53
  -m owner --uid-owner clickhouse
  -j REJECT --reject-with tcp-reset
)
# -C asks whether the rule is already there, so a restart does not stack copies.
if ! iptables -w -C OUTPUT "${RULE[@]}" 2>/dev/null; then
  iptables -w -I OUTPUT "${RULE[@]}"
fi
