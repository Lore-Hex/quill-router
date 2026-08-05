#!/usr/bin/env python3
"""Deploy and publish every usable model in TrustedRouter's Azure account.

The Azure catalog is broader than an account's usable inference surface.  This
job therefore requires all of the following before a route reaches the public
manifest: active lifecycle, synchronous chat capability, remaining quota for a
pay-per-token SKU, an exact price, a successful deployment, and a direct PONG.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pricing.providers.azure import (  # noqa: E402, I001
    MANIFEST_PATH,
    URL as PRICING_URL,
    canonical_model_id,
    deployment_name,
    fetch_retail_rows,
    parse_retail_prices,
)

DEFAULT_SUBSCRIPTION = "2fc83893-ca6c-48e4-b090-8860fba33d33"
DEFAULT_RESOURCE_GROUP = "tr-model-providers"
DEFAULT_ACCOUNT = "trustedrouter-foundry-eastus2"
DEFAULT_LOCATION = "eastus2"
MANAGEMENT_API_VERSION = "2025-10-01-preview"
OPENAI_BASE_URL = "https://trustedrouter-foundry-eastus2.openai.azure.com/openai/v1"
ANTHROPIC_BASE_URL = "https://trustedrouter-foundry-eastus2.services.ai.azure.com/anthropic/v1"
CANARY_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)
_SKU_PREFERENCE = ("GlobalStandard", "DataZoneStandard", "Standard")
_ACTIVE_LIFECYCLES = frozenset({"GenerallyAvailable", "Preview"})


@dataclass(frozen=True)
class DeploymentCandidate:
    canonical_id: str
    native_name: str
    version: str
    model_format: str
    deployment_name: str
    sku: str
    capacity: int
    is_default_version: bool


def _run_json(command: list[str]) -> Any:
    completed = subprocess.run(  # noqa: S603 - argv only, never a shell
        command,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _azure_access_token() -> str:
    az_binary = shutil.which("az")
    if az_binary is None:
        raise RuntimeError("Azure CLI is not installed")
    completed = subprocess.run(  # noqa: S603 - resolved Azure CLI binary, no shell
        [
            az_binary,
            "account",
            "get-access-token",
            "--resource",
            "https://management.azure.com/",
            "--query",
            "accessToken",
            "--output",
            "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    token = completed.stdout.strip()
    if not token:
        raise RuntimeError("Azure CLI returned an empty management token")
    return token


def fetch_account_models(*, resource_group: str, account: str) -> list[dict[str, Any]]:
    payload = _run_json(
        [
            "az",
            "cognitiveservices",
            "account",
            "list-models",
            "--resource-group",
            resource_group,
            "--name",
            account,
            "--output",
            "json",
        ]
    )
    if not isinstance(payload, list):
        raise RuntimeError("Azure model catalog did not return a list")
    return [row for row in payload if isinstance(row, dict)]


def fetch_account_usage(*, location: str) -> list[dict[str, Any]]:
    payload = _run_json(
        [
            "az",
            "cognitiveservices",
            "usage",
            "list",
            "--location",
            location,
            "--output",
            "json",
        ]
    )
    if not isinstance(payload, list):
        raise RuntimeError("Azure usage endpoint did not return a list")
    return [row for row in payload if isinstance(row, dict)]


def _remaining_quota(usage_rows: list[dict[str, Any]]) -> dict[str, float]:
    remaining: dict[str, float] = {}
    for row in usage_rows:
        name = row.get("name")
        usage_name = name.get("value") if isinstance(name, dict) else None
        if not isinstance(usage_name, str):
            continue
        try:
            limit = float(row.get("limit") or 0)
            current = float(row.get("currentValue") or 0)
        except (TypeError, ValueError):
            continue
        remaining[usage_name] = max(0.0, limit - current)
    return remaining


def _version_key(value: str) -> tuple[tuple[int, str], ...]:
    parts = re.findall(r"\d+|[^\d]+", value)
    return tuple((1, f"{int(part):012d}") if part.isdigit() else (0, part) for part in parts)


def _choose_sku(model: dict[str, Any], remaining: dict[str, float]) -> tuple[str, int] | None:
    skus = model.get("skus")
    if not isinstance(skus, list):
        return None
    for wanted in _SKU_PREFERENCE:
        for sku in skus:
            if not isinstance(sku, dict) or sku.get("name") != wanted:
                continue
            usage_name = sku.get("usageName")
            if not isinstance(usage_name, str) or remaining.get(usage_name, 0) <= 0:
                continue
            # Fine-tuning quota sometimes appears under a GlobalStandard SKU.
            # It is not synchronous inference capacity.
            if "finetune" in usage_name.lower() or "fine-tune" in usage_name.lower():
                continue
            capacity = sku.get("capacity")
            minimum = capacity.get("minimum") if isinstance(capacity, dict) else None
            required_capacity = max(1, int(minimum or 1))
            if remaining.get(usage_name, 0) < required_capacity:
                continue
            return wanted, required_capacity
    return None


def select_deployment_candidates(
    model_rows: list[dict[str, Any]],
    usage_rows: list[dict[str, Any]],
    priced_model_ids: frozenset[str],
) -> list[DeploymentCandidate]:
    remaining = _remaining_quota(usage_rows)
    grouped: dict[str, list[tuple[dict[str, Any], tuple[str, int]]]] = {}
    for model in model_rows:
        if model.get("lifecycleStatus") not in _ACTIVE_LIFECYCLES:
            continue
        capabilities = model.get("capabilities")
        if (
            not isinstance(capabilities, dict)
            or str(capabilities.get("chatCompletion", "")).lower() != "true"
        ):
            continue
        native_name = model.get("name")
        version = model.get("version")
        model_format = model.get("format")
        if not all(
            isinstance(value, str) and value for value in (native_name, version, model_format)
        ):
            continue
        canonical_id = canonical_model_id(native_name)
        if canonical_id is None or canonical_id not in priced_model_ids:
            continue
        sku = _choose_sku(model, remaining)
        if sku is None:
            continue
        grouped.setdefault(native_name.lower(), []).append((model, sku))

    selected: list[DeploymentCandidate] = []
    for choices in grouped.values():
        choices.sort(
            key=lambda item: (
                bool(item[0].get("isDefaultVersion")),
                _version_key(str(item[0]["version"])),
            ),
            reverse=True,
        )
        model, (sku_name, capacity) = choices[0]
        native_name = str(model["name"])
        canonical_id = canonical_model_id(native_name)
        assert canonical_id is not None
        selected.append(
            DeploymentCandidate(
                canonical_id=canonical_id,
                native_name=native_name,
                version=str(model["version"]),
                model_format=str(model["format"]),
                deployment_name=deployment_name(native_name),
                sku=sku_name,
                capacity=capacity,
                is_default_version=bool(model.get("isDefaultVersion")),
            )
        )
    return sorted(selected, key=lambda candidate: candidate.canonical_id)


class AzureManagementClient:
    def __init__(
        self,
        *,
        token: str,
        subscription: str,
        resource_group: str,
        account: str,
    ) -> None:
        self._client = httpx.Client(
            timeout=CANARY_TIMEOUT,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        self._subscription_base = (
            f"https://management.azure.com/subscriptions/{quote(subscription)}"
        )
        self._base = (
            self._subscription_base + f"/resourceGroups/{quote(resource_group)}"
            "/providers/Microsoft.CognitiveServices"
            f"/accounts/{quote(account)}"
        )

    def close(self) -> None:
        self._client.close()

    def list_deployments(self) -> dict[str, dict[str, Any]]:
        response = self._client.get(
            f"{self._base}/deployments?api-version={MANAGEMENT_API_VERSION}"
        )
        response.raise_for_status()
        rows = response.json().get("value", [])
        return {
            str(row["name"]): row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("name"), str)
        }

    def account_key(self) -> str:
        response = self._client.post(f"{self._base}/listKeys?api-version={MANAGEMENT_API_VERSION}")
        response.raise_for_status()
        key = response.json().get("key1")
        if not isinstance(key, str) or not key:
            raise RuntimeError("Azure resource returned no account key")
        return key

    def marketplace_terms_accepted(self, candidate: DeploymentCandidate) -> bool:
        if candidate.model_format != "Anthropic":
            return True
        offer = f"anthropic-{candidate.native_name}-offer"
        plan = f"anthropic-{candidate.native_name}-plan-new"
        response = self._client.get(
            f"{self._subscription_base}/providers/Microsoft.MarketplaceOrdering"
            "/offerTypes/virtualmachine/publishers/anthropic"
            f"/offers/{quote(offer)}/plans/{quote(plan)}"
            "/agreements/current?api-version=2021-01-01"
        )
        response.raise_for_status()
        properties = response.json().get("properties")
        return isinstance(properties, dict) and properties.get("accepted") is True

    def deploy(self, candidate: DeploymentCandidate) -> None:
        properties: dict[str, Any] = {
            "model": {
                "format": candidate.model_format,
                "name": candidate.native_name,
                "version": candidate.version,
            },
            "versionUpgradeOption": "OnceNewDefaultVersionAvailable",
            "raiPolicyName": "Microsoft.DefaultV2",
        }
        if candidate.model_format == "Anthropic":
            properties["modelProviderData"] = {
                "organizationName": "Lore Hex Corp",
                "countryCode": "US",
                "industry": "technology",
            }
        response = self._client.put(
            f"{self._base}/deployments/{quote(candidate.deployment_name)}"
            f"?api-version={MANAGEMENT_API_VERSION}",
            json={
                "sku": {"name": candidate.sku, "capacity": candidate.capacity},
                "properties": properties,
            },
        )
        response.raise_for_status()
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            current = self._client.get(
                f"{self._base}/deployments/{quote(candidate.deployment_name)}"
                f"?api-version={MANAGEMENT_API_VERSION}"
            )
            current.raise_for_status()
            state = current.json().get("properties", {}).get("provisioningState")
            if state == "Succeeded":
                return
            if state in {"Failed", "Canceled"}:
                raise RuntimeError(f"Azure deployment {candidate.deployment_name} ended in {state}")
            time.sleep(5)
        raise TimeoutError(f"Azure deployment timed out: {candidate.deployment_name}")


def _extract_openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def _stream_text(lines: Iterable[str], *, protocol: str) -> str:
    parts: list[str] = []
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if protocol == "anthropic":
            delta = payload.get("delta")
            text = delta.get("text") if isinstance(delta, dict) else None
        elif protocol == "openai":
            choices = payload.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            delta = choice.get("delta") if isinstance(choice, dict) else None
            text = delta.get("content") if isinstance(delta, dict) else None
        else:
            raise ValueError(f"unknown Azure canary protocol: {protocol}")
        if isinstance(text, str):
            parts.append(text)
            # The canary contract is exact PONG presence, not graceful stream
            # shutdown. Stop reading as soon as it is proven so a provider
            # cannot hold the catalog refresh open after sending valid output.
            if "PONG" in "".join(parts).upper():
                break
    return "".join(parts).strip()


def canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
    if candidate.model_format == "Anthropic":
        with httpx.stream(
            "POST",
            f"{ANTHROPIC_BASE_URL}/messages",
            headers={"x-api-key": account_key, "anthropic-version": "2023-06-01"},
            json={
                "model": candidate.deployment_name,
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "Reply with exactly PONG"}],
            },
            timeout=CANARY_TIMEOUT,
        ) as response:
            response.raise_for_status()
            text = _stream_text(response.iter_lines(), protocol="anthropic")
    else:
        model_name = candidate.deployment_name.lower()
        token_field = (
            "max_completion_tokens"
            if model_name.startswith(("gpt-5", "o1", "o3", "o4"))
            else "max_tokens"
        )
        request: dict[str, Any] = {
            "model": candidate.deployment_name,
            token_field: 256,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply with exactly PONG"}],
        }
        if candidate.canonical_id != "mistralai/codestral-2501":
            request["stream_options"] = {"include_usage": True}
        with httpx.stream(
            "POST",
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"api-key": account_key},
            json=request,
            timeout=120,
        ) as response:
            response.raise_for_status()
            text = _stream_text(response.iter_lines(), protocol="openai")
    if "PONG" not in text.upper():
        raise RuntimeError(f"Azure canary returned no PONG for {candidate.deployment_name}")


def _retryable_canary_error(exc: Exception) -> bool:
    # A connection failure may be transient. Once connected, 30 seconds with
    # no response bytes is a route-health failure, not a useful retry signal;
    # retrying it made one hung model stall every account refresh for minutes.
    if isinstance(exc, (httpx.ConnectTimeout, httpx.NetworkError)) and not isinstance(
        exc, httpx.ReadTimeout
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def canary_with_retries(
    candidate: DeploymentCandidate,
    *,
    account_key: str,
    max_attempts: int = 3,
) -> None:
    for attempt in range(max_attempts):
        try:
            canary(candidate, account_key=account_key)
            return
        except Exception as exc:
            if attempt + 1 >= max_attempts or not _retryable_canary_error(exc):
                raise
            time.sleep(2**attempt)


def manifest_row(candidate: DeploymentCandidate, price: Any) -> dict[str, Any]:
    tier = price.tiers[0]
    row: dict[str, Any] = {
        "id": candidate.canonical_id,
        "upstream_id": candidate.deployment_name,
        "display_name": candidate.native_name,
        "title": candidate.native_name,
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "input_token_price_per_m": tier.prompt_micro_per_m,
        "output_token_price_per_m": tier.completion_micro_per_m,
        "status": 1,
        "routable": True,
        "azure_model_name": candidate.native_name,
        "azure_model_version": candidate.version,
        "azure_model_format": candidate.model_format,
        "azure_deployment_sku": candidate.sku,
    }
    if tier.prompt_cached_micro_per_m is not None:
        row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
    if candidate.canonical_id == "microsoft/phi-4-multimodal-instruct":
        row["input_modalities"] = ["text", "image", "audio"]
    return row


def write_manifest(rows: list[dict[str, Any]]) -> None:
    payload = {
        "_about": (
            "Azure AI Foundry deployments verified for this TrustedRouter subscription. "
            "The automatic sync publishes only synchronous chat deployments with "
            "remaining quota, exact pricing, and a successful direct PONG canary."
        ),
        "provider": "azure",
        "source": (
            "https://management.azure.com/providers/Microsoft.CognitiveServices/"
            "locations/eastus2/models"
        ),
        "pricing_source": PRICING_URL,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "price_scale": "microdollars_per_million",
        "model_count": len(rows),
        "models": sorted(rows, key=lambda row: str(row["id"])),
    }
    MANIFEST_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _load_json(path: str | None, fetcher: Any) -> list[dict[str, Any]]:
    if path is None:
        return fetcher()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON list in {path}")
    return [row for row in payload if isinstance(row, dict)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--models-json")
    parser.add_argument("--usage-json")
    parser.add_argument("--prices-json")
    parser.add_argument(
        "--subscription", default=os.environ.get("AZURE_SUBSCRIPTION_ID", DEFAULT_SUBSCRIPTION)
    )
    parser.add_argument("--resource-group", default=DEFAULT_RESOURCE_GROUP)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--location", default=DEFAULT_LOCATION)
    args = parser.parse_args()

    models = _load_json(
        args.models_json,
        lambda: fetch_account_models(resource_group=args.resource_group, account=args.account),
    )
    usage = _load_json(args.usage_json, lambda: fetch_account_usage(location=args.location))
    retail_rows = _load_json(args.prices_json, fetch_retail_rows)
    prices = parse_retail_prices(retail_rows)
    candidates = select_deployment_candidates(models, usage, frozenset(prices))
    print(f"Azure: {len(candidates)} deployable synchronous priced model(s)")
    if not args.apply:
        for candidate in candidates:
            print(f"  {candidate.canonical_id} -> {candidate.deployment_name} ({candidate.sku})")
        return 0

    management = AzureManagementClient(
        token=_azure_access_token(),
        subscription=args.subscription,
        resource_group=args.resource_group,
        account=args.account,
    )
    healthy_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        existing = management.list_deployments()
        account_key = management.account_key()
        for candidate in candidates:
            try:
                if not management.marketplace_terms_accepted(candidate):
                    raise RuntimeError(
                        "Azure Marketplace terms are not accepted for this Anthropic model"
                    )
                current = existing.get(candidate.deployment_name)
                properties = current.get("properties", {}) if isinstance(current, dict) else {}
                current_model = properties.get("model", {}) if isinstance(properties, dict) else {}
                needs_deploy = (
                    not isinstance(current_model, dict)
                    or current_model.get("name") != candidate.native_name
                    or str(current_model.get("version")) != candidate.version
                )
                if needs_deploy:
                    print(f"Azure: deploying {candidate.canonical_id}", flush=True)
                    management.deploy(candidate)
                canary_with_retries(candidate, account_key=account_key)
                healthy_rows.append(manifest_row(candidate, prices[candidate.canonical_id]))
                print(f"Azure: PONG {candidate.canonical_id}", flush=True)
            except Exception as exc:  # noqa: BLE001 - isolate one upstream model
                failures.append(f"{candidate.canonical_id}: {type(exc).__name__}: {exc}")
                print(f"Azure: dark {failures[-1]}", file=sys.stderr, flush=True)
    finally:
        management.close()

    if not healthy_rows:
        raise RuntimeError("Azure sync produced no healthy model routes; manifest unchanged")
    write_manifest(healthy_rows)
    print(f"Azure: published {len(healthy_rows)} healthy route(s)")
    if failures:
        print(f"Azure: {len(failures)} model(s) remain dark", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
