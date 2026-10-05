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
   1 — a majority of providers failed, or a publication safety gate failed;
       all published files kept unchanged
"""

from __future__ import annotations

import argparse
import atexit
import importlib
import json
import logging
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path
from typing import Any

from scripts.check_price_spike import spiking_providers
from scripts.pricing.base import (
    PARSERS_DIR,
    ModelPrice,
    PriceTier,
    ProviderPricingResult,
    configure_runtime_required_models,
    guard_manifest_prune,
    log,
    parser_path,
    read_stale_provider_manifest,
    safe_exception_summary,
)

# Reuse the existing OR-ingest code so the cross-check runs against
# exactly the same snapshot format that catalog.py reads.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingest_openrouter_catalog import build_snapshot as build_openrouter_snapshot  # noqa: E402

from trusted_router.provider_lifecycle import provider_model_retired  # noqa: E402
from trusted_router.provider_manifest_policy import (  # noqa: E402
    EXPIRING_PROVIDER_MANIFEST_SLUGS,
)

HELD_FOR_REVIEW_HEADING = "Held for price review (last published prices kept):"

# Retiring particular models must not disable discovery for an entire provider.
RETIRED_PROVIDER_SLUGS: frozenset[str] = frozenset()

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "src" / "trusted_router" / "data" / "openrouter_snapshot.json"
PROVIDER_MANIFEST_DIR = SNAPSHOT_PATH.parent / "provider_models"

# Provider modules in order of execution. Together stays first because existing
# refresh contracts depend on it; Meta is another JSON API path that does not
# touch the LLM-rewriteable parser tier.
PROVIDER_SLUGS = [
    "together",
    # Meta Muse is served through OpenRouter, so OpenRouter is the provider API
    # and billing source for this one explicitly labelled downstream route.
    "meta",
    # Laguna S 2.1 is an explicit one-model OpenRouter route: the module
    # name and the runtime slug are both "openrouter".
    "openrouter",
    "anthropic",
    "openai",
    "gemini",
    "google_vertex",
    "cerebras",
    "deepseek",
    "mistral",
    "kimi",
    "zai",
    "fireworks",
    # New backends added 2026-05-08. Each has a provider-owned pricing source
    # plus a deterministic parser in scripts/pricing/parsers/.
    "grok",
    "novita",
    "phala",
    "siliconflow",
    "tinfoil",
    "near_ai",
    "venice",
    # 2026-05-11 batch — three new providers that all serve
    # google/gemma-4-31b-it. Lightning + GMI publish per-model
    # pricing in /v1/models (API-direct, no parser file needed).
    # Parasail's pricing is hand-maintained in providers/parasail.py
    # because their public page is paywalled.
    "parasail",
    "lightning",
    "gmi",
    "deepinfra",
    "friendli",
    "baseten",
    "thinkingmachines",
    "wafer",
    "crusoe",
    "minimax",
    "nebius",
    "xiaomi",
    "alibaba",
    "azure",
    "makora",
    "telnyx",
    "chutes",
    "digitalocean",
    "cloudflare_workers_ai",
    "inceptron",
    "morph",
    "atlas_cloud",
    "streamlake",
    "neurometric",
    "engy",
    "pearl",
    "stepfun",
    "relace",
    "recraft",
    "bfl",
    "decart",
    "nvidia_nim",
    "databricks",
    "upstage",
    "sail_research",
    "reka",
    "nextbit",
    "akashml",
    "mancer",
    "abliterate",
    "aion_labs",
    "sambanova",
    "arcee",
    "inception",
    "io_net",
    "tencent",
    "scaleway",
    "regolo",
    "lyceum",
    "byteplus",
    "privatemode",
    "featherless",
    "sakana",
    "jina",
    "wandb",
    "nscale",
    "confidential_ai",
    "scaledown",
    "perplexity",
    "krea",
    "fal",
    # 0G Private Computer publishes exact per-route prices and trust metadata
    # in its public marketplace hydration data. The adapter admits only
    # healthy TeeML/private chat routes and keeps them dark until a keyed PONG.
    "zero_g",
    # First-party embedding providers. Their parsers feed committed provider
    # manifests that the runtime embedding catalog reads directly.
    "cohere",
    "voyage",
    # Input-only decision model read from Vercel's public model list.
    "vercel_ai_gateway",
    # The same decision model at its vendor, read from TypeSafe's models page.
    "typesafe",
    "system1models",
    "system1models_eu",
]

# Product adapters own independent availability and prices. Aliases here map
# Python module names to public provider identities, never across products.
_PRICING_RESULT_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "system1models_eu": ("system1models-eu",),
    "confidential_ai": ("confidential-ai",),
    "gemini": ("google-ai-studio",),
    "google_vertex": ("google-vertex",),
    "cloudflare_workers_ai": ("cloudflare-workers-ai",),
    "atlas_cloud": ("atlas-cloud",),
    "zero_g": ("zero-g",),
    "nvidia_nim": ("nvidia-nim",),
    "sail_research": ("sail-research",),
    "aion_labs": ("aion-labs",),
    "vercel_ai_gateway": ("vercel-ai-gateway",),
    "io_net": ("io-net",),
    "tencent": ("tencent",),
    "near_ai": ("near-ai",),
}

# These providers run a pricing-page parser (Kimi has a custom multi-page
# wrapper around the same parser contract). A new model seen in a fresh
# upstream catalog is a strict parser requirement only for this set; API-priced
# providers such as Cerebras, Makora, and Phala discover prices directly.
_SELF_HEALING_PARSER_SLUGS = frozenset(
    {
        "anthropic",
        "cohere",
        "deepseek",
        "fireworks",
        "gemini",
        "kimi",
        "minimax",
        "mistral",
        "morph",
        "novita",
        "openai",
        "siliconflow",
        "streamlake",
        "thinkingmachines",
        "telnyx",
        "voyage",
        "xiaomi",
        "zai",
    }
)

# Vertex-native manifests publish independently canaried routes. Do not invent
# additional Vertex endpoints from the third-party snapshot's model metadata.
_NO_SYNTHETIC_ENDPOINT_PROVIDER_SLUGS = frozenset({"google-vertex"})

# Threshold for cross-check disagreements between provider-direct and
# OR. Above this, we log a note. Provider-direct still wins.
CROSS_CHECK_DISAGREE_THRESHOLD = 0.02  # 2%

# OpenRouter is the actual serving and billing API for this deliberately
# labelled downstream provider. Keep its provenance distinct from both direct
# provider prices and emergency OpenRouter fallback prices.
_OPENROUTER_BACKED_PROVIDER_SLUGS = frozenset({"meta", "openrouter"})


def _endpoint_pricing_source(slug: str, healed_slugs: set[str]) -> str:
    source_slug = next(
        (
            result_slug
            for result_slug, provider_slugs in _PRICING_RESULT_PROVIDER_ALIASES.items()
            if slug in provider_slugs
        ),
        slug,
    )
    if source_slug in healed_slugs:
        return "self_healed_provider"
    if slug in _OPENROUTER_BACKED_PROVIDER_SLUGS:
        return "openrouter_provider"
    return "provider_direct"


def _import_provider(slug: str):
    return importlib.import_module(f"scripts.pricing.providers.{slug}")


def _copy_published_prices() -> Path:
    """Copy the published snapshot, manifests and parsers before this run writes any."""
    baseline = Path(tempfile.mkdtemp(prefix="pricing-baseline-"))
    atexit.register(shutil.rmtree, baseline, ignore_errors=True)
    shutil.copyfile(SNAPSHOT_PATH, baseline / SNAPSHOT_PATH.name)
    shutil.copytree(PROVIDER_MANIFEST_DIR, baseline / PROVIDER_MANIFEST_DIR.name)
    shutil.copytree(PARSERS_DIR, baseline / PARSERS_DIR.name)
    return baseline


def _restore_all_published_files(baseline: Path) -> None:
    """Roll back generated data and self-healed parsers on a rejected run."""
    shutil.copyfile(baseline / SNAPSHOT_PATH.name, SNAPSHOT_PATH)
    for target in (PROVIDER_MANIFEST_DIR, PARSERS_DIR):
        published = baseline / target.name
        for path in target.iterdir():
            if path.is_file() and not (published / path.name).exists():
                path.unlink()
        shutil.copytree(published, target, dirs_exist_ok=True)


def _spiking_results(
    baseline: Path,
    results: dict[str, ProviderPricingResult],
    held: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Freshly refreshed providers whose published routes now fail the spike gate."""
    try:
        spiking = spiking_providers(
            baseline / SNAPSHOT_PATH.name,
            SNAPSHOT_PATH,
            baseline / PROVIDER_MANIFEST_DIR.name,
            PROVIDER_MANIFEST_DIR,
        )
    except Exception as exc:  # noqa: BLE001 - the workflow gate reports unusable input
        log.warning("pricing.spike_hold_skipped error=%s", safe_exception_summary(exc))
        return {}
    hold: dict[str, list[str]] = {}
    for provider, routes in spiking.items():
        slug = _result_slug_for_provider(provider) if provider else None
        result = results.get(slug) if slug else None
        if result is not None and slug not in held and not result.source.startswith("stale_"):
            hold.setdefault(slug, []).extend(routes)
    return hold


