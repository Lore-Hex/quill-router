#!/usr/bin/env python3
"""Pair a revision's authorize 503s with the lines the app marks them with,
by trace.

Used by scripts/deploy/assert_no_billing_5xx.sh. Authorize answers two 503s
by design, both retryable (Retry-After: 1), neither router-core impact, and
neither may roll a canary back:

* lease headroom: with TR_FASTPATH_LEASE_HEADROOM_503 on, a workspace's
  shortfall is money held in fast-path leases;
* rebalance busy: a workspace's credit covers the hold but is spread across
  credit shards, and the rebalance cooldown is held by its other requests.

Cloud Run's request log records only the status and the URL, so each 503 is
told apart by the line the app writes for it (routes/internal/gateway.py,
_lease_headroom_error and _log_credit_rebalance_busy):

    INFO trusted_router.routes.internal.gateway billing.lease_headroom_unavailable ... trace=<32 hex>
    INFO trusted_router.routes.internal.gateway billing.credit_rebalance_busy ... trace=<32 hex>

The request log carries the same trace as "projects/P/traces/<32 hex>".

Each request-log 503 is excused only by a line with its trace, and each line
excuses one 503: an enclave that retries under one trace makes several 503s
and several lines, and a real 503 among them is still left over. A 503 with
no trace, or whose line was never written or has not been ingested yet, is
not excused: when in doubt, the 503 counts.

usage: billing_5xx_lease_headroom.py REQUESTS_JSON LINES_JSON
  REQUESTS_JSON: gcloud logging read --format=json of the authorize 503s
  LINES_JSON:    gcloud logging read --format=json of the app's lines
Exits 0 when every 503 is paired, 1 when any is not (printing those), and
2 on unreadable input.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from typing import Any

LINE_PREFIX = "INFO trusted_router.routes.internal.gateway billing.lease_headroom_unavailable "
REBALANCE_BUSY_LINE_PREFIX = "INFO trusted_router.routes.internal.gateway billing.credit_rebalance_busy "
#: The lines that excuse an authorize 503, by the name the report gives each.
LINE_PREFIXES = {
    "lease headroom": LINE_PREFIX,
    "rebalance busy": REBALANCE_BUSY_LINE_PREFIX,
}
_LINE_TRACE = re.compile(r"(?:^|\s)trace=([0-9a-f]{32})(?:\s|$)")
_REQUEST_TRACE = re.compile(r"^projects/[^/]+/traces/([0-9a-fA-F]{32})$")


def request_trace(entry: dict[str, Any]) -> str | None:
    match = _REQUEST_TRACE.match(str(entry.get("trace") or ""))
    return match.group(1).lower() if match else None


def line_kind(entry: dict[str, Any]) -> str | None:
    """Which of the app's marks a log entry is, or None when it is neither."""
    payload = entry.get("textPayload")
    if not isinstance(payload, str):
        return None
    for kind, prefix in LINE_PREFIXES.items():
        if payload.startswith(prefix):
            return kind
    return None


def line_trace(entry: dict[str, Any]) -> str | None:
    if line_kind(entry) is None:
        return None
    match = _LINE_TRACE.search(entry["textPayload"])
    return match.group(1) if match else None


def pair(
    requests: list[dict[str, Any]], lines: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """The request-log 503s that no line excuses, and how many each kind of
    line excused."""
    available: Counter[tuple[str, str]] = Counter()
    for entry in lines:
        kind, trace = line_kind(entry), line_trace(entry)
        if kind is not None and trace is not None:
            available[(trace, kind)] += 1
    left: list[dict[str, Any]] = []
    excused: Counter[str] = Counter()
    for entry in requests:
        trace = request_trace(entry)
        kind = next(
            (kind for kind in LINE_PREFIXES if trace is not None and available[(trace, kind)] > 0),
            None,
        )
        if trace is not None and kind is not None:
            available[(trace, kind)] -= 1
            excused[kind] += 1
        else:
            left.append(entry)
    return left, excused


def unpaired(requests: list[dict[str, Any]], lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The request-log 503s that no line of the app excuses."""
    return pair(requests, lines)[0]


def _load(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        text = handle.read().strip()
    data = json.loads(text) if text else []
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"{path}: expected a JSON list of log entries")
    return data


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: billing_5xx_lease_headroom.py REQUESTS_JSON LINES_JSON", file=sys.stderr)
        return 2
    try:
        requests, lines = _load(argv[1]), _load(argv[2])
    except (OSError, ValueError) as exc:
        print(f"unreadable gate input: {exc}", file=sys.stderr)
        return 2
    left, excused = pair(requests, lines)
    kinds = ", ".join(f"{kind}: {excused[kind]}" for kind in LINE_PREFIXES)
    print(f"authorize 503s: {len(requests)}, paired with a line of the app: "
          f"{len(requests) - len(left)} ({kinds}), unpaired: {len(left)}")
    for entry in left:
        http = entry.get("httpRequest") or {}
        print(f"unpaired 503: {entry.get('timestamp')} {http.get('requestUrl')} "
              f"{http.get('status')} trace={entry.get('trace') or '-'}")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
