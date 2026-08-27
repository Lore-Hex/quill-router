#!/usr/bin/env python3
"""Export the production-derived provider-check contract snapshot."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib.util
import inspect
import json
import sys
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "src/trusted_router/data/provider_check_contract.json"


class _CatalogFreeProvider(str):
    """Keep leaderboard deadline fallback coverage out of the catalog branch."""

    def __bool__(self) -> bool:
        return False


def _load_script_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    root_text = str(ROOT)
    inserted_root = root_text not in sys.path
    if inserted_root:
        sys.path.insert(0, root_text)
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted_root:
            sys.path.remove(root_text)
    return module


def _normalized_source(fn: Any) -> str:
    """Return stable source text while ignoring docstrings and comment-only churn."""
    source = textwrap.dedent(inspect.getsource(fn))
    parsed = ast.parse(source)
    definition = parsed.body[0]
    ignored_lines: set[int] = set()
    if isinstance(definition, (ast.AsyncFunctionDef, ast.FunctionDef)) and definition.body:
        first_statement = definition.body[0]
        if (
            isinstance(first_statement, ast.Expr)
            and isinstance(first_statement.value, ast.Constant)
            and isinstance(first_statement.value.value, str)
        ):
            ignored_lines.update(
                range(first_statement.lineno, (first_statement.end_lineno or first_statement.lineno) + 1)
            )
    lines = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if line_number in ignored_lines or not stripped or stripped.startswith("#"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines)


def _source_hashes(
    probes: ModuleType,
    provider_reliability: ModuleType,
    components: ModuleType,
    leaderboard: ModuleType,
    route_classifier: ModuleType,
    catalog: ModuleType,
) -> dict[str, str]:
    functions = {
        "catalog._decimal": catalog._decimal,
        "components.is_router_origin_error": components.is_router_origin_error,
        "leaderboard._effective_throughput": leaderboard._effective_throughput,
        "leaderboard._excluded_from_uptime": leaderboard._excluded_from_uptime,
        "leaderboard._aggregate_providers": leaderboard._aggregate_providers,
        "leaderboard._percentile": leaderboard._percentile,
        "leaderboard._sort_key": leaderboard._sort_key,
        "leaderboard.aggregate_leaderboard": leaderboard.aggregate_leaderboard,
        "provider_reliability.classify_provider_failure": (
            provider_reliability.classify_provider_failure
        ),
        "provider_reliability.model_deadlines": provider_reliability.model_deadlines,
        "probes._chat_text": probes._chat_text,
        "probes._first_int": probes._first_int,
        "probes._elapsed_ms_with_clock": probes._elapsed_ms_with_clock,
        "probes._observe_provider_stream": probes._observe_provider_stream,
        "probes._pong_matches": probes._pong_matches,
        "probes._response_error": probes._response_error,
        "probes._responses_text": probes._responses_text,
        "probes._rotation_error_type": probes._rotation_error_type,
        "probes._rotation_max_tokens": probes._rotation_max_tokens,
        "probes._rotation_omits_temperature": probes._rotation_omits_temperature,
        "probes._sse_line_error": probes._sse_line_error,
        "probes._sse_line_finish_reason": probes._sse_line_finish_reason,
        "probes._sse_line_has_content": probes._sse_line_has_content,
        "probes._sse_line_payload": probes._sse_line_payload,
        "probes._sse_line_usage": probes._sse_line_usage,
        "routes._classify": route_classifier._classify,
    }
    return {
        name: hashlib.sha256(_normalized_source(fn).encode()).hexdigest()
        for name, fn in sorted(functions.items())
    }


def _catalog_contract(catalog: ModuleType) -> dict[str, Any]:
    frozensets = {
        name: sorted(value)
        for name, value in vars(catalog).items()
        if isinstance(value, frozenset)
    }
    decimal_inputs: list[tuple[str, object, bool]] = [
        ("valid_decimal", "12.340", False),
        ("leading_zero", "01.5", False),
        ("missing_fraction", "1.", False),
        ("missing_integer", ".5", False),
        ("negative", "-1", False),
        ("non_string", 7, False),
        ("empty", "", False),
        ("nullable_none", None, True),
        ("non_nullable_none", None, False),
        ("junk", "not-a-decimal", False),
    ]
    decimal_behavior: list[dict[str, Any]] = []
    for name, value, nullable in decimal_inputs:
        row: dict[str, Any] = {"name": name, "input": value, "nullable": nullable}
        try:
            parsed = catalog._decimal(value, label="provider_check", nullable=nullable)
        except Exception as exc:  # noqa: BLE001 - exception type is the contract.
            row["exception"] = type(exc).__name__
        else:
            row["result"] = str(parsed) if parsed is not None else None
        decimal_behavior.append(row)
    return {
        "decimal_behavior": decimal_behavior,
        "frozensets": frozensets,
        "model_id_pattern": catalog._MODEL_ID_RE.pattern,
        "owner_pattern": catalog._OWNER_RE.pattern,
        "invariants": {
            "error_contract": {
                "rate_limit_status": 429,
                "overload_status": 503,
                "retry_after_header": "Retry-After",
            },
            "receipts": {
                "required": False,
                "specs": sorted(catalog._RECEIPT_SPECS),
                "algorithms": sorted(catalog._RECEIPT_ALGORITHMS),
                "delivery": sorted(catalog._RECEIPT_DELIVERY),
            },
            "pricing": {
                "currency": "USD",
                "unit": "per_1m_tokens",
                "minimum_request": 0,
                "cache_write_allowed_values": [None, 0],
                "prompt_caching_matches_cached_input_presence": True,
            },
        },
    }


def _decision_tables(probes: ModuleType) -> list[dict[str, Any]]:
    # This literal grid pins every marker and provider branch in both policies.
    grid = [
        ("openai", "vendor/o1-preview"),
        ("openai", "vendor/o3-mini"),
        ("openai", "vendor/o4-mini"),
        ("openai", "vendor/gpt-5.4"),
        ("generic", "google/gemini-2.5-pro"),
        ("generic", "google/gemini-3-pro"),
        ("generic", "openai/gpt-oss-120b"),
        ("generic", "zai/glm-4.6"),
        ("generic", "zai/glm-4.7"),
        ("generic", "zai/glm-5"),
        ("generic", "nvidia/nemotron-3"),
        ("generic", "anthropic/claude-fable-5"),
        ("generic", "anthropic/claude-sonnet-5"),
        ("generic", "acme/reasoning-model"),
        ("generic", "acme/thinking-model"),
        ("kimi", "moonshotai/kimi-k2.5"),
        ("generic", "x-ai/grok-4"),
        ("anthropic", "anthropic/claude-opus-4.7"),
        ("anthropic", "anthropic/claude-opus-4.8"),
        ("generic", "openai/gpt-5.1"),
        ("openai", "openai/gpt-4.1"),
        ("novita", "moonshotai/kimi-k2.5"),
        ("kimi", "moonshotai/kimi-latest"),
        ("generic", "anthropic/claude-opus-4.7"),
        ("anthropic", "anthropic/claude-haiku-5"),
        ("generic", "acme/plain-model"),
    ]
    return [
        {
            "provider": provider,
            "model": model,
            "max_tokens": probes._rotation_max_tokens(provider, model),
            "omits_temperature": probes._rotation_omits_temperature(provider, model),
        }
        for provider, model in grid
    ]


def _attribution_dict(attribution: Any) -> dict[str, Any]:
    return {
        "owner": attribution.owner.value,
        "failure_class": attribution.failure_class.value,
        "counts_toward_provider_availability": (
            attribution.counts_toward_provider_availability
        ),
        "capacity_rejected": attribution.capacity_rejected,
    }


def _failure_classification(
    markers: dict[str, list[Any]],
    classify_provider_failure: Any,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def add_case(
        name: str,
        *,
        status: str = "error",
        error_type: str | None = None,
        error_status: int | None = None,
        error_message: str | None = None,
    ) -> None:
        inputs = {
            "status": status,
            "error_type": error_type,
            "error_status": error_status,
            "error_message": error_message,
        }
        cases.append(
            {
                "name": name,
                "input": inputs,
                "attribution": _attribution_dict(classify_provider_failure(**inputs)),
            }
        )

    error_type_groups = [
        "unsupported_route_error_types",
        "probe_config_error_types",
        "customer_quota_types",
        "config_types",
        "monitor_configuration_error_types",
    ]
    substring_type_groups = [
        "timeout_markers",
        "stream_markers",
        "slow_reasoning_markers",
        "fast_markers",
    ]
    message_groups = [
        "unsupported_route_message_markers",
        "probe_config_message_markers",
        "dead_markers",
    ]
    for group in error_type_groups:
        for marker in markers[group]:
            add_case(f"{group}:{marker}", error_type=str(marker))
    for group in substring_type_groups:
        for marker in markers[group]:
            add_case(f"{group}:{marker}", error_type=f"synthetic_{marker}_failure")
    for group in message_groups:
        for marker in markers[group]:
            add_case(
                f"{group}:{marker}",
                error_type="provider_error",
                error_message=f"upstream reported {marker} for this request",
            )
    for marker in markers["account_quota_markers"]:
        add_case(
            f"account_quota_markers:{marker}",
            error_type="rate_limit_error",
            error_status=429,
            error_message=f"upstream reported {marker}",
        )
    for marker in markers["dead_statuses"]:
        add_case(
            f"dead_statuses:{marker}",
            error_type="provider_error",
            error_status=int(marker),
        )

    add_case("generic:plain_500", error_status=500)
    add_case("generic:plain_402", error_status=402)
    add_case("generic:plain_503", error_status=503)
    add_case("generic:plain_529", error_status=529)
    add_case("generic:plain_429", error_status=429)
    add_case("generic:overloaded_type", error_type="overloaded")
    add_case("generic:capacity_type", error_type="capacity_exceeded")
    add_case(
        "generic:unsupported_status_only",
        status="unsupported",
        error_type="unmapped_failure",
    )
    add_case(
        "generic:not_found_with_401",
        error_type="not_found",
        error_status=401,
    )
    add_case("generic:timeout_type", error_type="ReadTimeout")
    add_case("generic:unknown_type", error_type="unmapped_failure")
    add_case(
        "generic:success_shaped",
        status="success",
        error_type="router_error",
        error_status=500,
        error_message="account quota",
    )
    add_case("generic:router_prefix", error_type="router_database_contention")
    return sorted(cases, key=lambda row: row["name"])


def _deadline_behavior(model_deadlines: Any) -> list[dict[str, Any]]:
    cases = [
        ("vendor/plain-default", None, 20.0),
        ("vendor/reasoning-pro", None, 20.0),
        ("vendor/reasoning-high-default", None, 60.0),
        ("vendor/reasoning-haiku", None, 20.0),
        ("anthropic/haiku", None, 20.0),
        ("vendor/plain-low-clamp", None, 1.0),
        ("vendor/plain-completion-floor", None, 6.0),
        ("vendor/plain-completion-ceiling", None, 80.0),
        ("vendor/plain-high-clamp", None, 400.0),
        ("vendor/fast-low-clamp", None, 1.0),
        ("vendor/fast-high-clamp", None, 400.0),
        ("vendor/invalid-default", None, 0.0),
    ]

    catalog_name = "trusted_router.catalog"
    missing = object()
    previous_catalog = sys.modules.pop(catalog_name, missing)
    catalog_stub = ModuleType(catalog_name)
    catalog_stub.MODEL_ENDPOINTS = {
        "floor": SimpleNamespace(
            model_id="catalog/first-floor",
            provider="catalog-provider",
            usage_type="Credits",
            catalog_valid_until=None,
            catalog_is_current=lambda: True,
            first_token_timeout_seconds=1.0,
            completion_timeout_seconds=40.0,
        ),
        "ceiling": SimpleNamespace(
            model_id="catalog/first-ceiling",
            provider="catalog-provider",
            usage_type="Credits",
            catalog_valid_until=None,
            catalog_is_current=lambda: True,
            first_token_timeout_seconds=400.0,
            completion_timeout_seconds=400.0,
        ),
        "completion_floor": SimpleNamespace(
            model_id="catalog/completion-floor",
            provider="catalog-provider",
            usage_type="Credits",
            catalog_valid_until=None,
            catalog_is_current=lambda: True,
            first_token_timeout_seconds=5.0,
            completion_timeout_seconds=10.0,
        ),
        "completion_ceiling": SimpleNamespace(
            model_id="catalog/completion-ceiling",
            provider="catalog-provider",
            usage_type="Credits",
            catalog_valid_until=None,
            catalog_is_current=lambda: True,
            first_token_timeout_seconds=100.0,
            completion_timeout_seconds=1_000.0,
        ),
        "completion_fallback": SimpleNamespace(
            model_id="catalog/completion-fallback",
            provider="catalog-provider",
            usage_type="Credits",
            catalog_valid_until=None,
            catalog_is_current=lambda: True,
            first_token_timeout_seconds=20.0,
            completion_timeout_seconds=None,
        ),
        "usage_filter": SimpleNamespace(
            model_id="catalog/usage-filter",
            provider="catalog-provider",
            usage_type="BYOK",
            catalog_valid_until=None,
            catalog_is_current=lambda: True,
            first_token_timeout_seconds=77.0,
            completion_timeout_seconds=88.0,
        ),
    }
    sys.modules[catalog_name] = catalog_stub
    cases.extend(
        [
            ("catalog/first-floor", "catalog-provider", 20.0),
            ("catalog/first-ceiling", "catalog-provider", 20.0),
            ("catalog/completion-floor", "catalog-provider", 20.0),
            ("catalog/completion-ceiling", "catalog-provider", 20.0),
            ("catalog/completion-fallback", "catalog-provider", 20.0),
            ("catalog/usage-filter", "catalog-provider", 20.0),
        ]
    )
    rows: list[dict[str, Any]] = []
    try:
        for model, provider, default_first_token_seconds in cases:
            row: dict[str, Any] = {
                "model": model,
                "provider": provider,
                "default_first_token_seconds": default_first_token_seconds,
            }
            try:
                deadlines = model_deadlines(
                    model,
                    provider=provider,
                    default_first_token_seconds=default_first_token_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - exception type is the contract.
                row["exception"] = type(exc).__name__
            else:
                row["first_token_seconds"] = deadlines.first_token_seconds
                row["completion_seconds"] = deadlines.completion_seconds
            rows.append(row)
    finally:
        if sys.modules.get(catalog_name) is not catalog_stub:
            raise RuntimeError("synthetic trusted_router.catalog module was replaced")
        del sys.modules[catalog_name]
        if previous_catalog is not missing:
            sys.modules[catalog_name] = previous_catalog  # type: ignore[assignment]
    if previous_catalog is missing and catalog_name in sys.modules:
        raise RuntimeError("synthetic trusted_router.catalog module leaked")
    return sorted(rows, key=lambda row: (row["model"], row["provider"] or ""))


def _stable_result(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _stable_result(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _stable_result(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_stable_result(item) for item in value]
    if value is None or isinstance(value, (bool, float, int, str)):
        return value
    return repr(value)


def _extractor_behavior(probes: ModuleType) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def record(name: str, input_value: Any, call: Any) -> None:
        try:
            result = call()
        except Exception as exc:  # noqa: BLE001 - exception type is the contract.
            stable_result: Any = type(exc).__name__
        else:
            stable_result = _stable_result(result)
        rows.append({"name": name, "input": input_value, "result": stable_result})

    pong_cases: list[tuple[str, Any]] = [
        ("exact", "PONG"),
        ("lowercase", "pong"),
        ("embedded_sentence", "The answer is PONG today."),
        ("markdown", "**PONG**"),
        ("empty", ""),
        ("none", None),
        ("nonmatching", "PING"),
    ]
    for case_name, text in pong_cases:
        record(
            f"pong_matches:{case_name}",
            {"text": text},
            lambda text=text: probes._pong_matches(text),
        )

    chat_cases: list[tuple[str, dict[str, Any]]] = [
        ("string_content", {"choices": [{"message": {"content": "PONG"}}]}),
        (
            "list_text_parts",
            {"choices": [{"message": {"content": [{"type": "text", "text": "PONG"}]}}]},
        ),
        (
            "list_content_parts",
            {"choices": [{"message": {"content": [{"content": "PONG"}]}}]},
        ),
        (
            "reasoning_content_string",
            {"choices": [{"message": {"content": "", "reasoning_content": "PONG"}}]},
        ),
        (
            "reasoning_list",
            {"choices": [{"message": {"reasoning": [{"text": "PONG"}]}}]},
        ),
        ("empty_choices", {"choices": []}),
        ("missing_keys", {}),
    ]
    for case_name, body in chat_cases:
        input_value = {"status_code": 200, "json": body}
        record(
            f"chat_text:{case_name}",
            input_value,
            lambda body=body: probes._chat_text(httpx.Response(200, json=body)),
        )
    record(
        "chat_text:non_200",
        {"status_code": 503, "json": chat_cases[0][1]},
        lambda: probes._chat_text(httpx.Response(503, json=chat_cases[0][1])),
    )
    record(
        "chat_text:malformed_json",
        {"status_code": 200, "text": "not-json"},
        lambda: probes._chat_text(httpx.Response(200, text="not-json")),
    )

    responses_cases: list[tuple[str, dict[str, Any]]] = [
        ("string_content", {"output": [{"content": "PONG"}]}),
        ("list_text_parts", {"output": [{"content": [{"text": "PONG"}]}]}),
        (
            "full_output_walk",
            {
                "output": [
                    {"type": "reasoning", "summary": [{"text": "THINK"}]},
                    {"type": "message", "content": [{"text": "PONG"}]},
                ]
            },
        ),
        ("reasoning_summary", {"output": [{"summary": [{"text": "PONG"}]}]}),
        ("non_dict_output_item", {"output": ["ignored"]}),
        ("missing_keys", {}),
    ]
    for case_name, body in responses_cases:
        record(
            f"responses_text:{case_name}",
            {"status_code": 200, "json": body},
            lambda body=body: probes._responses_text(httpx.Response(200, json=body)),
        )
    record(
        "responses_text:non_200",
        {"status_code": 503, "json": responses_cases[0][1]},
        lambda: probes._responses_text(httpx.Response(503, json=responses_cases[0][1])),
    )
    record(
        "responses_text:malformed_json",
        {"status_code": 200, "text": "not-json"},
        lambda: probes._responses_text(httpx.Response(200, text="not-json")),
    )

    payload_lines = [
        ("data_space", 'data: {"value":1}'),
        ("data_no_space", 'data:{"value":1}'),
        ("done", "data: [DONE]"),
        ("comment", ": ping"),
        ("non_dict", "data: [1,2]"),
        ("malformed", "data: {oops"),
        ("empty", ""),
    ]
    for case_name, line in payload_lines:
        record(
            f"sse_line_payload:{case_name}",
            {"line": line},
            lambda line=line: probes._sse_line_payload(line),
        )

    def sse_line(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload, sort_keys=True, separators=(',', ':'))}"

    for key in (
        "content",
        "reasoning_content",
        "reasoning",
        "thinking",
        "text",
        "output_text",
    ):
        line = sse_line({"choices": [{"delta": {key: "PONG"}}]})
        record(
            f"sse_line_has_content:delta_{key}",
            {"line": line},
            lambda line=line: probes._sse_line_has_content(line),
        )
    for key in ("content", "reasoning_content", "reasoning", "thinking", "text"):
        line = sse_line({"choices": [{"message": {key: "PONG"}}]})
        record(
            f"sse_line_has_content:message_{key}",
            {"line": line},
            lambda line=line: probes._sse_line_has_content(line),
        )
    content_cases = [
        ("choice_text", {"choices": [{"text": "PONG"}]}),
        ("role_only", {"choices": [{"delta": {"role": "assistant"}}]}),
        ("empty_content", {"choices": [{"delta": {"content": ""}}]}),
        ("unrecognized_audio", {"choices": [{"delta": {"audio": "PONG"}}]}),
    ]
    for case_name, payload in content_cases:
        line = sse_line(payload)
        record(
            f"sse_line_has_content:{case_name}",
            {"line": line},
            lambda line=line: probes._sse_line_has_content(line),
        )

    error_cases = [
        ("default_type", {"error": {"message": "boom"}}),
        (
            "status_precedence",
            {"error": {"type": "upstream", "status": 409, "code": 410, "status_code": 411}},
        ),
        ("code_precedence", {"error": {"type": "upstream", "code": 410, "status_code": 411}}),
        ("status_code", {"error": {"type": "upstream", "status_code": 411}}),
    ]
    for case_name, payload in error_cases:
        line = sse_line(payload)
        record(
            f"sse_line_error:{case_name}",
            {"line": line},
            lambda line=line: probes._sse_line_error(line),
        )

    for reason in ("stop", "length", "tool_calls", "content_filter", "function_call", "future"):
        line = sse_line({"choices": [{"finish_reason": reason}]})
        record(
            f"sse_line_finish_reason:{reason}",
            {"line": line},
            lambda line=line: probes._sse_line_finish_reason(line),
        )

    usage_cases = [
        (
            "canonical_precedence",
            {
                "usage": {
                    "prompt_tokens": 1,
                    "input_tokens": 2,
                    "completion_tokens": 3,
                    "output_tokens": 4,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                }
            },
        ),
        ("alias_fallback", {"usage": {"input_tokens": 6, "output_tokens": 7}}),
        (
            "negative_clamp",
            {
                "usage": {
                    "prompt_tokens": -1,
                    "completion_tokens": -2,
                    "completion_tokens_details": {"reasoning_tokens": -3},
                }
            },
        ),
        ("missing_keys", {"usage": {}}),
        (
            "non_int_values",
            {
                "usage": {
                    "prompt_tokens": "bad",
                    "input_tokens": "8",
                    "completion_tokens": [],
                    "output_tokens": "9",
                    "completion_tokens_details": {"reasoning_tokens": "bad"},
                }
            },
        ),
        ("missing_usage", {}),
    ]
    for case_name, payload in usage_cases:
        line = sse_line(payload)
        record(
            f"sse_line_usage:{case_name}",
            {"line": line},
            lambda line=line: probes._sse_line_usage(line),
        )

    first_int_cases = [
        ("first_key_precedence", {"values": {"a": 1, "b": 2}, "keys": ["a", "b"]}),
        ("second_key_fallback", {"values": {"a": None, "b": 2}, "keys": ["a", "b"]}),
        ("negative_clamp", {"values": {"a": -7}, "keys": ["a"]}),
        ("invalid_then_valid", {"values": {"a": "bad", "b": "3"}, "keys": ["a", "b"]}),
        ("missing", {"values": {}, "keys": ["a", "b"]}),
        ("non_int", {"values": {"a": []}, "keys": ["a"]}),
    ]
    for case_name, input_value in first_int_cases:
        values = input_value["values"]
        keys = input_value["keys"]
        record(
            f"first_int:{case_name}",
            input_value,
            lambda values=values, keys=keys: probes._first_int(values, *keys),
        )

    response_error_cases: list[tuple[str, int, str | dict[str, Any]]] = [
        (
            "json_error",
            503,
            {"error": {"type": "overloaded", "message": "busy", "status": 529}},
        ),
        (
            "nested_detail",
            422,
            {"detail": {"error": {"type": "invalid_request", "message": "bad"}}},
        ),
        ("html_body", 502, "<html>bad gateway</html>"),
        ("empty_body", 504, ""),
        ("success_200", 200, {}),
        ("json_error_on_200", 200, {"error": {"type": "provider_error"}}),
    ]
    for case_name, status_code, body in response_error_cases:
        input_value: dict[str, Any] = {"status_code": status_code}
        if isinstance(body, dict):
            input_value["json"] = body
            response = httpx.Response(status_code, json=body)
        else:
            input_value["text"] = body
            response = httpx.Response(status_code, text=body)
        record(
            f"response_error:{case_name}",
            input_value,
            lambda response=response: probes._response_error(response),
        )

    return sorted(rows, key=lambda row: row["name"])


def _rotation_error_behavior(probes: ModuleType) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(
        name: str,
        *,
        error_type: str = "provider_error",
        status: int | None = None,
        message: str | None = None,
        source: str | None = None,
    ) -> None:
        inputs = {
            "error_type": error_type,
            "status": status,
            "message": message,
            "source": source,
        }
        rows.append(
            {
                "name": name,
                "input": inputs,
                "result": probes._rotation_error_type(**inputs),
            }
        )

    add("message:workspace_billing_paused", message="workspace billing is paused")
    add("message:database_contention", message="database contention")
    add("message:deadlock", message="deadlock")
    add("message:read_only_mode", message="read-only mode")
    add("message:planned_maintenance", message="planned maintenance")
    for marker in (
        "insufficient credits",
        "api key is disabled",
        "api key expired",
        "invalid api key",
        "api key not found",
    ):
        add(f"router_account:{marker}", message=marker, source="router")
    for marker in sorted(probes._UNSUPPORTED_ROUTE_ERROR_TYPES):
        add(f"unsupported_type:{marker}", error_type=marker)
    for marker in probes._UNSUPPORTED_ROUTE_MESSAGE_MARKERS:
        add(f"unsupported_message:{marker}", message=marker)
    for marker in sorted(probes._PROBE_CONFIG_ERROR_TYPES):
        add(f"probe_config_type:{marker}", error_type=marker)
    for status in (400, 422):
        for marker in probes._PROBE_CONFIG_MESSAGE_MARKERS:
            add(f"probe_config_message:{status}:{marker}", status=status, message=marker)
    add("router:fallback", source="router")
    add("auth:401", status=401)
    add("auth:403", status=403)
    add("fallback:unchanged", error_type="custom_provider_error", status=500)
    add(
        "precedence:workspace_before_contention",
        message="workspace billing is paused after database contention",
    )
    add(
        "precedence:router_account_before_unsupported",
        error_type="unsupported_route",
        message="insufficient credits",
        source="router",
    )
    add(
        "precedence:unsupported_before_auth",
        error_type="unsupported_route",
        status=401,
    )
    return sorted(rows, key=lambda row: row["name"])


def _rotation_error_exclusions(
    probes: ModuleType,
    *,
    failure_classification: list[dict[str, Any]],
    extractor_behavior: list[dict[str, Any]],
    rotation_error_behavior: list[dict[str, Any]],
    marker_error_types: list[Any],
) -> list[dict[str, Any]]:
    error_types: set[str | None] = {
        None,
        "unsupported_route",
        "probe_config_error",
        "provider_auth_config",
        "insufficient_throughput_sample",
    }
    error_types.update(str(value) for value in marker_error_types)
    for row in failure_classification:
        value = row["input"]["error_type"]
        error_types.add(value)
    for row in rotation_error_behavior:
        error_types.add(row["input"]["error_type"])
        error_types.add(row["result"])
    for row in extractor_behavior:
        if row["name"].startswith(("response_error:", "sse_line_error:")):
            result = row["result"]
            if isinstance(result, list) and result:
                error_types.add(str(result[0]))
    return [
        {"input": error_type, "result": probes._rotation_error_excluded_from_uptime(error_type)}
        for error_type in sorted(error_types, key=lambda value: (value is not None, value or ""))
    ]


SAMPLE_FIELDS = sorted(
    [
        "provider",
        "model",
        "source",
        "status",
        "error_type",
        "error_status",
        "error_message",
        "created_at",
        "output_tokens",
        "elapsed_milliseconds",
        "speed_tokens_per_second",
        "first_token_milliseconds",
        "ttfb_milliseconds",
    ]
)


def _leaderboard_behavior(aggregate_leaderboard: Any) -> dict[str, Any]:
    alpha = _CatalogFreeProvider("alpha")
    beta = _CatalogFreeProvider("beta")

    def sample(
        provider: str,
        model: str,
        created_at: str,
        **overrides: Any,
    ) -> SimpleNamespace:
        values: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "source": "synthetic",
            "status": "success",
            "error_type": None,
            "error_status": None,
            "error_message": None,
            "created_at": created_at,
            "output_tokens": 0,
            "elapsed_milliseconds": None,
            "speed_tokens_per_second": None,
            "first_token_milliseconds": None,
            "ttfb_milliseconds": None,
        }
        values.update(overrides)
        if sorted(values) != SAMPLE_FIELDS:
            raise RuntimeError("leaderboard sample fields drifted")
        return SimpleNamespace(**values)

    # The aggregator does not bucket by wall clock; these fixed values only
    # exercise deterministic last-seen selection.
    samples = [
        sample(alpha, "alpha/steady", "2026-01-01T00:00:00Z", first_token_milliseconds=100, ttfb_milliseconds=80),
        sample(alpha, "alpha/steady", "2026-01-01T01:00:00Z", first_token_milliseconds=140, ttfb_milliseconds=100),
        sample(alpha, "alpha/steady", "2026-01-01T02:00:00Z", first_token_milliseconds=18_000, ttfb_milliseconds=120),
        sample(alpha, "alpha/steady", "2026-01-01T03:00:00Z", status="error", error_type="ReadTimeout"),
        sample(alpha, "alpha/steady", "2026-01-01T04:00:00Z", source="synthetic_throughput", output_tokens=200, elapsed_milliseconds=10_000, speed_tokens_per_second=999.0),
        sample(alpha, "alpha/steady", "2026-01-01T05:00:00Z", source="synthetic_throughput", speed_tokens_per_second=30.0),
        sample(alpha, "alpha/thin", "2026-01-01T06:00:00Z", status="unsupported", error_type="unsupported_route", error_status=404),
        sample(alpha, "alpha/thin", "2026-01-01T07:00:00Z"),
        sample(alpha, "alpha/challenger", "2026-01-01T07:10:00Z", first_token_milliseconds=500, ttfb_milliseconds=400),
        sample(alpha, "alpha/challenger", "2026-01-01T07:20:00Z", first_token_milliseconds=600, ttfb_milliseconds=450),
        sample(alpha, "alpha/challenger", "2026-01-01T07:30:00Z", first_token_milliseconds=700, ttfb_milliseconds=500),
        sample(alpha, "alpha/challenger", "2026-01-01T07:40:00Z", first_token_milliseconds=800, ttfb_milliseconds=550),
        sample(alpha, "alpha/throughput-only", "2026-01-01T07:50:00Z", source="synthetic_throughput", output_tokens=300, elapsed_milliseconds=10_000),
        sample(beta, "beta/stable", "2026-01-01T08:00:00Z", first_token_milliseconds=200, ttfb_milliseconds=150),
        sample(beta, "beta/stable", "2026-01-01T09:00:00Z", first_token_milliseconds=250, ttfb_milliseconds=175),
        sample(beta, "beta/stable", "2026-01-01T10:00:00Z", status="error", error_type="rate_limit_error", error_status=429),
        sample(beta, "beta/stable", "2026-01-01T10:30:00Z", first_token_milliseconds=225, ttfb_milliseconds=160),
        sample(beta, "beta/thin", "2026-01-01T11:00:00Z", first_token_milliseconds=30_000, ttfb_milliseconds=200),
        sample(beta, "beta/thin", "2026-01-01T12:00:00Z", status="error", error_type="router_error", error_status=503),
        sample(beta, "beta/thin", "2026-01-01T13:00:00Z", status="error", error_type="rate_limit_exceeded", error_status=429),
        sample(beta, "beta/thin", "2026-01-01T14:00:00Z", status="error", error_type="provider_auth_config", error_status=401),
        sample(beta, "beta/thin", "2026-01-01T15:00:00Z", status="error", error_type=None, error_status=500),
        sample(beta, "beta/thin", "2026-01-01T16:00:00Z", status="error", error_type=None, error_status=None),
    ]
    result = aggregate_leaderboard(
        samples,
        min_samples=1,
        model_rank_min_samples=4,
        provider_rank_min_samples=5,
        rank_min_ttft_samples=3,
    )
    result["models_order"] = [
        f"{row['provider']}/{row['model']}" for row in result["models"]
    ]
    result["providers_order"] = [row["provider"] for row in result["providers"]]
    result["models"] = sorted(
        result["models"], key=lambda row: (row["provider"], row["model"])
    )
    result["providers"] = sorted(result["providers"], key=lambda row: row["provider"])
    return json.loads(json.dumps(result, sort_keys=True))


def build_contract() -> dict[str, Any]:
    """Build the deterministic provider-check contract without writing it."""
    from trusted_router import provider_reliability
    from trusted_router.synthetic import components, leaderboard, probes

    route_classifier = _load_script_module(
        ROOT / "scripts/classify_provider_routes.py",
        "_provider_check_route_classifier",
    )
    catalog = _load_script_module(
        ROOT / "scripts/pricing/provider_contract_catalog.py",
        "_provider_check_catalog_contract",
    )
    markers: dict[str, list[Any]] = {
        "account_quota_markers": sorted(provider_reliability._ACCOUNT_QUOTA_MARKERS),
        "config_types": sorted(provider_reliability._CONFIG_TYPES),
        "customer_quota_types": sorted(provider_reliability._CUSTOMER_QUOTA_TYPES),
        "dead_markers": sorted(route_classifier._DEAD_MARKERS),
        "dead_statuses": sorted(route_classifier._DEAD_STATUSES),
        "fast_markers": sorted(provider_reliability._FAST_MARKERS),
        "monitor_configuration_error_types": sorted(
            components.MONITOR_CONFIGURATION_ERROR_TYPES
        ),
        "probe_config_error_types": sorted(probes._PROBE_CONFIG_ERROR_TYPES),
        "probe_config_message_markers": sorted(probes._PROBE_CONFIG_MESSAGE_MARKERS),
        "slow_reasoning_markers": sorted(provider_reliability._SLOW_REASONING_MARKERS),
        "stream_markers": sorted(provider_reliability._STREAM_MARKERS),
        "timeout_markers": sorted(provider_reliability._TIMEOUT_MARKERS),
        "unsupported_route_error_types": sorted(probes._UNSUPPORTED_ROUTE_ERROR_TYPES),
        "unsupported_route_message_markers": sorted(
            probes._UNSUPPORTED_ROUTE_MESSAGE_MARKERS
        ),
    }
    router_origin_inputs = [
        None,
        "",
        *markers["monitor_configuration_error_types"],
        "router_database_contention",
        "provider_timeout",
        "Router_error",
    ]
    markers["router_origin_behavior"] = [
        {"input": value, "result": components.is_router_origin_error(value)}
        for value in router_origin_inputs
    ]

    failure_classification = _failure_classification(
        markers,
        provider_reliability.classify_provider_failure,
    )
    extractor_behavior = _extractor_behavior(probes)
    rotation_error_behavior = _rotation_error_behavior(probes)
    marker_error_types = [
        *markers["config_types"],
        *markers["customer_quota_types"],
        *markers["monitor_configuration_error_types"],
        *markers["probe_config_error_types"],
        *markers["unsupported_route_error_types"],
        *router_origin_inputs,
    ]

    contract: dict[str, Any] = {
        "prompts": {
            "pong": probes.PONG_PROMPT,
            "throughput": probes._THROUGHPUT_PROMPT,
        },
        "markers": markers,
        "catalog_contract": _catalog_contract(catalog),
        "decision_tables": _decision_tables(probes),
        "failure_classification": failure_classification,
        "model_deadlines": _deadline_behavior(provider_reliability.model_deadlines),
        "leaderboard": _leaderboard_behavior(leaderboard.aggregate_leaderboard),
        "extractors": extractor_behavior,
        "rotation_errors": {
            "classification": rotation_error_behavior,
            "excluded_from_uptime": _rotation_error_exclusions(
                probes,
                failure_classification=failure_classification,
                extractor_behavior=extractor_behavior,
                rotation_error_behavior=rotation_error_behavior,
                marker_error_types=marker_error_types,
            ),
        },
        "source_hashes": _source_hashes(
            probes,
            provider_reliability,
            components,
            leaderboard,
            route_classifier,
            catalog,
        ),
        "sample_fields": SAMPLE_FIELDS,
    }
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    contract["contract_version"] = hashlib.sha256(canonical.encode()).hexdigest()
    return contract


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(build_contract(), indent=2, sort_keys=True) + "\n"
    OUTPUT_PATH.write_text(payload, encoding="utf-8")
    print(OUTPUT_PATH.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