def _held_route(model: dict[str, Any], endpoint: Any, held: dict[str, list[str]]) -> str | None:
    """The route key of a held provider's snapshot endpoint, or None for any other endpoint."""
    if not isinstance(endpoint, dict):
        return None
    provider = endpoint.get("tr_provider_slug")
    if not isinstance(provider, str) or _result_slug_for_provider(provider) not in held:
        return None
    return f"{model.get('id')} [{provider}:{endpoint.get('tag') or ''}:{endpoint.get('model_id')}]"


# The keys a provider's own price sets in an endpoint block (see
# _price_to_pricing_block). Snapshots published before 2026-09-28 also carry
# OpenRouter's other keys (discount, web_search, input_cache_write, ...), which
# nothing reads and the merge no longer publishes; a hold compares prices only.
_PROVIDER_PRICING_KEYS = frozenset(
    {"prompt", "completion", "input_cache_read", "prompt_tiers", "completion_tiers"}
)


def _held_endpoint_pricing(snapshot: Any, held: dict[str, list[str]]) -> dict[str, list[str]]:
    """Every held provider's snapshot endpoint prices, grouped by route.

    Nothing stops two endpoints from sharing a route key, so each key keeps
    all of its pricing blocks rather than the last one.
    """
    out: dict[str, list[str]] = {}
    models = snapshot.get("models") if isinstance(snapshot, dict) else None
    for model in models or []:
        if not isinstance(model, dict):
            continue
        for endpoint in model.get("endpoints") or []:
            route = _held_route(model, endpoint, held)
            if route is None:
                continue
            pricing = endpoint.get("pricing")
            if isinstance(pricing, dict):
                pricing = {key: pricing[key] for key in sorted(pricing) if key in _PROVIDER_PRICING_KEYS}
            out.setdefault(route, []).append(json.dumps(pricing, sort_keys=True))
    return {route: sorted(blocks) for route, blocks in out.items()}


