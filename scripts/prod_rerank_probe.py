#!/usr/bin/env python3
"""One small daily GCP rerank request using the existing monitor credential."""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
from typing import Any

API_BASE = "https://api.trustedrouter.com/v1"
MODEL = "voyage/rerank-2.5"
PROVIDER = "voyage"
QUERY = "Which city is in France?"
# The matching document is second, so a response that leaves the input order
# alone (or scores everything the same) cannot pass.
DOCUMENTS = ["Tokyo is in Japan.", "Paris is in France."]
RELEVANT_INDEX = 1
TIMEOUT_SECONDS = 30
MAX_BODY_BYTES = 65_536
SUCCESS_DETAIL = "relevant document first; positive cost"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The credential goes to the fixed origin only; a redirect is a failure."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def check_body(body: Any) -> str | None:
    """Return a fixed label for the first failed check, or None when all hold."""
    if not isinstance(body, dict):
        return "response is not an object"
    if type(body.get("id")) is not str or not body["id"]:
        return "missing response id"
    if body.get("model") != MODEL or body.get("provider") != PROVIDER:
        return "unexpected model or provider"
    results = body.get("results")
    if type(results) is not list or len(results) != len(DOCUMENTS):
        return "incorrect result count"
    if not all(isinstance(row, dict) for row in results):
        return "malformed result"
    if any(type(row.get("index")) is not int for row in results):
        return "invalid index"
    if sorted(row["index"] for row in results) != list(range(len(DOCUMENTS))):
        return "incorrect indices"
    if not all(_is_number(row.get("relevance_score")) for row in results):
        return "invalid score"
    if results != sorted(results, key=lambda row: (-row["relevance_score"], row["index"])):
        return "incorrect ordering"
    # With ties ordered by index, the second document can only be first when
    # its score is strictly higher.
    if results[0]["index"] != RELEVANT_INDEX:
        return "unexpected top result"
    if any("document" in row for row in results):
        return "document returned when not requested"
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return "missing usage"
    if not _is_positive_int(usage.get("total_tokens")):
        return "missing token count"
    cost = usage.get("cost_microdollars")
    if not _is_positive_int(cost) or usage.get("total_cost_microdollars") != cost:
        return "missing positive cost"
    return None


def probe(api_key: str) -> tuple[bool, str]:
    try:
        request = urllib.request.Request(  # noqa: S310 - fixed GCP API origin
            f"{API_BASE}/rerank",
            data=json.dumps({
                "model": MODEL,
                "query": QUERY,
                "documents": DOCUMENTS,
                "top_n": len(DOCUMENTS),
                "return_documents": False,
            }).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "User-Agent": "TrustedRouter-RerankProbe/1.0"},
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return False, "unexpected HTTP status"
            raw = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {int(exc.code)}"
    except Exception:  # noqa: BLE001 - transport details stay private
        return False, "request failed"
    if len(raw) > MAX_BODY_BYTES:
        return False, "response too large"
    try:
        body = json.loads(raw)
    except ValueError:
        return False, "response is not JSON"
    failure = check_body(body)
    if failure is not None:
        return False, failure
    return True, SUCCESS_DETAIL


def main() -> int:
    api_key = os.environ.get("TR_MONITOR_API_KEY", "")
    if not api_key:
        print("FATAL: TR_MONITOR_API_KEY is not set", file=sys.stderr)
        return 2
    ok, detail = probe(api_key)
    print(f"Rerank {'PASS' if ok else 'FAIL'}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
