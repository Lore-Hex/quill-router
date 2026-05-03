#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trusted_router.openrouter_coverage import coverage_map


def main() -> int:
    spec = httpx.get("https://openrouter.ai/openapi.json", timeout=30).json()
    methods: set[tuple[str, str]] = set()
    for path, item in spec["paths"].items():
        if item is None:
            continue
        for method in item:
            upper = method.upper()
            if upper in {"GET", "POST", "PATCH", "DELETE", "PUT"}:
                methods.add((path, upper))
    expected = set(coverage_map())
    missing = methods - expected
    extra = expected - methods
    if missing or extra:
        print("OpenRouter coverage drift detected", file=sys.stderr)
        if missing:
            print("Missing classifications:", sorted(missing), file=sys.stderr)
        if extra:
            print("Classified but absent upstream:", sorted(extra), file=sys.stderr)
        return 1
    print(f"OpenRouter coverage OK: {len(methods)} path/method pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