def _published_disabled_held_routes(
    baseline: Path, snapshot: dict[str, Any], held: dict[str, list[str]]
) -> set[str]:
    """Prove which published snapshot routes were already excluded at runtime.

    Use the baseline manifest, never fresh discovery: a new canary failure
    must not authorize a change to an otherwise exact price hold. Absence or
    unreadable evidence is not proof that a previously published route is dark.
    """
    disabled_models: dict[str, set[str]] = {}
    for slug in held:
        manifest = getattr(_import_provider(slug), "MANIFEST_PATH", None)
        if manifest is None:
            continue
        try:
            raw = json.loads((baseline / PROVIDER_MANIFEST_DIR.name / Path(manifest).name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        provider_slugs = _PRICING_RESULT_PROVIDER_ALIASES.get(slug, (slug,))
        rows = raw.get("models") if isinstance(raw, dict) and raw.get("provider") in provider_slugs else None
        if isinstance(rows, list):
            disabled_models[slug] = {
                row["id"] for row in rows
                if isinstance(row, dict) and isinstance(row.get("id"), str) and row.get("routable") is False
            }

    disabled_routes: set[str] = set()
    for model in snapshot.get("models") or []:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        for endpoint in model.get("endpoints") or []:
            route = _held_route(model, endpoint, held)
            if route is None:
                continue
            provider = endpoint["tr_provider_slug"]
            if model["id"] in disabled_models.get(_result_slug_for_provider(provider), set()) or provider_model_retired(
                provider, model["id"], endpoint.get("model_id")
            ):
                disabled_routes.add(route)
    return disabled_routes


def _held_routes_changed(baseline: Path, held: dict[str, list[str]]) -> list[str]:
    """Held providers' routes that differ from what was published, if any.

    A hold may only publish when it kept every held provider exactly as
    published: the same snapshot endpoints with the same prices (prompt,
    completion, cached input and tiers), and byte-identical manifests. Only
    removal of an already disabled/retired route is safe: runtime had already
    excluded it, and stale recovery must not revive it to satisfy this guard.
    Anything else is reported so the affected models can keep their published rows.
    """
    snapshot = json.loads((baseline / SNAPSHOT_PATH.name).read_text(encoding="utf-8"))
    published = _held_endpoint_pricing(snapshot, held)
    now = _held_endpoint_pricing(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")), held)
    disabled = _published_disabled_held_routes(baseline, snapshot, held)
    changed = [
        route for route in sorted(set(published) | set(now))
        if published.get(route) != now.get(route) and not (route in disabled and route not in now)
    ]
    for slug in held:
        manifest_path_value = getattr(_import_provider(slug), "MANIFEST_PATH", None)
        if manifest_path_value is None:
            continue
        target = PROVIDER_MANIFEST_DIR / Path(manifest_path_value).name
        before = baseline / PROVIDER_MANIFEST_DIR.name / target.name
        before_bytes = before.read_bytes() if before.exists() else None
        after_bytes = target.read_bytes() if target.exists() else None
        if before_bytes != after_bytes:
            changed.append(f"{target.name} (manifest)")
    return changed


def _restore_published_files(baseline: Path, slug: str) -> None:
    """Put back the provider's manifest and parser exactly as last published."""
    # Write through this module's directories. In production they are the same
    # files as MANIFEST_PATH and parser_path(); in tests they point at a temp
    # copy, so a restore can never overwrite the repository's real files.
    manifest_path_value = getattr(_import_provider(slug), "MANIFEST_PATH", None)
    if manifest_path_value is not None:
        target = PROVIDER_MANIFEST_DIR / Path(manifest_path_value).name
        published = baseline / PROVIDER_MANIFEST_DIR.name / target.name
        if published.exists():
            shutil.copyfile(published, target)
        else:
            target.unlink(missing_ok=True)
    # A self-healed parser that produced the spike must not be committed.
    live_parser = PARSERS_DIR / parser_path(slug).name
    published_parser = baseline / PARSERS_DIR.name / live_parser.name
    if published_parser.exists():
        shutil.copyfile(published_parser, live_parser)
    else:
        live_parser.unlink(missing_ok=True)


def _keep_failed_snapshot_routes(
    merged: dict[str, Any], published: dict[str, Any], failed: dict[str, list[str]],
    *, disabled_routes: set[str] | None = None,
) -> dict[str, str]:
    """Copy failed or spike-held endpoints verbatim, without re-pricing them.

    Recovery into ModelPrice is lossy (two regional routes can have different
    cache prices). Failed providers contribute no fresh price or route: only
    their committed endpoints, including models absent from today's OR feed.
    If no surviving endpoint can price a model, hold its entire committed row
    (or keep it absent) and return the reason for the refresh summary.
    """
    if not failed:
        return {}
    disabled_routes = disabled_routes or set()
    models = {row["id"]: row for row in merged["models"]}
    published_models = {row["id"]: row for row in published.get("models", [])}
    held_models: dict[str, str] = {}
    reprice: set[str] = set()
    # The all-zero OR fallback can also carry endpoints from failed providers.
    for model in models.values():
        kept = [
            ep for ep in model.get("endpoints", []) if _held_route(model, ep, failed) is None
        ]
        if kept != model.get("endpoints", []):
            reprice.add(model["id"])
        model["endpoints"] = kept
    for old in published.get("models", []):
        endpoints = [
            ep for ep in old.get("endpoints", [])
            if (route := _held_route(old, ep, failed)) is not None
            and route not in disabled_routes
            and not provider_model_retired(ep["tr_provider_slug"], old["id"], ep.get("model_id"))
        ]
        if not endpoints:
            continue
        model = models.get(old["id"])
        if model is None or not model["endpoints"]:
            models[old["id"]] = {**old, "endpoints": endpoints}
            if endpoints == old.get("endpoints"):
                reprice.discard(old["id"])
            else:
                reprice.add(old["id"])
            continue
        model["endpoints"].extend(endpoints)
        reprice.add(old["id"])
    for model_id in sorted(reprice):
        model = models[model_id]
        # A shared model's headline follows the cheapest surviving endpoint;
        # each endpoint keeps its own exact published or freshly fetched price.
        prices = [
            (price, ep) for ep in model["endpoints"]
            if (price := _or_pricing_to_micro_per_m(ep.get("pricing") or {})) is not None
            and not _is_unpriced(price)
        ]
        if prices:
            cheapest, endpoint = min(
                prices, key=lambda item: (item[0].prompt_micro_per_m, item[0].completion_micro_per_m)
            )
            model["pricing"] = _price_to_pricing_block(cheapest)
            model["pricing_source"] = endpoint.get("pricing_source", "stale_snapshot")
        else:
            # Hold this row instead of blocking every other model's refresh.
            # Never retain a fresh headline that might price a removed route.
            reason = "no surviving endpoint can price the headline"
            if model_id in published_models:
                models[model_id] = dict(published_models[model_id])
                held_models[model_id] = f"{reason}; kept committed row"
            else:
                del models[model_id]
                held_models[model_id] = f"{reason}; kept unpublished (no committed row)"
    merged["models"] = [model for _, model in sorted(models.items()) if model["endpoints"]]
    merged["model_count"] = len(merged["models"])
    return held_models


def _hold_inexact_snapshot_models(
    merged: dict[str, Any], published: dict[str, Any], held: dict[str, list[str]],
    disabled_routes: set[str],
) -> dict[str, str]:
    """Keep only affected model rows when endpoint preservation was not exact.

    Compare whole route multisets, including duplicates and cached/tier prices.
    A new model with an unexpected held route stays unpublished. Restoring a
    shared model also restores its headline and other providers' endpoints;
    unrelated models and healthy manifests can still refresh.
    """
    models = {row["id"]: row for row in merged["models"]}
    old_models = {row["id"]: row for row in published.get("models", [])}
    held_models: dict[str, str] = {}
    for model_id in sorted(models.keys() | old_models.keys()):
        before = _held_endpoint_pricing({"models": [old_models.get(model_id)]}, held)
        after = _held_endpoint_pricing({"models": [models.get(model_id)]}, held)
        changed = any(
            before.get(route) != after.get(route)
            and not (route in disabled_routes and route not in after)
            for route in before.keys() | after.keys()
        )
        if not changed:
            continue
        reason = "held provider routes could not be kept exact"
        if model_id in old_models:
            models[model_id] = old_models[model_id]
            held_models[model_id] = f"{reason}; kept committed row"
        else:
            del models[model_id]
            held_models[model_id] = f"{reason}; kept unpublished (no committed row)"
    merged["models"] = [model for _, model in sorted(models.items())]
    merged["model_count"] = len(merged["models"])
    return held_models


def _result_slug_for_provider(provider_slug: str) -> str:
    return next(
        (
            result_slug
            for result_slug, provider_slugs in _PRICING_RESULT_PROVIDER_ALIASES.items()
            if provider_slug in provider_slugs
        ),
        provider_slug,
    )


def _model_outputs_text(model: dict[str, Any]) -> bool:
    """Return false only when upstream metadata explicitly excludes text."""

    architecture = model.get("architecture")
    if not isinstance(architecture, dict):
        return True
    output_modalities = architecture.get("output_modalities")
    if isinstance(output_modalities, list) and output_modalities:
        return "text" in {str(value).casefold() for value in output_modalities}
    modality = architecture.get("modality")
    if isinstance(modality, str) and "->" in modality:
        return "text" in modality.rsplit("->", 1)[-1].casefold()
    return True


def _manifest_model_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return set()
    return {
        model_id
        for row in rows
        if isinstance(row, dict) and isinstance((model_id := row.get("id")), str) and model_id
    }


def _known_model_ids(snapshot: dict[str, Any]) -> set[str]:
    known = {
        model_id
        for model in snapshot.get("models", [])
        if isinstance(model, dict) and isinstance((model_id := model.get("id")), str) and model_id
    }
    for path in PROVIDER_MANIFEST_DIR.glob("*.json"):
        known.update(_manifest_model_ids(path))
    return known


def _latest_model_created(snapshot: dict[str, Any]) -> int:
    created_values = [
        created
        for model in snapshot.get("models", [])
        if isinstance(model, dict)
        and isinstance((created := model.get("created")), int)
        and not isinstance(created, bool)
        and created > 0
    ]
    return max(created_values, default=0)


def _is_launch_candidate(model_id: str) -> bool:
    """Exclude catalog aliases that do not represent independently priced launches."""

    # OpenRouter-style suffixes describe routing or billing modes for an
    # existing model (for example ``:batch``, ``:free``, or ``:nitro``). They
    # are not independently launched provider SKUs and therefore must not
    # become parser requirements. Provider-native discovery is responsible
    # for publishing actual provider model IDs.
    return not model_id.startswith("~") and ":" not in model_id


def _new_parser_requirements(
    upstream_snapshot: dict[str, Any],
    committed_snapshot: dict[str, Any],
) -> dict[str, set[str]]:
    """Find genuinely new model launches that need parsed prices.

    This is deliberately launch-oriented.  Existing manifest rows, including
    explicitly unresolved historical rows, are not reclassified here.  The
    separate coverage audit reports those. OpenRouter frequently adds an old
    model to another provider long after launch; treating that provider/model
    pair as a launch produced hundreds of false parser failures. A hard gate
    now requires all three signals: globally unknown ID, newer creation time
    than the committed catalog, and a text endpoint on a parser-priced
    provider. Provider-native discovery remains stricter for providers that
    expose authenticated model lists.
    """

    known = _known_model_ids(committed_snapshot)
    latest_known_created = _latest_model_created(committed_snapshot)
    required: dict[str, set[str]] = {}
    for model in upstream_snapshot.get("models", []):
        if not isinstance(model, dict) or not _model_outputs_text(model):
            continue
        model_id = model.get("id")
        endpoints = model.get("endpoints")
        created = model.get("created")
        if (
            not isinstance(model_id, str)
            or not isinstance(endpoints, list)
            or model_id in known
            or not _is_launch_candidate(model_id)
            or not isinstance(created, int)
            or isinstance(created, bool)
            or created <= latest_known_created
        ):
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            provider_slug = endpoint.get("tr_provider_slug")
            if not isinstance(provider_slug, str):
                continue
            result_slug = _result_slug_for_provider(provider_slug)
            if result_slug not in _SELF_HEALING_PARSER_SLUGS:
                continue
            required.setdefault(result_slug, set()).add(model_id)
    return required


def _upstream_id_map_for(slug: str) -> dict[str, str]:
    """Read an optional `UPSTREAM_ID_MAP` from a provider's human-only
    config module.

    Some providers (Venice today) use a different model-id namespace
    than OpenRouter's canonical form. The enclave puts the snapshot
    endpoint's `model_id` verbatim into the upstream request body, so
    a mismatch produces 404 "model not found" from the provider. When
    the provider config defines `UPSTREAM_ID_MAP: dict[str, str]`
    (OR-id -> provider-native-id), `_merge_snapshot` overrides the
    endpoint's `model_id` with the native id at merge time.

    Returns `{}` for providers that don't need a translation, which
    means the merger leaves OR's `model_id` untouched. Putting the map
    in `providers/<slug>.py` (human-only) means the LLM self-heal
    cannot rewrite it — only humans can change authoritative routing
    config.
    """
    try:
        module = _import_provider(slug)
    except Exception:
        return {}
    raw = getattr(module, "UPSTREAM_ID_MAP", None)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _fetch_one(slug: str) -> tuple[str, ProviderPricingResult | None, str | None]:
    """Fetch one provider. Returns (slug, result, error_message)."""
    try:
        module = _import_provider(slug)
        result = module.fetch()
        return slug, result, None
    except Exception as exc:  # noqa: BLE001 — we genuinely want to catch everything
        return slug, None, safe_exception_summary(exc)


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
    results: dict[str, ProviderPricingResult],
) -> dict[str, dict[str, ModelPrice]]:
    """Flatten {slug: ProviderPricingResult} into
    {model_id: {slug: price, slug2: price2, ...}}. A single model can be
    served by multiple keyed providers (e.g. meta-llama/llama-3.1-8b is
    on Cerebras AND Novita; moonshotai/kimi-k2.6 is on Kimi-direct AND
    Together) — each gets its own provider-direct price for billing. Effective-
    dated provider retirements are filtered here so neither a fresh API result
    nor a stale snapshot fallback can reintroduce a retired route."""
    out: dict[str, dict[str, ModelPrice]] = {}
    upstream_id_maps: dict[str, dict[str, str]] = {}
    for slug, result in results.items():
        if not result.include_in_price_index:
            continue
        provider_slugs = _PRICING_RESULT_PROVIDER_ALIASES.get(slug, (slug,))
        for model_id, price in result.prices.items():
            if (
                result.price_index_model_ids is not None
                and model_id not in result.price_index_model_ids
            ):
                continue
            for provider_slug in provider_slugs:
                upstream_id_map = upstream_id_maps.get(provider_slug)
                if upstream_id_map is None:
                    upstream_id_map = _upstream_id_map_for(provider_slug)
                    upstream_id_maps[provider_slug] = upstream_id_map
                if provider_model_retired(
                    provider_slug,
                    model_id,
                    upstream_id_map.get(model_id),
                ):
                    continue
                out.setdefault(model_id, {})[provider_slug] = price
    return out


def _or_pricing_to_micro_per_m(pricing: dict[str, Any]) -> ModelPrice | None:
    """Read OR's pricing block (dollars-per-token strings) and convert
    to microdollars per million."""
    tiered = _snapshot_pricing_to_model_price(pricing)
    if tiered is not None:
        return tiered

    return _flat_snapshot_pricing_to_model_price(pricing)


def _micro_per_m_from_dollars_per_token(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        value = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None
    if not value.is_finite() or value < 0:
        return None
    return int((value * Decimal(1_000_000_000_000)).to_integral_value())


def _flat_snapshot_pricing_to_model_price(pricing: dict[str, Any]) -> ModelPrice | None:
    prompt = _micro_per_m_from_dollars_per_token(pricing.get("prompt"))
    completion = _micro_per_m_from_dollars_per_token(pricing.get("completion"))
    if prompt is None or completion is None:
        return None
    cache_read = _micro_per_m_from_dollars_per_token(pricing.get("input_cache_read"))
    return ModelPrice(
        prompt_micro_per_m=prompt,
        completion_micro_per_m=completion,
        prompt_cached_micro_per_m=cache_read,
    )


def _snapshot_pricing_to_model_price(pricing: dict[str, Any]) -> ModelPrice | None:
    prompt_tiers = pricing.get("prompt_tiers")
    completion_tiers = pricing.get("completion_tiers")
    if not isinstance(prompt_tiers, list) or not isinstance(completion_tiers, list):
        return None
    if not prompt_tiers or len(prompt_tiers) != len(completion_tiers):
        return None

    tiers: list[PriceTier] = []
    for prompt_tier, completion_tier in zip(prompt_tiers, completion_tiers, strict=False):
        if not isinstance(prompt_tier, dict) or not isinstance(completion_tier, dict):
            return None
        threshold = prompt_tier.get("max_prompt_tokens")
        if threshold is not None and not isinstance(threshold, int):
            return None
        prompt = _micro_per_m_from_dollars_per_token(prompt_tier.get("prompt"))
        completion = _micro_per_m_from_dollars_per_token(completion_tier.get("completion"))
        if prompt is None or completion is None:
            return None
        cache_read = _micro_per_m_from_dollars_per_token(prompt_tier.get("input_cache_read"))
        tiers.append(
            PriceTier(
                max_prompt_tokens=threshold,
                prompt_micro_per_m=prompt,
                completion_micro_per_m=completion,
                prompt_cached_micro_per_m=cache_read,
            )
        )
    if tiers[-1].max_prompt_tokens is not None:
        return None
    return ModelPrice(tiers=tiers)


def _read_existing_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_PATH.exists():
        return {"models": []}
    try:
        raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"models": []}
    return raw if isinstance(raw, dict) else {"models": []}


def _stale_results_from_snapshot(
    snapshot: dict[str, Any], failed_slugs: list[str]
) -> dict[str, ProviderPricingResult]:
    """Reuse committed endpoint prices for providers whose live refresh
    failed. This makes the "kept last hour's value" workflow guarantee
    literal: a temporary Together/API outage does not delete routes from
    the catalog or reset prices to OpenRouter fallback data."""
    failed = set(failed_slugs)
    prices_by_slug: dict[str, dict[str, ModelPrice]] = {slug: {} for slug in failed}
    for model in snapshot.get("models", []):
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue
        endpoints = model.get("endpoints")
        if not isinstance(endpoints, list):
            continue
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            endpoint_slug = endpoint.get("tr_provider_slug")
            if not isinstance(endpoint_slug, str):
                continue
            slug = next(
                (
                    failed_slug
                    for failed_slug in failed
                    if endpoint_slug
                    in _PRICING_RESULT_PROVIDER_ALIASES.get(failed_slug, (failed_slug,))
                ),
                None,
            )
            if slug is None:
                continue
            pricing = endpoint.get("pricing")
            if not isinstance(pricing, dict):
                continue
            price = _or_pricing_to_micro_per_m(pricing)
            if price is None:
                continue
            if not _safe_stale_snapshot_price(price):
                log.warning(
                    "pricing.stale_snapshot_price_rejected slug=%s model=%s",
                    slug,
                    model_id,
                )
                continue
            prices_by_slug[slug][model_id] = price

    out: dict[str, ProviderPricingResult] = {}
    for slug, prices in prices_by_slug.items():
        if not prices:
            continue
        out[slug] = ProviderPricingResult(
            slug=slug,
            prices=prices,
            source="stale_snapshot",
            fetched_url=str(SNAPSHOT_PATH),
            notes=["provider refresh failed; reused previous committed endpoint prices"],
        )
    return out


def _safe_stale_snapshot_price(price: ModelPrice) -> bool:
    """Reject malformed stale prices without outlawing genuine free rows."""

    for tier in price.tiers:
        prompt = tier.prompt_micro_per_m
        completion = tier.completion_micro_per_m
        cached = tier.prompt_cached_micro_per_m
        if prompt < 0 or completion < 0 or (cached is not None and cached < 0):
            return False
        # A one-sided zero is almost always a parser/unit failure and can lose
        # money. Both-zero rows remain valid for explicitly free routes.
        if (prompt == 0) != (completion == 0):
            return False
    return True


def _apply_stale_fallbacks(
    results: dict[str, ProviderPricingResult],
    failures: list[tuple[str, str]],
    snapshot: dict[str, Any],
) -> list[tuple[str, str]]:
    """Recover failed provider refreshes from committed endpoint prices.

    Recovery does not renew a manifest or clear a provider failure. The
    caller preserves failed providers' committed files and snapshot endpoints
    even when their prices cannot be represented as a fallback result.
    """
    if not failures:
        return []

    manifest_modules: dict[str, Any] = {}
    manifest_errors: dict[str, str] = {}
    snapshot_failure_slugs: list[str] = []
    for slug, _err in failures:
        module_name = f"scripts.pricing.providers.{slug}"
        try:
            module = _import_provider(slug)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            snapshot_failure_slugs.append(slug)
            continue
        if bool(getattr(module, "MANIFEST_STALE_FALLBACK", False)):
            manifest_modules[slug] = module
        else:
            snapshot_failure_slugs.append(slug)

    # Providers with an authenticated, provider-owned manifest must recover
    # from that manifest. The merged global snapshot is rewritten every hour
    # and therefore cannot prove when that provider last refreshed live.
    stale_results = _stale_results_from_snapshot(
        snapshot,
        snapshot_failure_slugs,
    )
    for slug, _err in failures:
        module = manifest_modules.get(slug)
        if module is None:
            continue
        manifest_value = getattr(module, "MANIFEST_PATH", None)
        if manifest_value is None:
            continue
        include_in_price_index = bool(getattr(module, "INCLUDE_IN_PRICE_INDEX", True))
        stale_result, manifest_error = read_stale_provider_manifest(
            slug=slug,
            manifest_path=Path(manifest_value),
            include_in_price_index=include_in_price_index,
        )
        if manifest_error is not None:
            manifest_errors[slug] = manifest_error
            log.warning(
                "pricing.stale_manifest_rejected slug=%s reason=%s",
                slug,
                manifest_error,
            )
            continue
        assert stale_result is not None
        stale_results[slug] = stale_result
    results.update(stale_results)
    unrecovered: list[tuple[str, str]] = []
    for slug, err in failures:
        if slug in stale_results:
            continue
        detail = f"{err}; {manifest_errors[slug]}" if slug in manifest_errors else err
        if slug in EXPIRING_PROVIDER_MANIFEST_SLUGS:
            # Catalog ingestion quarantines every endpoint for this provider
            # when its manifest is missing or invalid. Do not let an already
            # contained provider failure become a missing-price failure.
            log.error(
                "pricing.provider_manifest_quarantined slug=%s reason=%s",
                slug,
                detail,
            )
            continue
        unrecovered.append((slug, detail))
    return unrecovered


def _cross_check(
    provider_index: dict[str, dict[str, ModelPrice]],
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
    for model_id, by_slug in provider_index.items():
        or_model = or_models.get(model_id)
        if or_model is None:
            continue
        or_price = _or_pricing_to_micro_per_m(or_model.get("pricing") or {})
        if or_price is None:
            continue
        for slug, provider_price in by_slug.items():
            for dim in ("prompt_micro_per_m", "completion_micro_per_m"):
                p = getattr(provider_price, dim)
                o = getattr(or_price, dim)
                if o == 0 and p == 0:
                    continue
                denom = max(p, o, 1)
                rel_diff = abs(p - o) / denom
                if rel_diff > CROSS_CHECK_DISAGREE_THRESHOLD:
                    notes.append(
                        f"{model_id} [{dim}]: provider({slug})={p} vs OR={o} (diff {rel_diff:.1%})"
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
            if not isinstance(slug, str):
                continue
            result_slug = next(
                (
                    candidate
                    for candidate, provider_slugs in _PRICING_RESULT_PROVIDER_ALIASES.items()
                    if slug in provider_slugs
                ),
                slug,
            )
            if result_slug in or_by_slug:
                or_by_slug[result_slug].add(model_id)

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
            extra = f" (+{len(only_provider) - 5} more)" if len(only_provider) > 5 else ""
            notes.append(
                f"{slug}: parser found {len(only_provider)} model id(s) OR "
                f"does not list: {sample}{extra}"
            )
    return notes


def _is_unpriced(price: ModelPrice) -> bool:
    """A provider-direct ModelPrice whose headline tier is $0 prompt AND
    $0 completion is treated as UNPRICED, not as a genuinely free model.

    Keyed providers in this catalog do not serve free models; a $0/$0 row
    is always a feed/parse artifact — a `:free`/preview variant that
    collapsed onto the paid id, a /v1/models row listed without pricing
    (coerced to 0), or an intermittent omission. Letting such a $0 into
    the `cheapest`-tier selection zeroes the model's headline, trips
    check_price_spike's both-prices-to-zero guard, and freezes the entire
    hourly refresh (observed: a ~2-week stall, last good commit 2026-06-07,
    over 6 popular open models like gemma-3-4b-it / llama-3.3-70b that are
    in fact served at real prices). If a genuinely-free model is ever
    needed, add it to an explicit allowlist rather than trusting a 0."""
    headline = price.tiers[0]
    return headline.prompt_micro_per_m <= 0 and headline.completion_micro_per_m <= 0


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
        "completion": _micro_per_m_to_dollars_per_token(headline.completion_micro_per_m),
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
                "completion": _micro_per_m_to_dollars_per_token(t.completion_micro_per_m),
            }
            for t in price.tiers
        ]
    return block


def _merge_snapshot(
    or_snapshot: dict[str, Any],
    provider_index: dict[str, dict[str, ModelPrice]],
    healed_slugs: set[str],
) -> dict[str, Any]:
    """Build the final snapshot.

    Policy: only models with an authoritative price from the API we actually
    pay are in the snapshot. Almost every route is provider-direct, with OR
    used only as a cross-check. The explicitly labelled Meta via OpenRouter
    route is the narrow exception: its OpenRouter endpoint is both the serving
    API and billing source. Unconfigured OR-only models still fall out.

    A single model can have multiple provider-direct endpoints (e.g.
    meta-llama/llama-3.1-8b on both Cerebras and Novita;
    moonshotai/kimi-k2.6 on both Kimi-direct and Together). Each such
    endpoint keeps its own provider-direct price. Endpoints whose slug
    has NO provider-direct price are dropped — TR can't bill them
    correctly without using OR data.
    """
    or_by_id = {
        m["id"]: m
        for m in or_snapshot.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    }
    # Per-slug OR-id -> provider-native-id map. Used at endpoint merge
    # time to override `model_id` for providers whose upstream API
    # rejects the OR canonical id (Venice today). Loaded once from each
    # provider's human-only config module. Iterates PROVIDER_SLUGS (the
    # 14 known providers), NOT provider_index whose keys are model ids,
    # not slugs.
    upstream_id_maps: dict[str, dict[str, str]] = {
        slug: _upstream_id_map_for(slug) for slug in PROVIDER_SLUGS
    }
    merged_models: list[dict[str, Any]] = []
    for model_id, by_slug in provider_index.items():
        or_model = or_by_id.get(model_id)
        if or_model is None:
            # Provider gave us a price for a model OR doesn't list. We
            # have no endpoint metadata, so we can't construct a valid
            # catalog entry. Skip — the cross-check note already flags
            # this case ("parser found N models OR doesn't list").
            continue
        new_model = dict(or_model)
        # Drop $0/$0 provider tiers BEFORE selecting the headline. A zero
        # price from a keyed provider is a feed artifact, not a free model
        # (see _is_unpriced) — and because the headline is the *cheapest*
        # tier, a single spurious $0 would otherwise win `min()` and zero
        # the model, freezing the whole refresh via the spike watchdog.
        priced_by_slug = {slug: price for slug, price in by_slug.items() if not _is_unpriced(price)}
        if priced_by_slug:
            # Model-level headline pricing = the cheapest *positively-priced*
            # provider-direct tier (matches OR's convention and what
            # /v1/models top-level pricing should show). It is that
            # provider's block and nothing else: OpenRouter's aggregate also
            # carries rates (cache writes, web search, time-of-day overrides)
            # set by providers TR does not route to.
            cheapest_slug, cheapest = min(
                priced_by_slug.items(),
                key=lambda item: (
                    item[1].tiers[0].prompt_micro_per_m,
                    item[1].tiers[0].completion_micro_per_m,
                    item[0],
                ),
            )
            new_model["pricing"] = _price_to_pricing_block(cheapest)
            # Tag pricing_source as self-healed if ANY of the slugs that
            # priced this model went through the LLM rewrite.
            new_model["pricing_source"] = _endpoint_pricing_source(cheapest_slug, healed_slugs)
        else:
            # Every keyed provider returned $0 for a model that OR prices
            # above $0 — a feed glitch across all of them at once. Fall
            # back to OR's cross-check price (the one place OR is used as a
            # billing source, by explicit design, as a last resort) rather
            # than emitting $0 (freezes the refresh) or dropping a model we
            # actually serve. If OR has no usable price either, skip it.
            or_price = _or_pricing_to_micro_per_m(or_model.get("pricing") or {})
            if or_price is None or _is_unpriced(or_price):
                continue
            new_pricing = dict(new_model.get("pricing") or {})
            new_pricing.update(_price_to_pricing_block(or_price))
            new_model["pricing"] = new_pricing
            new_model["pricing_source"] = "openrouter_fallback"
            # Keep OR's endpoints (with their OR pricing) so the model
            # stays routable — rebuilding from priced_by_slug would drop
            # every endpoint (it's empty here) and the no-endpoints guard
            # below would then delete a model we actually serve.
            or_endpoints: list[dict[str, Any]] = []
            for ep in new_model.get("endpoints") or []:
                if not isinstance(ep, dict) or not isinstance(ep.get("tr_provider_slug"), str):
                    continue
                or_ep = dict(ep)
                or_ep["pricing_source"] = "openrouter_fallback"
                or_endpoints.append(or_ep)
            if not or_endpoints:
                continue
            new_model["endpoints"] = or_endpoints
            merged_models.append(new_model)
            continue

        new_endpoints: list[dict[str, Any]] = []
        seen_slugs: set[str] = set()
        for ep in new_model.get("endpoints") or []:
            new_ep = dict(ep)
            ep_slug = new_ep.get("tr_provider_slug")
            if not isinstance(ep_slug, str):
                continue
            ep_price = priced_by_slug.get(ep_slug)
            if ep_price is None:
                # Drop non-priced AND $0-priced endpoints — without a real
                # provider-direct price we can't bill the route, so listing
                # it is misleading (and a $0 here would understate cost).
                continue
            # The provider's block and nothing else, as for the headline above.
            # OpenRouter's listing of this endpoint can carry rates the
            # provider's own price does not state -- a cached-input discount
            # most of all -- and ingest bills `input_cache_read` from this
            # block, as does the stale-snapshot fallback when the provider's
            # next refresh fails.
            new_ep["pricing"] = _price_to_pricing_block(ep_price)
            new_ep["pricing_source"] = _endpoint_pricing_source(ep_slug, healed_slugs)
            # If this provider's config module exports an
            # UPSTREAM_ID_MAP, override the endpoint's model_id with
            # the provider-native id so the enclave sends what the
            # upstream API actually accepts. OR's `model_id` is the
            # canonical cross-provider id, not necessarily what any
            # single provider's API understands.
            slug_map = upstream_id_maps.get(ep_slug) or {}
            native_id = slug_map.get(model_id)
            if native_id:
                new_ep["model_id"] = native_id
            new_endpoints.append(new_ep)
            seen_slugs.add(ep_slug)
        # Synthesize endpoints for keyed providers OR doesn't list. This
        # is how new TR-keyed providers (siliconflow, tinfoil, ...) make
        # it into the catalog — OR's /endpoints feed lags or omits them
        # entirely, but we still bill correctly because the parser pulled
        # the price straight off the provider's own pricing page / API.
        for missing_slug, missing_price in priced_by_slug.items():
            if missing_slug in seen_slugs:
                continue
            if missing_slug in _NO_SYNTHETIC_ENDPOINT_PROVIDER_SLUGS:
                continue
            synth_slug_map = upstream_id_maps.get(missing_slug) or {}
            synth_model_id = synth_slug_map.get(model_id, model_id)
            synth_ep: dict[str, Any] = {
                "name": f"{missing_slug} | {model_id}",
                "model_id": synth_model_id,
                "model_name": str(new_model.get("name") or model_id),
                "provider_name": missing_slug,
                "tag": missing_slug,
                "tr_provider_slug": missing_slug,
                "context_length": int(new_model.get("context_length") or 0),
                "pricing": _price_to_pricing_block(missing_price),
                "pricing_source": _endpoint_pricing_source(missing_slug, healed_slugs),
                "supported_parameters": list(new_model.get("supported_parameters") or []),
                "quantization": "unknown",
            }
            new_endpoints.append(synth_ep)
        if not new_endpoints:
            # Edge case: provider-direct has the model but OR records
            # no matching endpoints for any priced slug. Drop.
            continue
        new_model["endpoints"] = new_endpoints
        merged_models.append(new_model)

    merged_models.sort(key=lambda m: str(m.get("id") or ""))
    return {
        "source": (
            "authoritative downstream APIs; provider-direct except explicitly "
            "labelled proxy-backed routes such as Meta via OpenRouter"
        ),
        "filter": (
            "kept only models with a configured, billable downstream route; "
            "unconfigured OpenRouter-only models are dropped"
        ),
        "tr_keyed_providers": or_snapshot.get("tr_keyed_providers", []),
        "model_count": len(merged_models),
        "models": merged_models,
    }


def _write_snapshot(snapshot: dict[str, Any]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    SNAPSHOT_PATH.write_text(text, encoding="utf-8")


def _write_provider_manifests(
    results: dict[str, ProviderPricingResult],
) -> tuple[list[str], list[tuple[str, str]]]:
    """Let provider adapters update supplemental provider model manifests.

    Most providers are fully represented by the OpenRouter-shaped snapshot.
    Providers such as Makora use `data/provider_models/<slug>.json` at runtime
    because their live API is ahead of OpenRouter's endpoint feed or needs
    provider-native upstream IDs. A provider module can expose
    `write_provider_manifest(result) -> list[str]` to keep that manifest
    hourly refreshed without making the shared merger know provider-specific
    JSON shape details.
    """

    notes: list[str] = []
    failures: list[tuple[str, str]] = []
    for slug, result in sorted(results.items()):
        # A stale fallback represents the last known good catalog state. Do not
        # let provider-specific hooks rewrite that state without fresh discovery
        # data; destructive hooks may otherwise prune every currently live row.
        if result.source.startswith("stale_"):
            continue
        module = _import_provider(slug)
        hook = getattr(module, "write_provider_manifest", None)
        if not callable(hook):
            # TODO: vet novita/together/kimi discovery semantics separately
            # before adding or expanding provider-owned rebuild behavior.
            continue
        manifest_path_value = getattr(module, "MANIFEST_PATH", None)
        if manifest_path_value is None:
            # Without a rollback target, a writer cannot be isolated safely.
            raise RuntimeError(f"{slug}: manifest writer must declare MANIFEST_PATH")
        manifest_path = Path(manifest_path_value)
        before_bytes: bytes | None = None
        before_rows: list[Any] | None = None
        if manifest_path.exists():
            before_bytes = manifest_path.read_bytes()
            try:
                before_raw = json.loads(before_bytes)
            except (TypeError, ValueError):
                before_raw = None
            if isinstance(before_raw, dict) and isinstance(before_raw.get("models"), list):
                before_rows = before_raw["models"]

        try:
            raw_notes = hook(result)
            after_raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            after_rows = after_raw.get("models") if isinstance(after_raw, dict) else None
            if not isinstance(after_rows, list) or not after_rows:
                raise ValueError("manifest writer produced no model rows")
            if before_rows is not None:
                guarded = guard_manifest_prune(
                    before_rows,
                    after_rows,
                    provider_slug=slug,
                    allow_confirmed_delistings=bool(
                        getattr(module, "ALLOW_CONFIRMED_MASS_DELISTINGS", False)
                    ),
                )
                if guarded is before_rows:
                    raise ValueError("manifest writer triggered mass-prune guard")
            if raw_notes is not None:
                notes.extend(str(note) for note in raw_notes)
        except Exception as exc:
            # A hook may fail after a partial write. Roll back before using the
            # same last-known-good recovery path as a provider fetch failure.
            # A rollback error deliberately aborts the entire publication.
            if before_bytes is not None:
                manifest_path.write_bytes(before_bytes)
            else:
                manifest_path.unlink(missing_ok=True)
            detail = f"stage=manifest {safe_exception_summary(exc)}"
            failures.append((slug, detail))
            log.error("pricing.manifest_failed slug=%s error=%s", slug, detail)
    return notes, failures


def _summary_lines(
    results: dict[str, ProviderPricingResult],
    healed: list[str],
    failures: list[tuple[str, str]],
    disagreements: list[str],
    id_mismatches: list[str],
    held: dict[str, list[str]] | None = None,
) -> list[str]:
    lines: list[str] = []
    lines.append("Per-provider results:")
    for slug in PROVIDER_SLUGS:
        if held and slug in held:
            lines.append(f"  {slug}: HELD (kept committed state)")
            continue
        result = results.get(slug)
        if result is None:
            err = next((e for s, e in failures if s == slug), "unknown")
            lines.append(f"  {slug}: FAILED ({err})")
            continue
        lines.append(f"  {slug}: {len(result.prices)} models via {result.source}")
        for note in result.notes[:3]:
            lines.append(f"    note: {note}")
        if len(result.notes) > 3:
            lines.append(f"    ... and {len(result.notes) - 3} more note(s)")
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
    baseline = _copy_published_prices()
    status = 1
    try:
        status = _refresh(summary_only=args.summary_only, baseline=baseline)
        return status
    finally:
        if status != 0 or args.summary_only:
            _restore_all_published_files(baseline)


def _refresh(*, summary_only: bool, baseline: Path) -> int:
    log.info("pricing.refresh.openrouter_ingest")
    or_snapshot = build_openrouter_snapshot()
    committed_snapshot = _read_existing_snapshot()
    required_by_slug = _new_parser_requirements(or_snapshot, committed_snapshot)
    configure_runtime_required_models(required_by_slug)
    if required_by_slug:
        log.info(
            "pricing.refresh.new_parser_models providers=%d models=%d",
            len(required_by_slug),
            sum(len(model_ids) for model_ids in required_by_slug.values()),
        )

    log.info("pricing.refresh.start providers=%d", len(PROVIDER_SLUGS))
    results, failures = _fetch_all_providers()

    unrecovered_failures = _apply_stale_fallbacks(
        results,
        failures,
        committed_snapshot,
    )
    manifest_notes: list[str] = []
    if not summary_only:
        manifest_notes, manifest_failures = _write_provider_manifests(results)
        for slug, _error in manifest_failures:
            # Never merge fresh prices from a rejected manifest. Recovery must
            # prove a valid previous snapshot, or leave this provider absent.
            results.pop(slug, None)
        failures.extend(manifest_failures)
        unrecovered_failures.extend(
            _apply_stale_fallbacks(results, manifest_failures, committed_snapshot)
        )
    healed = [slug for slug, res in results.items() if res.heal_diff is not None]

    # One systemic-failure rule: a strict majority of attempted providers
    # failed to refresh, whether or not committed prices could be recovered.
    # Local failures cannot consume an arbitrary global publication budget.
    failed = {slug: [f"refresh failed: {error}"] for slug, error in failures}
    for slug, error in unrecovered_failures:
        failed[slug] = [f"refresh failed: {error}"]
    if len(failed) * 2 > len(PROVIDER_SLUGS):
        log.error(
            "pricing.refresh.systemic_failure failed=%d providers=%d failures=%s",
            len(failed), len(PROVIDER_SLUGS), failures,
        )
        print(f"Systemic refresh failure: {len(failed)}/{len(PROVIDER_SLUGS)} providers failed; nothing published.")
        for line in _summary_lines(results, healed, failures, [], []):
            print(line)
        return 1

    for slug in failed:
        _restore_published_files(baseline, slug)
    healed = [slug for slug in healed if slug not in failed]
    provider_index = _index_provider_prices({slug: res for slug, res in results.items() if slug not in failed})
    disagreements = _cross_check(provider_index, or_snapshot)
    id_mismatches = _cross_check_ids(results, or_snapshot)

    merged = _merge_snapshot(or_snapshot, provider_index, set(healed))
    held_models = _keep_failed_snapshot_routes(merged, committed_snapshot, failed)

    held: dict[str, list[str]] = {}
    if not summary_only:
        _write_snapshot(merged)
        # A price spike holds only its own provider at the last published
        # prices, so the rest of the catalog still refreshes. The workflow's
        # spike gate still fails the run on any spike not held here.
        while hold := _spiking_results(baseline, results, held):
            held.update(hold)
            for slug in hold:
                _restore_published_files(baseline, slug)
                results.pop(slug, None)
            # Held providers never enter the price index: recovery through a
            # ModelPrice loses route identity, duplicate prices and precision.
            # Keep their endpoints just like failed providers' endpoints.
            healed = [slug for slug in healed if slug not in held]
            provider_index = _index_provider_prices({slug: res for slug, res in results.items() if slug not in failed})
            disagreements = _cross_check(provider_index, or_snapshot)
            id_mismatches = _cross_check_ids(results, or_snapshot)
            merged = _merge_snapshot(or_snapshot, provider_index, set(healed))
            all_held = {**held, **failed}
            disabled = _published_disabled_held_routes(baseline, committed_snapshot, all_held)
            held_models = _keep_failed_snapshot_routes(
                merged, committed_snapshot, all_held, disabled_routes=disabled,
            )
            _write_snapshot(merged)
        held.update(failed)
        if held and (changed := _held_routes_changed(baseline, held)):
            log.warning("pricing.hold_not_exact providers=%s routes=%s", sorted(held), changed[:20])
            # Reload the immutable baseline: merged endpoints can share objects
            # with committed_snapshot, so a damaged copy is not recovery data.
            published = json.loads((baseline / SNAPSHOT_PATH.name).read_text(encoding="utf-8"))
            held_models.update(_hold_inexact_snapshot_models(
                merged, published, held,
                _published_disabled_held_routes(baseline, published, held),
            ))
            _write_snapshot(merged)
            # The endpoint guard repairs only affected model rows. A failed
            # manifest rollback remains fatal: no snapshot row can make a
            # changed held manifest safe to publish.
            if changed := _held_routes_changed(baseline, held):
                print(f"Held providers could not be kept exactly as published: {', '.join(sorted(held))}")
                for route in changed:
                    print(f"  {route}")
                return 1
        log.info("pricing.refresh.wrote path=%s models=%d", SNAPSHOT_PATH, merged["model_count"])

    held.update(failed)
    summary = _summary_lines(results, healed, failures, disagreements, id_mismatches, held)
    print(f"Hourly price refresh — {merged['model_count']} models")
    print(
        f"Sources: {sum(1 for r in results.values() if r.source == 'deterministic')} "
        f"deterministic, {len(healed)} self-healed, "
        f"{sum(1 for r in results.values() if r.source == 'api')} api, "
        f"{sum(1 for r in results.values() if r.source == 'stale_snapshot')} stale, "
        f"{len(failures)} failed (kept committed state; absent providers remain absent)"
    )
    print()
    for line in summary:
        print(line)
    if manifest_notes:
        print()
        print("Supplemental provider manifests:")
        for note in manifest_notes:
            print(f"  {note}")
    if held:
        print()
        print(HELD_FOR_REVIEW_HEADING)
        for slug, routes in sorted(held.items()):
            print(f"  {slug}:")
            for route in routes:
                print(f"    {route}")
        print(
            "::warning title=Provider prices held for review::"
            f"{', '.join(sorted(held))} kept their committed state. Repair refresh "
            "failures; verified price changes use APPROVED_ENDPOINT_PRICE_TRANSITIONS."
        )
    if held_models:
        print()
        print("Snapshot models held:")
        for model_id, reason in sorted(held_models.items()):
            print(f"  {model_id}: {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
