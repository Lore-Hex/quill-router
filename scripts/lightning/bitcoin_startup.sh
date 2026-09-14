#!/usr/bin/env bash
# The metadata source is uploaded alongside this script, never fetched from a
# moving external branch. No wallet, cloud credential or RPC secret is metadata.
set -euo pipefail
exec python3 -c '
import pathlib, subprocess, urllib.request
request = urllib.request.Request(
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/bitcoin-bootstrap",
    headers={"Metadata-Flavor": "Google"},
)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(request, timeout=15) as response:
    source = response.read(131073)
if len(source) > 131072:
    raise RuntimeError("oversized Bitcoin bootstrap")
directory = pathlib.Path("/opt/tr-bitcoin")
directory.mkdir(mode=0o755, parents=True, exist_ok=True)
target = directory / "node.py"
target.write_bytes(source)
target.chmod(0o755)
subprocess.run(["python3", str(target), "install"], check=True)
'
