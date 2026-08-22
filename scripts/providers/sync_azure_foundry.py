#!/usr/bin/env python3
"""Deploy and publish every usable model in TrustedRouter's Azure account.

The Azure catalog is broader than an account's usable inference surface.  This
job therefore requires all of the following before a route reaches the public
manifest: active lifecycle, synchronous chat capability, remaining quota for a
pay-per-token SKU, an exact price, a successful deployment, and direct text,
tool-call, and (where advertised) image capability canaries.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
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
    DISCOVERY_URL,
    MANIFEST_PATH,
    URL as PRICING_URL,
    canonical_model_id,
    deployment_name,
    fetch_retail_rows,
    parse_retail_prices,
    retail_model_ids,
    retail_model_versions,
)

DEFAULT_SUBSCRIPTION = "2fc83893-ca6c-48e4-b090-8860fba33d33"
DEFAULT_RESOURCE_GROUP = "tr-model-providers"
DEFAULT_ACCOUNT = "trustedrouter-foundry-eastus2"
DEFAULT_LOCATION = "eastus2"
MANAGEMENT_API_VERSION = "2025-10-01-preview"
OPENAI_BASE_URL = "https://trustedrouter-foundry-eastus2.openai.azure.com/openai/v1"
ANTHROPIC_BASE_URL = "https://trustedrouter-foundry-eastus2.services.ai.azure.com/anthropic/v1"
CANARY_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=10)
DEPLOYMENT_VERSION_UPGRADE_OPTION = "NoAutoUpgrade"
MINIMUM_LAUNCH_CAPACITY = 10
IMAGE_CANARY_OUTPUT_TOKEN_BUDGET = 4096
_IMAGE_CANARY_MODEL_IDS = frozenset(
    {
        "moonshotai/kimi-k2.5",
        "moonshotai/kimi-k2.6",
        "moonshotai/kimi-k2.7-code",
        "openai/gpt-5-mini",
        "x-ai/grok-4.20-reasoning",
    }
)
# Prevalidated deterministic 64x64 solid RGB PNGs avoid third-party URLs. Two
# distinct assets are cryptographically selected for each admission attempt,
# so a model cannot pass by returning one canned color without reading images.
_IMAGE_CANARY_ASSETS = (
    (
        "RED",
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAS0lEQVR42u3PQQkAAAgAsetfWiP4FgYrsKZe"
        "S0BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEDgsqnc8OJg6Ln3AAAAAElF"
        "TkSuQmCC",
    ),
    (
        "GREEN",
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAS0lEQVR42u3PQQkAAAgAsetfWiP4FgYrsJp+"
        "ExAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBC4LLjs8OJxKlMxAAAAAElF"
        "TkSuQmCC",
    ),
    (
        "BLUE",
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAS0lEQVR42u3PQQkAAAgAsetfWiP4FgYrsGqe"
        "ExAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBA4LMf88OL0EKXAAAAAAElF"
        "TkSuQmCC",
    ),
    (
        "YELLOW",
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAATElEQVR42u3PMQkAAAwDsPo33UnoPQjEQNLm"
        "tQgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgILAcyl+HSEp61MQAAAABJRU5E"
        "rkJggg==",
    ),
)
# The pricing adapter intentionally admits only global meters. Azure's Data
# Zone and regional SKUs have different rates; selecting one while attaching a
# global price underbills requests (for example GPT-5.4 Mini is 10% higher in
# Data Zone as of 2026-08-20). Add SKU-keyed pricing before broadening this.
_SKU_PREFERENCE = ("GlobalStandard",)
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


def deployment_needs_reconcile(
    current: dict[str, Any] | None,
    candidate: DeploymentCandidate,
) -> bool:
    """Return whether an existing deployment is unsafe to reuse.

    The selected capacity is a minimum, so a deployment with more capacity can
    remain in place. Missing or malformed SKU/upgrade metadata fails closed
    because the manifest price is valid only for the candidate's exact model
    version and SKU.
    """
    if not isinstance(current, dict):
        return True
    properties = current.get("properties")
    current_model = properties.get("model") if isinstance(properties, dict) else None
    if (
        not isinstance(current_model, dict)
        or current_model.get("name") != candidate.native_name
        or str(current_model.get("version")) != candidate.version
        or properties.get("versionUpgradeOption") != DEPLOYMENT_VERSION_UPGRADE_OPTION
    ):
        return True

    sku = current.get("sku")
    if not isinstance(sku, dict) or sku.get("name") != candidate.sku:
        return True
    capacity = sku.get("capacity")
    if not isinstance(capacity, int) or isinstance(capacity, bool):
        return True
    return capacity < candidate.capacity


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
            if not isinstance(usage_name, str):
                continue
            available_capacity = remaining.get(usage_name)
            if (
                available_capacity is None
                or not math.isfinite(available_capacity)
                or available_capacity <= 0
            ):
                continue
            # Fine-tuning quota sometimes appears under a GlobalStandard SKU.
            # It is not synchronous inference capacity.
            if "finetune" in usage_name.lower() or "fine-tune" in usage_name.lower():
                continue
            capacity = sku.get("capacity")
            if capacity is not None and not isinstance(capacity, dict):
                continue
            minimum = capacity.get("minimum") if isinstance(capacity, dict) else None
            if minimum is None:
                catalog_minimum = 0
            elif (
                not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or minimum < 0
            ):
                continue
            else:
                catalog_minimum = minimum
            required_capacity = max(MINIMUM_LAUNCH_CAPACITY, catalog_minimum)
            if available_capacity < required_capacity:
                continue
            return wanted, required_capacity
    return None


def select_deployment_candidates(
    model_rows: list[dict[str, Any]],
    usage_rows: list[dict[str, Any]],
    priced_model_ids: frozenset[str],
    *,
    allowed_versions: Mapping[str, frozenset[str]] | None = None,
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
        if allowed_versions is not None and version not in allowed_versions.get(
            canonical_id, frozenset()
        ):
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
            # Prices are selected for this exact model version. Letting Azure
            # silently move the deployment to a future default would serve a
            # different checkpoint under stale billing metadata.
            "versionUpgradeOption": DEPLOYMENT_VERSION_UPGRADE_OPTION,
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
            current_payload = current.json()
            current_properties = (
                current_payload.get("properties") if isinstance(current_payload, dict) else None
            )
            state = (
                current_properties.get("provisioningState")
                if isinstance(current_properties, dict)
                else None
            )
            if state == "Succeeded":
                if deployment_needs_reconcile(current_payload, candidate):
                    raise RuntimeError(
                        f"Azure deployment {candidate.deployment_name} succeeded with an "
                        "unexpected model, version, upgrade policy, SKU, or capacity"
                    )
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


def _stream_canary(
    lines: Iterable[str],
    *,
    protocol: str,
) -> tuple[str, Any, bool]:
    parts: list[str] = []
    terminal_usage: Any = None
    saw_done = False
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            saw_done = True
            break
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
            if "usage" in payload:
                terminal_usage = payload["usage"]
            choices = payload.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            delta = choice.get("delta") if isinstance(choice, dict) else None
            text = delta.get("content") if isinstance(delta, dict) else None
        else:
            raise ValueError(f"unknown Azure canary protocol: {protocol}")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip(), terminal_usage, saw_done


def _stream_text(lines: Iterable[str], *, protocol: str) -> str:
    text, _usage, _saw_done = _stream_canary(lines, protocol=protocol)
    return text


def _validate_openai_stream_usage(
    usage: Any,
    *,
    saw_done: bool,
    deployment_name: str,
) -> None:
    if not saw_done or not isinstance(usage, dict):
        raise RuntimeError(
            f"Azure text canary returned no terminal usage for {deployment_name}"
        )
    prompt_tokens = usage.get("prompt_tokens")
    if (
        not isinstance(prompt_tokens, int)
        or isinstance(prompt_tokens, bool)
        or prompt_tokens <= 0
    ):
        raise RuntimeError(
            f"Azure text canary returned invalid prompt usage for {deployment_name}"
        )

    completion_tokens = usage.get("completion_tokens")
    if completion_tokens is not None and (
        not isinstance(completion_tokens, int)
        or isinstance(completion_tokens, bool)
        or completion_tokens < 0
    ):
        raise RuntimeError(
            f"Azure text canary returned invalid completion usage for {deployment_name}"
        )
    total_tokens = usage.get("total_tokens")
    minimum_total = prompt_tokens + (
        completion_tokens if isinstance(completion_tokens, int) else 0
    )
    if total_tokens is not None and (
        not isinstance(total_tokens, int)
        or isinstance(total_tokens, bool)
        or total_tokens < minimum_total
    ):
        raise RuntimeError(
            f"Azure text canary returned incoherent total usage for {deployment_name}"
        )

    output_tokens = completion_tokens if isinstance(completion_tokens, int) else 0
    if output_tokens <= 0 and isinstance(total_tokens, int):
        output_tokens = total_tokens - prompt_tokens
    if output_tokens <= 0:
        raise RuntimeError(
            f"Azure text canary returned no positive output usage for {deployment_name}"
        )


def _openai_token_field(candidate: DeploymentCandidate) -> str:
    model_name = candidate.deployment_name.lower()
    if model_name.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    request: dict[str, Any],
) -> dict[str, Any]:
    response = httpx.post(
        url,
        headers=headers,
        json=request,
        timeout=CANARY_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Azure capability canary returned a non-object response")
    return payload


def _validate_openai_tool_call(payload: dict[str, Any], *, deployment_name: str) -> None:
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
    if (
        not isinstance(choice, dict)
        or choice.get("finish_reason") != "tool_calls"
        or not isinstance(tool_calls, list)
        or len(tool_calls) != 1
    ):
        raise RuntimeError(
            f"Azure tool canary returned no single structured tool call for {deployment_name}"
        )
    tool_call = tool_calls[0]
    function = tool_call.get("function") if isinstance(tool_call, dict) else None
    arguments = function.get("arguments") if isinstance(function, dict) else None
    if (
        not isinstance(tool_call, dict)
        or not isinstance(tool_call.get("id"), str)
        or not tool_call["id"].strip()
        or tool_call.get("type") != "function"
        or not isinstance(function, dict)
        or function.get("name") != "pong"
        or not isinstance(arguments, str)
    ):
        raise RuntimeError(f"Azure tool canary returned an invalid call for {deployment_name}")
    try:
        decoded_arguments = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Azure tool canary returned invalid JSON arguments for {deployment_name}"
        ) from exc
    if decoded_arguments != {}:
        raise RuntimeError(
            f"Azure tool canary returned non-empty arguments for {deployment_name}"
        )


def _validate_anthropic_tool_use(payload: dict[str, Any], *, deployment_name: str) -> None:
    content = payload.get("content")
    if (
        payload.get("stop_reason") != "tool_use"
        or not isinstance(content, list)
        or len(content) != 1
    ):
        raise RuntimeError(
            f"Azure tool canary returned no single structured tool use for {deployment_name}"
        )
    tool_use = content[0]
    if (
        not isinstance(tool_use, dict)
        or tool_use.get("type") != "tool_use"
        or not isinstance(tool_use.get("id"), str)
        or not tool_use["id"].strip()
        or tool_use.get("name") != "pong"
        or tool_use.get("input") != {}
    ):
        raise RuntimeError(f"Azure tool canary returned an invalid use for {deployment_name}")


def _tool_canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
    prompt = "Call the pong tool now. Do not answer in text."
    if candidate.model_format == "Anthropic":
        payload = _post_json(
            f"{ANTHROPIC_BASE_URL}/messages",
            headers={"x-api-key": account_key, "anthropic-version": "2023-06-01"},
            request={
                "model": candidate.deployment_name,
                "max_tokens": 64,
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "tools": [
                    {
                        "name": "pong",
                        "description": "Confirm tool-call support.",
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
                ],
                "tool_choice": {
                    "type": "tool",
                    "name": "pong",
                    "disable_parallel_tool_use": True,
                },
            },
        )
        _validate_anthropic_tool_use(payload, deployment_name=candidate.deployment_name)
        return

    payload = _post_json(
        f"{OPENAI_BASE_URL}/chat/completions",
        headers={"api-key": account_key},
        request={
            "model": candidate.deployment_name,
            _openai_token_field(candidate): 1024,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "pong",
                        "description": "Confirm tool-call support.",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "pong"}},
        },
    )
    _validate_openai_tool_call(payload, deployment_name=candidate.deployment_name)


def _image_canary_challenges() -> list[tuple[str, str]]:
    return secrets.SystemRandom().sample(list(_IMAGE_CANARY_ASSETS), k=2)


def _image_canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
    allowed_labels = frozenset(label.casefold() for label, _data_url in _IMAGE_CANARY_ASSETS)
    labels = ", ".join(sorted(label for label, _data_url in _IMAGE_CANARY_ASSETS))
    challenges = _image_canary_challenges()
    expected_labels = tuple(label.casefold() for label, _data_url in challenges)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Two images follow in order. Identify each image's single solid color. "
                "Reply with exactly two comma-separated labels in image order. "
                f"Allowed labels: {labels}."
            ),
        }
    ]
    content.extend(
        {"type": "image_url", "image_url": {"url": data_url}}
        for _label, data_url in challenges
    )
    payload = _post_json(
        f"{OPENAI_BASE_URL}/chat/completions",
        headers={"api-key": account_key},
        request={
            "model": candidate.deployment_name,
            _openai_token_field(candidate): IMAGE_CANARY_OUTPUT_TOKEN_BUDGET,
            "stream": False,
            "messages": [{"role": "user", "content": content}],
        },
    )
    text = _extract_openai_text(payload)
    answer_parts = text.split(",")
    answer_labels = tuple(part.strip().casefold() for part in answer_parts)
    if (
        len(answer_labels) != 2
        or any(label not in allowed_labels for label in answer_labels)
        or answer_labels != expected_labels
    ):
        raise RuntimeError(
            f"Azure image canary did not identify the embedded images for "
            f"{candidate.deployment_name}"
        )


def _text_canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
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
            text, _usage, _saw_done = _stream_canary(
                response.iter_lines(), protocol="anthropic"
            )
    else:
        request: dict[str, Any] = {
            "model": candidate.deployment_name,
            _openai_token_field(candidate): 256,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply with exactly PONG"}],
        }
        request["stream_options"] = {"include_usage": True}
        with httpx.stream(
            "POST",
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"api-key": account_key},
            json=request,
            timeout=CANARY_TIMEOUT,
        ) as response:
            response.raise_for_status()
            text, usage, saw_done = _stream_canary(response.iter_lines(), protocol="openai")
        _validate_openai_stream_usage(
            usage,
            saw_done=saw_done,
            deployment_name=candidate.deployment_name,
        )
    if "PONG" not in text.upper():
        raise RuntimeError(f"Azure canary returned no PONG for {candidate.deployment_name}")


def canary(candidate: DeploymentCandidate, *, account_key: str) -> None:
    _text_canary(candidate, account_key=account_key)
    _tool_canary(candidate, account_key=account_key)
    if candidate.canonical_id in _IMAGE_CANARY_MODEL_IDS:
        if candidate.model_format == "Anthropic":
            raise RuntimeError(
                f"Azure image canary has no Anthropic request path for {candidate.deployment_name}"
            )
        _image_canary(candidate, account_key=account_key)


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


def _canary_retry_delay(exc: Exception, *, attempt: int) -> float:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        raw_retry_after = exc.response.headers.get("Retry-After")
        try:
            retry_after = float(raw_retry_after) if raw_retry_after is not None else math.nan
        except ValueError:
            retry_after = math.nan
        if math.isfinite(retry_after) and retry_after >= 0:
            return retry_after
        return 60.0
    return float(2**attempt)


def _canary_phase_with_retries(
    phase: Callable[..., None],
    candidate: DeploymentCandidate,
    *,
    account_key: str,
    max_attempts: int,
) -> None:
    for attempt in range(max_attempts):
        try:
            phase(candidate, account_key=account_key)
            return
        except Exception as exc:
            if attempt + 1 >= max_attempts or not _retryable_canary_error(exc):
                raise
            time.sleep(_canary_retry_delay(exc, attempt=attempt))


def canary_with_retries(
    candidate: DeploymentCandidate,
    *,
    account_key: str,
    max_attempts: int = 3,
) -> None:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    _canary_phase_with_retries(
        _text_canary,
        candidate,
        account_key=account_key,
        max_attempts=max_attempts,
    )
    _canary_phase_with_retries(
        _tool_canary,
        candidate,
        account_key=account_key,
        max_attempts=max_attempts,
    )
    if candidate.canonical_id in _IMAGE_CANARY_MODEL_IDS:
        if candidate.model_format == "Anthropic":
            raise RuntimeError(
                f"Azure image canary has no Anthropic request path for "
                f"{candidate.deployment_name}"
            )
        _canary_phase_with_retries(
            _image_canary,
            candidate,
            account_key=account_key,
            max_attempts=max_attempts,
        )


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
    if len(price.tiers) > 1:
        row["price_tiers"] = [
            {
                "max_prompt_tokens": price_tier.max_prompt_tokens,
                "input_token_price_per_m": price_tier.prompt_micro_per_m,
                "output_token_price_per_m": price_tier.completion_micro_per_m,
                **(
                    {
                        "cached_input_token_price_per_m": (
                            price_tier.prompt_cached_micro_per_m
                        )
                    }
                    if price_tier.prompt_cached_micro_per_m is not None
                    else {}
                ),
            }
            for price_tier in price.tiers
        ]
    if candidate.canonical_id in _IMAGE_CANARY_MODEL_IDS:
        row["input_modalities"] = ["text", "image"]
    elif candidate.canonical_id == "microsoft/phi-4-multimodal-instruct":
        row["input_modalities"] = ["text", "image", "audio"]
    return row


def write_manifest(rows: list[dict[str, Any]]) -> bool:
    stable_payload = {
        "_about": (
            "Azure AI Foundry deployments verified for this TrustedRouter subscription. "
            "The account sync publishes only synchronous chat deployments with "
            "remaining quota, exact pricing, and successful direct text, tool-call, "
            "and required image capability canaries."
        ),
        "provider": "azure",
        "source": DISCOVERY_URL,
        "pricing_source": PRICING_URL,
        "price_scale": "microdollars_per_million",
        "model_count": len(rows),
        "models": sorted(rows, key=lambda row: str(row["id"])),
    }
    if MANIFEST_PATH.exists():
        existing = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            existing_without_timestamp = dict(existing)
            existing_without_timestamp.pop("generated_at", None)
            if existing_without_timestamp == stable_payload:
                return False
    payload = {
        **stable_payload,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    MANIFEST_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def _load_json(path: str | None, fetcher: Any) -> list[dict[str, Any]]:
    if path is None:
        return fetcher()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON list in {path}")
    return [row for row in payload if isinstance(row, dict)]


def _admit_candidates(
    candidates: Iterable[DeploymentCandidate],
    prices: Mapping[str, Any],
    *,
    management: AzureManagementClient,
    existing: Mapping[str, dict[str, Any]],
    account_key: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Deploy and canary candidates without letting one failure hide another."""
    healthy_rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for candidate in candidates:
        try:
            if not management.marketplace_terms_accepted(candidate):
                raise RuntimeError(
                    "Azure Marketplace terms are not accepted for this Anthropic model"
                )
            current = existing.get(candidate.deployment_name)
            if deployment_needs_reconcile(current, candidate):
                print(f"Azure: deploying {candidate.canonical_id}", flush=True)
                management.deploy(candidate)
            canary_with_retries(candidate, account_key=account_key)
            healthy_rows.append(manifest_row(candidate, prices[candidate.canonical_id]))
            print(f"Azure: capabilities verified {candidate.canonical_id}", flush=True)
        except Exception as exc:  # noqa: BLE001 - isolate one upstream model
            failures.append(f"{candidate.canonical_id}: {type(exc).__name__}: {exc}")
            print(f"Azure: dark {failures[-1]}", file=sys.stderr, flush=True)
    return healthy_rows, failures


