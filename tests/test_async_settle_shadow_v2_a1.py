"""A1 contract proofs, independent fixtures, endpoints and retained delegation."""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests import shadow_v2_a1_fixture_generator as generator
from tests.test_async_settle_shadow_v2_contract import assert_round_trip, strict_equal
from tests.test_async_settle_shadow_v2_evidence import (  # noqa: F401 - fixture used by exhaustive
    exhaustive,
    untraced,
)
from trusted_router import async_settle_shadow_v2_a1_deltas as d
from trusted_router import async_settle_shadow_v2_a1_evidence as a
from trusted_router import async_settle_shadow_v2_a1_health as h
from trusted_router import async_settle_shadow_v2_a1_numbers as numbers
from trusted_router import async_settle_shadow_v2_a1_reconciliation as r
from trusted_router import async_settle_shadow_v2_contract as contract
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router import async_settle_shadow_v2_schema as s
from trusted_router.detached_jws import canonical

FIXTURES = Path(__file__).parent / "fixtures/async_settlement/shadow_v2_a1"
VECTORS = [(p.stem, v) for p in sorted(FIXTURES.glob("*.json"))
           if p.stem in generator.CHANGED | generator.ADDED
           for v in json.loads(p.read_bytes())["variants"]]
SCHEMAS = {name: schema for module in (a, h, r) for name, schema in vars(module).items() if isinstance(schema, s.Schema)}


def vector(file: str, name: str) -> dict[str, Any]:
    return copy.deepcopy(next(v for f, v in VECTORS if f == file and v["name"] == name))


def obj(file: str, name: str) -> dict[str, Any]:
    return vector(file, name)["object"]


def run_vector(v: dict[str, Any]) -> Any:
    value = v["object"]
    target = v["target"]
    if target in SCHEMAS:
        schema = SCHEMAS[target]
        raw = canonical(value)
        if "number_lexeme" in v:
            raw = raw.replace(b'"backlog_age_at_seconds":' + canonical(value["backlog_age_at_seconds"]),
                              b'"backlog_age_at_seconds":' + v["number_lexeme"].encode("ascii"))
        if "transport_bytes" in v:
            raw += b" " * (v["transport_bytes"] - len(raw))
        return schema.parse(raw)
    if target == "validate_completion":
        return r.validate_completion(**value)
    if target == "health_usable":
        return h.health_usable(**value)
    if target == "coverage_root":
        return a.coverage_root(value)
    if target == "validate_publisher_traces":
        return h.validate_publisher_traces(**value)
    if target == "validate_publisher_final":
        return h.validate_publisher_final(value)
    if target == "validate_close_result":
        return a.validate_close_result(**value)
    if target == "authorization_obligation":
        return r.authorization_obligation(**value)
    if target == "obligation_projection":
        return r.obligation_projection(**value)
    raise AssertionError(target)


@pytest.mark.parametrize("file,v", VECTORS, ids=[f + ":" + v["name"] for f, v in VECTORS])
def test_frozen_vectors(file: str, v: dict[str, Any]) -> None:
    if v["expected"] == "reject":
        with pytest.raises(ValueError):
            run_vector(v)
    else:
        result = run_vector(v)
        if "usable" in v:
            assert result is v["usable"]
        elif "result" in v:
            assert strict_equal(result, v["result"])
        if v["target"] in SCHEMAS:
            assert_round_trip(SCHEMAS[v["target"]], v["object"])


