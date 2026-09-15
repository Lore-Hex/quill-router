"""Install committed invoice retention only; never restart or reconfigure LND."""

from __future__ import annotations

import argparse
import base64
import shlex
import subprocess
from pathlib import Path

from scripts.lightning.activate_web import Operator

SOURCE = "scripts/lightning/prune_node.py"
SERVICE = """[Unit]
Description=Bounded canceled Lightning invoice cleanup
After=tr-lnd.service
RequiresMountsFor=/srv/lnd
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 /opt/tr-lightning/prune_node.py --apply
TimeoutStartSec=200
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/srv/lnd/recovery
MemoryMax=128M
LimitCORE=0
"""
TIMER = """[Unit]
Description=Hourly bounded Lightning invoice retention
[Timer]
OnBootSec=10min
OnUnitInactiveSec=1h
RandomizedDelaySec=60
[Install]
WantedBy=timers.target
"""


def install(operator: Operator) -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(  # noqa: S603 - fixed git command
        [  # noqa: S607 - developer's git executable
            "git",
            "diff",
            "--exit-code",
            "HEAD",
            "--",
            SOURCE,
            "scripts/lightning/install_maintenance.py",
        ],
        cwd=root,
        check=True,
    )  # noqa: S603,S607
    source = subprocess.check_output(["git", "show", "HEAD:" + SOURCE], cwd=root)  # noqa: S603,S607
    compile(source, SOURCE, "exec")
    script = """import base64, os, subprocess
from pathlib import Path
macaroon = Path('/etc/lnd/cleanup.macaroon')
if not macaroon.exists():
    subprocess.run(['/opt/lnd-v0.21.3-beta/lncli', '--lnddir=/srv/lnd', 'bakemacaroon',
        '--root_key_id=24', '--save_to=' + str(macaroon),
        'uri:/lnrpc.Lightning/ListInvoices', 'uri:/lnrpc.Lightning/DeleteCanceledInvoice'],
        check=True, capture_output=True, timeout=30)
os.chmod(macaroon, 0o600)
"""
    for path, content in [
        ("/opt/tr-lightning/prune_node.py", source),
        ("/etc/systemd/system/tr-lnd-invoice-cleanup.service", SERVICE.encode()),
        ("/etc/systemd/system/tr-lnd-invoice-cleanup.timer", TIMER.encode()),
    ]:
        script += f"Path({path!r}).write_bytes(base64.b64decode({base64.b64encode(content).decode()!r}))\n"
    script += """subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'enable', '--now', 'tr-lnd-invoice-cleanup.timer'], check=True)
subprocess.run(['python3', '/opt/tr-lightning/prune_node.py'], check=True)
"""
    print(
        operator.gc(
            "compute",
            "ssh",
            "tr-bitcoin-1",
            "--zone=us-central1-a",
            "--tunnel-through-iap",
            "--ssh-key-expire-after=30m",
            "--command=sudo python3 -c " + shlex.quote(script),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        install(Operator(args.account))
    else:
        print(
            "Install invoice-only local cleanup timer: 100 scans, 10 canceled/unpaid removals per hour; retain 45 days. No LND restart."
        )


if __name__ == "__main__":
    main()
