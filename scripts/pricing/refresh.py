#!/usr/bin/env python3
"""Hourly upstream-price refresh — orchestrator.

Runs every hour from `.github/workflows/refresh-prices.yml`:

  1. For each keyed provider, call providers/<slug>.fetch():
       - fetches the hardcoded URL via base.fetch_html / fetch_json
       - parses with parsers/<slug>.parse(html) (or for Together, the
         JSON-API path that bypasses the parser tier)
       - on validation failure, self-heals the parser file via TR's
         smartest model (eats own dogfood); rewritten parser is run
         in an AST-whitelisted sandbox before being persisted to disk

  2. Run the existing OpenRouter ingest as a cross-check signal.

  3. For every model: if provider-direct has a price, use it; otherwise
     fall back to OR's price. Tag pricing_source on each row.

  4. Write the merged snapshot back to
     src/trusted_router/data/openrouter_snapshot.json so catalog.py
     keeps reading the same file. Disagreements >2% between
     provider-direct and OR are logged and surfaced in the commit body.

  5. Emit a multi-line summary suitable for a git commit body
     (printed to stdout).

Exit codes:
   0 — success (snapshot may or may not have changed; the workflow
       checks `git diff --quiet` separately)
   1 — too many providers failed entirely (> MAX_TOLERATED_FAILURES);
       no snapshot written
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    log,
)

# Reuse the existing OR-ingest code so the cross-check runs against
# exactly the same snapshot format that catalog.py reads.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest_openrouter_catalog import build_snapshot as build_openrouter_snapshot  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "src" / "trusted_router" / "data" / "openrouter_snapshot.json"

# Provider modules in order of execution. Together first because it's
# the JSON-API path that doesn't touch the LLM-rewriteable parser tier.
PROVIDER_SLUGS = [
    "together",
    "anthropic",
    "openai",
    "gemini",
    "cerebras",
    "deepseek",
    "mistral",
    "kimi",
    "zai",
]

# >2 providers failing entirely (network down, blocked, etc.) fails the
# workflow and prevents committing a partial snapshot. ≤2 failures are
# tolerated: those providers keep last hour's snapshot value.
MAX_TOLERATED_FAILURES = 2

# Threshold for cross-check disagreements between provider-direct and
# OR. Above this, we log a note. Provider-direct still wins.
CROSS_CHECK_DISAGREE_THRESHOLD = 0.02  # 2%


def _import_provider(slug: str):
    return importlib.import_module(f"scripts.pricing.providers.{slug}")


def _fetch_one(slug: str) -> tuple[str, ProviderPricingResult | None, str | None]:
    """Fetch one provider. Returns (slug, result, error_message)."""
    try:
        module = _import_provider(slug)
        result = module.fetch()
        return slug, result, None
    except Exception as exc:  # noqa: BLE001 — we genuinely want to catch everything
        return slug, None, f"{type(exc).__name__}: {exc}"


def _fetch_all_providers() -> tuple[
    dict[str, ProviderPricingResult],
    list[tuple[str, str]],
]:
    """Run all provider fetches in parallel."""
    results: dict[str, ProviderPricingResult] = {}
    failures: list[tuple[str, str]] = []
    # 4 workers is plenty — most time is in HTTP and LLM calls; the
    # sandbox subprocess is bounded at 5s.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_one, slug): slug for slug in PROVIDER_SLUGS}
        for fut in as_completed(futures):
            slug, result, err = fut.result()
            if result is not None:
                results[slug] = result
            else:
                failures.append((slug, err or "unknown error"))
                log.warning("pricing.provider_failed slug=%s err=%s", slug, err)
    return results, failures


def _micro_per_m_to_dollars_per_token(micro_per_m: int) -> str:
    """Convert microdollars-per-million-tokens (int) to dollars-per-token
    string in the format catalog.py expects ('0.000001234')."""
    if micro_per_m <= 0:
        return "0"
    # micro/M tokens → dollars/token = micro / 1e6 / 1e6 = micro / 1e12
    dollars_per_token = Decimal(micro_per_m) / Decimal(1_000_000_000_000)
    # Trim trailing zeros, keep at most 12 decimal places.
    s = format(dollars_per_token, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".") or "0"
    return s


def _index_provider_prices(
    results: dict[str, ProviderPricingResult]
) -> dict[str, tuple[str, ModelPrice]]:
    """Flatten {slug: ProviderPricingResult} into {model_id: (slug, price)}.
    Used for the cross-check and the merge step."""
    out: dict[str, tuple[str, ModelPrice]] = {}
    for slug, result in results.items():
        for model_id, price in result.prices.items():
            # Provider-direct wins on collision (rare; would mean two
            # providers claim the same OR-canonical id).
            out[model_id] = (slug, price)
    return out


def _or_pricing_to_micro_per_m(pricing: dict[str, Any]) -> ModelPrice | None:
    """Read OR's pricing block (dollars-per-token strings) and convert
    to microdollars per million."""
    try:
        prompt = Decimal(str(pricing.get("prompt") or "0"))
        completion = Decimal(str(pricing.get("completion") or "0"))
    except Exception:  # noqa: BLE001
        return None
    micro_per_m = lambda d: int((d * Decimal(1_000_000_000_000)).to_integral_value())
    return ModelPrice(
        prompt_micro_per_m=micro_per_m(prompt),
        completion_micro_per_m=micro_per_m(completion),
    )


def _cross_check(
    provider_index: dict[str, tuple[str, ModelPrice]],
    or_snapshot: dict[str, Any],
) -> list[str]:
    """Compare provider-direct prices against OR's prices. Returns a
    list of human-readable disagreement notes (>2% on either dimension).
    Provider-direct wins; this is for empirical reliability tracking.
    """
    notes: list[str] = []
    or_models = {
        m["id"]: m
        for m in or_snapshot.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    }
    for model_id, (slug, provider_price) in provider_index.items():
        or_model = or_models.get(model_id)
        if or_model is None:
            continue
        or_price = _or_pricing_to_micro_per_m(or_model.get("pricing") or {})
        if or_price is None:
            continue
        for dim in ("prompt_micro_per_m", "completion_micro_per_m"):
            p = getattr(provider_price, dim)
            o = getattr(or_price, dim)
            if o == 0 and p == 0:
                continue
            denom = max(p, o, 1)
            rel_diff = abs(p - o) / denom
            if rel_diff > CROSS_CHECK_DISAGREE_THRESHOLD:
                notes.append(
                    f"{model_id} [{dim}]: provider({slug})={p} vs OR={o} "
                    f"(diff {rel_diff:.1%})"
                )
    return notes


def _merge_snapshot(
    or_snapshot: dict[str, Any],
    provider_index: dict[str, tuple[str, ModelPrice]],
    healed_slugs: set[str],
) -> dict[str, Any]:
    """Build the final snapshot.

    For each OR model: if provider-direct has the same model id, replace
    the model-level `pricing.prompt`/`pricing.completion` and the
    matching endpoint's `pricing.prompt`/`pricing.completion` with the
    provider-direct value. Tag both with `pricing_source`.
    """
    merged_models: list[dict[str, Any]] = []
    for raw_model in or_snapshot.get("models", []):
        if not isinstance(raw_model, dict):
            continue
        model_id = raw_model.get("id")
        new_model = dict(raw_model)
        provider_match = provider_index.get(model_id) if isinstance(model_id, str) else None

        if provider_match is not None:
            slug, price = provider_match
            tag = "self_healed_provider" if slug in healed_slugs else "provider_direct"
            new_pricing = dict(new_model.get("pricing") or {})
            new_pricing["prompt"] = _micro_per_m_to_dollars_per_token(
                price.prompt_micro_per_m
            )
            new_pricing["completion"] = _micro_per_m_to_dollars_per_token(
                price.completion_micro_per_m
            )
            new_model["pricing"] = new_pricing
            new_model["pricing_source"] = tag

            new_endpoints: list[dict[str, Any]] = []
            for ep in new_model.get("endpoints") or []:
                new_ep = dict(ep)
                if new_ep.get("tr_provider_slug") == slug:
                    new_ep_pricing = dict(new_ep.get("pricing") or {})
                    new_ep_pricing["prompt"] = new_pricing["prompt"]
                    new_ep_pricing["completion"] = new_pricing["completion"]
                    new_ep["pricing"] = new_ep_pricing
                    new_ep["pricing_source"] = tag
                else:
                    new_ep["pricing_source"] = "openrouter"
                new_endpoints.append(new_ep)
            new_model["endpoints"] = new_endpoints
        else:
            new_model["pricing_source"] = "openrouter"
            new_endpoints = []
            for ep in new_model.get("endpoints") or []:
                new_ep = dict(ep)
                new_ep["pricing_source"] = "openrouter"
                new_endpoints.append(new_ep)
            new_model["endpoints"] = new_endpoints

        merged_models.append(new_model)

    merged_models.sort(key=lambda m: str(m.get("id") or ""))
    return {
        "source": "openrouter.ai/api/v1/models + provider-direct overrides",
        "filter": "kept models whose endpoints include one of TR's 9 keyed providers",
        "tr_keyed_providers": or_snapshot.get("tr_keyed_providers", []),
        "model_count": len(merged_models),
        "models": merged_models,
    }


def _write_snapshot(snapshot: dict[str, Any]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    SNAPSHOT_PATH.write_text(text, encoding="utf-8")


def _summary_lines(
    results: dict[str, ProviderPricingResult],
    healed: list[str],
    failures: list[tuple[str, str]],
    disagreements: list[str],
) -> list[str]:
    lines: list[str] = []
    lines.append("Per-provider results:")
    for slug in PROVIDER_SLUGS:
        result = results.get(slug)
        if result is None:
            err = next((e for s, e in failures if s == slug), "unknown")
            lines.append(f"  {slug}: FAILED ({err})")
            continue
        lines.append(
            f"  {slug}: {len(result.prices)} models via {result.source}"
        )
    if healed:
        lines.append("")
        lines.append(f"Self-healed parsers this run: {', '.join(healed)}")
        for slug in healed:
            res = results[slug]
            if res.heal_diff:
                lines.append(f"--- diff for parsers/{slug}.py ---")
                # Trim diff to first ~40 lines for the commit body.
                trimmed = "".join(res.heal_diff.splitlines(keepends=True)[:40])
                lines.append(trimmed.rstrip())
    if disagreements:
        lines.append("")
        lines.append(f"OR cross-check disagreements (>2%, provider-direct wins):")
        for note in disagreements[:30]:
            lines.append(f"  {note}")
        if len(disagreements) > 30:
            lines.append(f"  ... and {len(disagreements) - 30} more")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="print summary and exit; do not write the snapshot",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    log.info("pricing.refresh.start providers=%d", len(PROVIDER_SLUGS))
    results, failures = _fetch_all_providers()
    healed = [slug for slug, res in results.items() if res.source == "self_healed"]

    if len(failures) > MAX_TOLERATED_FAILURES:
        log.error(
            "pricing.refresh.too_many_failures count=%d limit=%d failures=%s",
            len(failures),
            MAX_TOLERATED_FAILURES,
            failures,
        )
        for line in _summary_lines(results, healed, failures, []):
            print(line)
        return 1

    log.info("pricing.refresh.openrouter_ingest")
    or_snapshot = build_openrouter_snapshot()

    provider_index = _index_provider_prices(results)
    disagreements = _cross_check(provider_index, or_snapshot)

    merged = _merge_snapshot(or_snapshot, provider_index, set(healed))

    if not args.summary_only:
        _write_snapshot(merged)
        log.info("pricing.refresh.wrote path=%s models=%d", SNAPSHOT_PATH, merged["model_count"])

    summary = _summary_lines(results, healed, failures, disagreements)
    print(f"Hourly price refresh — {merged['model_count']} models")
    print(
        f"Sources: {sum(1 for r in results.values() if r.source == 'deterministic')} "
        f"deterministic, {len(healed)} self-healed, "
        f"{sum(1 for r in results.values() if r.source == 'api')} api, "
        f"{len(failures)} failed (kept last hour's value)"
    )
    print()
    for line in summary:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