def test_release_manifest_and_baseline_frozen() -> None:
    manifest_raw = (FIXTURES / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest_raw).hexdigest() == a.FIXTURE_MANIFEST_SHA256
    manifest = json.loads(manifest_raw)
    baseline_raw = (generator.BASE / "manifest.json").read_bytes()
    assert hashlib.sha256(baseline_raw).hexdigest() == contract.FIXTURE_MANIFEST_SHA256 == manifest["baseline_manifest_sha256"]
    baseline_manifest = json.loads(baseline_raw)
    for filename, entry in baseline_manifest["files"].items():
        raw = (generator.BASE / filename).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        if Path(filename).stem not in generator.CHANGED:
            assert (FIXTURES / filename).read_bytes() == raw
    assert set(manifest["files"]) == set(baseline_manifest["files"]) | {n + ".json" for n in generator.ADDED}
    assert {p.name for p in FIXTURES.iterdir()} == set(manifest["files"]) | {"manifest.json"}
    for filename, entry in manifest["files"].items():
        raw = (FIXTURES / filename).read_bytes()
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        names = [v["name"] for v in json.loads(raw)["variants"]]
        assert entry["variants"] == names and len(names) == len(set(names))
        assert entry["variant_sha256"] == {v["name"]: hashlib.sha256(canonical(v)).hexdigest() for v in json.loads(raw)["variants"]}
    assert generator.build() == {p.name: p.read_bytes() for p in FIXTURES.iterdir()}


@pytest.mark.parametrize("file,name", [("health", "empty"), ("health_basis", "installed"), ("health_basis", "batch")])
def test_binary64_original_bytes_and_native_domain(file: str, name: str) -> None:
    v = vector(file, name)
    schema, value = SCHEMAS[v["target"]], v["object"]
    raw = schema.serialize(value)
    for spelling in (b"1791676800.1250", b"1.791676800125e9", b"NaN", b"Infinity", b"1e999"):
        with pytest.raises(ValueError):
            schema.parse(raw.replace(b"1791676800.125", spelling))
    for damaged in (raw.replace(b'"v":2', b'"v":2.0'), raw.replace(b'"boot"', b'"bo\\u006ft"') if b'"boot"' in raw else raw.replace(b'"source"', b'"sourc\\u0065"'), b"\xef\xbb\xbf" + raw):
        with pytest.raises(ValueError):
            schema.parse(damaged)
    with pytest.raises(ValueError):
        schema({**value, "v": 2.0})


@pytest.mark.parametrize("value", [None, True, [], {}, "0", 1.0, -1, 1 << 63])
def test_reader_total_json_inputs(value: Any) -> None:
    body = obj("health", "empty")
    meta = obj("health_read_metadata", "metadata")
    for args in ((value, meta, 0, 0), (body, value, 0, 0), (body, meta, value, 0), (body, meta, 0, value)):
        with pytest.raises(ValueError):
            h.health_usable(*args)


@pytest.mark.parametrize("name", ["commit-after-read", "observation-over", "age-q-required", "age-e-required", "age-exact-five"])
def test_reader_mutation_witnesses(name: str) -> None:
    assert run_vector(vector("health_read_metadata", name)) is False


@exhaustive
def test_reader_integer_endpoint_grid() -> None:
    body = obj("health", "empty")
    boundary = body["slot"] * 2000000000
    for o, c, rr, q, ee in itertools.product(
        (-1, 0, 250000000, 250000001), (0, 400000000, 750000000, 750000001),
        (0, 400000000, 3499999999, 4750000000, 5000000000),
        (0, 250000000, 250000001), (0, 1, 1250000000, 1500000001)):
        # Independent interval intersection and integer sum, no production helpers.
        expected = (0 <= o <= 250000000 and o <= c <= 750000000 and c <= rr
                    and q <= 250000000 and q + ee <= 1500000000 and rr + q + ee < o + 5000000000)
        candidate = {**body, "observed_at_ns": boundary + o, "publisher_heartbeat_ns": boundary + o}
        assert h.health_usable(candidate, {"committed_at_ns": boundary + c, "read_at_ns": boundary + rr}, q, ee) is expected


def test_publisher_availability_is_separate_from_reader() -> None:
    assert run_vector(vector("health_read_metadata", "dd1-late-or-lost-reply-fresh"))
    for name in ("unknown-reply", "late-reply", "unknown-and-late"):
        trace = obj("publisher_slot_trace", name)
        h.PUBLISHER_SLOT_TRACE(trace)
        assert trace["outcome"] == "failed"
    assert not run_vector(vector("health_read_metadata", "dd1-start-250-commit-900"))
    assert run_vector(vector("health_read_metadata", "dd1-late-commit-fresh"))


