"""A1 endpoint, delegation and totality proofs beyond the committed examples."""
from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from typing import Any

import pytest

from tests.test_async_settle_shadow_v2_a1 import SCHEMAS, VECTORS, obj, vector
from tests.test_async_settle_shadow_v2_contract import assert_round_trip
from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
from trusted_router import async_settle_shadow_v2_a1_deltas as d
from trusted_router import async_settle_shadow_v2_a1_evidence as a
from trusted_router import async_settle_shadow_v2_a1_health as h
from trusted_router import async_settle_shadow_v2_a1_reconciliation as r
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router import async_settle_shadow_v2_schema as s
from trusted_router.detached_jws import canonical

SEEDS: dict[str, dict[str, Any]] = {}
for _file, _v in VECTORS:
    if _v["target"] in SCHEMAS and _v["expected"] == "accept":
        SEEDS.setdefault(_v["target"], _v["object"])


def primitive(check: Any) -> Any:
    if check.__name__ == "validate" and getattr(check, "__closure__", None):
        cells = [cell.cell_contents for cell in check.__closure__]
        if len(cells) == 1 and cells[0] is s.integer:
            return s.integer
    return check


INTEGER_FIELDS = [(name, field, check) for name, seed in SEEDS.items()
                  for field, check in SCHEMAS[name].fields.items()
                  if callable(check) and not isinstance(check, s.Schema) and primitive(check) is s.integer]

INTEGER_FIELDS.sort(key=lambda row: (row[0], row[1]))


@pytest.mark.parametrize("name,field,check", INTEGER_FIELDS, ids=[name + ":" + field for name, field, _ in INTEGER_FIELDS])
def test_every_numeric_field_int64_endpoints(name: str, field: str, check: Any) -> None:
    for value in (0, (1 << 63) - 1):
        check(value)
    for value in (-1, 1 << 63, True, 1.0, "1", [], {}):
        with pytest.raises(ValueError):
            check(value)
        with pytest.raises(ValueError):
            SCHEMAS[name]({**SEEDS[name], field: value})


LITERAL_FIELDS = [(name, field, check) for name in SEEDS for field, check in SCHEMAS[name].fields.items()
                  if callable(check) and getattr(check, "__qualname__", "") == "literal.<locals>.check"]

LITERAL_FIELDS.sort(key=lambda row: (row[0], row[1]))


@pytest.mark.parametrize("name,field,check", LITERAL_FIELDS, ids=[name + ":" + field for name, field, _ in LITERAL_FIELDS])
def test_every_literal_exact(name: str, field: str, check: Any) -> None:
    choices = check.__closure__[0].cell_contents
    for choice in choices:
        check(choice)
    for value in ("invented", "", None, [], {}, 3.0):
        if any(type(value) is type(c) and value == c for c in choices):
            continue
        with pytest.raises(ValueError):
            check(value)
        with pytest.raises(ValueError):
            SCHEMAS[name]({**SEEDS[name], field: value})
    for choice in choices:
        if type(choice) is int:
            with pytest.raises(ValueError):
                check(float(choice))
            with pytest.raises(ValueError):
                check(bool(choice))


@pytest.mark.parametrize("field", ["reader_boot", "source_state", "body", "committed_at_ns", "read_at_ns", "read_elapsed_ns", "since_reply_ns"])
def test_basis_no_missing_defaults(field: str) -> None:
    value = obj("health_basis", "installed")
    del value[field]
    with pytest.raises(ValueError):
        h.HEALTH_BASIS(value)


@pytest.mark.parametrize("limit", [0, 1, 64, 65])
def test_boot_width(limit: int) -> None:
    value = {**obj("close_result", "shutdown"), "boot": "b" * limit}
    if 1 <= limit <= 64:
        assert_round_trip(a.CLOSE_RESULT, value)
        assert len(value["day"] + "/" + value["boot"]) <= 75
    else:
        with pytest.raises(ValueError):
            a.CLOSE_RESULT(value)


