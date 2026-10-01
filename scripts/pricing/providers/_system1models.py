"""System1's regional typed-decision catalogs, priced by actual input tokens."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)

BASE_URL = "https://api.system1models.ai/v1"
URL = f"{BASE_URL}/models"


class System1Catalog:
    def __init__(self, tier: str) -> None:
        if tier not in {"eu", "global"}:
            raise ValueError("unknown System1 tier")
        self.tier = tier
        self.slug = "system1models-eu" if tier == "eu" else "system1models"
        self.key_env = f"SYSTEM1MODELS_{tier.upper()}_API_KEY"
        self.manifest_path = (
            Path(__file__).resolve().parents[3]
            / f"src/trusted_router/data/provider_models/{self.slug}.json"
        )
        self.upstream_id_map: dict[str, str] = {}
        self.rows: dict[str, dict[str, Any]] = {}

    def canonical_model_id(self, native_id: str) -> str | None:
        return (
            f"{self.slug}/{native_id}"
            if re.fullmatch(r"s1-[a-z0-9]+(?:-[a-z0-9]+)*", native_id)
            else None
        )

    def discover(self, payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"{self.slug}: catalog has no data list")
        prices: dict[str, ModelPrice] = {}
        rows: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for item in payload["data"]:
            native = item.get("id") if isinstance(item, dict) else None
            model_id = self.canonical_model_id(native) if isinstance(native, str) else None
            if not model_id or model_id in seen:
                raise RuntimeError(f"{self.slug}: invalid or duplicate model ID")
            seen.add(model_id)
            availability = item.get("tier_availability")
            if not isinstance(availability, dict):
                raise RuntimeError(
                    f"{self.slug}: {native} has no regional availability declaration"
                )
            if item.get("status") != "available" or availability.get(self.tier) is not True:
                continue
            rates = item.get("prices")
            if (
                not isinstance(rates, dict)
                or rates.get("unit") != "per_million_input_tokens"
                or rates.get("output_tokens") != "free"
            ):
                raise RuntimeError(f"{self.slug}: {native} changed its input-only billing contract")
            try:
                raw = rates["USD"][self.tier]
                if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
                    raise ValueError("not a decimal USD string")
                amount = Decimal(raw) * 1_000_000
                if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
                    raise ValueError("invalid precision or nonpositive rate")
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise RuntimeError(
                    f"{self.slug}: {native} has no exact USD/{self.tier} price"
                ) from exc
            modalities = item.get("modalities")
            question_types = item.get("question_types")
            if (
                modalities not in (["text"], ["text", "image"])
                or not isinstance(question_types, list)
                or not all(isinstance(kind, str) for kind in question_types)
                or set(question_types) != {"noul", "choice", "score"}
                or ("image" in modalities and native != "s1-vision")
            ):
                raise RuntimeError(f"{self.slug}: {native} has an unsupported decision contract")
            prices[model_id] = ModelPrice(int(amount), 0)
            example = {
                "model": model_id,
                "state": "Mia owns a red bicycle.",
                "questions": {
                    "color": {
                        "type": "choice",
                        "instructions": "Which color is the bicycle?",
                        "criteria": {"red": None, "blue": None},
                    }
                },
            }
            rows[model_id] = {
                "id": model_id,
                "upstream_id": native,
                "display_name": f"System1 {native} ({self.tier.upper()})",
                # The contract publishes byte/tokenizer bounds, not a model context window.
                "context_length": 0,
                "model_type": "decision",
                "endpoints": ["decide"],
                "input_modalities": modalities,
                "output_modalities": ["decision"],
                "supported_features": [],
                "supported_sampling_parameters": [],
                "documentation": {
                    "description": str(
                        item.get("description") or "Typed decisions with probabilities."
                    ),
                    "input_format": "POST /v1/decide: state (at most 16 KiB serialized JSON) and exactly one boolean/noul, choice or score question. Vision accepts one images entry: PNG, JPEG or WebP data URL, at most 4 MiB decoded and 2 megapixels. No streaming.",
                    "output_format": "Verified answers and probabilities, with inputTokens usage. Input-only billing; output tokens are free. The EU route cannot fall back to Global.",
                    "example_input": json.dumps(example),
                    "example_output": json.dumps(
                        {
                            "model": model_id,
                            "answers": {
                                "color": {
                                    "type": "choice",
                                    "choice": "red",
                                    "probabilities": {"red": 0.99, "blue": 0.01},
                                }
                            },
                        }
                    ),
                },
            }
        if not prices:
            raise RuntimeError(f"{self.slug}: no available priced decision models")
        return prices, rows

    def probe(self, key: str, row: dict[str, Any]) -> bool:
        try:
            response = httpx.post(
                f"{BASE_URL}/systemone",
                timeout=30,
                headers={"Authorization": f"Bearer {key}", "S1-Region": self.tier},
                json={
                    "model": row["upstream_id"],
                    "state": "Mia owns a red bicycle.",
                    "questions": {
                        "color": {
                            "type": "choice",
                            "instructions": "Which color is the bicycle?",
                            "criteria": {"red": None, "blue": None},
                        }
                    },
                },
            )
            response.raise_for_status()
            body = response.json()
            usage = body["usage"]
            return bool(
                response.headers.get("S1-Region") == self.tier
                and body["model"] == row["upstream_id"]
                and body["tier"] == self.tier
                and body["answers"]["color"]["choice"] == "red"
                and type(usage["input_tokens"]) is int
                and usage["input_tokens"] > 0
                and type(usage["output_tokens"]) is int
                and usage["output_tokens"] == 0
                and type(usage["decisions"]) is int
                and usage["decisions"] == 1
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return False

    def fetch(self) -> ProviderPricingResult:
        self.rows = {}
        self.upstream_id_map.clear()
        key = os.environ.get(self.key_env, "").strip()
        if not key:
            raise RuntimeError(f"{self.slug}: {self.key_env} required for discovery canaries")
        prices, rows = self.discover(fetch_json(URL))
        checked = models_requiring_canary(self.manifest_path, set(prices))
        healthy = {model_id for model_id in sorted(checked) if self.probe(key, rows[model_id])}
        apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
        self.rows = rows
        self.upstream_id_map.update({model: row["upstream_id"] for model, row in rows.items()})
        return ProviderPricingResult(
            slug=self.slug,
            prices=prices,
            source="api",
            fetched_url=URL,
            include_in_price_index=False,
        )

    def write_provider_manifest(self, result: ProviderPricingResult) -> list[str]:
        if not self.rows:
            raise RuntimeError(f"{self.slug}: fetch must succeed before writing manifest")
        return write_discovered_chat_manifest(
            result,
            manifest_path=self.manifest_path,
            discovered_rows=self.rows,
            source_url=URL,
            pricing_source_url=URL,
        )
