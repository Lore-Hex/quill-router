#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if [ "${1:-}" != "--configure-only" ]; then
  apt-get update -qq
  apt-get install -y -qq docker.io docker-compose ca-certificates python3 unattended-upgrades
fi
systemctl enable --now docker
install -d -m 0700 /opt/tr-btcpay /opt/tr-btcpay/secrets /opt/tr-btcpay/backups
cat >/etc/systemd/system/tr-btcpay.service <<'UNIT'
[Unit]
Description=Isolated LightningRouter BTCPay dashboard
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/tr-btcpay
ExecStart=/usr/bin/docker-compose up -d
ExecStop=/usr/bin/docker-compose stop
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
UNIT
cat >/etc/systemd/system/tr-btcpay-backup.service <<'UNIT'
[Unit]
Description=Consistent BTCPay database dump before disk snapshots
After=tr-btcpay.service

[Service]
Type=oneshot
WorkingDirectory=/opt/tr-btcpay
UMask=0077
ExecStart=/bin/bash -c 'set -euo pipefail; for db in btcpay nbxplorer; do f=backups/db-$db-$(date -u +%%F).dump; docker-compose exec -T postgres pg_dump -U btcpay -d "$db" -Fc >"$f.tmp"; mv "$f.tmp" "$f"; done; find backups -name "db-*.dump" -mtime +7 -delete'
UNIT
cat >/etc/systemd/system/tr-btcpay-backup.timer <<'UNIT'
[Unit]
Description=Nightly BTCPay database dump

[Timer]
OnCalendar=*-*-* 04:00:00 UTC
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable tr-btcpay.service tr-btcpay-backup.timer
