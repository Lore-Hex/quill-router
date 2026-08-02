"""0G Private Computer private-route catalog and pricing refresh.

0G publishes its live marketplace catalog as structured Next.js hydration data
on the public models page. TrustedRouter intentionally ingests only healthy
``TeeML`` chat routes and forces 0G's ``private`` trust mode at inference time.
``TeeTLS`` verifies the proxy hop but not the model execution, so those rows and
ordinary unverified routes are excluded from this provider integration.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html, validate
from scripts.pricing.manifest import (
    set_manifest_canary_state,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.openai_catalog import (
    dollars_per_token_to_micro_per_m,
    positive_int,
    probe_openai_chat,
)

SLUG = "zero-g"
URL = "https://pc.0g.ai/models"
BASE_URL = "https://router-api.0g.ai/v1"
PRIVATE_TRUST_HEADERS = {"X-0G-Provider-Trust-Mode": "private"}
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "zero-g.json"
)
EXPECTED_MODELS = [
    "zero-g/0gm-1.0-35b-a3b",
    "zero-g/0gm-1.0-35b-a3b-sia",
    "z-ai/glm-5.2",
]

_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = False


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self._in_script = False
        self._current: list[str] = []

    def handle_starttag(
        self, tag: str, _attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag == "script":
            self._in_script = True
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._current))
            self._in_script = False
            self._current = []


def _walk_json(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _flight_values(html: str) -> Iterator[object]:
    collector = _ScriptCollector()
    collector.feed(html)
    decoder = json.JSONDecoder()
    for script in collector.scripts:
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\)", script, flags=re.DOTALL)
        if match is None:
            continue
        try:
            flight_call = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if (
            not isinstance(flight_call, list)
            or len(flight_call) < 2
            or not isinstance(flight_call[1], str)
            or ":" not in flight_call[1]
        ):
            continue
        encoded_value = flight_call[1].split(":", 1)[1].lstrip()
        try:
            value, _end = decoder.raw_decode(encoded_value)
        except ValueError:
            continue
        yield value


def _query_rows(html: str, query_name: str) -> list[dict[str, Any]]:
    for value in _flight_values(html):
        for candidate in _walk_json(value):
            query_key = candidate.get("queryKey")
            if not isinstance(query_key, list) or not query_key:
                continue
            if query_key[0] != query_name:
                continue
            state = candidate.get("state")
            rows = state.get("data") if isinstance(state, dict) else None
            if not isinstance(rows, list):
                raise RuntimeError(f"0G {query_name} query has no data list")
            return [row for row in rows if isinstance(row, dict)]
    raise RuntimeError(f"0G page has no {query_name} catalog query")


def _canonical_model_id(native_id: str) -> str | None:
    normalized = native_id.strip().casefold().replace("_", "-")
    if normalized.startswith("0gm-"):
        return f"zero-g/{normalized}"
    if normalized == "hy3":
        return "tencent/hy3"
    for prefix, author in (
        ("claude-", "anthropic"),
        ("gemini-", "google"),
        ("gpt-", "openai"),
        ("mistral-", "mistralai"),
        ("command-", "cohere"),
    ):
        if normalized.startswith(prefix):
            return f"{author}/{normalized}"
    return canonicalize_unqualified_model_id(normalized)


def _modalities(value: object, *, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return default
    allowed = {"text", "image"}
    result = list(
        dict.fromkeys(
            str(item).strip().casefold()
            for item in value
            if str(item).strip().casefold() in allowed
        )
    )
    return result or default


def _private_chat_route(row: dict[str, Any]) -> bool:
    return (
        row.get("service_type") == "chatbot"
        and row.get("type") == "chatbot"
        and row.get("trust_mode") == "private"
        and row.get("verifiability") == "TeeML"
        and row.get("tee_attested") is True
        and row.get("is_healthy") is True
    )


def parse_private_catalog(
    html: str,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    """Return healthy private chat prices and manifest rows.

    When multiple TeeML providers serve the same canonical model, the highest
    observed rates win. This avoids underbilling if 0G's health-aware router
    moves a request to a more expensive private provider.
    """

    model_rows = {
        str(row.get("id")): row
        for row in _query_rows(html, "models")
        if isinstance(row.get("id"), str)
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for route in _query_rows(html, "providers"):
        if not _private_chat_route(route):
            continue
        native_id = route.get("canonical_id") or route.get("model_id")
        if not isinstance(native_id, str):
            continue
        model_id = _canonical_model_id(native_id)
        if model_id is None:
            continue
        grouped.setdefault(model_id, []).append(route)

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for model_id, routes in sorted(grouped.items()):
        priced_routes: list[tuple[dict[str, Any], int, int, int | None]] = []
        for route in routes:
            pricing = route.get("pricing_usd")
            if not isinstance(pricing, dict):
                continue
            prompt = dollars_per_token_to_micro_per_m(pricing.get("prompt"))
            completion = dollars_per_token_to_micro_per_m(pricing.get("completion"))
            cached = dollars_per_token_to_micro_per_m(pricing.get("cached_prompt"))
            if prompt is None or completion is None or prompt <= 0 or completion <= 0:
                continue
            priced_routes.append((route, prompt, completion, cached))
        if not priced_routes:
            continue

        representative, _prompt, _completion, _cached = priced_routes[0]
        prompt = max(item[1] for item in priced_routes)
        completion = max(item[2] for item in priced_routes)
        cached_values = [item[3] for item in priced_routes if item[3] is not None]
        cached = max(cached_values) if cached_values else None
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )

        native_id = str(representative.get("canonical_id") or representative["model_id"])
        public_model = model_rows.get(native_id, {})
        architecture = representative.get("architecture")
        if not isinstance(architecture, dict):
            architecture = {}
        parameters = representative.get("supported_parameters")
        parameter_set = {
            str(item).casefold() for item in parameters
        } if isinstance(parameters, list) else set()
        features = ["chat", "completion", "private_inference", "teeml"]
        if "tools" in parameter_set:
            features.append("tools")
        if "response_format" in parameter_set:
            features.extend(["json_mode", "structured_outputs"])
        if parameter_set & {"thinking", "enable_thinking", "reasoning_effort"}:
            features.append("reasoning")
        if cached is not None:
            features.append("prompt_caching")

        display_name = str(
            public_model.get("name")
            or representative.get("name")
            or representative.get("model_id")
            or native_id
        )
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": display_name,
            "title": display_name,
            "model_type": "chat",
            "context_length": positive_int(representative.get("context_length"))
            or positive_int(public_model.get("context_length"))
            or 131072,
            "max_output_tokens": positive_int(
                representative.get("max_completion_tokens")
            )
            or positive_int(public_model.get("max_completion_tokens"))
            or 32768,
            "input_modalities": _modalities(
                architecture.get("input_modalities"), default=["text"]
            ),
            "output_modalities": _modalities(
                architecture.get("output_modalities"), default=["text"]
            ),
            "endpoints": ["chat/completions"],
            "supported_features": features,
            "supported_parameters": sorted(parameter_set),
            "status": 1,
            "routable": _LIVE_CANARY_OK,
            "routable_reason": None if _LIVE_CANARY_OK else "provider-canary-failed",
            "trust_mode": "private",
            "verifiability": "TeeML",
            "tee_attested": True,
            "tee_type": representative.get("tee_type"),
            "tee_verifier": representative.get("tee_verifier"),
            "private_provider_count": len(priced_routes),
            "pricing_policy": "maximum-active-private-route",
        }
        discovered[model_id] = row
    return prices, discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    html = fetch_html(URL)
    prices, discovered = parse_private_catalog(html)
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))

    api_key = os.environ.get("ZERO_G_API_KEY")
    _LIVE_CANARY_OK = probe_openai_chat(
        base_url=BASE_URL,
        api_key=api_key,
        model="0gm-1.0-35b-a3b",
        extra_headers=PRIVATE_TRUST_HEADERS,
        expected_content="PONG",
    )
    for row in discovered.values():
        row["routable"] = _LIVE_CANARY_OK
        if _LIVE_CANARY_OK:
            row.pop("routable_reason", None)
        else:
            row["routable_reason"] = "provider-canary-failed"
    _DISCOVERED_MANIFEST_ROWS = discovered

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"discovered {len(discovered)} healthy TeeML private chat models",
            f"private account canary {'passed' if _LIVE_CANARY_OK else 'not enabled'}",
            "TeeTLS and standard routes intentionally excluded",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
    set_manifest_canary_state(MANIFEST_PATH, healthy=_LIVE_CANARY_OK)
    return notes
