"""Production stream identities and complete, isolated-cohort denominators."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
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
                "up", identity if not shape.stream or stream_ids else None, None, shape.name,
                int(now.timestamp() * 1e6) - 120_000_000, int(now.timestamp() * 1e6) - 90_000_000))
            rows.append(dict(workspace_fingerprint=analytics_surrogate("workspace", "cohort"),
                             observed_at_us=int(now.timestamp() * 1e6), authorization_id=authorization,
                             authorize_at_us=int(now.timestamp() * 1e6) - 100_000_000,
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
    results[index] = replace(results[index], status="down", generation_id=None, error_type="ValueError")
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


@pytest.mark.parametrize("trailer", ["dropped", "edited"])
def test_missing_streams_cannot_be_hidden_by_trailer(trailer):
    now, rows, results, evidence, _ = full_pass()
    lines = shadow_probe.denominator(results).splitlines()
    rows[:] = [row for row in rows if not row["streamed"]]
    if trailer == "dropped":
        lines.pop()
    else:
        summary = json.loads(lines[-1].removeprefix(shadow_probe.DENOMINATOR_PREFIX))
        summary.update(requests_sent=16, streams_sent=0, streams_without_id=0)
        for shape in SHAPES:
            if shape.stream:
                summary["by_shape"][shape.name]["sent"] = 0
        lines[-1] = shadow_probe.DENOMINATOR_PREFIX + json.dumps(summary)
    try:
        result = check(evidence, "cohort", now, lines)
    except ValueError:
        return
    assert result["errors"], result


@pytest.mark.parametrize("replacement", ["buffered", "wrong_shape", "adjacent_pass", "retry"])
def test_unrelated_sample_cannot_replace_missing_stream(replacement):
    now, rows, results, evidence, _ = full_pass()
    missing = next(row for row in rows if row["streamed"])
    rows.remove(missing)
    stray = missing | {"authorization_id": "auth-stray"}
    if replacement == "buffered":
        stray["streamed"] = False
    elif replacement == "wrong_shape":
        stray["adapter"] = "anthropic"
        stray["route_type"] = "responses"
    elif replacement == "adjacent_pass":
        stray["observed_at_us"] -= 9 * 60 * 1_000_000
        stray["authorize_at_us"] -= 9 * 60 * 1_000_000
    else:
        duplicate_request = next(row for row in rows if row["streamed"])
        stray = duplicate_request | {"authorization_id": "auth-retry-new-authorization"}
    rows.append(stray)
    lines = shadow_probe.denominator(results).splitlines()
    result = check(evidence, "cohort", now, lines)
    if replacement == "retry":
        # Gatekeeper ruling: cardinality cannot distinguish a same-shape
        # missing/extra pair. Membership must be explicitly required.
        assert result["errors"] == []
        assert result["stream_membership"] == "not_proven"
        assert result["buffered_level"] == "identity"
        assert result["stream_level"] == "count_in_isolated_window"
        result = check(evidence, "cohort", now, lines, require_stream_membership=True)
        assert "cannot be told apart by this tool" in result["errors"][0]
    assert result["errors"], result


@pytest.mark.parametrize("shape", [s for s in SHAPES if s.stream], ids=lambda s: s.name)
@pytest.mark.parametrize("framing", ["unterminated", "merged_events"])
@pytest.mark.asyncio
async def test_unfinished_or_invalid_sse_frame_is_down(shape, framing):
    text = stream_reply(shape, stream_payloads(shape)).text
    if framing == "unterminated":
        text = text.rstrip("\n")
    else:
        text = text.replace("\n\n", "\n") + "\n"
    result = await run_reply(shape, httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=text))
    assert result.status == "down", result


@pytest.mark.parametrize("shape", STREAMS, ids=lambda s: s.name)
@pytest.mark.parametrize("variant", ["crlf", "cr", "comments", "bom", "multiline", "no_space"])
async def test_sse_format_complete_events(shape, variant):
    text = stream_reply(shape, stream_payloads(shape)).text
    if variant in {"crlf", "cr"}:
        text = text.replace("\n", "\r\n" if variant == "crlf" else "\r")
    elif variant == "comments":
        text = ": keepalive\n\nid: ignored\nretry: 1000\n" + text.replace("\n\n", "\n: comment\n\n")
    elif variant == "bom":
        text = "\ufeff" + text
    elif variant == "multiline":
        text = text.replace('data: {', 'data: {\ndata: ')
    else:
        text = text.replace("data: ", "data:")
    assert (await run_reply(shape, httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=text))).status == "up"


@pytest.mark.parametrize("shape", STREAMS, ids=lambda s: s.name)
@pytest.mark.parametrize("tail", ["unfinished", "error", "echo", "multiline_error", "multiline_echo"])
async def test_sse_validates_frames_after_terminal(shape, tail):
    text = stream_reply(shape, stream_payloads(shape)).text
    text += {
        "unfinished": 'data: {"ignored":true}',
        "error": 'data: {"error":"bad"}\n\n',
        "echo": 'data: {"nested":["fixture-secret"]}\n\n',
        "multiline_error": 'data: {\ndata: "error":"bad"}\n\n',
        "multiline_echo": 'data: {\ndata: "nested":["fixture-secret"]}\n\n',
    }[tail]
    assert (await run_reply(shape, httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=text))).status == "down"


@pytest.mark.parametrize("damage", ["zero", "unequal", "non_multiple", "too_many", "missing_shape"])
def test_fixed_grid_rejected_before_evidence_reads(damage):
    now, _, results, evidence, calls = full_pass()
    lines = shadow_probe.denominator(results).splitlines()
    summary = json.loads(lines[-1].removeprefix(shadow_probe.DENOMINATOR_PREFIX))
    shapes = summary["by_shape"]
    # Keep all aggregate arithmetic consistent: only the grid rule detects this.
    for shape in SHAPES:
        sent = {"zero": 0, "unequal": 4 if shape.stream else 8,
                "non_multiple": 5, "too_many": 36, "missing_shape": 4}[damage]
        shapes[shape.name] = {"sent": sent, "with_id": 0 if shape.stream else sent}
    ids = ["gen-" + f"{i:032x}" for i in range(sum(c["with_id"] for c in shapes.values()))]
    summary.update(requests_sent=sum(c["sent"] for c in shapes.values()),
                   buffered_with_id=len(ids), streams_sent=sum(shapes[s.name]["sent"] for s in STREAMS))
    summary["streams_without_id"] = summary["streams_sent"]
    if damage == "missing_shape":
        shapes.pop(SHAPES[0].name)
    with pytest.raises(ValueError):
        check(evidence, "cohort", now, ids + [shadow_probe.DENOMINATOR_PREFIX + json.dumps(summary)])
    assert calls == []


@pytest.mark.parametrize("repetitions", [1, 2, 8])
def test_supported_fixed_grids(repetitions):
    now, rows, results, evidence, _ = full_pass()
    originals = list(rows)
    for repeat in range(1, repetitions):
        for row, result in zip(originals, results[:32], strict=True):
            authorization = row["authorization_id"] + f"-pass-{repeat}"
            rows.append(row | {"authorization_id": authorization})
            results.append(replace(result, generation_id=None if row["streamed"]
                                   else generation_id_for_authorization(authorization)))
    assert check(evidence, "cohort", now, shadow_probe.denominator(results).splitlines())["errors"] == []


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("replacement", [False, True])
def test_pass_guard_traffic_fails(side, replacement):
    from scripts.async_settle.probe_cohort_check import PASS_CLOCK_SKEW_US
    now, rows, results, evidence, _ = full_pass()
    stream = next(row for row in rows if row["streamed"])
    boundary = (results[0].request_sent_at_us - PASS_CLOCK_SKEW_US - 1 if side == "before"
                else results[0].response_finished_at_us + PASS_CLOCK_SKEW_US + 1)
    if replacement:
        rows.remove(stream)
    rows.append(stream | {"authorization_id": "guard-traffic", "authorize_at_us": boundary})
    result = check(evidence, "cohort", now, shadow_probe.denominator(results).splitlines())
    assert any("traffic in pass guard interval" in error for error in result["errors"])


@pytest.mark.parametrize("damage", ["absent", "bool", "old", "future", "observation_before_authorization"])
def test_authorization_time_required_for_every_sample(damage):
    now, rows, results, evidence, _ = full_pass()
    row = rows[0]
    if damage == "absent":
        row.pop("authorize_at_us")
    elif damage == "bool":
        row["authorize_at_us"] = True
    elif damage == "old":
        row["authorize_at_us"] -= 100_000_000
    elif damage == "future":
        row["authorize_at_us"] += 100_000_000
    else:
        row["observed_at_us"] -= 200_000_000
    assert check(evidence, "cohort", now, shadow_probe.denominator(results).splitlines())["errors"]


@pytest.mark.parametrize("side", ["start", "end"])
def test_fixed_clock_skew_is_inclusive(side):
    from scripts.async_settle.probe_cohort_check import PASS_CLOCK_SKEW_US
    now, rows, results, evidence, _ = full_pass()
    rows[0]["authorize_at_us"] = (results[0].request_sent_at_us - PASS_CLOCK_SKEW_US if side == "start"
                                  else results[0].response_finished_at_us + PASS_CLOCK_SKEW_US)
    assert check(evidence, "cohort", now, shadow_probe.denominator(results).splitlines())["errors"] == []


@pytest.mark.parametrize("damage", ["missing", "bool", "reversed", "start_uncovered", "end_uncovered"])
def test_pass_boundaries_validated_before_reads(damage):
    now, _, results, evidence, calls = full_pass()
    lines = shadow_probe.denominator(results).splitlines()
    summary = json.loads(lines[-1].removeprefix(shadow_probe.DENOMINATOR_PREFIX))
    if damage == "missing":
        summary.pop("first_request_sent_at_us")
    elif damage == "bool":
        summary["first_request_sent_at_us"] = True
    elif damage == "reversed":
        summary["first_request_sent_at_us"] = summary["last_response_finished_at_us"] + 1
    elif damage == "start_uncovered":
        summary["first_request_sent_at_us"] = int(now.timestamp() * 1e6) - 600_000_000
    else:
        summary["last_response_finished_at_us"] = int(now.timestamp() * 1e6)
    lines[-1] = shadow_probe.DENOMINATOR_PREFIX + json.dumps(summary)
    with pytest.raises(ValueError):
        check(evidence, "cohort", now, lines)
    assert calls == []


@pytest.mark.parametrize("require_membership", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_diagnostic_cli_membership_exit_and_wording(require_membership, legacy, tmp_path, monkeypatch, capsys):
    from scripts.async_settle import probe_cohort_check as checker
    now, _, results, evidence, calls = full_pass()
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(checker.dt, "datetime", Clock)
    monkeypatch.setattr(checker, "create_store", lambda *a, **kw: SimpleNamespace(_database=None))
    monkeypatch.setattr(checker, "EvidenceStore", lambda _: evidence)
    path = tmp_path / "pass"
    lines = shadow_probe.denominator(results).splitlines()
    path.write_text("\n".join(lines[:-1] if legacy else lines))
    argv = ["--workspace", "cohort", "--generation-ids", str(path)]
    if require_membership:
        argv.append("--require-stream-membership")
    assert checker.main(argv) == (1 if legacy or require_membership else 0)
    output = capsys.readouterr().out
    assert checker.STREAM_MEMBERSHIP_LIMITATION in output
    assert '"stream_membership": "not_proven"' in output
    if legacy:
        assert checker.UNCERTIFIED_INPUT in output
        assert calls == []


async def test_probe_clock_bounds_are_recorded(monkeypatch):
    ticks = iter([1_000_001_000, 2_000_002_000])
    monkeypatch.setattr(shadow_probe.time, "time_ns", lambda: next(ticks))
    result = await run_reply(SHAPES[0], success(SHAPES[0]))
    assert result.request_sent_at_us == 1_000_001
    assert result.response_finished_at_us == 2_000_002
    summary = json.loads(shadow_probe.denominator([result]).splitlines()[-1].removeprefix(shadow_probe.DENOMINATOR_PREFIX))
    assert summary["first_request_sent_at_us"] == 1_000_001
    assert summary["last_response_finished_at_us"] == 2_000_002


@pytest.mark.parametrize("separator", ["\v", "\f", "\u0085", "\u2028", "\u2029"])
async def test_only_cr_and_lf_delimit_sse(separator):
    shape = STREAMS[0]
    text = stream_reply(shape, stream_payloads(shape)).text.replace("\n", separator)
    assert (await run_reply(shape, httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=text))).status == "down"


async def test_crlf_and_bom_across_byte_chunks():
    class ByteChunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            text = "\ufeff" + stream_reply(STREAMS[0], stream_payloads(STREAMS[0])).text.replace("\n", "\r\n")
            for byte in text.encode():
                yield bytes([byte])
    assert (await run_reply(STREAMS[0], httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ByteChunks()))).status == "up"