def test_publication_slot_binding_and_cache_age_cannot_refresh_observation() -> None:
    claim, body = obj("health_claim", "server-slot"), obj("health", "empty")
    h.validate_publication(claim, body)
    for changed in ({**claim, "owner_boot_id": "other"}, {**claim, "slot": claim["slot"] + 1}, {**claim, "claim_at_ns": claim["claim_at_ns"] + 1}):
        with pytest.raises(ValueError):
            h.validate_publication(changed, body)
    with pytest.raises(ValueError):
        h.validate_publication(claim, body, body)
    reading = obj("health_read_metadata", "fresh")
    assert h.health_usable(**reading)
    reading["metadata"]["read_at_ns"] += 5000000000
    assert not h.health_usable(**reading)
    basis, trace = obj("health_basis", "installed"), obj("publisher_slot_trace", "published")
    h.validate_basis_trace(basis, trace)
    with pytest.raises(ValueError):
        h.validate_basis_trace(basis, {**trace, "body_sha256": "f" * 64})


@pytest.mark.parametrize("age", [0.0, 4.999999999999999, 5.0, math.nextafter(5.0, math.inf), 5.000000001, 1e-300])
def test_backlog_ceiling_preserves_money_threshold(age: float) -> None:
    n, den = age.as_integer_ratio()
    result = numbers.age_microseconds(age)
    assert result * den >= n * 1000000
    assert result == 0 or (result - 1) * den < n * 1000000
    assert (result <= 5000000) == (age <= 5.0)


