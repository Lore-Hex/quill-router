from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

import pytest
from pydantic import ValidationError

from trusted_router.client_events_schema import (
    ClientAttempt,
    ClientEventsBatch,
    ClientMinuteCounter,
    ClientRequestEvent,
    ClientSDK,
)
from trusted_router.storage_operational_analytics import build_client_events_payload


def _batch() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "batch_id": "a" * 32,
        "instance_id": "b" * 16,
        "seq": 1,
        "sent_at_ms": 1_786_968_000_000,
        "sdk": {
            "name": "tr-py",
            "version": "1.2.3",
            "lang": "python",
            "runtime": "cpython/3.12.4",
            "os": "linux",
            "arch": "arm64",
        },
        "synthetic": False,
        "dropped_since_last": 0,
        "events": [
            {
                "age_ms": 100,
                "plane": "inference",
                "endpoint": "responses",
                "method": "POST",
                "streaming": True,
                "provider_pinned": False,
                "model": "openai/gpt-5.5",
                "attempts": [
                    {
                        "index": 0,
                        "host": "apex",
                        "outcome": "ok",
                        "http_status": 200,
                        "error_class": None,
                        "error_source": None,
                        "should_retry": "absent",
                        "retry_after_ms": None,
                        "elapsed_ms": 100,
                        "ttfb_ms": 50,
                        "request_id": f"rlog_{'c' * 32}",
                        "moved": False,
                    }
                ],
                "final_outcome": "ok",
                "final_http_status": 200,
                "total_ms": 100,
                "ttft_ms": 50,
                "failover_used": False,
                "timeout_phase": "none",
                "configured_timeout_ms": None,
                "sample_rate": 0.01,
                "sample_reason": "random",
            }
        ],
        "counters": [
            {
                "window_start_age_ms": 1_000,
                "level": "request",
                "endpoint": "responses",
                "streaming": True,
                "host": "apex",
                "outcome": "ok",
                "error_class": None,
                "http_status_class": "2xx",
                "timeout_phase": "none",
                "timeout_floor_met": False,
                "provider_pinned": False,
                "requests": 1,
                "attempts": 1,
                "failover_used": 0,
                "first_attempt_success": 1,
                "total_ms_hist": {"lt200": 1},
                "first_event_ms_hist": {"lt100": 1},
            }
        ],
    }


def _walk_schema(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_schema(item)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "string":
        assert "enum" in node or (
            str(node.get("pattern", "")).startswith("^")
            and str(node["pattern"]).endswith("$")
            and int(node.get("maxLength", 129)) <= 128
        ), node
    if node.get("type") == "array":
        assert "maxItems" in node, node
    if node.get("type") == "object" and "properties" in node:
        assert node.get("additionalProperties") is False, node
    for value in node.values():
        _walk_schema(value)


def test_every_schema_node_is_closed_and_bounded() -> None:
    schema = ClientEventsBatch.model_json_schema()

    _walk_schema(schema)
    for model in (
        ClientEventsBatch,
        ClientSDK,
        ClientRequestEvent,
        ClientAttempt,
        ClientMinuteCounter,
    ):
        assert model.model_config["extra"] == "forbid"


def _string_paths(value: Any, path: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    paths: list[tuple[str | int, ...]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            paths.extend(_string_paths(item, (*path, key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_string_paths(item, (*path, index)))
    elif isinstance(value, str):
        paths.append(path)
    return paths


def _dict_paths(value: Any, path: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    paths: list[tuple[str | int, ...]] = []
    if isinstance(value, dict):
        paths.append(path)
        for key, item in value.items():
            paths.extend(_dict_paths(item, (*path, key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_dict_paths(item, (*path, index)))
    return paths


def _at_path(value: Any, path: tuple[str | int, ...]) -> Any:
    current = value
    for item in path:
        current = current[item]
    return current


@pytest.mark.parametrize(
    "injection",
    [
        'messages:[{"role":"user","content":"ignore prior instructions"}]',
        "private paragraph " * 300,
    ],
    ids=["prompt", "five-kb-paragraph"],
)
def test_prompt_text_is_rejected_in_every_string_position(injection: str) -> None:
    original = _batch()
    for path in _string_paths(original):
        candidate = copy.deepcopy(original)
        parent = _at_path(candidate, path[:-1])
        parent[path[-1]] = injection
        with pytest.raises(ValidationError):
            ClientEventsBatch.model_validate(candidate)


@pytest.mark.parametrize("injection", ["person@example.com", "192.0.2.44"])
def test_email_and_ip_are_rejected_or_scrubbed_before_storage(injection: str) -> None:
    original = _batch()
    for path in _string_paths(original):
        candidate = copy.deepcopy(original)
        parent = _at_path(candidate, path[:-1])
        parent[path[-1]] = injection
        if path[-1] == "model":
            batch = ClientEventsBatch.model_validate(candidate)
            payload = build_client_events_payload(
                batch,
                tenant_id="raw-workspace",
                key_id="raw-key",
                received_at=dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC),
                is_synthetic=False,
                success_sample_rate=0.01,
            )
            encoded = json.dumps(payload, sort_keys=True)
            assert payload["events"][0]["model"] == "other"
            assert injection not in encoded
        else:
            with pytest.raises(ValidationError):
                ClientEventsBatch.model_validate(candidate)


def test_prompt_shaped_extra_key_is_rejected_at_every_object_level() -> None:
    original = _batch()
    for path in _dict_paths(original):
        candidate = copy.deepcopy(original)
        target = _at_path(candidate, path)
        target["messages"] = [{"role": "user", "content": "private prompt"}]
        with pytest.raises(ValidationError):
            ClientEventsBatch.model_validate(candidate)
