#!/usr/bin/env bash
# Installs only the independent observer on an existing internal host.
set -euo pipefail
export CLOUDSDK_CORE_PROJECT=quill-cloud-proxy
# roach-gcp, the first host, was deleted on 2026-11-11. tr-clickhouse-3 is a
# ClickHouse standby with no worker units and the resolver the unit expects.
host=${OBSERVER_HOST:-tr-clickhouse-3}
zone=${OBSERVER_ZONE:-us-central1-c}
sha=${1:?Pass the full tested commit SHA already merged into origin/main}
[[ "$sha" =~ ^[a-f0-9]{40}$ ]] || exit 2
if [[ ${CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE:-} == *tr-ops-local.json ]]; then
  echo "Use a separately authenticated deployment identity, not the read-only ops key." >&2
  exit 2
fi
root=$(git rev-parse --show-toplevel)
cd "$root"
git merge-base --is-ancestor "$sha" origin/main
tmp=$(mktemp -d)
remote="/tmp/tr-price-observer-$(basename "$tmp")"
ssh_args=(compute ssh "$host" --zone="$zone" --tunnel-through-iap --quiet)
cleanup() {
  rm -rf "$tmp"
  gcloud "${ssh_args[@]}" --command="rm -rf '$remote'" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Committed objects only: never bundle concurrent or uncommitted source.
git archive "$sha" scripts/price_refresh_alerts.py \
  scripts/ops/price-refresh-observer.service scripts/ops/price-refresh-observer.timer > "$tmp/observer.tar"
# Both secrets are read by this operator's own gcloud identity (the owner login
# or tr-deploy, which already read every secret); the host never gets IAM.
umask 077
gcloud secrets versions access latest --secret=trustedrouter-sentry-dsn --out-file="$tmp/ingest.dsn"
chmod 600 "$tmp/ingest.dsn"
# The service dispatches stalled refreshes with this token. Fail closed: never
# install a dispatch-enabled service without it. Its value is never printed.
token_secret=trustedrouter-price-refresh-dispatch-token
if ! gcloud secrets versions access latest --secret="$token_secret" \
    --out-file="$tmp/github-dispatch.token" >/dev/null 2>&1; then
  echo "Could not read Secret Manager secret $token_secret (project quill-cloud-proxy) as $(gcloud config get-value account 2>/dev/null). Nothing was installed." >&2
  exit 1
fi
chmod 600 "$tmp/github-dispatch.token"
if [[ $(wc -l < "$tmp/github-dispatch.token") -gt 1 ]] \
    || ! LC_ALL=C grep -Eqx 'github_pat_[A-Za-z0-9_]{20,250}' "$tmp/github-dispatch.token"; then
  echo "Secret $token_secret is not one fine-grained GitHub token. Nothing was installed." >&2
  exit 1
fi
gcloud "${ssh_args[@]}" --command="umask 077; mkdir '$remote'"
gcloud compute scp --zone="$zone" --tunnel-through-iap --quiet \
  "$tmp/observer.tar" "$tmp/ingest.dsn" "$tmp/github-dispatch.token" "$host:$remote/"
gcloud "${ssh_args[@]}" --command="sudo bash -s -- '$remote' '$sha'" <<'REMOTE'
set -euo pipefail
staging=$1
sha=$2
tar -xf "$staging/observer.tar" -C "$staging"
# The unit's LoadCredential needs this file; refuse before changing anything.
test -s "$staging/github-dispatch.token"
# py_compile accepts imports that only fail at run time (datetime.UTC is 3.11).
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'
/usr/bin/python3 -m py_compile "$staging/scripts/price_refresh_alerts.py"
systemd-analyze verify "$staging/scripts/ops/price-refresh-observer.service" "$staging/scripts/ops/price-refresh-observer.timer"
systemctl is-active --quiet systemd-resolved.service
test -r /run/systemd/resolve/stub-resolv.conf
backup="/var/backups/tr-price-refresh-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup" /etc/tr-price-refresh
for path in /opt/tr-price-refresh /etc/systemd/system/price-refresh-observer.service /etc/systemd/system/price-refresh-observer.timer /etc/tr-price-refresh/ingest.dsn /etc/tr-price-refresh/github-dispatch.token; do
  if [[ -e "$path" ]]; then
    # GNU cp 9.1 --parents can fail restoring absolute parents outside /.
    install -d -m 0700 "$backup$(dirname "$path")"
    cp -a -- "$path" "$backup$path"
  fi
done
install -d -m 0755 /opt/tr-price-refresh
install -m 0644 "$staging/scripts/price_refresh_alerts.py" /opt/tr-price-refresh/price_refresh_alerts.py
install -m 0600 "$staging/ingest.dsn" /etc/tr-price-refresh/ingest.dsn
install -m 0600 "$staging/github-dispatch.token" /etc/tr-price-refresh/github-dispatch.token
install -m 0644 "$staging/scripts/ops/price-refresh-observer.service" /etc/systemd/system/
install -m 0644 "$staging/scripts/ops/price-refresh-observer.timer" /etc/systemd/system/
printf '%s\n' "$sha" > /opt/tr-price-refresh/SOURCE_SHA
systemctl daemon-reload
systemctl start price-refresh-observer.service
systemctl enable --now price-refresh-observer.timer
systemctl show price-refresh-observer.service -p Result -p ExecMainStatus
systemctl list-timers price-refresh-observer.timer --no-pager
REMOTE
