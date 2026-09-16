#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose ca-certificates python3 unattended-upgrades
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
ExecStart=/bin/bash -c 'set -euo pipefail; f=backups/db-$(date -u +%%F).dump; docker-compose exec -T postgres pg_dump -U btcpay -d btcpay -Fc >"$f.tmp"; mv "$f.tmp" "$f"; find backups -name "db-*.dump" -mtime +7 -delete'
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
