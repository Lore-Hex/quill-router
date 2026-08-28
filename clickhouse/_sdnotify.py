"""Minimal systemd notification support without a runtime dependency."""

from __future__ import annotations

import contextlib
import os
import socket


def sd_notify(state: str) -> None:
    """Send *state* to systemd, or do nothing outside a notify unit.

    Send failures are swallowed, matching systemd's own sd_notify(3): a stale
    NOTIFY_SOCKET (manual run with a copied environment) or a transient socket
    error must not crash the drain loop it exists to keep alive — the watchdog
    then fires, which is the correct failure mode, not an exception here.
    """
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect(address)
            notifier.sendall(state.encode())