def test_binary64_range_and_exact_integer_roundtrip() -> None:
    body = obj("health", "empty")
    for value in (0, 0.0, -0.0, 1.0, 5e-324, 1.7976931348623157e308,
                  int(1.7976931348623157e308), 2**100):
        assert_round_trip(h.HEALTH, {**body, "backlog_age_at_seconds": value})
    for value in (float("nan"), math.inf, -math.inf, -5e-324, 2**1024, 2**53 + 1):
        with pytest.raises(ValueError):
            h.HEALTH({**body, "backlog_age_at_seconds": value})
    # Nanosecond arithmetic remains integer even above binary64's exact domain.
    body.update(slot=((1 << 63) - 1) // 2000000000,
                observed_at_ns=((1 << 63) - 1) // 2000000000 * 2000000000)
    body["publisher_heartbeat_ns"] = body["observed_at_ns"]
    o = body["observed_at_ns"]
    assert h.health_usable(body, {"committed_at_ns": o + 1, "read_at_ns": o + 1}, 250000000, 1250000000)
    assert not h.health_usable(body, {"committed_at_ns": o + 1, "read_at_ns": o}, 0, 0)


def test_batch_count_and_sequence_boundaries() -> None:
    batch = obj("health_basis", "batch")
    bases = [{**batch["bases"][0], "since_reply_ns": i} for i in range(11)]
    bases.sort(key=s.sha256)
    assert_round_trip(h.HEALTH_BASIS_BATCH, {**batch, "bases": bases[:10]})
    with pytest.raises(ValueError):
        h.HEALTH_BASIS_BATCH({**batch, "bases": bases})
    for value in ("00000000000000000000", "18446744073709551616", "1", 1, True):
        with pytest.raises(ValueError):
            h.HEALTH_BASIS_BATCH({**batch, "sequence": value})
    for seq in ("00000000000000000002", "18446744073709551615"):
        assert_round_trip(h.HEALTH_BASIS_BATCH, {**batch, "sequence": seq, "previous_sha256": "a" * 64})
    with pytest.raises(ValueError):
        h.HEALTH_BASIS_BATCH({**batch, "previous_sha256": "a" * 64})
    with pytest.raises(ValueError):
        h.HEALTH_BASIS_BATCH({**batch, "bases": [{**batch["bases"][0], "reader_boot": "other"}]})


def test_trace_page_sixty_and_over_and_nullable_stages() -> None:
    page = obj("publisher_slot_trace", "page")
    seed = obj("publisher_slot_trace", "missed")
    entries = [{**seed, "slot": page["first_slot"] + i} for i in range(61)]
    assert_round_trip(h.PUBLISHER_TRACE_PAGE, {**page, "last_slot": entries[59]["slot"], "entries": entries[:60]})
    with pytest.raises(ValueError):
        h.PUBLISHER_TRACE_PAGE({**page, "last_slot": entries[-1]["slot"], "entries": entries})
    published = obj("publisher_slot_trace", "published")
    for field in ("claim_start_us", "claim_at_ns", "observed_at_ns", "publisher_heartbeat_ns", "body_sha256", "ack_at_us", "cycle_us"):
        with pytest.raises(ValueError):
            h.PUBLISHER_SLOT_TRACE({**published, field: None})
        bad = {**seed, field: published[field]}
        with pytest.raises(ValueError):
            h.PUBLISHER_SLOT_TRACE(bad)
    assert_round_trip(h.PUBLISHER_TRACE_PAGE, {**page, "previous_sha256": "a" * 64})


def test_coverage_count_key_hash_and_cross_page_order() -> None:
    page = obj("controls", "coverage-close_result")
    entries = [{"key": f"key/{i:04}", "sha256": None, "bytes": 0, "state": "missing"} for i in range(1001)]
    assert_round_trip(a.COVERAGE_PAGE, {**page, "entries": entries[:1000]})
    with pytest.raises(ValueError):
        a.COVERAGE_PAGE({**page, "entries": entries})
    assert_round_trip(a.COVERAGE_ENTRY, {**entries[0], "key": "k" * 512})
    empty_key = {**entries[0], "key": ""}
    assert_round_trip(a.COVERAGE_ENTRY, empty_key)
    assert_round_trip(a.COVERAGE_PAGE, {**page, "entries": [empty_key]})
    for key in ("\x00" * 512, '"' * 512, "\\" * 512):
        for check in (a.COVERAGE_ENTRY, a.COVERAGE_ENTRY.serialize):
            with pytest.raises(ValueError):
                check({**entries[0], "key": key})
    for invalid in ({**entries[0], "key": "k" * 513}, {**entries[0], "bytes": 1}, {**entries[0], "sha256": "a" * 64},
                    {**entries[0], "state": "present"}):
        with pytest.raises(ValueError):
            a.COVERAGE_ENTRY(invalid)
    next_page = {**page, "page_seq": "00000000000000000002", "previous_sha256": s.sha256(page),
                 "entries": [{**entries[0], "key": "z/final"}]}
    assert a.coverage_root([page, next_page]) == s.sha256([s.sha256(page), s.sha256(next_page)])
    for changed in ({**next_page, "previous_sha256": "f" * 64}, {**next_page, "entries": [entries[0] | {"key": "a/earlier"}]}):
        with pytest.raises(ValueError):
            a.coverage_root([page, changed])


def test_obligation_page_count_limit_is_conjunctive() -> None:
    page = obj("authorization_obligations", "page")
    entry = page["entries"][0]
    entries = [{**entry, "original_receipt": {**entry["original_receipt"], "event_seq": f"{i + 1:020}"}} for i in range(1001)]
    # 1,000 complete entries exceed 256 KiB: the byte cap wins first.
    with pytest.raises(ValueError, match="evidence_size"):
        r.AUTHORIZATION_OBLIGATION_PAGE({**page, "entries": entries[:1000]})
    # Isolate the independent count ceiling without weakening production.
    bounded = s.Schema(r.AUTHORIZATION_OBLIGATION_PAGE.fields, 1048576, r.AUTHORIZATION_OBLIGATION_PAGE.invariant)
    bounded({**page, "entries": entries[:1000]})
    with pytest.raises(ValueError):
        bounded({**page, "entries": entries})


@pytest.mark.parametrize("name", ["HEALTH", "HEALTH_CLAIM", "PUBLISHER_OPERATIONS", "COUNTER", "CLOSURE", "MANIFEST", "PROOF_MANIFEST", "PREREQUISITE_LEDGER", "GATE_OUTPUT", "CAP", "RETENTION", "SEALED_CLOSURE"])
def test_retained_rules_delegate_to_baseline(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    base = getattr(p1, name)
    new = SCHEMAS[name]
    calls = []
    original = s.Schema.__call__
    def spy(self: s.Schema, value: Any) -> None:
        if self is base:
            calls.append(copy.deepcopy(value))
            raise ValueError("baseline-delegation")
        original(self, value)
    monkeypatch.setattr(s.Schema, "__call__", spy)
    for check, value in ((new, SEEDS[name]), (new.serialize, SEEDS[name]), (new.parse, canonical(SEEDS[name]))):
        with pytest.raises(ValueError, match="baseline-delegation"):
            check(value)
    assert len(calls) == 3
    assert "contract_revision" not in calls[0]
    for key, value in calls[0].items():
        if name == "HEALTH" and key in ("observed_at_us", "publisher_heartbeat_us"):
            assert value == SEEDS[name][key.replace("_us", "_ns")]
        elif name == "HEALTH_CLAIM" and key == "claim_at_us":
            assert value == SEEDS[name]["claim_at_ns"]
        elif name == "COUNTER" and key == "publisher":
            assert value == {k: v for k, v in SEEDS[name][key].items() if k not in {"scheduled_slots", "lost_claims", "unknown_outcomes", "late_cycles"}}
        elif name == "RETENTION" and key == "kinds":
            assert value == [k for k in SEEDS[name][key] if k != "async_settle_shadow_close_result_v2"]
        else:
            assert value == SEEDS[name][key]


@pytest.mark.parametrize("name", ["same-day", "cross-day-equal-sequence"])
def test_completion_preservation_delegation(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    context = obj("completion_context", name)
    seen = []
    def spy(original: Any, completion: Any) -> None:
        seen.append((original, completion))
        raise ValueError("retained-completion")
    monkeypatch.setattr(p1, "validate_completion", spy)
    with pytest.raises(ValueError, match="retained-completion"):
        r.validate_completion(**context)
    original, completed = seen[0]
    for field in context["original_page"]["events"][0]:
        if field != "seq":
            assert original[field] == context["original_page"]["events"][0][field]
    for field in context["completion_page"]["events"][0]:
        if field not in ("seq", "completion_of"):
            assert completed[field] == context["completion_page"]["events"][0][field]


@pytest.mark.parametrize("name", ["a1-cross-day-sequence", "a1-close-kind", "a1-binary64-wall"])
def test_broadening_enforcement_routes(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d, "DELTAS", tuple(replace(item, predicate=lambda v: False) if item.name == name else item for item in d.DELTAS))
    with pytest.raises(ValueError):
        if name == "a1-cross-day-sequence":
            r.validate_completion(**obj("completion_context", "cross-day-equal-sequence"))
        elif name == "a1-close-kind":
            a.RETENTION(obj("controls", "retention"))
        else:
            h.HEALTH(obj("health", "empty"))


@exhaustive
@pytest.mark.parametrize("value", [None, False, 0, 1.0, "", [], {}, {"a": []}])
def test_completion_obligation_functions_total(value: Any) -> None:
    context = obj("completion_context", "same-day")
    arguments = [context[k] for k in ("original_page", "original_locator", "completion_page", "completion_locator")]
    for i in range(4):
        changed = list(arguments)
        changed[i] = value
        with pytest.raises(ValueError):
            r.validate_completion(*changed)
    fresh = vector("authorization_obligations", "derive-fresh")["object"]
    for field in ("page", "event_seq"):
        with pytest.raises(ValueError):
            r.authorization_obligation(**{**fresh, field: value})
    with pytest.raises(ValueError):
        r.obligation_projection(value, [])
    if value != []:
        with pytest.raises(ValueError):
            r.obligation_projection(obj("closure", "shutdown"), value)


def test_obligation_status_roots_and_terminal_independence() -> None:
    closure = obj("closure", "shutdown")
    first, replay = obj("authorization_obligations", "fresh"), obj("authorization_obligations", "replay")
    loc = obj("completion_context", "same-day")["original_locator"]
    first.update(status="terminal_reconciled", terminal_reference=loc)
    replay.update(status="blocked", gap="authorization_horizon_gap")
    projection = r.obligation_projection(closure, [replay, first])
    assert projection["open_obligation_count"] == 1
    assert projection["open_obligations_root"] == s.sha256([replay])
    assert projection["reconciliation_root"] == s.sha256([first, replay])
    with pytest.raises(ValueError):
        r.obligation_projection(closure, [first, first])
    with pytest.raises(ValueError):
        r.obligation_projection({**closure, "boot": "other"}, [first])
    fresh = vector("authorization_obligations", "derive-fresh")["object"]
    terminal = obj("completion_context", "same-day")["original_page"]
    terminal["boot"] = "fresh-router"
    terminal_loc = r.event_locator(terminal, terminal["events"][0]["seq"])
    obligation = r.authorization_obligation(**fresh)
    # Cross-boot first terminals are allowed; same-writer recovery is separate.
    r.validate_terminal_join(obligation, fresh["page"], terminal, terminal_loc)


def test_binary64_independent_delta_oracle() -> None:
    for value, expected in ((0, True), (1.0, True), (-0.0, True), (2**100, True),
        (math.inf, False), (float("nan"), False), (True, False), ("1", False),
        (-1, False), (2**53 + 1, False), (2**1024, False)):
        assert d.broadening("a1-binary64-wall", value) is expected


def test_replay_after_terminal_uses_original_lifetime() -> None:
    inputs = vector("authorization_obligations", "derive-replay")["object"]
    inputs["page"]["events"][0]["event_at_us"] += 1
    inputs["page"]["events"][0]["receipt_at_us"] += 1
    obligation = r.authorization_obligation(**inputs)
    context = obj("completion_context", "same-day")
    r.validate_terminal_join(obligation, inputs["page"], context["original_page"], context["original_locator"],
        original_page=inputs["original_page"], original_event_seq=inputs["original_event_seq"])
    with pytest.raises(ValueError):
        r.validate_terminal_join(obligation, inputs["page"], context["original_page"], context["original_locator"])


def test_numeric_scalar_policy_follows_retained_strings() -> None:
    for retained in (False, True):
        for paths in ((), (("n",),)):
            schema = s.Schema({"n": lambda v: None, "s": lambda v: None}, 100,
                              retained_strings=retained, binary64_paths=paths)
            with pytest.raises(ValueError, match="scalar-string" if retained else "json_encoding"):
                schema({"n": 1, "s": "\ud800\udc00"})


def test_completion_cannot_precede_original_receipt() -> None:
    context = obj("completion_context", "same-day")
    original = context["original_page"]["events"][0]
    completed = context["completion_page"]["events"][0]
    original["receipt_at_us"] += 2
    completed["event_at_us"] += 1
    completed["receipt_at_us"] += 3
    context["original_locator"] = r.event_locator(context["original_page"], original["seq"])
    context["completion_locator"] = r.event_locator(context["completion_page"], completed["seq"])
    with pytest.raises(ValueError):
        r.validate_completion(**context)


def test_zero_scheduled_slots_and_trace_stage_lower_bounds() -> None:
    counter = obj("counter", "publisher-idle")
    h.validate_publisher_traces(counter, [], day="2026-10-11", boot="issuer", first_slot=None, last_slot=None)
    with pytest.raises(ValueError):
        h.validate_publisher_traces(counter, [], day="2026-10-11", boot="issuer", first_slot=1, last_slot=None)
    trace = obj("publisher_slot_trace", "published")
    counter.update(scheduled_slots=1, claim_attempts=1, publications=1)
    with pytest.raises(ValueError, match="publisher_accounting_gap"):
        h.validate_publisher_traces(counter, [trace], day=trace["day"], boot=trace["boot"], first_slot=trace["slot"], last_slot=trace["slot"])


@pytest.mark.parametrize("empty_count", [1, 3])
@pytest.mark.parametrize("last", ["a", "z"])
def test_coverage_order_survives_empty_pages(empty_count: int, last: str) -> None:
    page = obj("controls", "coverage-close_result")
    entry = page["entries"][0]
    pages = []
    for rows in ([{**entry, "key": "z"}], *([[]] * empty_count), [{**entry, "key": last}]):
        pages.append({**page, "page_seq": f"{len(pages) + 1:020}",
                      "previous_sha256": s.sha256(pages[-1]) if pages else None, "entries": rows})
    with pytest.raises(ValueError):
        a.coverage_root(pages)


@pytest.mark.parametrize("text", ["é", "\n", "\\"])
def test_binary64_optin_preserves_strict_string_policy(text: str) -> None:
    for paths in ((), (("n",),)):
        schema = s.Schema({"n": lambda v: None, "s": lambda v: None}, 100, binary64_paths=paths)
        with pytest.raises(ValueError, match="^json_encoding$"):
            schema({"n": 1, "s": text})
        if paths:
            with pytest.raises(ValueError, match="^json_encoding$"):
                schema({"n": 1.0, "s": text})


@pytest.mark.parametrize("padding", [b"\n", b"\r", b"\t"])
def test_a1_health_does_not_broaden_strict_transport_whitespace(padding: bytes) -> None:
    body = obj("health", "empty")
    for schema, value in ((p1.HEALTH, d.health_projection(body)), (h.HEALTH, body)):
        with pytest.raises(ValueError, match="^json_encoding$"):
            schema.parse(canonical(value) + padding)


def trace_counter(trace: dict[str, Any]) -> dict[str, Any]:
    counter = obj("counter", "publisher-idle")
    outcome = trace["outcome"]
    counter.update(scheduled_slots=1, claim_attempts=int(outcome != "missed"),
                   missed_slots=int(outcome == "missed"), lost_claims=int(outcome == "lost_claim"),
                   claims=int(outcome == "published"), scans=int(outcome == "published"),
                   publications=int(outcome == "published"), failures=int(outcome == "failed"))
    if trace["cycle_us"] is not None:
        # Independent explicit retained interval oracle (upper endpoint inclusive).
        bounds = [100, 500, 1000, 2000, 5000, 10000, 50000, 200000]
        index = next((i for i, bound in enumerate(bounds) if trace["cycle_us"] <= bound), 8)
        counter["cycle_us"]["counts"][index] = 1
        counter["rpc_us"]["counts"][0] = trace["rpc_count"] - 1
        counter["rpc_us"]["counts"][-1] = 1
    return counter


def reconcile(counter: dict[str, Any], trace: dict[str, Any]) -> None:
    h.validate_publisher_traces(counter, [trace], day=trace["day"], boot=trace["boot"],
                               first_slot=trace["slot"], last_slot=trace["slot"])


@pytest.mark.parametrize("field", ["cycle_us", "rpc_us"])
def test_trace_rejects_missing_histogram_samples(field: str) -> None:
    trace = obj("publisher_slot_trace", "published")
    counter = trace_counter(trace)
    reconcile(counter, trace)
    counter[field]["counts"] = [0] * 9
    with pytest.raises(ValueError, match="publisher_accounting_gap"):
        reconcile(counter, trace)


@pytest.mark.parametrize("coverage", ["empty", "missed"])
@pytest.mark.parametrize("field", ["cycle_us", "rpc_us", "claims", "scans"])
def test_zero_coverage_rejects_ghost_totals(coverage: str, field: str) -> None:
    trace = obj("publisher_slot_trace", "missed")
    counter = obj("counter", "publisher-idle") if coverage == "empty" else trace_counter(trace)
    if field.endswith("_us"):
        counter[field]["counts"][0] = 1
    else:
        counter[field] = 1
    with pytest.raises(ValueError):
        h.validate_publisher_traces(counter, [] if coverage == "empty" else [trace],
            day=trace["day"], boot=trace["boot"], first_slot=None if coverage == "empty" else trace["slot"],
            last_slot=None if coverage == "empty" else trace["slot"])


@pytest.mark.parametrize("duration,index", [(0, 0), (100, 0), (101, 1), (500, 1), (501, 2),
    (1000, 2), (1001, 3), (2000, 3), (2001, 4), (5000, 4), (5001, 5),
    (10000, 5), (10001, 6), (50000, 6), (50001, 7), (200000, 7), (200001, 8)])
def test_cycle_histogram_endpoints_and_distribution(duration: int, index: int) -> None:
    trace = {**obj("publisher_slot_trace", "published"), "cycle_us": duration}
    counter = trace_counter(trace)
    assert counter["cycle_us"]["counts"][index] == 1
    reconcile(counter, trace)
    counter["cycle_us"]["counts"][index] = 0
    counter["cycle_us"]["counts"][(index + 1) % 9] = 1
    with pytest.raises(ValueError, match="publisher_accounting_gap"):
        reconcile(counter, trace)


@pytest.mark.parametrize("offset", [-1, 250000001, 2000000000])
@pytest.mark.parametrize("route", ["schema", "aggregate"])
def test_out_of_slot_server_claim_cannot_be_a_healthy_loss(offset: int, route: str) -> None:
    trace = obj("publisher_slot_trace", "lost-claim")
    trace["claim_at_ns"] = trace["slot"] * 2000000000 + offset
    with pytest.raises(ValueError, match="a1-server-slot"):
        if route == "schema":
            h.PUBLISHER_SLOT_TRACE(trace)
        else:
            reconcile(trace_counter(trace), trace)
    # An out-of-slot attempted claim is representable as a failure diagnostic.
    failed = {**trace, "outcome": "failed"}
    h.PUBLISHER_SLOT_TRACE(failed)
    reconcile(trace_counter(failed), failed)


@pytest.mark.parametrize("offset", [0, 250000000])
def test_lost_claim_endpoints(offset: int) -> None:
    trace = obj("publisher_slot_trace", "lost-claim")
    trace["claim_at_ns"] = trace["slot"] * 2000000000 + offset
    h.PUBLISHER_SLOT_TRACE(trace)
    reconcile(trace_counter(trace), trace)


def test_reader_delegates_retained_usability_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    body = obj("health", "empty")
    metadata = {"committed_at_ns": body["observed_at_ns"], "read_at_ns": body["observed_at_ns"]}
    assert h.health_usable(body, metadata, 0, 0) is True
    seen = []

    def veto(value: Any, now: int) -> bool:
        seen.append((copy.deepcopy(value), now))
        return False

    monkeypatch.setattr(p1, "health_usable", veto)
    assert h.health_usable(body, metadata, 0, 0) is False
    assert seen == [(d.health_projection(body), body["observed_at_ns"])]


def decode_result(raw: bytes, retained: bool, paths: tuple[tuple[str, ...], ...], limit: int = 65536) -> Any:
    try:
        value = s.decode(raw, limit, retained_strings=retained, binary64_paths=paths)
        return ("ok", canonical(value), type(value))
    except ValueError as exc:
        return (type(exc), str(exc))


@pytest.mark.parametrize("retained", [False, True])
def test_numeric_transport_policy_and_diagnostics(retained: bool) -> None:
    # Traced branch witnesses; declarations never change unrelated transport.
    raws = [b'{"s":"ok"}', b'{"s":"\\u00e9"}', b'{"s":"\\n"}', b'{"s":"\\\\"}',
            b'{"s":"\\ud800"}', b'{"s":"\\u0061"}', b'{"s":1,"s":2}', b'NaN',
            b'1.0', b'-0', b'9223372036854775808', b'{"s":1.0,"s":2}',
            b'{"s":1.0,}', b'\xef\xbb\xbf{}', b'[' * 17 + b'0' + b']' * 17]
    for raw in raws:
        for suffix in (b'', b' ', b'\n', b'\r', b'\t'):
            assert decode_result(raw + suffix, retained, ()) == decode_result(raw + suffix, retained, (("n",),))
    assert decode_result(b'{"n":1.0,"n":2.0}', retained, (("n",),)) == (ValueError, "duplicate_key")
    for limit in (0, 10, 100):
        assert decode_result(b'{"s":"ok"}', retained, (), limit) == decode_result(b'{"s":"ok"}', retained, (("n",),), limit)
    for paths, n in (((), 1), ((("n",),), 1.0)):
        for text in ("ok", "é", "\n", "\\"):
            schema = s.Schema({"n": lambda v: None, "s": lambda v: None}, 100,
                              retained_strings=retained, binary64_paths=paths)
            if retained or text == "ok":
                assert_round_trip(schema, {"n": n, "s": text})
            else:
                with pytest.raises(ValueError, match="^json_encoding$"):
                    schema({"n": n, "s": text})


# Drive the complete existing raw-token transport corpus through both numeric
# selections, in both policies, with actual declared numbers before the payload.
from tests.test_async_settle_shadow_v2_evidence import (  # noqa: E402
    CONSTRUCTION,
    OBJECT_TOKEN_CASES,
    RAW_TOKEN_CASES,
    parity_get,
    raw_token_variant,
)

TOKEN_CASES = RAW_TOKEN_CASES + OBJECT_TOKEN_CASES


@exhaustive
@pytest.mark.parametrize("start", range(0, len(TOKEN_CASES), 32))
def test_numeric_optin_baseline_transport_corpus(start: int) -> None:
    for name, path, key in TOKEN_CASES[start:start + 32]:
        for scalar in (None,) if key else (None, "é", "😀", "\n", "\x00"):
            value = copy.deepcopy(CONSTRUCTION[name][1])
            if scalar is not None:
                parity_get(value, path[:-1])[path[-1]] = scalar
            for mode in ("canonical", "utf8", "alias", "control"):
                raw = raw_token_variant(value, path, key, mode)
                for retained in (False, True):
                    assert decode_result(raw, retained, ()) == decode_result(raw, retained, (("absent-number",),))
                    assert_active_number_transport(raw, retained)
                assert_number_token_differential(raw)


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("tail", [b'"s":', b'"s":"unterminated}', b'"s":0,}'])
def test_malformed_tail_keeps_declared_number_policy(retained: bool, tail: bytes) -> None:
    with pytest.raises(ValueError, match="^json_shape$"):
        s.decode(b'{"n":1,' + tail, 65536, retained_strings=retained)
    for token in (b"1.0", str(2**100).encode()):
        with pytest.raises(ValueError, match="^json_shape$"):
            s.decode(b'{"n":' + token + b',' + tail, 65536,
                     retained_strings=retained, binary64_paths=(("n",),))


def test_actual_health_valid_float_does_not_mask_trailing_comma() -> None:
    body = obj("health", "empty")
    for value in (1, 1.0):
        raw = canonical(dict(body, backlog_age_at_seconds=value))
        h.HEALTH.parse(raw)
        with pytest.raises(ValueError, match="^json_shape$"):
            h.HEALTH.parse(raw[:-1] + b',}')


def assert_active_number_transport(payload: bytes, retained: bool) -> None:
    def result(token: bytes, paths: tuple[tuple[str, ...], ...]) -> Any:
        try:
            value = s.decode(b'{"n":' + token + b',"payload":' + payload + b'}',
                             262144, retained_strings=retained, binary64_paths=paths)
            assert type(value["n"]) is (float if token == b"1.0" else int)
            assert value["n"] == (2**100 if len(token) > 20 else 1)
            # Normalize ONLY the declared field, retaining every payload type.
            value["n"] = 1
            return ("ok", canonical(value))
        except ValueError as exc:
            return (type(exc), str(exc))
    baseline = result(b"1", ())
    for token in (b"1.0", str(2**100).encode()):
        assert result(token, (("n",),)) == baseline, (payload, retained, token)


NUMBER_PATHS = ((), ("n",), ("payload", "n"), ("a", "*", "n"), ("a", "*", "*"),
                ("*", "n"), ("*", "*"), ("escaped1",), ("*",))


def assert_number_token_differential(raw: bytes) -> None:
    from trusted_router import async_settle_shadow_v2_a1_numbers as numbers
    from trusted_router.async_settle_shadow_wire import bounded_json

    # Independent whole-document selection oracle; callbacks preserve duplicate
    # members and all number lexemes without numeric conversion or range limits.
    class Members(list):
        pass

    tokens: list[tuple[str, object]] = []

    def token(lexeme: str) -> object:
        marker = object()
        tokens.append((lexeme, marker))
        return marker

    valid = recursive = False
    selected: set[int] = set()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=Members,
                           parse_int=token, parse_float=token)
        valid = True
    except RecursionError:
        recursive = True
    except (ValueError, UnicodeError):
        pass

    def visit(value: Any, paths: tuple[tuple[str, ...], ...]) -> None:
        if type(value) is Members:
            for key, child in value:
                visit(child, tuple(p[1:] for p in paths if p and p[0] == key))
        elif type(value) is list:
            for child in value:
                visit(child, tuple(p[1:] for p in paths if p and p[0] == "*"))
        elif () in paths:
            selected.add(id(value))

    if valid:
        visit(value, NUMBER_PATHS)
    if not recursive:
        planned = list(numbers._number_tokens(raw, NUMBER_PATHS))
        assert [lexeme for lexeme, _ in planned] == [lexeme for lexeme, _ in tokens], raw
        if valid:
            assert [allowed for _, allowed in planned] == [id(marker) in selected for _, marker in tokens], raw

    # Compare actual hook calls through the authoritative retained decoder, so
    # byte/depth gates, duplicate keys and constants can stop either run early.
    # Isolate selection from numeric validity: both numeric callbacks return 0.
    expected: list[str] = []
    actual: list[str] = []

    def record(lexeme: str, retained: Any) -> int:
        expected.append(lexeme)
        return 0

    def selected_number(lexeme: str) -> int:
        actual.append(lexeme)
        return 0

    def result(hook: Any) -> Any:
        try:
            return ("ok", bounded_json(raw, 262144, signed=True, number_hook=hook))
        except ValueError as exc:
            return (type(exc), str(exc))

    baseline = result(record)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(numbers, "_float", selected_number)
        patch.setattr(numbers, "_int", selected_number)
        patch.setattr(numbers, "binary64", lambda value: None)
        hook = numbers.number_decoder(raw, NUMBER_PATHS)
        observed = result(lambda lexeme, retained: hook(lexeme, selected_number))
    assert actual == expected, raw
    assert observed == baseline, raw


def hostile_number_documents() -> list[bytes]:
    return [
        b'', b'"unterminated', b'{"s":0,}', b'undefined', b'[0,]',
        b'{"x":1,"x":2}', b'{"x":1,"x":2,}', b'[' * 17 + b'0' + b']' * 17,
        b'{"n":1.0,"s":', b'{"n":1.0,"s":"unterminated}', b'{"n":1.0,"s":0,}',
        b'{"a":[{"n":1.0,"s":2},[3,4.0]],"n":5,"n":6.0}',
        b'{"n":NaN,"a":[Infinity,-Infinity,1.0],"x":2}',
        b'{"n":-0,"a":[-0.0,1e+2,1E-2,1e,1e+]}',
        b'{"n":' + str(2**100).encode() + b',"x":' + b'9' * 5000 + b'}',
        br'{"n":1.0,"s":"123 \" 4 \\ 5 \u0036","escaped\u0031":7.0}',
        br'{"n":1.0,"s":"\ud800","a":["\ud800\udc00",8]}',
        br'{"n":1.0,"s":"\uXX00","x":2}', b'{"n":1.0,"s":"\x00","x":2}',
        b'{"n":1.0,"s":"\xff","x":2}', b'\xef\xbb\xbf{"n":1.0}',
        *[('{"n":' + token + ',"a":[2]}').encode() for token in
          ("1١", "1.١", "1e١", "+1", "01", "00", "-.1", "-Infinity", "1e+", "1.")],
        b'{"n":1.0} 2', b'  \t\n\r{"n":1.0,"a":[2,3]} \r\n',
        b'{"n":1.0,\v"x":2}', b'{"n":1.0,\f"x":2}',
        *[b'[' * depth + b'{"n":1.0}' + b']' * depth for depth in (15, 16, 17, 100, 10000)],
    ]


@exhaustive
def test_number_scanner_every_hostile_prefix() -> None:
    for raw in hostile_number_documents():
        for end in range(len(raw) + 1):
            prefix = raw[:end]
            assert_number_token_differential(prefix)
            for retained in (False, True):
                assert_active_number_transport(prefix, retained)


def test_number_scanner_string_tokens_and_paths() -> None:
    from trusted_router.async_settle_shadow_v2_a1_numbers import _number_tokens
    raw = b'{"s":"123", "a":[{"n":1.0,"s":"2"},[3,4]],"n":5,"n":6}'
    assert list(_number_tokens(raw, (("a", "*", "n"), ("n",)))) == [
        ("1.0", True), ("3", False), ("4", False), ("5", True), ("6", True)]


@pytest.mark.parametrize("retained", [False, True])
def test_no_paths_never_invokes_number_planner(monkeypatch: pytest.MonkeyPatch, retained: bool) -> None:
    from trusted_router import async_settle_shadow_v2_a1_numbers as numbers

    def forbidden(*args: Any) -> Any:
        pytest.fail("number planner invoked without a declared path")

    monkeypatch.setattr(numbers, "number_decoder", forbidden)
    assert s.decode(b'{"n":1}', 100, retained_strings=retained) == {"n": 1}
    with pytest.raises(ValueError, match="^integer$"):
        s.decode(b'{"n":1.0}', 100, retained_strings=retained)
