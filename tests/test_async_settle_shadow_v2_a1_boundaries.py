"""A1 endpoint, delegation and totality proofs beyond the committed examples."""
from __future__ import annotations

import copy
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


def test_new_numeric_schemas_scalar_gate_precedes_serialization(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(value: Any) -> bytes:
        pytest.fail("serialization preceded scalar-string rejection")
    value = obj("health", "empty")
    value["publisher_region"] = "\ud800\udc00"
    monkeypatch.setattr(s, "canonical", forbidden)
    with pytest.raises(ValueError, match="scalar-string"):
        h.HEALTH(value)


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
