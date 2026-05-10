#!/usr/bin/env python3
"""Smoke-test every TR keyed provider in every region; report TTFB.

Sends a 1-token-target chat completion via each provider's /credits
endpoint at each region's attested gateway. Measures time-to-first-byte
(TTFB) — the latency until the first SSE chunk arrives, which is the
metric that matters for streaming inference UX.

Usage:
    TR_API_KEY=sk-tr-... python scripts/smoke_all_providers.py
    TR_API_KEY=sk-tr-... python scripts/smoke_all_providers.py --regions us europe

Reads TR_API_KEY from env. If not set, prints a no-op message.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import httpx

# (provider_slug, model_id) — picks one canonical-routed model per
# provider that should always be available on the keyed credential.
PROBES: list[tuple[str, str]] = [
    ("anthropic",   "anthropic/claude-haiku-4.5"),
    ("openai",      "openai/gpt-5.4-mini"),
    ("gemini",      "google/gemini-2.5-flash"),
    ("cerebras",    "meta-llama/llama-3.1-8b-instruct"),
    ("deepseek",    "deepseek/deepseek-v4-flash"),
    ("mistral",     "mistralai/mistral-small-2603"),
    ("kimi",        "moonshotai/kimi-k2.6"),
    ("zai",         "z-ai/glm-4.6"),
    ("together",    "moonshotai/kimi-k2.6"),
    ("grok",        "x-ai/grok-4.20"),
    ("novita",      "deepseek/deepseek-v4-flash"),
    ("phala",       "qwen/qwen3.5-27b"),
    ("siliconflow", "deepseek/deepseek-v4-flash"),
    ("tinfoil",     "moonshotai/kimi-k2.6"),
    ("venice",      "z-ai/glm-4.6"),
]

# Per-region enclave URLs the smoke targets. We deliberately use the
# regional hostnames (api-<region>.quillrouter.com) instead of letting
# the global LB on api.quillrouter.com pick a backend, because we
# want to *prove* each warm region's enclave is healthy. The global
# LB's geolocation routing was flattening all probe traffic into
# whichever region was geographically closest to the smoke client,
# which left other regions un-monitored.
#
# Regions are listed in the same order as TR_REGIONS in
# scripts/deploy/_lib.sh — the source of truth for "which regions
# are configured."
#
# Caveats per region:
#   - us-central1: this is the primary region. The canonical hostname
#     api.quillrouter.com points here; no separate api-us-central1
#     cert exists (would TLS-fail because the enclave's autocert
#     entry covers only the canonical name in primary, see
#     regions.py::region_payload).
#   - us-east4: regional autocert is configured (QUILL_API_HOST=
#     api-us-east4.quillrouter.com on the enclave MIG template), but
#     the cert can only be issued once the MIG has at least one
#     running instance. If the MIG is at targetSize=0 the smoke will
#     fail TLS for this region — that's a deployment-state signal,
#     not a smoke bug. Resize the MIG and re-probe.
#   - asia-northeast1, asia-southeast1, southamerica-east1: control-
#     plane only (no enclave MIG by design). They serve
#     authorize/settle from local Cloud Run instances but the
#     inference path lands on the closest warm enclave. The smoke
#     skips the enclave probe for these regions; the synthetic
#     monitor's separate /health probe via Cloud Run direct URLs
#     covers the control-plane health for them.
REGIONS = {
    "us-central1":        "https://api.quillrouter.com",
    "europe-west4":       "https://api-europe-west4.quillrouter.com",
    "us-east4":           "https://api-us-east4.quillrouter.com",
    # Aliases preserved for backward-compat with operator muscle memory.
    "us":                 "https://api.quillrouter.com",
    "europe":             "https://api-europe-west4.quillrouter.com",
}

# Regions that have a regional enclave MIG. The smoke iterates these
# by default; the aliases above are accepted via --regions for legacy
# callers but produce duplicate probes against the same backend.
ENCLAVE_REGIONS = ("us-central1", "europe-west4", "us-east4")


@dataclass
class Result:
    provider: str
    model: str
    region: str
    ttfb_ms: float | None
    total_ms: float
    status: int
    error: str = ""


def probe_one(api_key: str, region_name: str, base_url: str, provider: str, model: str) -> Result:
    """Send one streaming chat completion. Returns TTFB + total."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "say hi"}],
        "max_tokens": 8,
        "stream": True,
        "provider": {"only": [provider]},
    }
    started = time.monotonic()
    ttfb_ms: float | None = None
    try:
        with httpx.Client(timeout=30.0) as client:
            with client.stream(
                "POST",
                f"{base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                first_chunk_time = None
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    if first_chunk_time is None:
                        first_chunk_time = time.monotonic()
                        ttfb_ms = (first_chunk_time - started) * 1000.0
                if response.status_code != 200:
                    err = ""
                    try:
                        err = response.read().decode("utf-8")[:200]
                    except (httpx.HTTPError, UnicodeDecodeError):
                        # Best-effort error-body capture; if decode or
                        # the trailing read fails the smoke still returns
                        # a useful Result with status_code + ttfb.
                        pass
                    return Result(
                        provider=provider,
                        model=model,
                        region=region_name,
                        ttfb_ms=ttfb_ms,
                        total_ms=(time.monotonic() - started) * 1000.0,
                        status=response.status_code,
                        error=err,
                    )
                return Result(
                    provider=provider,
                    model=model,
                    region=region_name,
                    ttfb_ms=ttfb_ms,
                    total_ms=(time.monotonic() - started) * 1000.0,
                    status=200,
                )
    except Exception as exc:
        return Result(
            provider=provider,
            model=model,
            region=region_name,
            ttfb_ms=None,
            total_ms=(time.monotonic() - started) * 1000.0,
            status=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--regions", nargs="+", default=list(ENCLAVE_REGIONS),
        help="Regions to probe. Default: all enclave-capable regions "
             "(us-central1, europe-west4, us-east4). Pass legacy "
             "aliases ('us', 'europe') if you want the old behavior."
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="parallel requests across (provider × region) pairs",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("TR_API_KEY")
    if not api_key:
        print("ERROR: TR_API_KEY not set in env", file=sys.stderr)
        return 1

    tasks: list[tuple[str, str, str, str]] = []
    for region_name in args.regions:
        if region_name not in REGIONS:
            print(f"WARN: unknown region {region_name!r}; valid: {list(REGIONS)}", file=sys.stderr)
            continue
        base = REGIONS[region_name]
        for provider, model in PROBES:
            tasks.append((region_name, base, provider, model))

    print(f"smoke: {len(tasks)} probes ({len(PROBES)} providers × {len(args.regions)} regions)")
    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(probe_one, api_key, rn, base, prov, mdl): (rn, prov)
            for rn, base, prov, mdl in tasks
        }
        for fut in as_completed(futures):
            results.append(fut.result())

    # Sort by region, then provider order from PROBES.
    provider_order = {p: i for i, (p, _) in enumerate(PROBES)}
    results.sort(key=lambda r: (r.region, provider_order.get(r.provider, 99)))

    # Print a table.
    print()
    print(f"{'REGION':<8} {'PROVIDER':<12} {'MODEL':<35} {'STATUS':<7} {'TTFB':>9} {'TOTAL':>9}  ERROR")
    print("-" * 110)
    for r in results:
        ttfb = f"{r.ttfb_ms:.0f}ms" if r.ttfb_ms is not None else "—"
        total = f"{r.total_ms:.0f}ms"
        err = r.error[:30] if r.error else ""
        status = "OK" if r.status == 200 else f"{r.status}"
        print(f"{r.region:<8} {r.provider:<12} {r.model:<35} {status:<7} {ttfb:>9} {total:>9}  {err}")

    # Per-region p50/p95/median.
    by_region: dict[str, list[float]] = {}
    for r in results:
        if r.status == 200 and r.ttfb_ms is not None:
            by_region.setdefault(r.region, []).append(r.ttfb_ms)
    print()
    print("Per-region TTFB summary (successful probes only):")
    for region, ttfbs in sorted(by_region.items()):
        ttfbs.sort()
        if not ttfbs:
            continue
        p50 = ttfbs[len(ttfbs) // 2]
        p95 = ttfbs[int(len(ttfbs) * 0.95)]
        worst = ttfbs[-1]
        print(
            f"  {region}: n={len(ttfbs)}  p50={p50:.0f}ms  p95={p95:.0f}ms  worst={worst:.0f}ms"
        )

    # Failure summary.
    fails = [r for r in results if r.status != 200]
    if fails:
        print()
        print(f"FAILURES ({len(fails)}):")
        for r in fails:
            print(f"  {r.region}/{r.provider}: status={r.status} {r.error[:80]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