@pytest.mark.parametrize("created", ["2026-10-10T23:59:55Z", "2026-10-10T23:59:54.999999Z", "2026-10-11T00:00:01Z", "1970-01-01T00:00:00Z", None, "invalid"])
def test_backlog_wall_sample_parity_with_money_summarizer(created: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from contextlib import nullcontext
    from types import SimpleNamespace

    from trusted_router import storage_gcp_async_admission as money

    snapshot = object()
    database = SimpleNamespace(snapshot=lambda: nullcontext(snapshot),
        run_in_transaction=lambda callback, **kwargs: callback(object()))
    monkeypatch.setattr(time, "time", lambda: 1791676800.0)
    monkeypatch.setattr(money, "_query", lambda transaction, statement: [(created, 1, "pending")] if transaction is snapshot else [])
    monkeypatch.setattr(money, "_write_control", lambda *args: None)
    baseline = money.publish_health(database)
    health = obj("health", "empty")
    health.update(complete=baseline["complete"], sample_count=1, backlog_count=1, frozen_micro=1,
        backlog_age_at_seconds=baseline["observed_at"],
        p50_age_us=numbers.age_microseconds(baseline["p50_age_seconds"]),
        p95_age_us=numbers.age_microseconds(baseline["p95_age_seconds"]),
        oldest_unresolved_age_us=numbers.age_microseconds(baseline["oldest_unresolved_age_seconds"]))
    assert_round_trip(h.HEALTH, health)
    assert (health["complete"] and health["p95_age_us"] <= 5000000) == (baseline["complete"] and baseline["p95_age_seconds"] <= 5.0)


DELTA_WITNESSES = {
    "a1-heartbeat-snapshot": ("health", "empty", "publisher_heartbeat_ns", generator.NS + 1),
    "a1-server-slot": ("health_claim", "server-slot", "claim_at_ns", generator.NS + 250000001),
    "a1-publisher-conservation": ("counter", "publisher-idle", "scheduled_slots", 1),
}


@pytest.mark.parametrize("name", sorted(DELTA_WITNESSES))
def test_independent_delta_oracle(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    file, seed, field, bad = DELTA_WITNESSES[name]
    base = obj(file, seed)
    delta = next(item for item in d.DELTAS if item.name == name)
    assert delta.predicate(base) is False
    assert delta.predicate({**base, field: bad}) is True
    check = {"a1-heartbeat-snapshot": h.HEALTH, "a1-server-slot": h.HEALTH_CLAIM,
             "a1-publisher-conservation": h.validate_publisher_final}[name]
    check(base)
    with pytest.raises(ValueError):
        check({**base, field: bad})
    monkeypatch.setattr(d, "DELTAS", tuple(replace(item, predicate=lambda v: True) if item.name == name else item for item in d.DELTAS))
    with pytest.raises(ValueError, match=name):
        check(base)


def test_broadening_delta_oracles() -> None:
    assert {item.name for item in d.DELTAS} == {*DELTA_WITNESSES, "a1-cross-day-sequence", "a1-close-kind", "a1-binary64-wall"}
    for original, completed, expected in (("2026-10-11", "2026-10-12", True), ("2026-10-11", "2026-10-11", False), ("2026-10-12", "2026-10-11", False)):
        assert d.broadening("a1-cross-day-sequence", {"original_day": original, "completion_day": completed}) is expected
    for kind in (*p1.KINDS, "async_settle_shadow_close_result_v2", "async_settle_shadow_close_result_v1"):
        assert d.broadening("a1-close-kind", kind) is (kind == "async_settle_shadow_close_result_v2")


@exhaustive
def test_publisher_conservation_product() -> None:
    base = obj("counter", "publisher-idle")
    for missed, lost, published, failed, unknown, late in itertools.product(range(3), repeat=6):
        candidate = {**base, "scheduled_slots": missed + lost + published + failed,
            "missed_slots": missed, "claim_attempts": lost + published + failed,
            "publications": published, "lost_claims": lost, "failures": failed,
            "unknown_outcomes": unknown, "late_cycles": late}
        if unknown <= failed and late <= failed:
            h.validate_publisher_final(candidate)
        else:
            with pytest.raises(ValueError):
                h.validate_publisher_final(candidate)


def test_trace_counter_independent_coverage_and_outcomes() -> None:
    traces = [obj("publisher_slot_trace", name) for name in ("published", "missed", "lost-claim", "unknown-and-late")]
    start = traces[0]["slot"]
    for i, trace in enumerate(traces):
        trace["slot"] = start + i
        for field in ("claim_at_ns", "observed_at_ns", "publisher_heartbeat_ns"):
            if trace[field] is not None:
                trace[field] += i * 2000000000
    counter = {**obj("counter", "publisher-idle"), "scheduled_slots": 4, "claim_attempts": 3,
        "claims": 2, "scans": 2, "publications": 1, "failures": 1, "missed_slots": 1,
        "lost_claims": 1, "unknown_outcomes": 1, "late_cycles": 1}
    counter["cycle_us"]["counts"][-1] = 3
    counter["rpc_us"]["counts"][0] = 15
    counter["rpc_us"]["counts"][-1] = 3
    args = dict(day=generator.DAY, boot="issuer", first_slot=start, last_slot=start + 3)
    h.validate_publisher_traces(counter, traces, **args)
    with pytest.raises(ValueError):
        h.validate_publisher_traces(counter, traces[:-1], **args)
    with pytest.raises(ValueError):
        h.validate_publisher_traces({**counter, "unknown_outcomes": 0}, traces, **args)


@pytest.mark.parametrize("name", ["same-day", "cross-day-equal-sequence", "original-authorize", "same-day-later-original", "cross-boot", "missing-page"])
def test_shared_completion_mutation_witnesses(name: str) -> None:
    v = vector("completion_context", name)
    if v["expected"] == "accept":
        r.validate_completion(**v["object"])
    else:
        with pytest.raises(ValueError):
            r.validate_completion(**v["object"])


def test_replay_own_locator_and_original_horizon() -> None:
    v = vector("authorization_obligations", "derive-replay")
    result = r.authorization_obligation(**v["object"])
    assert strict_equal(result, v["result"])
    assert result["original_receipt"]["event_seq"] == "00000000000000000004"
    assert result["original_receipt"] != r.event_locator(v["object"]["original_page"], v["object"]["original_event_seq"])
    assert result["horizon_at_us"] == generator.NOW + 172800000000
    v["object"]["page"]["events"][0]["event_at_us"] += 1
    v["object"]["page"]["events"][0]["receipt_at_us"] += 1
    assert r.authorization_obligation(**v["object"])["horizon_at_us"] == result["horizon_at_us"]


def test_obligation_roots_sorted_and_empty() -> None:
    for name in ("compute-unsorted", "compute-empty"):
        v = vector("authorization_obligations", name)
        assert strict_equal(r.obligation_projection(**v["object"]), v["result"])
    empty = r.obligation_projection(obj("closure", "shutdown"), [])
    assert empty["reconciliation_root"] == empty["open_obligations_root"] == hashlib.sha256(b"[]").hexdigest()
    assert empty["open_obligation_count"] == 0


def test_terminal_join_requires_terminal_identity_and_horizon() -> None:
    inputs = vector("authorization_obligations", "derive-fresh")["object"]
    obligation = r.authorization_obligation(**inputs)
    context = obj("completion_context", "same-day")
    terminal_page, loc = context["original_page"], context["original_locator"]
    r.validate_terminal_join(obligation, inputs["page"], terminal_page, loc)
    with pytest.raises(ValueError):
        r.validate_terminal_join(obligation, inputs["page"], inputs["page"], r.event_locator(inputs["page"], inputs["event_seq"]))
    with pytest.raises(ValueError):
        r.validate_terminal_join({**obligation, "horizon_at_us": generator.NOW}, inputs["page"], terminal_page, loc)
    with pytest.raises(ValueError):
        r.validate_terminal_join({**obligation, "identity_sha256": "f" * 64}, inputs["page"], terminal_page, loc)


def test_sealed_projection_close_budget_and_coverage() -> None:
    result, closure, counter = obj("close_result", "shutdown"), obj("closure", "shutdown"), obj("counter", "closed-idle")
    lifecycle = {"issuer": {"revision": closure["router_revision"], "region": closure["region"], "lifetime": {"start_us": generator.NOW, "end_us": generator.NOW}}}
    assert a.sealed_closure(result, closure, counter, lifecycle=lifecycle, termination_applies=True) == obj("closure", "sealed")
    for field, bad in (("handoff_attempt_limit", 2), ("shutdown_remaining_us", None), ("closure_sha256", "f" * 64), ("boot", "other"), ("final_checkpoint_seq", 2)):
        with pytest.raises(ValueError):
            a.sealed_closure({**result, field: bad}, closure, counter, lifecycle=lifecycle, termination_applies=True)
    with pytest.raises(ValueError):
        a.sealed_closure(result, closure, counter, lifecycle={}, termination_applies=True)
    assert p1.TRANSACTION_ROWS == 6 and p1.TRANSACTION_BODY_BYTES == 331776
    assert p1.TRANSACTION_BODY_BYTES + a.CLOSE_RESULT.limit == 335872


def test_close_kind_row_and_allowlists() -> None:
    result = obj("close_result", "shutdown")
    key = result["day"] + "/" + result["boot"]
    kind = "async_settle_shadow_close_result_v2"
    assert kind in a.KINDS and a.KINDS == a.RETENTION_KINDS == a.EXPORT_KINDS
    assert a.validate_row(kind, key, canonical(result)) == result
    for wrong in (key + "x", "local/fleet"):
        with pytest.raises(ValueError):
            a.validate_row(kind, wrong, canonical(result))
    with pytest.raises(ValueError):
        p1.validate_row(kind, key, canonical(result))
    page = obj("controls", "coverage-close_result")
    assert a.coverage_root([page]) == hashlib.sha256(canonical([s.sha256(page)])).hexdigest()


def test_pure_a1_imports() -> None:
    root = Path(__file__).parents[1]
    script = 'import importlib, pathlib, sys\nfor p in pathlib.Path("src/trusted_router").glob("async_settle_shadow_v2_a1_*.py"): importlib.import_module("trusted_router." + p.stem)\nassert not any(n.startswith(("trusted_router.services", "trusted_router.storage", "trusted_router.routes")) or n == "trusted_router.main" for n in sys.modules)'
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=30,  # noqa: S603 - local pure import probe
        env={**os.environ, "PYTHONPATH": str(root / "src")})
    assert result.returncode == 0, result.stderr
