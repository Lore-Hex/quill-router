#!/usr/bin/env python3
"""Classify a provider's prepaid routes as DEAD (config) vs FLAKY (real health).

A provider's published catalog can list models its chat API doesn't actually
serve on our operator key — those return 401/403/404 or "model/deployment not
found" and should be added to `_PROVIDER_UNSERVED_CREDITS_MODELS[provider]` in
catalog.py so prepaid traffic never routes there. Transient failures (429,
5xx, timeouts) are REAL provider health and must NOT be denylisted — the
metric (`_excluded_from_uptime` in synthetic/leaderboard.py) already filters
the config class out of uptime; this tool tells you which routes to also stop
routing to.

This is the verification step the catalog comments (see the `novita` entry)
require before denylisting. It hits the live gateway, so it needs a key:

    export TR_SMOKE_KEY=$(gcloud secrets versions access latest \
        --secret=trustedrouter-synthetic-monitor-api-key --project=quill-cloud-proxy)
    uv run python scripts/classify_provider_routes.py parasail

Prints a per-route table and a ready-to-paste frozenset of DEAD routes.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

from trusted_router.synthetic.probes import rotation_candidates

API_BASE = os.environ.get("TR_API_BASE", "https://api.trustedrouter.com").rstrip("/")
KEY = os.environ.get("TR_SMOKE_KEY") or os.environ.get("TR_API_KEY")
# 401/403/404 = the provider does not serve this route on our key (config).
_DEAD_STATUSES = frozenset({400, 401, 403, 404, 422})
_DEAD_MARKERS = (
    "not found",
    "does not exist",
    "deployment",
    "not available",
    "unavailable",
    "not supported",
    "no endpoint",
    "unknown model",
)


def _classify(status: int | None, body: str) -> str:
    low = body.casefold()
    if status == 200:
        return "ok"
    if status in _DEAD_STATUSES or any(m in low for m in _DEAD_MARKERS):
        return "dead"
    return "flaky"  # 429 / 5xx / timeout / network — real provider health


def probe_route(client: httpx.Client, provider: str, model: str) -> tuple[str, int | None, str]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 4,
        "provider": {"only": [provider]},
    }
    try:
        r = client.post(
            f"{API_BASE}/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=30.0,
        )
        return _classify(r.status_code, r.text), r.status_code, r.text[:120]
    except httpx.HTTPError as exc:  # network / timeout = real health
        return "flaky", None, str(exc)[:120]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    provider = argv[0] if argv else "parasail"
    if not KEY:
        print("error: set TR_SMOKE_KEY (or TR_API_KEY) to a gateway key", file=sys.stderr)
        return 2
    models = sorted(rotation_candidates().get(provider, []))
    if not models:
        print(f"no prepaid routes for provider '{provider}'", file=sys.stderr)
        return 1

    dead: list[str] = []
    print(f"probing {len(models)} prepaid routes for '{provider}' against {API_BASE}\n")
    with httpx.Client() as client:
        for model in models:
            verdict, status, detail = probe_route(client, provider, model)
            mark = {"ok": "  ok ", "dead": "DEAD ", "flaky": "flaky"}[verdict]
            print(f"  {mark} [{status or '---'}] {model}  {detail if verdict != 'ok' else ''}".rstrip())
            if verdict == "dead":
                dead.append(model)
            time.sleep(1.5)  # serialize to avoid tripping 429s

    print(f"\n{len(dead)} dead / {len(models)} routes")
    if dead:
        print("\n# Paste into _PROVIDER_UNSERVED_CREDITS_MODELS in catalog.py:")
        inner = ", ".join(f'"{m}"' for m in dead)
        print(f'    "{provider}": frozenset({{{inner}}}),')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