def _publish_admission(
    candidates: Iterable[DeploymentCandidate],
    healthy_rows: list[dict[str, Any]],
    failures: list[str],
) -> bool:
    """Write only a complete, exact launch set; failed admission is immutable."""
    expected_ids = retail_model_ids()
    candidate_list = list(candidates)
    candidate_ids = {candidate.canonical_id for candidate in candidate_list}
    healthy_ids = {
        str(row["id"])
        for row in healthy_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    missing_candidates = sorted(expected_ids - candidate_ids)
    missing_healthy = sorted(expected_ids - healthy_ids)
    unexpected_candidates = sorted(candidate_ids - expected_ids)
    unexpected_healthy = sorted(healthy_ids - expected_ids)
    malformed_candidates = len(candidate_list) != len(candidate_ids)
    malformed_healthy = len(healthy_rows) != len(healthy_ids)
    if (
        missing_candidates
        or missing_healthy
        or unexpected_candidates
        or unexpected_healthy
        or malformed_candidates
        or malformed_healthy
        or failures
    ):
        raise RuntimeError(
            "Azure admission failed the exact "
            f"{len(expected_ids)}-route launch contract before manifest write: "
            f"missing_candidates={missing_candidates}, missing_healthy={missing_healthy}, "
            f"unexpected_candidates={unexpected_candidates}, "
            f"unexpected_healthy={unexpected_healthy}, "
            f"malformed_candidates={malformed_candidates}, "
            f"malformed_healthy={malformed_healthy}, failures={failures}"
        )
    return write_manifest(healthy_rows)


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
    version_contract = retail_model_versions()
    candidate_pool = select_deployment_candidates(
        models,
        usage,
        frozenset(version_contract),
        allowed_versions=version_contract,
    )
    prices = parse_retail_prices(
        retail_rows,
        model_versions={candidate.canonical_id: candidate.version for candidate in candidate_pool},
    )
    candidates = [
        candidate for candidate in candidate_pool if candidate.canonical_id in prices
    ]
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
    try:
        existing = management.list_deployments()
        account_key = management.account_key()
        healthy_rows, failures = _admit_candidates(
            candidates,
            prices,
            management=management,
            existing=existing,
            account_key=account_key,
        )
    finally:
        management.close()

    changed = _publish_admission(candidates, healthy_rows, failures)
    state = "updated" if changed else "unchanged"
    print(f"Azure: published {len(healthy_rows)} healthy route(s); manifest {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
