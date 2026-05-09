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
import os
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
    # New backends added 2026-05-08. Each has a Jina-rendered or
    # direct-fetch pricing source + a parser in scripts/pricing/parsers/.
    "grok",
    "novita",
    "phala",
    "siliconflow",
    "tinfoil",
    "venice",
]

# >N providers failing entirely (network down, blocked, etc.) fails
# the workflow and prevents committing a partial snapshot. ≤N failures
# are tolerated: those providers keep last hour's snapshot value.
#
# Default 2 of 9 (~22%) is a guess. Tune via TR_PRICING_MAX_FAILURES
# env var once we have a few weeks of empirical failure-rate data.
# Numbers we'd expect from observation:
#   - blockable scrapers (OpenAI 403 to bot UA): ~rare with real UA
#   - DNS hiccups / TLS handshake failures: ~1-2% per provider per run
#   - LLM self-heal that the AST gate or sandbox rejects: rare-but-real
# Set higher (e.g. 4) if observed failure rate is steady at ~30%; set
# lower (e.g. 0) if we want strict "all-or-nothing" hourly refreshes.
MAX_TOLERATED_FAILURES = int(os.environ.get("TR_PRICING_MAX_FAILURES", "2"))

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

    def _micro_per_m(d: Decimal) -> int:
        return int((d * Decimal(1_000_000_000_000)).to_integral_value())

    return ModelPrice(
        prompt_micro_per_m=_micro_per_m(prompt),
        completion_micro_per_m=_micro_per_m(completion),
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


def _cross_check_ids(
    results: dict[str, ProviderPricingResult],
    or_snapshot: dict[str, Any],
) -> list[str]:
    """Compare the set of model IDs each provider-direct parser produced
    against the set of model IDs OR knows for that provider. Surfaces:

    * Models OR has for this slug that our parser did NOT find (parser
      is incomplete — likely missed a row, or page hides legacy SKUs).
    * Models our parser found that OR does NOT know (legitimate new
      model OR hasn't picked up yet, OR a parser hallucination/typo).

    This is informational only — the workflow does not fail on
    mismatches. The hardcoded `EXPECTED_MODELS` list per provider
    remains the strict floor that triggers self-heal on validation
    failure; this function operates on the looser "OR catalog vs
    page reality" comparison.
    """
    notes: list[str] = []

    # Build {slug: set(or_canonical_ids)} from OR snapshot. A model
    # belongs to a slug if any of its endpoints is keyed to that slug.
    or_by_slug: dict[str, set[str]] = {s: set() for s in PROVIDER_SLUGS}
    for raw_model in or_snapshot.get("models", []):
        if not isinstance(raw_model, dict):
            continue
        model_id = raw_model.get("id")
        if not isinstance(model_id, str):
            continue
        for ep in raw_model.get("endpoints") or []:
            if not isinstance(ep, dict):
                continue
            slug = ep.get("tr_provider_slug")
            if isinstance(slug, str) and slug in or_by_slug:
                or_by_slug[slug].add(model_id)

    # Build {slug: set(model_ids_returned_by_parser)} from results.
    provider_by_slug: dict[str, set[str]] = {
        slug: set(result.prices.keys()) for slug, result in results.items()
    }

    for slug in PROVIDER_SLUGS:
        or_set = or_by_slug.get(slug, set())
        provider_set = provider_by_slug.get(slug, set())
        if not provider_set and not or_set:
            continue
        only_or = or_set - provider_set
        only_provider = provider_set - or_set
        if only_or:
            sample = sorted(only_or)[:5]
            extra = f" (+{len(only_or) - 5} more)" if len(only_or) > 5 else ""
            notes.append(
                f"{slug}: OR knows {len(only_or)} model id(s) the parser did "
                f"not find: {sample}{extra}"
            )
        if only_provider:
            sample = sorted(only_provider)[:5]
            extra = (
                f" (+{len(only_provider) - 5} more)"
                if len(only_provider) > 5
                else ""
            )
            notes.append(
                f"{slug}: parser found {len(only_provider)} model id(s) OR "
                f"does not list: {sample}{extra}"
            )
    return notes


def _price_to_pricing_block(price: ModelPrice) -> dict[str, Any]:
    """Render a ModelPrice into the snapshot's `pricing` block. The
    headline (low-tier) rate is exposed as `pricing.prompt` /
    `pricing.completion` / `pricing.input_cache_read` for back-compat
    with consumers (and catalog.py) that read flat fields. When a
    model has multiple tiers, also emit `pricing.prompt_tiers` /
    `pricing.completion_tiers` arrays so the billing path can pick
    the right rate per request."""
    headline = price.tiers[0]
    block: dict[str, Any] = {
        "prompt": _micro_per_m_to_dollars_per_token(headline.prompt_micro_per_m),
        "completion": _micro_per_m_to_dollars_per_token(
            headline.completion_micro_per_m
        ),
    }
    if headline.prompt_cached_micro_per_m is not None:
        # Field name `input_cache_read` matches OR's snapshot convention
        # (and Anthropic's own pricing block) so consumers that read
        # the OR-shaped format don't need to learn a new key.
        block["input_cache_read"] = _micro_per_m_to_dollars_per_token(
            headline.prompt_cached_micro_per_m
        )
    if len(price.tiers) > 1:
        block["prompt_tiers"] = [
            {
                "max_prompt_tokens": t.max_prompt_tokens,
                "prompt": _micro_per_m_to_dollars_per_token(t.prompt_micro_per_m),
                **(
                    {
                        "input_cache_read": _micro_per_m_to_dollars_per_token(
                            t.prompt_cached_micro_per_m
                        )
                    }
                    if t.prompt_cached_micro_per_m is not None
                    else {}
                ),
            }
            for t in price.tiers
        ]
        block["completion_tiers"] = [
            {
                "max_prompt_tokens": t.max_prompt_tokens,
                "completion": _micro_per_m_to_dollars_per_token(
                    t.completion_micro_per_m
                ),
            }
            for t in price.tiers
        ]
    return block


def _merge_snapshot(
    or_snapshot: dict[str, Any],
    provider_index: dict[str, tuple[str, ModelPrice]],
    healed_slugs: set[str],
) -> dict[str, Any]:
    """Build the final snapshot.

    Policy: only models we have provider-direct prices for are in the
    snapshot. OR is a cross-check signal, never a billing source — TR
    routes directly to each provider (Anthropic, OpenAI, Gemini, etc.)
    using TR's own keys, so prices MUST come from provider-direct
    parsers. Anything OR-only falls out of the catalog by design.

    For each provider-direct (model_id, price) pair we look up the OR
    snapshot row to inherit non-pricing metadata (display name,
    description, context_length, supported_parameters, endpoint shape).
    OR-only models are dropped silently — they get listed in the
    cross-check ID-mismatch notes for visibility.
    """
    or_by_id = {
        m["id"]: m
        for m in or_snapshot.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    }
    merged_models: list[dict[str, Any]] = []
    for model_id, (slug, price) in provider_index.items():
        or_model = or_by_id.get(model_id)
        if or_model is None:
            # Provider gave us a price for a model OR doesn't list. We
            # have no endpoint metadata, so we can't construct a valid
            # catalog entry. Skip — the cross-check note already flags
            # this case ("parser found N models OR doesn't list").
            continue
        new_model = dict(or_model)
        new_pricing = dict(new_model.get("pricing") or {})
        new_pricing.update(_price_to_pricing_block(price))
        new_model["pricing"] = new_pricing
        tag = "self_healed_provider" if slug in healed_slugs else "provider_direct"
        new_model["pricing_source"] = tag

        new_endpoints: list[dict[str, Any]] = []
        for ep in new_model.get("endpoints") or []:
            new_ep = dict(ep)
            # Only the matching-slug endpoint gets the new prices; we
            # don't have provider-direct prices for non-keyed endpoints.
            # But we keep them — TR may route to them later when we add
            # support, and OR's pricing for those endpoints is just
            # informational on the snapshot.
            if new_ep.get("tr_provider_slug") == slug:
                new_ep_pricing = dict(new_ep.get("pricing") or {})
                new_ep_pricing.update(_price_to_pricing_block(price))
                new_ep["pricing"] = new_ep_pricing
                new_ep["pricing_source"] = tag
            else:
                # Drop non-keyed endpoints from the snapshot — TR can't
                # route to them. Keeping them would leak OR's price as
                # part of the per-endpoint detail and confuse the
                # billing path.
                continue
            new_endpoints.append(new_ep)
        if not new_endpoints:
            # Edge case: provider-direct has the model but OR records
            # only non-matching endpoints for it. Without a routable
            # endpoint we can't list the model — drop.
            continue
        new_model["endpoints"] = new_endpoints
        merged_models.append(new_model)

    merged_models.sort(key=lambda m: str(m.get("id") or ""))
    return {
        "source": (
            "provider-direct (anthropic.com, openai.com, ai.google.dev, ...) "
            "with openrouter.ai used only for cross-check sanity"
        ),
        "filter": (
            "kept ONLY models with provider-direct prices; OR-only models are "
            "dropped (TR never routes via OR — every model in this snapshot is "
            "billable at the price set by the provider's own pricing page)"
        ),
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
    id_mismatches: list[str],
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
    if id_mismatches:
        lines.append("")
        lines.append("ID mismatches between provider parser and OR catalog:")
        for note in id_mismatches[:20]:
            lines.append(f"  {note}")
        if len(id_mismatches) > 20:
            lines.append(f"  ... and {len(id_mismatches) - 20} more")
    if disagreements:
        lines.append("")
        lines.append("OR cross-check price disagreements (>2%, provider-direct wins):")
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
        for line in _summary_lines(results, healed, failures, [], []):
            print(line)
        return 1

    log.info("pricing.refresh.openrouter_ingest")
    or_snapshot = build_openrouter_snapshot()

    provider_index = _index_provider_prices(results)
    disagreements = _cross_check(provider_index, or_snapshot)
    id_mismatches = _cross_check_ids(results, or_snapshot)

    merged = _merge_snapshot(or_snapshot, provider_index, set(healed))

    if not args.summary_only:
        _write_snapshot(merged)
        log.info("pricing.refresh.wrote path=%s models=%d", SNAPSHOT_PATH, merged["model_count"])

    summary = _summary_lines(results, healed, failures, disagreements, id_mismatches)
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
