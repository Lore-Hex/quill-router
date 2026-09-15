"""Bounded node-local retention for old canceled, unpaid website invoices."""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import subprocess
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

BIN = "/opt/lnd-v0.21.3-beta/lncli"
MACAROON = Path("/etc/lnd/cleanup.macaroon")
CURSOR = Path("/srv/lnd/recovery/canceled-invoice-cursor")


def prune_page(
    rows: list[dict[str, Any]], offset: int, now: int, remove: Callable[[str], None], *, apply: bool
) -> dict[str, int]:
    scanned = deleted = eligible = 0
    cursor = offset
    for row in rows[:100]:
        index = int(row["add_index"])
        if index <= cursor:
            raise ValueError("Nonmonotonic invoice page")
        cursor = index
        scanned += 1
        if (
            row.get("state") != "CANCELED"
            or row.get("settled", False)
            or row.get("memo") != "LightningRouter API credit"
            or int(row.get("amt_paid_msat", -1)) != 0
            or int(row.get("settle_index", -1)) != 0
            or int(row["creation_date"]) + int(row["expiry"]) >= now - 45 * 86400
        ):
            continue
        digest = base64.b64decode(row["r_hash"], validate=True)
        if len(digest) != 32:
            raise ValueError("Invalid invoice hash")
        eligible += 1
        if apply:
            remove(digest.hex())
            deleted += 1
        if eligible == 10:
            break
    return {
        "scanned": scanned,
        "deleted": deleted,
        "eligible": eligible,
        "cursor": cursor if rows else 0,
    }


def command(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed binary, validated invoice hash
        [BIN, "--lnddir=/srv/lnd", "--macaroonpath=" + str(MACAROON), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise RuntimeError("Invoice maintenance RPC failed")
    return result.stdout


def run(*, apply: bool) -> dict[str, int]:
    offset = int(CURSOR.read_text()) if CURSOR.exists() else 0
    if offset < 0:
        raise ValueError("Invalid maintenance cursor")
    # Node TLS stays pinned and this macaroon can only list/delete canceled invoices.
    context = ssl.create_default_context(cafile="/srv/lnd/tls.cert")
    req = urllib.request.Request(
        f"https://127.0.0.1:8080/v1/invoices?index_offset={offset}&num_max_invoices=100",
        headers={"Grpc-Metadata-macaroon": MACAROON.read_bytes().hex()},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    with opener.open(req, timeout=15) as response:  # noqa: S310 - fixed loopback TLS
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError("Invoice page too large")
    result = prune_page(
        json.loads(body).get("invoices", []),
        offset,
        int(time.time()),
        lambda digest: command("deletecanceledinvoice", "--invoice_hash=" + digest),
        apply=apply,
    )
    if apply:
        temporary = CURSOR.with_suffix(".next")
        with temporary.open("w") as output:
            os.chmod(temporary, 0o600)
            output.write(str(result["cursor"]))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(CURSOR)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps({"event": "lightning.invoice_cleanup", **run(apply=args.apply)}))
    except Exception:
        # CLI/RPC errors can contain invoice details. Never print their text.
        raise SystemExit("lightning.invoice_cleanup_failed") from None


if __name__ == "__main__":
    main()
