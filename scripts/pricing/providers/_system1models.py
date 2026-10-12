"""Decision Models' regional catalogs, retaining the System1 API namespaces."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json
from scripts.pricing.manifest import (
    apply_canary_results,
    set_manifest_canary_state,
    write_discovered_chat_manifest,
)

BASE_URL = "https://api.decisionmodels.io/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://decisionmodels.io/pricing"
PRO_LEGACY_UNTIL = date(2026, 11, 12)


def _input_price(rates: Any, tier: str) -> int:
    if (
        not isinstance(rates, dict)
        or rates.get("unit") != "per_million_input_tokens"
        or rates.get("output_tokens") != "free"
    ):
        raise ValueError("changed input-only billing contract")
    raw = rates["USD"][tier]
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
        raise ValueError("not a decimal USD string")
    amount = Decimal(raw) * 1_000_000
    if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
        raise ValueError("invalid precision or nonpositive rate")
    return int(amount)


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
            if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", native_id)
            else None
        )

    def discover(self, payload: object, *, today: date | None = None) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError(f"{self.slug}: catalog has no data list")
        prices: dict[str, ModelPrice] = {}
        rows: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        today = today or datetime.now(UTC).date()
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
            try:
                amount = _input_price(rates, self.tier)
                image_rates = rates.get("image_requests")
                image_amount = None
                if image_rates is not None:
                    image_amount = _input_price(image_rates, self.tier)
                    if image_rates.get("applies_to") != "requests_with_image":
                        raise ValueError("unknown image billing condition")
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
            ):
                raise RuntimeError(f"{self.slug}: {native} has an unsupported decision contract")
            # Published regional existing-customer terms, corroborated by the
            # EU account's EUR receipts on 2026-10-12. Never convert charge_nanos
            # as USD or apply the discount to a different future tariff.
            legacy_pro = (
                native in {"s1-pro", "s1-pro-2"}
                and today < PRO_LEGACY_UNTIL
                and amount == {"eu": 84_000, "global": 80_000}[self.tier]
            )
            if legacy_pro:
                amount = {"eu": 40_000, "global": 38_000}[self.tier]
            prices[model_id] = ModelPrice(amount, 0)
            context = item.get("max_input_tokens", 0)
            if type(context) is not int or context < 0:
                raise RuntimeError(f"{self.slug}: {native} has invalid input-token limit")
            hold = None
            if item.get("fixed_questions") is not None:
                hold = "decision-fixed-questions-unsupported"
            elif not native.startswith("s1-"):
                hold = "decision-model-namespace-unsupported"
            elif image_amount is not None:
                hold = "decision-image-pricing-unsupported"
            elif "image" in modalities and native != "s1-vision":
                hold = "decision-image-contract-unsupported"
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
                "display_name": f"Decision Models {native} ({self.tier.upper()})",
                "context_length": context,
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
                    "input_format": "POST /v1/decide: state (at most 16 KiB serialized JSON) and exactly one boolean/noul, choice or score question. The model's input-token limit also applies. No streaming."
                    + (" This model is not currently routable through TrustedRouter: " + hold + "." if hold else ""),
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
                "decision_contract_hold": hold,
                "decision_image_input_price_per_m": image_amount,
                "pricing_valid_until": f"{PRO_LEGACY_UNTIL.isoformat()}T00:00:00Z" if legacy_pro else None,
            }
            if hold:
                rows[model_id].update(routable=False, routable_reason=hold)
                rows[model_id]["documentation"].pop("example_input")
                rows[model_id]["documentation"].pop("example_output")
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
        # This is a small catalog. Recheck existing routes too: /models can
        # advertise an available Global tier while inference returns 503.
        checked = {model_id for model_id, row in rows.items() if not row["decision_contract_hold"]}
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
        holds = {model: row["decision_contract_hold"] for model, row in self.rows.items() if row["decision_contract_hold"]}
        # An all-failed live canary is evidence of unavailability, not a
        # discovery disappearance. The mass-prune guard must not keep serving
        # these old routes. Recovery is checked on the next hourly refresh.
        candidates = [row for row in self.rows.values() if not row["decision_contract_hold"]]
        all_failed = bool(candidates) and all(row.get("routable") is False for row in candidates)
        if all_failed and self.manifest_path.exists():
            set_manifest_canary_state(self.manifest_path, healthy=False)
        notes = write_discovered_chat_manifest(
            result,
            manifest_path=self.manifest_path,
            discovered_rows=self.rows,
            source_url=URL,
            pricing_source_url=URL,
            operator_hold_reasons=holds,
        )
        raw = json.loads(self.manifest_path.read_text())
        deadlines = [row["pricing_valid_until"] for row in self.rows.values() if row["pricing_valid_until"]]
        if deadlines:
            raw["pricing_valid_until"] = min(deadlines)
        else:
            raw.pop("pricing_valid_until", None)
        self.manifest_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
        return notes
