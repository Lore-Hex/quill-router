"""Aggregate self-reported app usage for the public /apps directory.

Privacy by construction: an app appears ONLY if the caller sent a public title
(Generation.app, from the X-Title / Referer header). Untitled traffic and the
gateway default bucket as anonymous "Direct"; the synthetic monitor is excluded
entirely. We surface the app name + request/token counts only — never a
workspace, key, or any prompt content. Built from the same recent benchmark
sample set as the performance leaderboard (cached, no per-view live reads).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from trusted_router.storage_models import ProviderBenchmarkSample

# Names that are not third-party apps: empty / the gateway default bucket as
# anonymous "Direct"; the synthetic monitor is dropped from the ranking.
_DIRECT_ALIASES = frozenset({"", "trustedrouter gateway"})
_EXCLUDED_ALIASES = frozenset({"trustedrouter synthetic"})


def aggregate_apps(
    samples: list[ProviderBenchmarkSample], *, min_requests: int = 1
) -> dict[str, Any]:
    """Rank self-reported apps by recent request volume (privacy-safe)."""
    requests: dict[str, int] = defaultdict(int)
    tokens: dict[str, int] = defaultdict(int)
    direct_requests = 0
    direct_tokens = 0

    for sample in samples:
        if sample.source == "synthetic":
            continue
        name = (sample.app or "").strip()
        folded = name.casefold()
        if folded in _EXCLUDED_ALIASES:
            continue
        toks = (sample.input_tokens or 0) + (sample.output_tokens or 0)
        if folded in _DIRECT_ALIASES:
            direct_requests += 1
            direct_tokens += toks
            continue
        requests[name] += 1
        tokens[name] += toks

    apps = [
        {"name": name, "requests": requests[name], "tokens": tokens[name]}
        for name in requests
        if requests[name] >= min_requests
    ]
    # Most-used first; stable tie-break on name.
    apps.sort(key=lambda a: (-a["requests"], a["name"]))

    return {
        "apps": apps,
        "named_app_count": len(apps),
        "total_named_requests": sum(a["requests"] for a in apps),
        "direct_requests": direct_requests,
        "direct_tokens": direct_tokens,
    }
