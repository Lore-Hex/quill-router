"""Neurometric canonical model catalog, pricing, and account canary."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import probe_openai_chat
from scripts.pricing.provider_contract_catalog import (
    discover_provider_contract_catalog,
)
from trusted_router.provider_contract import PROVIDER_MODEL_DOCUMENTATION_EXAMPLE

SLUG = "neurometric"
BASE_URL = "https://wharf.neurometric.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "neurometric.json"
)
DOCUMENT_STRUCTURED_EXTRACTION_MODEL = "neurometric/document-structured-extraction"
GROUNDED_DOCUMENT_QA_MODEL = "neurometric/grounded-document-qa"
CONVERSATION_SUMMARY_MODEL = "neurometric/conversation-summary"
CLASSIFICATION_ROUTER_MODEL = "neurometric/classification-router"
EXPECTED_MODELS = [
    "ibm-granite/granite-4.1-8b",
    DOCUMENT_STRUCTURED_EXTRACTION_MODEL,
    GROUNDED_DOCUMENT_QA_MODEL,
    CONVERSATION_SUMMARY_MODEL,
    CLASSIFICATION_ROUTER_MODEL,
]
TASK_DOCUMENTATION = {
    DOCUMENT_STRUCTURED_EXTRACTION_MODEL: PROVIDER_MODEL_DOCUMENTATION_EXAMPLE,
    GROUNDED_DOCUMENT_QA_MODEL: {
        "description": (
            "Answer questions from supplied document text and return citations grounded in "
            "that text."
        ),
        "input_format": (
            "Send the source document and question in the user message. Number passages when "
            "stable citation identifiers matter."
        ),
        "output_format": (
            "A JSON object with answer and citations fields; citations identify the supporting "
            "source passages."
        ),
        "example_input": (
            "Document:\n[1] The Acme renewal date is October 15, 2026.\n\n"
            "Question: What is the Acme renewal date? Answer only from the document."
        ),
        "example_output": (
            '{\n  "answer": "October 15, 2026",\n  "citations": ["1"]\n}'
        ),
    },
    CONVERSATION_SUMMARY_MODEL: {
        "description": "Turn a conversation transcript into a structured operational summary.",
        "input_format": (
            "Send a transcript with speakers clearly labeled. Include dates and owners in the "
            "transcript when they should be retained."
        ),
        "output_format": (
            "A JSON object with decision, current_status, open_items, and risks; open items "
            "include action, owner, and due_date."
        ),
        "example_input": (
            "Summarize this conversation:\nAlice: The launch moves to Friday.\n"
            "Bob: I will update the release calendar.\nAlice: Please notify support."
        ),
        "example_output": (
            '{\n  "decision": "The launch is moved to Friday.",\n'
            '  "current_status": "Launch date updated to Friday.",\n'
            '  "open_items": [\n'
            '    {"action": "Update the release calendar", "owner": "Bob", '
            '"due_date": null},\n'
            '    {"action": "Notify support", "owner": "Alice", "due_date": null}\n'
            '  ],\n  "risks": []\n}'
        ),
    },
    CLASSIFICATION_ROUTER_MODEL: {
        "description": "Classify one or more requests into caller-provided route labels.",
        "input_format": "Provide the allowed labels and the request or requests to classify.",
        "output_format": (
            "A JSON object mapping each request identifier to one allowed label."
        ),
        "example_input": (
            "Classify this request as exactly one of billing, technical, or sales: "
            "My invoice contains the wrong tax amount."
        ),
        "example_output": '{"request_1":"billing"}',
    },
}
TOOL_CHOICE_MODELS = frozenset({"neurometric/tool-choice"})
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = False


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    api_key = os.environ.get("NEUROMETRIC_API_KEY")
    if not api_key:
        raise RuntimeError("NEUROMETRIC_API_KEY is required for model discovery")
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(
            URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": PROVIDER_FETCH_UA,
            },
        )
        response.raise_for_status()
        payload = response.json()

    prices, discovered = discover_provider_contract_catalog(
        payload,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    for model_id, documentation in TASK_DOCUMENTATION.items():
        if model_id in discovered:
            discovered[model_id].setdefault("documentation", documentation)
    for model_id in TOOL_CHOICE_MODELS & discovered.keys():
        discovered[model_id]["supported_parameters"] = ["tool_choice"]
    checked = models_requiring_canary(MANIFEST_PATH, discovered)
    healthy = {
        model_id
        for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=UPSTREAM_ID_MAP.get(model_id, model_id),
            max_tokens=32,
        )
    }
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    _LIVE_CANARY_OK = len(healthy) == len(checked)
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"validated canonical provider contract with {len(discovered)} active chat models",
            f"canaried {len(checked)} new or unhealthy routes; {len(healthy)} passed",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
    return notes
