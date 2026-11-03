#!/usr/bin/env bash
# Installs only the independent observer on the existing monitoring host.
set -euo pipefail
export CLOUDSDK_CORE_PROJECT=quill-cloud-proxy
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
ssh_args=(compute ssh roach-gcp --zone=us-central1-a --tunnel-through-iap --quiet)
cleanup() {
  rm -rf "$tmp"
  gcloud "${ssh_args[@]}" --command="rm -rf '$remote'" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Committed objects only: never bundle concurrent or uncommitted source.
git archive "$sha" scripts/price_refresh_alerts.py \
  scripts/ops/price-refresh-observer.service scripts/ops/price-refresh-observer.timer > "$tmp/observer.tar"
gcloud secrets versions access latest --secret=trustedrouter-sentry-dsn --out-file="$tmp/ingest.dsn"
chmod 600 "$tmp/ingest.dsn"
gcloud "${ssh_args[@]}" --command="umask 077; mkdir '$remote'"
gcloud compute scp --zone=us-central1-a --tunnel-through-iap --quiet \
  "$tmp/observer.tar" "$tmp/ingest.dsn" "roach-gcp:$remote/"
gcloud "${ssh_args[@]}" --command="sudo bash -s -- '$remote' '$sha'" <<'REMOTE'
set -euo pipefail
staging=$1
sha=$2
tar -xf "$staging/observer.tar" -C "$staging"
/usr/bin/python3 -m py_compile "$staging/scripts/price_refresh_alerts.py"
systemd-analyze verify "$staging/scripts/ops/price-refresh-observer.service" "$staging/scripts/ops/price-refresh-observer.timer"
backup="/var/backups/tr-price-refresh-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup" /etc/tr-price-refresh
for path in /opt/tr-price-refresh /etc/systemd/system/price-refresh-observer.service /etc/systemd/system/price-refresh-observer.timer /etc/tr-price-refresh/ingest.dsn; do
  if [[ -e "$path" ]]; then cp -a --parents "$path" "$backup/"; fi
done
install -d -m 0755 /opt/tr-price-refresh
install -m 0644 "$staging/scripts/price_refresh_alerts.py" /opt/tr-price-refresh/price_refresh_alerts.py
install -m 0600 "$staging/ingest.dsn" /etc/tr-price-refresh/ingest.dsn
install -m 0644 "$staging/scripts/ops/price-refresh-observer.service" /etc/systemd/system/
install -m 0644 "$staging/scripts/ops/price-refresh-observer.timer" /etc/systemd/system/
printf '%s\n' "$sha" > /opt/tr-price-refresh/SOURCE_SHA
systemctl daemon-reload
systemctl start price-refresh-observer.service
systemctl enable --now price-refresh-observer.timer
systemctl show price-refresh-observer.service -p Result -p ExecMainStatus
systemctl list-timers price-refresh-observer.timer --no-pager
REMOTE
