"""Supervise the loopback receive-only sidecar and web process together."""

import os
import signal
import subprocess
import sys
import time

import httpx


def sidecar_command() -> list[str]:
    return ["/usr/local/bin/lexe-sidecar", "--listen-addr", "127.0.0.1:5393", "--network", "mainnet",
            "--client-credentials-path", "/var/secrets/lexe/receive-client", "--data-dir", "/var/lib/lexe"]


def sidecar_environment() -> dict[str, str]:
    # Do not pass SQL/checkout credentials, inherited Lexe options, or proxy
    # settings to the wallet process. The root seed is never mounted here.
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/var/lib/lexe", "RUST_LOG": "off"}


def main() -> int:
    web = [sys.executable, "-m", "uvicorn", "lightning_router.app:from_environment", "--factory",
           "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-proxy-headers"]  # noqa: S104 - Cloud Run ingress only; sidecar is loopback
    if not os.environ.get("LR_LEXE_WALLET_ID"):
        os.execv(sys.executable, web)  # noqa: S606 - fixed local web command
    children: list[subprocess.Popen[bytes]] = []
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        sidecar = subprocess.Popen(sidecar_command(), env=sidecar_environment(), cwd="/var/lib/lexe")  # noqa: S603 - fixed pinned binary, no shell
        children.append(sidecar)
        with httpx.Client(timeout=1, trust_env=False, follow_redirects=False) as client:
            for _ in range(60):
                if stopping or sidecar.poll() is not None:
                    return 1
                try:
                    response = client.get("http://127.0.0.1:5393/v2/health")
                    if response.status_code == 200 and response.json().get("status") == "ok":
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(1)
            else:
                return 1
        children.append(subprocess.Popen(web))  # noqa: S603 - fixed local web command
        while not stopping:
            if any(child.poll() is not None for child in children):
                return 1
            time.sleep(0.2)
        return 0
    finally:
        # Let the web worker stop first. No wallet database or funds are kept
        # in this sidecar cache; restarting it cannot duplicate the remote node.
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 8
        for child in reversed(children):
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
