from __future__ import annotations

import hashlib
import json
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stage_d" / "terminals"
GOLDEN_NAMES = (
    "chat_cap.sse",
    "chat_heartbeat_lost.sse",
    "responses_cap_function_call.sse",
    "responses_cap_mixed.sse",
    "responses_cap_reasoning.sse",
    "responses_cap_text.sse",
    "responses_heartbeat_lost.sse",
)
RESPONSE_CAP_NAMES = {
    "responses_cap_function_call.sse",
    "responses_cap_mixed.sse",
    "responses_cap_reasoning.sse",
    "responses_cap_text.sse",
}
HEARTBEAT_LOST_NAMES = {
    "chat_heartbeat_lost.sse",
    "responses_heartbeat_lost.sse",
}


def _read_sse_payloads(path: Path) -> list[dict[str, Any]]:
    literal_bytes = path.read_bytes()
    non_empty_lines = [line for line in literal_bytes.splitlines() if line]
    assert non_empty_lines[-1] == b"data: [DONE]"

    payloads: list[dict[str, Any]] = []
    for line in non_empty_lines:
        if not line.startswith(b"data:"):
            continue
        assert line.startswith(b"data: ")
        payload_bytes = line.removeprefix(b"data: ")
        if payload_bytes == b"[DONE]":
            continue
        payload = json.loads(payload_bytes)
        assert isinstance(payload, dict)
        payloads.append(cast(dict[str, Any], payload))
    return payloads


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_stage_d_terminal_golden_contract(name: str) -> None:
    payloads = _read_sse_payloads(FIXTURE_DIR / name)
    assert payloads
    final_event = payloads[-1]

    if name in RESPONSE_CAP_NAMES:
        assert final_event["type"] == "response.incomplete"
        response = final_event["response"]
        assert response["status"] == "incomplete"
        assert response["incomplete_details"]["reason"] == "max_output_tokens"
        assert response["tr_finish_reason"] == "cap_reached"

        sequence_numbers = [event["sequence_number"] for event in payloads]
        assert all(isinstance(number, int) for number in sequence_numbers)
        assert all(left < right for left, right in pairwise(sequence_numbers))
        assert all("status" in output_item for output_item in response["output"])

    if name == "chat_cap.sse":
        assert final_event["choices"][0]["finish_reason"] == "length"
        assert final_event["tr_finish_reason"] == "cap_reached"

    if name in HEARTBEAT_LOST_NAMES:
        terminal = final_event["response"] if name.startswith("responses_") else final_event
        assert terminal["tr_finish_reason"] == "heartbeat_lost"


def test_stage_d_terminal_golden_manifest() -> None:
    manifest_lines = (FIXTURE_DIR / "MANIFEST.sha256").read_bytes().splitlines()
    assert len(manifest_lines) == len(GOLDEN_NAMES)
    assert all(manifest_lines)

    entries: dict[bytes, bytes] = {}
    for line in manifest_lines:
        digest, manifest_name = line.split(b"  ", maxsplit=1)
        assert manifest_name not in entries
        entries[manifest_name] = digest

    expected_names = {name.encode("ascii") for name in GOLDEN_NAMES}
    assert entries.keys() == expected_names
    for name in GOLDEN_NAMES:
        actual_digest = hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest()
        assert entries[name.encode("ascii")] == actual_digest.encode("ascii")
