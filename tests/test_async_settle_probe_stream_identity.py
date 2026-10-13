"""Production stream identities and complete, isolated-cohort denominators."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest

from scripts.async_settle.probe_cohort_check import check
from tests.test_async_settle_probe import stream_payloads, stream_reply, success
from trusted_router.async_settle_shadow_evidence import SAMPLE
from trusted_router.config import Settings
from trusted_router.storage_models import generation_id_for_authorization
from trusted_router.storage_operational_analytics import analytics_surrogate
from trusted_router.synthetic import shadow_probe
from trusted_router.synthetic.shadow_probe_contract import SHAPES

STREAMS = [shape for shape in SHAPES if shape.stream]


async def run_reply(shape, reply, key="fixture-secret"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply)) as client:
        return await shadow_probe.probe(
            client, shadow_probe.targets(Settings(environment="test"))[0], shape,
            api_key=key, monitor_region="test",
        )


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: s.name)
async def test_production_shapes_up(shape):
    result = await run_reply(shape, success(shape))
    assert result.status == "up"
    assert (result.generation_id is None) is shape.stream


@pytest.mark.parametrize("shape", STREAMS, ids=lambda s: s.name)
def test_production_stream_field_sets(shape):
    # Production field-name evidence dated 2026-10-13; no invented router IDs.
    payloads = stream_payloads(shape)
    if shape.route == "/chat/completions":
        assert len(payloads) == 4
        assert all("usage" not in p and "trustedrouter" not in p for p in payloads)
        payloads = stream_payloads(shape, include_usage=True)
        assert len(payloads) == 5
        assert set(payloads[-1]["usage"]) == {
            "completion_tokens", "cost_microdollars", "inference_location", "prompt_tokens",
            "total_cost_microdollars", "total_tokens", "usage_estimated",
        }
    else:
        assert len(payloads) == 10
        assert set(payloads[-1]["response"]["usage"]) == {
            "cost_microdollars", "inference_location", "input_tokens", "input_tokens_details",
            "output_tokens", "output_tokens_details", "total_cost_microdollars", "total_tokens",
            "usage_estimated",
        }
    assert all(shadow_probe._generation_id(p) is None for p in payloads)


@pytest.mark.parametrize("shape", [s for s in STREAMS if s.route == "/chat/completions"], ids=lambda s: s.name)
async def test_include_usage_still_has_no_identity(shape):
    # Negative control only: the fixed request body must not add stream_options.
    result = await run_reply(shape, stream_reply(shape, stream_payloads(shape, include_usage=True)))
    assert result.status == "up" and result.generation_id is None


@pytest.mark.parametrize("shape", STREAMS, ids=lambda s: s.name)
@pytest.mark.parametrize("damage", ["error", "late_error", "missing_terminal", "wrong_type", "echo", "http"])
async def test_stream_failures(shape, damage):
    payloads = stream_payloads(shape)
    if damage == "error":
        payloads.insert(0, {"error": {"message": "bad"}})
    elif damage == "late_error":
        payloads.append({"type": "error", "message": "bad"})
    elif damage == "missing_terminal":
        payloads.pop()
    elif damage == "echo":
        payloads.insert(0, {"nested": [{"fixture-secret": "redact"}]})
    reply = stream_reply(shape, payloads)
    if damage == "wrong_type":
        reply.headers["content-type"] = "application/json"
    elif damage == "http":
        reply.status_code = 503
    result = await run_reply(shape, reply)
    assert result.status == "down" and result.generation_id is None


@pytest.mark.parametrize("shape", [s for s in STREAMS if s.route == "/chat/completions"], ids=lambda s: s.name)
async def test_chat_missing_done(shape):
    result = await run_reply(shape, stream_reply(shape, stream_payloads(shape), done=False))
    assert result.status == "down"


@pytest.mark.parametrize("damage", ["event_type", "status", "nested_error"])
async def test_responses_requires_completed_event_and_status(damage):
    shape = SHAPES[3]
    payloads = stream_payloads(shape)
    if damage == "event_type":
        payloads[-1]["type"] = "response.in_progress"
    elif damage == "status":
        payloads[-1]["response"]["status"] = "incomplete"
    else:
        payloads[-1]["response"]["error"] = {"message": "failed"}
    assert (await run_reply(shape, stream_reply(shape, payloads))).status == "down"


@pytest.mark.parametrize("shape", STREAMS, ids=lambda s: s.name)
async def test_optional_stream_identity_is_recorded_separately(shape):
    payloads = stream_payloads(shape, include_usage=True)
    inner = payloads[-1].get("response", payloads[-1])
    identity = "gen-" + "b" * 32
    inner["usage"]["provider_usage"] = {"generation_id": identity}
    result = await run_reply(shape, stream_reply(shape, payloads))
    assert result.status == "up" and result.generation_id == identity
    lines = shadow_probe.denominator([result]).splitlines()
    assert len(lines) == 1  # Stream IDs never become buffered-ID lines.
    summary = json.loads(lines[0].removeprefix(shadow_probe.DENOMINATOR_PREFIX))
    assert summary["stream_generation_ids"] == [identity]
    assert summary["streams_sent"] == 1 and summary["streams_without_id"] == 0


def full_pass(*, stream_ids=False):
    now = dt.datetime(2026, 10, 13, 0, 5, tzinfo=dt.UTC)
    rows, results = [], []
    for shape in SHAPES:
        for region in range(4):
            authorization = f"auth-{shape.name}-{region}"
            identity = generation_id_for_authorization(authorization)
            results.append(shadow_probe.ProbeResult(
                "up", identity if not shape.stream or stream_ids else None, None, shape.name))
            rows.append(dict(workspace_fingerprint=analytics_surrogate("workspace", "cohort"),
                             observed_at_us=int(now.timestamp() * 1e6), authorization_id=authorization,
                             adapter=shape.provider,
                             route_type="responses" if shape.route == "/responses" else "chat.completions",
                             streamed=shape.stream, classification="exact"))
    calls = []

    def day(kind, day):
        calls.append((kind, day))
        return [(str(i), json.dumps(row)) for i, row in enumerate(rows)] if kind == SAMPLE and day == now.date().isoformat() else []

    return now, rows, results, SimpleNamespace(day=day), calls


@pytest.mark.parametrize("stream_ids", [False, True])
def test_reconcile_full_pass(stream_ids):
    now, rows, results, evidence, calls = full_pass(stream_ids=stream_ids)
    lines = shadow_probe.denominator(results).splitlines()
    assert len(lines) == 17
    result = check(evidence, "cohort", now, lines)
    assert result["errors"] == []
    assert result["reconciled"] == 16  # Buffered ID matches only, even with optional stream IDs.
    assert sum(bucket["compared"] for bucket in result["buckets"]) == 32
    assert len(calls) == len(set(calls)) == 4  # Two existing reads/day, at most two days.


@pytest.mark.parametrize("damage", ["15", "17", "non_exact", "duplicate", "excluded", "buffered_missing"])
def test_reconcile_stream_counts_fail_closed(damage):
    now, rows, results, evidence, _ = full_pass()
    stream = next(row for row in rows if row["streamed"])
    identity = generation_id_for_authorization(stream["authorization_id"])
    if damage == "15":
        rows.remove(stream)
    elif damage == "17":
        rows.append(stream | {"authorization_id": "auth-extra"})
    elif damage == "non_exact":
        stream["classification"] = "unevaluable"
    elif damage == "duplicate":
        # Count stays 16: duplicates must fail independently of count checks.
        rows.remove(next(row for row in rows if row["streamed"] and row is not stream))
        rows.append(dict(stream))
    elif damage == "excluded":
        stream["eligibility"] = {"exclusion": "excluded"}
    else:
        buffered = next(row for row in rows if not row["streamed"])
        identity = generation_id_for_authorization(buffered["authorization_id"])
        buffered["authorization_id"] = "auth-unexpected"
    errors = check(evidence, "cohort", now, shadow_probe.denominator(results).splitlines())["errors"]
    assert errors
    if damage in {"15", "17"}:
        assert any(f"workspace sample count: expected 32, found {31 if damage == '15' else 33}" in e for e in errors)
        assert any(f"unmatched stream count: expected 16, found {damage}" in e for e in errors)
    else:
        assert any(identity in error for error in errors)


@pytest.mark.parametrize("field", ["requests_sent", "buffered_with_id", "streams_sent", "streams_without_id", "by_shape", "stream_generation_ids"])
def test_denominator_totals_must_add_up(field):
    now, _, results, evidence, calls = full_pass()
    lines = shadow_probe.denominator(results).splitlines()
    summary = json.loads(lines[-1].removeprefix(shadow_probe.DENOMINATOR_PREFIX))
    if field == "by_shape":
        summary[field][SHAPES[0].name]["sent"] += 1
    elif field == "stream_generation_ids":
        summary[field].append("gen-" + "b" * 32)
    else:
        summary[field] += 1
    lines[-1] = shadow_probe.DENOMINATOR_PREFIX + json.dumps(summary)
    with pytest.raises(ValueError, match="totals do not add up"):
        check(evidence, "cohort", now, lines)
    assert calls == []


@pytest.mark.parametrize("stream", [False, True])
def test_failed_requests_remain_in_denominator(stream):
    now, _, results, evidence, _ = full_pass()
    index = next(i for i, r in enumerate(results) if r.shape_name.endswith("_stream") is stream)
    results[index] = shadow_probe.ProbeResult("down", None, "ValueError", results[index].shape_name)
    lines = shadow_probe.denominator(results).splitlines()
    summary = json.loads(lines[-1].removeprefix(shadow_probe.DENOMINATOR_PREFIX))
    assert summary["requests_sent"] == 32 and summary["failed"] == 1
    assert "probe pass failed: 1 requests" in check(evidence, "cohort", now, lines)["errors"]


# Frozen from main 231419d7; all eight reviewed request bodies must stay byte-identical.
CANONICAL_BODIES = [
    b'{"max_tokens":8,"messages":[{"content":"Reply OK.","role":"user"}],"model":"openai/gpt-4.1-mini","provider":{"allow_fallbacks":false,"only":["openai"]},"stream":false}',
    b'{"max_tokens":8,"messages":[{"content":"Reply OK.","role":"user"}],"model":"openai/gpt-4.1-mini","provider":{"allow_fallbacks":false,"only":["openai"]},"stream":true}',
    b'{"input":"Reply OK.","max_output_tokens":8,"model":"openai/gpt-4.1-mini","provider":{"allow_fallbacks":false,"only":["openai"]},"stream":false}',
    b'{"input":"Reply OK.","max_output_tokens":8,"model":"openai/gpt-4.1-mini","provider":{"allow_fallbacks":false,"only":["openai"]},"stream":true}',
    b'{"max_tokens":8,"messages":[{"content":"Reply OK.","role":"user"}],"model":"anthropic/claude-haiku-4.5","provider":{"allow_fallbacks":false,"only":["anthropic"]},"stream":false}',
    b'{"max_tokens":8,"messages":[{"content":"Reply OK.","role":"user"}],"model":"anthropic/claude-haiku-4.5","provider":{"allow_fallbacks":false,"only":["anthropic"]},"stream":true}',
    b'{"input":"Reply OK.","max_output_tokens":8,"model":"anthropic/claude-haiku-4.5","provider":{"allow_fallbacks":false,"only":["anthropic"]},"stream":false}',
    b'{"input":"Reply OK.","max_output_tokens":8,"model":"anthropic/claude-haiku-4.5","provider":{"allow_fallbacks":false,"only":["anthropic"]},"stream":true}',
]


def test_canonical_request_bytes_unchanged():
    assert [json.dumps(s.body(), sort_keys=True, separators=(",", ":")).encode() for s in SHAPES] == CANONICAL_BODIES
