#!/usr/bin/env python3
"""One small daily GCP rerank request using the existing monitor credential."""
from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.trustedrouter.com/v1"
MODEL = "voyage/rerank-2.5"


def probe(api_key: str) -> tuple[bool, str]:
    request = urllib.request.Request(  # noqa: S310 - fixed GCP API origin
        f"{API_BASE}/rerank",
        data=json.dumps({
            "model": MODEL,
            "query": "Which city is in France?",
            "documents": ["Paris is in France.", "Tokyo is in Japan."],
            "top_n": 2,
            "return_documents": False,
        }).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "TrustedRouter-RerankProbe/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            if response.status != 200:
                return False, "unexpected HTTP status"
            body = json.loads(response.read())
        results = body["results"]
        if len(results) != 2 or {row["index"] for row in results} != {0, 1}:
            return False, "incorrect result count or indices"
        if any(type(row["relevance_score"]) not in (int, float)
               or not math.isfinite(row["relevance_score"]) for row in results):
            return False, "invalid score"
        if results != sorted(results, key=lambda row: (-row["relevance_score"], row["index"])):
            return False, "incorrect ordering"
        usage = body["usage"]
        if not any(type(usage.get(field)) in (int, float) and math.isfinite(usage[field])
                   and usage[field] > 0
                   for field in ("cost", "cost_microdollars", "total_cost_microdollars")):
            return False, "missing positive cost"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception:  # noqa: BLE001 - content and transport details stay private
        return False, "request or response validation failed"
    return True, "two ordered results; positive cost"


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
