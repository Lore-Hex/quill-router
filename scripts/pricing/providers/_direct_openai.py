"""Reusable fail-closed adapter for provider-owned OpenAI-compatible catalogs."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id
from scripts.pricing.openai_catalog import (
    discover_available_priced_chat_catalog,
    discover_openai_chat_catalog,
    probe_openai_chat,
)

IncludeRow = Callable[[dict[str, Any]], bool]
NormalizeRows = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
PriceLoader = Callable[[], dict[str, ModelPrice]]
CatalogLoader = Callable[[str], list[dict[str, Any]]]


def positive_chat_prices(prices: dict[str, ModelPrice]) -> dict[str, ModelPrice]:
    """Keep only routes whose every tier charges both token directions."""

    return {
        model_id: price
        for model_id, price in prices.items()
        if all(
            tier.prompt_micro_per_m > 0 and tier.completion_micro_per_m > 0 for tier in price.tiers
        )
    }


@dataclass(frozen=True)
class DirectOpenAIProviderSpec:
    slug: str
    base_url: str
    api_key_env: str | tuple[str, ...]
    explicit_model_map: dict[str, str]
    namespace_unqualified: str | None = None
    expected_models: tuple[str, ...] = ()
    catalog_url: str | None = None
    pricing_source_url: str | None = None
    static_prices: dict[str, ModelPrice] = field(default_factory=dict)
    price_loader: PriceLoader | None = None
    catalog_loader: CatalogLoader | None = None
    include: IncludeRow | None = None
    normalize_rows: NormalizeRows | None = None
    # Only enable when normalize_rows constructs the upstream ID locally.
    # Raw provider catalogs are not trusted to redirect one priced model ID to
    # another model silently.
    accept_normalized_upstream_id: bool = False
    # Operator safety holds are applied after every canary. Keeping them in
    # the shared fetcher prevents a healthy PONG from making a deliberately
    # dark route live during a refactor or manifest rebuild.
    operator_hold_reasons: dict[str, str] = field(default_factory=dict)
    canary_max_tokens: int = 16
    canary_expected_content: str | None = None
    canary_endpoint_path: str = "/chat/completions"
    canary_extra_body: dict[str, Any] = field(default_factory=dict)
    canary_concurrency: int = 1


def _catalog_rows(payload: object, *, slug: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next(
            (value for key in ("data", "models") if isinstance((value := payload.get(key)), list)),
            None,
        )
    else:
        rows = None
    if not isinstance(rows, list):
        raise RuntimeError(f"{slug}: model catalog has no data/models list")
    return [row for row in rows if isinstance(row, dict)]


class DirectOpenAIProvider:
    """Own one provider's discovery state without duplicating network policy."""

    def __init__(self, spec: DirectOpenAIProviderSpec, *, manifest_path: Path) -> None:
        if spec.static_prices and spec.price_loader is not None:
            raise ValueError(f"{spec.slug}: configure static_prices or price_loader, not both")
        if spec.accept_normalized_upstream_id and spec.normalize_rows is None:
            raise ValueError(f"{spec.slug}: accept_normalized_upstream_id requires normalize_rows")
        if not 1 <= spec.canary_concurrency <= 16:
            raise ValueError(f"{spec.slug}: canary_concurrency must be between 1 and 16")
        self.spec = spec
        self.manifest_path = manifest_path
        self.upstream_id_map = {
            model_id: native_id for native_id, model_id in spec.explicit_model_map.items()
        }
        self.discovered_rows: dict[str, dict[str, Any]] = {}
        self._fetched = False

    @property
    def api_key_envs(self) -> tuple[str, ...]:
        value = self.spec.api_key_env
        return (value,) if isinstance(value, str) else value

    def model_id(self, native_id: str) -> str | None:
        """Apply the exact normalization policy used by this refresher."""

        value = native_id.strip()
        if not value:
            return None
        mapped = self.spec.explicit_model_map.get(value)
        if mapped is not None:
            return mapped
        if self.spec.namespace_unqualified and "/" not in value:
            return f"{self.spec.namespace_unqualified}/{value.casefold()}"
        return mapped_or_canonical_model_id(value, {})

    def _api_key(self) -> str | None:
        return next(
            (value for env_name in self.api_key_envs if (value := os.environ.get(env_name))),
            None,
        )

    def _joined_prices(self) -> dict[str, ModelPrice] | None:
        if self.spec.price_loader is not None:
            prices = self.spec.price_loader()
            if not prices:
                raise RuntimeError(f"{self.spec.slug}: price loader returned no prices")
            return prices
        return self.spec.static_prices or None

    def fetch(self) -> ProviderPricingResult:
        self._fetched = False
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError(
                f"{self.spec.slug}: one of {self.api_key_envs!r} is required for discovery"
            )
        catalog_url = self.spec.catalog_url or f"{self.spec.base_url.rstrip('/')}/models"
        if self.spec.catalog_loader is None:
            payload = fetch_json(
                catalog_url,
                extra_headers={"Authorization": f"Bearer {api_key}"},
            )
            rows = _catalog_rows(payload, slug=self.spec.slug)
        else:
            rows = self.spec.catalog_loader(api_key)
        if self.spec.normalize_rows is not None:
            rows = self.spec.normalize_rows(rows)

        explicit_model_map = dict(self.spec.explicit_model_map)
        for row in rows:
            native_id = row.get("id")
            if not isinstance(native_id, str):
                continue
            model_id = self.model_id(native_id)
            if model_id is not None:
                explicit_model_map.setdefault(native_id, model_id)

        joined_prices = self._joined_prices()

        if joined_prices is not None:
            discovered = discover_available_priced_chat_catalog(
                rows,
                prices=joined_prices,
                explicit_map=explicit_model_map,
                upstream_id_map=self.upstream_id_map,
                include=self.spec.include,
                accept_source_upstream_id=self.spec.accept_normalized_upstream_id,
            )
            prices = {model_id: joined_prices[model_id] for model_id in discovered}
        else:
            prices, discovered = discover_openai_chat_catalog(
                rows,
                explicit_map=explicit_model_map,
                upstream_id_map=self.upstream_id_map,
                include=self.spec.include,
                accept_source_upstream_id=self.spec.accept_normalized_upstream_id,
            )
        if not prices:
            raise RuntimeError(f"{self.spec.slug}: no priced chat models discovered")

        prices = positive_chat_prices(prices)
        if not prices:
            raise RuntimeError(
                f"{self.spec.slug}: no chat model has positive input and output prices"
            )

        for model_id, row in discovered.items():
            native_id = row.get("upstream_id")
            if isinstance(native_id, str):
                self.upstream_id_map.setdefault(model_id, native_id)

        checked = models_requiring_canary(
            self.manifest_path,
            (set(discovered) & set(prices)) - self.spec.operator_hold_reasons.keys(),
        )

        def probe(model_id: str) -> tuple[str, bool]:
            return model_id, probe_openai_chat(
                base_url=self.spec.base_url,
                api_key=api_key,
                model=self.upstream_id_map[model_id],
                max_tokens=self.spec.canary_max_tokens,
                expected_content=self.spec.canary_expected_content,
                endpoint_path=self.spec.canary_endpoint_path,
                extra_body=self.spec.canary_extra_body,
            )

        if self.spec.canary_concurrency == 1 or len(checked) <= 1:
            outcomes = [probe(model_id) for model_id in sorted(checked)]
        else:
            with ThreadPoolExecutor(
                max_workers=min(self.spec.canary_concurrency, len(checked)),
                thread_name_prefix=f"{self.spec.slug}-canary",
            ) as executor:
                outcomes = list(executor.map(probe, sorted(checked)))
            failed = sorted(model_id for model_id, succeeded in outcomes if not succeeded)
            if failed:
                # A bounded parallel catalog sweep can briefly trip a provider's
                # shared-key rate limit. Retry only those failures once, serially,
                # before holding routes dark. A genuinely broken route still fails
                # closed, while self-inflicted 429s do not shrink the catalog.
                time.sleep(1.0)
                retry_outcomes = dict(probe(model_id) for model_id in failed)
                outcomes = [
                    (model_id, succeeded or retry_outcomes.get(model_id, False))
                    for model_id, succeeded in outcomes
                ]
        healthy = {model_id for model_id, succeeded in outcomes if succeeded}
        apply_canary_results(
            discovered,
            checked_model_ids=checked,
            healthy_model_ids=healthy,
        )
        for model_id, reason in self.spec.operator_hold_reasons.items():
            row = discovered.get(model_id)
            if row is None:
                continue
            row["routable"] = False
            row["routable_reason"] = reason
        self.discovered_rows = discovered

        errors = validate(prices, list(self.spec.expected_models))
        if errors:
            raise RuntimeError("; ".join(errors))
        result = ProviderPricingResult(
            slug=self.spec.slug,
            prices=prices,
            source="api",
            fetched_url=self.spec.pricing_source_url or catalog_url,
            notes=[
                f"discovered {len(discovered)} priced chat models",
                f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed",
            ],
        )
        self._fetched = True
        return result

    def write_provider_manifest(self, result: ProviderPricingResult) -> list[str]:
        if not self._fetched:
            raise RuntimeError(f"{self.spec.slug}: fetch must succeed before writing manifest")
        return write_discovered_chat_manifest(
            result,
            manifest_path=self.manifest_path,
            discovered_rows=self.discovered_rows,
            source_url=self.spec.catalog_url or f"{self.spec.base_url.rstrip('/')}/models",
            pricing_source_url=self.spec.pricing_source_url,
            operator_hold_reasons=self.spec.operator_hold_reasons,
        )
