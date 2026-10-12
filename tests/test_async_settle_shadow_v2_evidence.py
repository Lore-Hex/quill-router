"""P1 locator, backing, evidence-bound and dormancy proof witnesses."""
from __future__ import annotations

import ast
import copy
import importlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_async_settle_shadow_v2_contract import CASES, fixture, proof, signer
from trusted_router import async_settle_shadow_v2_certificate as cert
from trusted_router import async_settle_shadow_v2_contract as contract
from trusted_router import async_settle_shadow_v2_deltas as deltas
from trusted_router import async_settle_shadow_v2_evidence as e
from trusted_router import async_settle_shadow_v2_projection as projection
from trusted_router import async_settle_shadow_v2_retained as retained
from trusted_router import async_settle_shadow_v2_schema as s
from trusted_router import billing_snapshot as billing
from trusted_router.detached_jws import canonical


def backing(p: dict[str, Any]) -> cert.ReceiptVerification:
    # Prove malformed backing still fails behind a valid, correctly signed token.
    token = signer().sign(p["claims"], p["now"])
    claims = cert.verify_signature(token, [signer().trusted], p["expected_identity"], p["now"])
    return cert.verify_original_receipt(claims, returned_key=tuple(p["page_key"]), page=canonical(p["page"]) if p["page"] is not None else None)


@pytest.mark.parametrize("change,expected", [
    ("boot", "identity"), ("page", "identity"), ("event", "identity"), ("key", "identity"),
    ("body_boot", "identity"), ("body_day", "identity"), ("identity", "identity"),
    ("sequence", "identity"), ("duplicate_sequence", "identity"), ("coordinate", "identity"),
    ("revision", "identity"), ("authorization_id", "identity"), ("event_digest", "digest"),
    ("facts_digest", "digest"), ("candidate_digest", "digest"), ("missing", "missing"),
    ("extra_field", "backing"), ("oversize", "backing"),
])
def test_correctly_signed_locator_faults(change: str, expected: str) -> None:
    p = proof(1)
    ev = p["page"]["events"][0]
    if change in ("boot", "page", "event"):
        field = {"boot": "boot", "page": "first_seq", "event": "event_seq"}[change]
        value = "other-boot" if change == "boot" else f"{2:020}"
        p["claims"]["original_receipt"][field] = value
        if change == "page":
            p["claims"]["original_receipt"]["event_seq"] = value
        p["page_key"] = s.receipt_key(p["claims"]["original_receipt"])
    elif change == "key":
        p["page_key"][1] += "x"
    elif change == "body_boot":
        p["page"]["boot"] = "wrong"
    elif change == "body_day":
        p["page"]["day"] = "2026-10-12"
    elif change == "identity":
        ev["identity"]["workspace_id_sha256"] = "1" * 64
    elif change == "sequence":
        p["page"]["last_seq"] = f"{2:020}"
    elif change == "duplicate_sequence":
        p["page"]["events"].append(copy.deepcopy(ev))
    elif change == "coordinate":
        ev["receipt_locator"]["boot"] = "wrong"
    elif change == "revision":
        ev["router_revision"] = "b" * 40
    elif change == "authorization_id":
        ev["authorization_id"] = "other"
    elif change == "event_digest":
        p["claims"]["original_receipt"]["event_sha256"] = "1" * 64
    elif change in ("facts_digest", "candidate_digest"):
        field = "facts_sha256" if change == "facts_digest" else "candidate_set_sha256"
        p["claims"][field] = "1" * 64
    elif change == "missing":
        p["page"] = None
    elif change == "extra_field":
        p["page"]["extra"] = None
    else:
        ev["identity"]["journal_region"] = "x" * 65536
    result = backing(p)
    assert result.partition == "C"
    suffix = expected if expected == "missing" else expected + "_gap"
    assert result.reason == "original_receipt_" + suffix


def test_locator_event_digest_is_checked() -> None:
    p = proof()
    p["claims"]["original_receipt"]["event_sha256"] = "1" * 64
    assert backing(p).reason == "original_receipt_digest_gap"


def test_digest_only_candidate_backing_is_not_proof() -> None:
    p = proof(1)
    p["page"]["events"][0]["candidates"] = None
    # Rehash AND resign: a valid signature over a digest-only event is not facts.
    p["claims"]["original_receipt"]["event_sha256"] = s.sha256(p["page"]["events"][0])
    result = backing(p)
    assert result.partition == "C" and result.reason == "original_receipt_backing_gap"


@pytest.mark.parametrize("field,value", [
    ("day", "2026-02-30"), ("day", "2026-1-01"), ("boot", "a/b"), ("boot", "a\\b"),
    ("boot", ""), ("boot", "x" * 65), ("first_seq", "1"), ("event_seq", "00000000000000000000"),
    ("event_seq", "18446744073709551616"), ("event_seq", 1), ("event_sha256", "A" * 64),
])
def test_malformed_locator_is_named_unresolved(field: str, value: Any) -> None:
    p = proof()
    p["claims"]["original_receipt"][field] = value
    result = cert.verify_original_receipt(p["claims"], returned_key=tuple(p["page_key"]), page=canonical(p["page"]))
    assert result.partition == "C" and result.reason == "original_receipt_locator_gap"
    with pytest.raises(ValueError):
        signer().sign(p["claims"], p["now"])


def test_locator_maximum_bytes_and_coordinates() -> None:
    loc = {"day": "2026-10-11", "boot": "b" * 64, "first_seq": f"{s.UINT64:020}",
           "event_seq": f"{s.UINT64:020}", "event_sha256": "f" * 64}
    s.LOCATOR(loc)
    assert len(canonical(loc)) == 246
    coords = {k: v for k, v in loc.items() if k != "event_sha256"}
    assert len(canonical(coords)) == 164
    assert len(s.receipt_key(loc)[1]) == 96
    assert len(canonical({"x": 0, "original_receipt": loc})) - len(canonical({"x": 0})) == 266
    assert len(canonical({"x": 0, "receipt_locator": coords})) - len(canonical({"x": 0})) == 183
    for field in loc:
        bad = dict(loc)
        del bad[field]
        with pytest.raises(ValueError):
            s.LOCATOR(bad)
    with pytest.raises(ValueError):
        s.LOCATOR({**loc, "extra": None})


@pytest.mark.parametrize("kind", ["settle", "refund"])
def test_certificate_terminal_identity_dimensions_and_booking(kind: str) -> None:
    p = proof()
    attempt = fixture("terminal_excluded_" + kind)[0]["object"]["attempt"]
    def verify(at: dict[str, Any], booking: str) -> cert.ReceiptVerification:
        return cert.verify_certificate(p["certificate"], [signer().trusted], p["expected_identity"], p["now"],
            returned_key=tuple(p["page_key"]), page=canonical(p["page"]), attempt=at,
            server_attempt=attempt, booking=booking)
    booked = "settled" if kind == "settle" else "refunded"
    assert verify(attempt, booked).partition == "B"
    assert verify(attempt, "unknown").reason == "booking_unknown"
    assert verify({**attempt, "streamed": True}, booked).partition == "C"
    assert verify({**attempt, "authorization_id": "wrong"}, booked).partition == "C"


def test_endpoint_membership_uses_original_projection_not_current_catalog() -> None:
    p = proof(1)
    attempt = {"authorization_id": p["claims"]["authorization_id"], "terminal_kind": "settle",
        "selected_endpoint": "partner/endpoint", "adapter": "other", "route_type": "chat.completions", "streamed": False}
    def verify(at: dict[str, Any]) -> cert.ReceiptVerification:
        return cert.verify_certificate(p["certificate"], [signer().trusted], p["expected_identity"], p["now"],
            returned_key=tuple(p["page_key"]), page=canonical(p["page"]), attempt=at,
            server_attempt=at, booking="settled")
    assert verify(attempt).partition == "B"
    assert verify({**attempt, "adapter": "openai"}).reason == "exclusion_conflict"
    assert verify({**attempt, "selected_endpoint": "new-live-catalog"}).reason == "exclusion_conflict"


def test_supplied_authorize_facts_must_match_signed_requested_dimensions() -> None:
    p = proof()
    p["claims"]["requested_streamed"] = True
    assert backing(p).reason == "original_receipt_identity_gap"
    ev = p["event"]
    ev["receipt_locator"]["event_seq"] = f"{2:020}"
    ev["seq"] = f"{2:020}"
    p["claims"]["original_receipt"] = {**ev["receipt_locator"], "event_sha256": s.sha256(ev)}
    # Later slots must enforce the same facts even without earlier slots at signing.
    assert signer().issue(p["claims"], ev, p["now"], fresh=True, complete_candidates=True).certificate is None


def page_with_events(count: int, *, full: bool = False) -> dict[str, Any]:
    p = proof(2 if full else 0)
    page = p["page"]
    page["events"] = []
    for i in range(count):
        ev = copy.deepcopy(p["event"])
        ev["seq"] = f"{i + 1:020}"
        ev["receipt_locator"]["event_seq"] = ev["seq"]
        page["events"].append(ev)
    page["last_seq"] = f"{count:020}"
    return page


def test_page_limits_are_conjunctive() -> None:
    page = page_with_events(31, full=True)
    assert all(len(canonical(ev)) == 2048 for ev in page["events"])
    e.PAGE(page)
    assert len(canonical(page)) < 65536
    with pytest.raises(ValueError):
        e.PAGE(page_with_events(32, full=True))
    # Count bound also enforced even if a schema's independent byte budget grows.
    unchecked_bytes = s.Schema(e.PAGE.fields, 1000000, e.PAGE.invariant)
    with pytest.raises(ValueError):
        unchecked_bytes(page_with_events(129))


def padded(value: dict[str, Any], limit: int) -> bytes:
    raw = canonical(value)
    assert len(raw) <= limit
    return raw + b" " * (limit - len(raw))


def test_six_row_serialized_body_budget_is_331776() -> None:
    pages = []
    for i in range(4):
        page = page_with_events(31, full=True)
        # A transaction is one boot, across <=3 retained day buckets.
        start = i * 31 + 1
        page["first_seq"] = f"{start:020}"
        page["last_seq"] = f"{start + 30:020}"
        for j, event in enumerate(page["events"]):
            event["seq"] = f"{start + j:020}"
            event["receipt_locator"]["first_seq"] = page["first_seq"]
            event["receipt_locator"]["event_seq"] = event["seq"]
        pages.append(padded(page, 65536))
    counter_obj = fixture("counter")[0]["object"]
    closure_obj = fixture("closure")[0]["object"]
    counter_obj["boot"] = closure_obj["boot"] = json.loads(pages[0])["boot"]
    counter = padded(counter_obj, 65536)
    closure = padded(closure_obj, 4096)
    assert e.transaction_bodies(pages, counter, closure) == 331776
    assert len(pages) + 2 == e.TRANSACTION_ROWS == 6
    with pytest.raises(ValueError):
        e.transaction_bodies(pages + [pages[0]], counter, closure)
    with pytest.raises(ValueError):
        e.transaction_bodies(pages, counter + b" ", closure)
    with pytest.raises(ValueError):
        e.transaction_bodies(pages, counter, closure + b" ")


@pytest.mark.parametrize("count", [4, 5])
def test_aggregate_page_cap_spans_days(count: int) -> None:
    pages = []
    for i in range(count):
        page = page_with_events(1)
        page["day"] = f"2026-10-{11 + i % 3:02}"
        page["first_seq"] = page["last_seq"] = f"{i + 1:020}"
        ev = page["events"][0]
        ev["seq"] = f"{i + 1:020}"
        ev["authorization_day"] = page["day"]
        ev["event_at_us"] += i % 3 * 86400000000
        ev["receipt_at_us"] = ev["event_at_us"]
        ev["receipt_locator"] = {"day": page["day"], "boot": page["boot"], "first_seq": page["first_seq"], "event_seq": ev["seq"]}
        pages.append(canonical(page))
    counter = canonical(fixture("counter")[0]["object"])
    if count == 4:
        e.transaction_bodies(pages, counter, None)
    else:
        with pytest.raises(ValueError, match="page_count"):
            e.transaction_bodies(pages, counter, None)


@pytest.mark.parametrize("field,cap", [("terminal_reserved", 100000), ("event_reserved", 400000)])
def test_cap_reservations_include_stranded_blocks(field: str, cap: int) -> None:
    obj = fixture("controls")[1]["object"]
    e.CAP({**obj, field: cap})
    assert cap // 100 == (1000 if field == "terminal_reserved" else 4000)
    for value in (cap + 1, cap + 100, 1, 99):
        with pytest.raises(ValueError):
            e.CAP({**obj, field: value})


def test_manifest_counts_do_not_override_byte_bound() -> None:
    obj = fixture("controls")[0]["object"]
    writer = obj["router_writers"]["issuer"]
    obj["router_writers"] = {f"boot-{i}": writer for i in range(4096)}
    assert len(canonical(obj)) > e.MANIFEST_BYTES
    with pytest.raises(ValueError, match="evidence_size"):
        e.MANIFEST(obj)
    e.ROSTER(obj["router_writers"])
    with pytest.raises(ValueError):
        e.ROSTER({**obj["router_writers"], "overflow": writer})
    obj = fixture("controls")[0]["object"]
    obj["revisions"]["router"] = [f"{i:040x}" for i in range(64)]
    e.MANIFEST(obj)
    obj["revisions"]["router"].append("f" * 40)
    with pytest.raises(ValueError):
        e.MANIFEST(obj)
    obj = fixture("controls")[0]["object"]
    gap = {"interval": {"start_us": 0, "end_us": 1}, "reason": "gap"}
    obj["complete"] = False
    obj["gaps"] = [gap] * 128
    e.MANIFEST(obj)
    obj["gaps"].append(gap)
    with pytest.raises(ValueError):
        e.MANIFEST(obj)


def test_health_is_observation_not_money_progress() -> None:
    health = fixture("health")[0]["object"]
    now = health["publisher_heartbeat_us"]
    assert e.health_usable(health, now)
    assert not e.health_usable({**health, "complete": False}, now)
    assert not e.health_usable(health, now - 1)
    assert not e.health_usable({**health, "sample_count": 10001, "backlog_count": 10001}, now)
    for changes in ({"source": "money_worker"}, {"observed_at_us": now + 1}, {"p50_age_us": 2}, {"dead_count": 1}, {"frozen_micro": 1}):
        with pytest.raises(ValueError):
            e.HEALTH({**health, **changes})


def test_g1_key_body_identity_and_policy() -> None:
    objects = [
        (e.KINDS[0], "2026-10-11/auth-v1", fixture("sample")[0]["object"]),
        (e.KINDS[1], "2026-10-11/auth-v1", proof()["record"]),
        (e.KINDS[2], proof()["page_key"][1], proof()["page"]),
        (e.KINDS[3], "2026-10-11/issuer", fixture("counter")[0]["object"]),
        (e.KINDS[4], "2026-10-11/issuer", fixture("closure")[0]["object"]),
        (e.KINDS[5], "2026-10-11/manifest-v2", fixture("controls")[0]["object"]),
        (e.KINDS[5], "2026-10-11/cap-v2", fixture("controls")[1]["object"]),
        (e.KINDS[5], "retention-v2", fixture("controls")[2]["object"]),
        (e.KINDS[6], "local/fleet", fixture("health")[0]["object"]),
        (e.KINDS[7], "local/publish", fixture("health_claim")[0]["object"]),
    ]
    for kind, key, obj in objects:
        assert e.validate_row(kind, key, canonical(obj)) == obj
        with pytest.raises(ValueError):
            e.validate_row(kind, key + "x", canonical(obj))
        with pytest.raises(ValueError):
            e.validate_row(kind, key, canonical({**obj, "policy_version": "shadow-v1"}))


def test_g3_g4_partition_schemas_reject_false_equations() -> None:
    counter = fixture("counter")[0]["object"]
    for field in ("authorize_attempts", "authorize_fresh", "settle_attempts", "refund_attempts", "A", "B", "C"):
        with pytest.raises(ValueError):
            e.COUNTER({**counter, field: 1})
    terminal = fixture("receipt_pages")[2]["object"]
    with pytest.raises(ValueError):
        e.EVENT({**terminal, "original_receipt": None})
    with pytest.raises(ValueError):
        e.EVENT({**terminal, "partition": "A"})


def test_no_runtime_imports_or_calls_to_new_modules() -> None:
    root = Path(__file__).parents[1] / "src"
    new = {path.stem for path in root.rglob("async_settle_shadow_v2_*.py")}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if path.stem not in new:
            assert not any(name in path.read_text() for name in new), str(path)
        else:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trusted_router"):
                    if path.stem == "async_settle_shadow_v2_retained" and node.module == "trusted_router.async_settle_shadow_evidence":
                        assert [n.name for n in node.names] == ["REASONS"]
                        continue  # Verbatim local import; private resolver is exercised by the pure import probe.
                    assert (node.module == "trusted_router" and all(n.name == "billing_snapshot" or n.name in new for n in node.names)) or node.module.rsplit(".", 1)[-1] in new | {"detached_jws", "async_settle_shadow_wire", "billing_snapshot"}, str(path)
                if isinstance(node, ast.Import):
                    assert all(not n.name.startswith("trusted_router") for n in node.names), str(path)


def test_main_routes_and_startup_hooks_are_identical_with_modules_present() -> None:
    root = Path(__file__).parents[1]
    script = '''
import importlib.abc, json, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if "async_settle_shadow_v2_" in fullname:
            raise AssertionError("dormant module imported")
if sys.argv[1] == "absent":
    sys.meta_path.insert(0, Block())
import trusted_router.main as main
app = main.app
assert not any("async_settle_shadow_v2_" in n for n in sys.modules)
def label(f):
    return f.__module__ + "." + f.__qualname__
print(json.dumps({"routes": [(getattr(r, "path", None), sorted(getattr(r, "methods", None) or []), getattr(r, "name", None)) for r in app.routes], "startup": [label(f) for f in app.router.on_startup], "shutdown": [label(f) for f in app.router.on_shutdown], "lifespan": label(type(app.router.lifespan_context))}, sort_keys=True))
'''
    outputs = []
    for mode in ("absent", "present"):
        result = subprocess.run([sys.executable, "-c", script, mode], cwd=root,  # noqa: S603 - fixed local probe
            env={**os.environ, "PYTHONPATH": str(root / "src"), "TR_STORAGE_BACKEND": "memory", "TR_ENVIRONMENT": "test"},
            capture_output=True, text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stderr
        outputs.append(json.loads(result.stdout.splitlines()[-1]))
    assert outputs[0] == outputs[1]


def test_pure_import_has_no_live_modules() -> None:
    # In-process conftest has already imported the app; a clean interpreter proves
    # the transitive import boundary independently of that existing state.
    script = '''
import sys
import trusted_router.async_settle_shadow_v2_certificate
assert not any(n.startswith("trusted_router.services") or n.startswith("trusted_router.storage") or n == "trusted_router.main" or n.startswith("trusted_router.routes") for n in sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", script], env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603 - fixed local probe
    assert result.returncode == 0, result.stderr


def test_schema_fixture_coverage() -> None:
    covered = {name for name, _, _ in CASES}
    required = {"CLAIMS", "HEADER", "COORDINATES", "LOCATOR", "AUTHORIZE_EVENT", "TERMINAL_EVENT", "PAGE", "CERTIFICATE_RECORD", "SAMPLE", "COUNTER", "CLOSURE", "MANIFEST", "CAP", "RETENTION", "HEALTH", "HEALTH_CLAIM", "PROOF_MANIFEST", "PREREQUISITE_LEDGER", "GATE_OUTPUT", "AUTHORIZE_COMPARED", "AUTHORIZE_EXCLUDED", "COMPARED_FULL", "COMPARED_HASH_ONLY", "EXCLUDED"}
    assert required <= covered
    for module_name in ("schema", "contract", "evidence", "certificate"):
        assert importlib.import_module("trusted_router.async_settle_shadow_v2_" + module_name)


def test_uint64_sequence_hole_is_bounded_rejection() -> None:
    page = page_with_events(2)
    page["last_seq"] = f"{s.UINT64:020}"
    page["events"][-1]["seq"] = page["last_seq"]
    page["events"][-1]["receipt_locator"]["event_seq"] = page["last_seq"]
    with pytest.raises(ValueError):
        e.PAGE(page)


def test_unrepresentable_calendar_time_rejects_without_overflow() -> None:
    page = page_with_events(1)
    page["events"][0]["event_at_us"] = s.INT64
    page["events"][0]["receipt_at_us"] = s.INT64
    with pytest.raises(ValueError):
        e.PAGE(page)


def test_legacy_dimensions_and_new_partition_totals_must_reconcile() -> None:
    counter = fixture("counter")[0]["object"]
    counter["counts"][0]["settle_attempts"] = 1
    with pytest.raises(ValueError):
        e.COUNTER(counter)
    bucket = fixture("counter")[1]["object"]
    bucket.update(settle_attempts=1, C=1)
    with pytest.raises(ValueError):
        e.BUCKET(bucket)


def test_excluded_attempt_is_never_exact_or_a_prediction() -> None:
    terminal = fixture("receipt_pages")[2]["object"]
    for comparison in ("exact", "explained-by-catalog-change"):
        with pytest.raises(ValueError):
            e.EVENT({**terminal, "comparison": comparison})
    with pytest.raises(ValueError):
        e.EVENT({**terminal, "prediction": fixture("sample")[1]["object"]})


def test_malformed_nested_backing_identity_is_named_gap() -> None:
    p = proof()
    p["page"]["events"][0]["identity"] = None
    result = cert.verify_original_receipt(p["claims"], returned_key=tuple(p["page_key"]), page=canonical(p["page"]))
    assert result.partition == "C" and result.reason == "original_receipt_backing_gap"


def test_transaction_cannot_mix_writer_bodies() -> None:
    page = page_with_events(1)
    counter = fixture("counter")[0]["object"]
    counter["boot"] = "different-writer"
    with pytest.raises(ValueError, match="page_identity"):
        e.transaction_bodies([canonical(page)], canonical(counter), None)


def test_freshness_is_exclusive_at_five_seconds() -> None:
    health = fixture("health")[0]["object"]
    observed = health["observed_at_us"]
    assert e.health_usable(health, observed + 4999999)
    assert not e.health_usable(health, observed + 5000000)


def stored_variant(filename: str, name: str) -> dict[str, Any]:
    return next(v for v in fixture(filename) if v["name"] == name)["object"]


STORED_CASES = [(filename, v) for filename in ("receipt_pages", "sample", "certificate_record", "counter")
                for v in fixture(filename) if "schema" in v]


@pytest.mark.parametrize("filename,v", STORED_CASES, ids=[f + ":" + v["name"] for f, v in STORED_CASES])
def test_committed_stored_vectors(filename: str, v: dict[str, Any]) -> None:
    schema = getattr(e, v["schema"])
    raw = v["raw"].encode() if "raw" in v else canonical(v["object"])
    def run() -> Any:
        if v.get("target") == "validate_row":
            return e.validate_row(v["kind"], v["row_key"], raw)
        return schema.parse(raw)
    if v["expected"] == "reject":
        with pytest.raises(ValueError):
            run()
    else:
        assert run()


def test_repro_eligible_failure_missing_prediction() -> None:
    v = stored_variant("receipt_pages", "eligible-failure")
    e.EVENT(v)
    with pytest.raises(ValueError):
        e.EVENT({**v, "prediction": None})


@pytest.mark.parametrize("phase", ["settle", "refund"])
@pytest.mark.parametrize("partition", ["A", "B"])
def test_repro_settle_receipt_can_claim_refund_winner_as_B(phase: str, partition: str) -> None:
    v = stored_variant("receipt_pages", phase + "-opposite-winner")
    if partition == "A":
        v.update(partition="A", eligibility="eligible", proof_kind="compared", comparison="exact",
            payload_sha256="0" * 64, prediction=fixture("sample")[1]["object"])
    with pytest.raises(ValueError):
        e.EVENT(v)
    v["booking"]["winner"] = None
    with pytest.raises(ValueError):
        e.EVENT(v)
    # C preserves the original opposite winner; no schema invents a winner.
    v["partition"] = "C"
    e.EVENT(v)


def test_repro_B_receipt_can_claim_computed_mismatch() -> None:
    with pytest.raises(ValueError):
        e.EVENT(stored_variant("receipt_pages", "B-computed-mismatch"))


def test_repro_v1_winner_delta_guard_lost() -> None:
    assert_sample_conformance(stored_variant("sample", "winner-deltas"), False)


def test_repro_observed_exclusion_sample_can_claim_admission_yes() -> None:
    with pytest.raises(ValueError):
        e.SAMPLE(stored_variant("sample", "excluded-admission-yes"))
    e.SAMPLE(stored_variant("sample", "excluded-admission-diagnostic-only"))


@pytest.mark.parametrize("name", ["epoch-zero", "invented-generation"])
def test_repro_certificate_projection_epoch_zero_and_wrong_generation(name: str) -> None:
    v = stored_variant("certificate_record", name)
    with pytest.raises(ValueError, match="identity"):
        e.validate_row(e.KINDS[1], v["authorization_day"] + "/" + v["claims"]["authorization_id"], canonical(v))


def assert_sample_conformance(v: dict[str, Any], accepted: bool) -> None:
    from trusted_router import async_settle_shadow_evidence as old
    legacy = {k: val for k, val in v.items() if k in old.SAMPLE_FIELDS}
    legacy.update(v=1, policy_version="shadow-v1")
    key = v["authorization_day"] + "/" + v["authorization_id"]
    for run in (lambda: old.validate_sample(legacy, key), lambda: e.SAMPLE(v)):
        if accepted:
            run()
        else:
            with pytest.raises(ValueError):
                run()


@pytest.mark.parametrize("path,value", [
    ("authorization_id", "bad/id"), ("authorization_day", "2026-02-30"),
    ("adapter", "bad"), ("route_type", "bad"), ("streamed", 1),
    ("reason_codes", ["usage_missing", "hash"]), ("reason_codes", ["hash", "hash"]),
    ("reason_codes", ["invented"]), ("snapshot_hash", "X" * 64), ("payload_hash", False),
    ("observed_at_us", None), ("authorize_at_us", -1), ("booked_micro", True),
    ("model_id", "bad space"), ("endpoint_id", "x" * 129),
    ("raw_usage.input_tokens", -1), ("python_usage.output_tokens", True),
    ("eligibility.requested", 1), ("eligibility.observed", 0), ("eligibility.exclusion", "invented"),
    ("booking.attempted_kind", "bad"), ("booking.outcome", "pending"), ("booking.source", "bad"),
    ("booking.price_source", "bad"), ("admission.prediction", "bad"), ("admission.reason", "invented"),
    ("admission.tier", -1), ("timing.evidence_write_us", 0), ("timing.comparator_us", True),
    ("provenance.fixture_sha256", "0" * 64), ("provenance.binding_verified", False),
    ("provenance.raw_matches_body", False), ("provenance.rebuild_matches_booking_view", 1),
    ("provenance.snapshot_transport", "bad"), ("provenance.s0_reconstruction", "bad"),
    ("provenance.legacy_oracle", "bad"), ("deployment.router_revision", "a" * 7),
    ("deployment.go_evaluator", "billing-v2"), ("deployment.instance", ""),
    ("python_micro", 3), ("legacy_frozen_micro", 3), ("go_usage.output_tokens", 2),
    ("python_minus_go", 1), ("booked_minus_frozen", 1), ("booked_minus_rebuilt", 0),
    ("rebuilt_minus_frozen", 0), ("classification", "explained-by-catalog-change"),
])
def test_v1_v2_local_sample_invariants(path: str, value: Any) -> None:
    v = fixture("sample")[0]["object"]
    parts = path.split(".")
    target = v
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    assert_sample_conformance(v, False)


def counter_conformance(v: dict[str, Any], accepted: bool) -> None:
    from scripts.async_settle.shadow_report import COUNTER_FIELDS, validate_counter
    boot = "00000000-0000-0000-0000-000000000001"
    old = {k: val for k, val in v.items() if k not in set(e.COUNTER.fields) - COUNTER_FIELDS}
    old.update(v=1, policy_version="shadow-v1", instance=boot, sequence=v["checkpoint_seq"])
    for run in (lambda: validate_counter(v["day"] + "/" + boot, old), lambda: e.COUNTER(v)):
        if accepted:
            run()
        else:
            with pytest.raises(ValueError):
                run()


def test_repro_retained_counter_diagnostic_bound_expanded() -> None:
    counter_conformance(stored_variant("counter", "combined-diagnostics-128"), True)
    counter_conformance(stored_variant("counter", "combined-diagnostics-129"), False)


def test_repro_duplicate_terminal_counter_is_accepted() -> None:
    counter_conformance(stored_variant("counter", "duplicate-terminal_counts"), False)


@pytest.mark.parametrize("field", ["exclusions", "rejections", "drops"])
def test_v1_v2_duplicate_diagnostic_identity(field: str) -> None:
    v = stored_variant("counter", "duplicate-" + field)
    v[field][1]["count"] += 1  # Identity, not whole-row equality.
    counter_conformance(v, False)


def test_completion_preserves_original_observation() -> None:
    original = stored_variant("receipt_pages", "eligible-failure")
    completed = copy.deepcopy(original)
    completed.update(phase="completion", completion_of=original["seq"], seq=f"{int(original['seq']) + 1:020}", prediction=None)
    e.validate_completion(original, completed)
    completed["prediction"] = original["prediction"]
    with pytest.raises(ValueError):
        e.validate_completion(original, completed)
    completed["prediction"] = None
    completed["completion_of"] = completed["seq"]
    with pytest.raises(ValueError):
        e.validate_completion(original, completed)
    completed["completion_of"] = original["seq"]
    completed.update(partition="A", comparison="exact", payload_sha256="0" * 64)
    original["payload_sha256"] = "0" * 64
    completed["booking"].update(outcome="refunded", winner="refund", reference="0" * 64)
    with pytest.raises(ValueError):
        e.validate_completion(original, completed)


@pytest.mark.parametrize("field,value", [
    ("checkpoint_seq", 0), ("counter_overflow", 1), ("started_at_us", -1),
    ("flushed_at_us", 0), ("first_gap_at_us", True), ("router_revision", "a" * 7),
    ("authorize_shadow_hist", [0] * 8), ("evidence_write_hist", [False] * 9),
    ("samples_inserted", 1),
])
def test_v1_v2_counter_local_invariants(field: str, value: Any) -> None:
    v = fixture("counter")[0]["object"]
    v[field] = value
    counter_conformance(v, False)


@pytest.mark.parametrize("field", ["observed_attempts", "evaluable", "header_absent", "envelope_present", "mismatch", "unevaluable"])
def test_v1_v2_counter_retained_equations(field: str) -> None:
    v = fixture("counter")[0]["object"]
    v["counts"][0][field] = 1
    counter_conformance(v, False)


@pytest.mark.parametrize("reason", ["invented", "usage_missing"])
def test_v1_v2_counter_reason_domain(reason: str) -> None:
    v = fixture("counter")[0]["object"]
    v["drops"] = [{"phase": "worker", "adapter": "unknown", "route_type": "unknown", "streamed": None,
                   "reason": reason, "count": 1}]
    counter_conformance(v, reason == "usage_missing")


def test_v1_v2_all_retained_sample_reasons() -> None:
    from trusted_router import async_settle_shadow_evidence as old
    assert e.LEGACY_REASONS == old.REASONS
    for reason in sorted(old.REASONS):
        v = fixture("sample")[0]["object"]
        v.update(classification="unevaluable", reason_codes=[reason], booked_minus_frozen=None)
        v["eligibility"]["exclusion"] = reason
        assert_sample_conformance(v, True)


@pytest.mark.parametrize("phase", ["replay", "completion"])
def test_nonterminal_records_never_add_predictions(phase: str) -> None:
    v = stored_variant("receipt_pages", "eligible-failure")
    v.update(phase=phase, completion_of=v["seq"] if phase == "completion" else None)
    with pytest.raises(ValueError):
        e.EVENT(v)
    v["prediction"] = None
    e.EVENT(v)


def test_completion_cannot_erase_mismatch_or_original_winner() -> None:
    v = stored_variant("receipt_pages", "eligible-failure")
    v.update(comparison="mismatch")
    v["booking"].update(outcome="refunded", winner="refund", reference="0" * 64)
    completed = {**copy.deepcopy(v), "phase": "completion", "completion_of": v["seq"],
                 "seq": f"{int(v['seq']) + 1:020}", "prediction": None}
    e.validate_completion(v, completed)
    completed["comparison"] = "unevaluable"
    with pytest.raises(ValueError):
        e.validate_completion(v, completed)
    completed["comparison"] = "mismatch"
    completed["booking"].update(outcome="settled", winner="settle")
    with pytest.raises(ValueError):
        e.validate_completion(v, completed)


@pytest.mark.parametrize("change", ["extra-root", "extra-object", "missing-object-key", "extra-usage", "missing-usage",
    "many-reasons", "int64", "negative-delta-overflow", "wrong-day", "winner-rebuilt-delta", "delta-null-operand"])
def test_v1_v2_sample_structural_and_numeric_invariants(change: str) -> None:
    v = fixture("sample")[0]["object"]
    if change == "extra-root":
        # Project the same illegal root field into each version explicitly.
        from trusted_router import async_settle_shadow_evidence as old
        legacy = {k: val for k, val in v.items() if k in old.SAMPLE_FIELDS}
        legacy.update(v=1, policy_version="shadow-v1", extra=None)
        with pytest.raises(ValueError):
            old.validate_sample(legacy, v["authorization_day"] + "/" + v["authorization_id"])
        with pytest.raises(ValueError):
            e.SAMPLE({**v, "extra": None})
        return
    if change == "extra-object":
        v["booking"]["extra"] = None
    elif change == "missing-object-key":
        del v["booking"]["source"]
    elif change == "extra-usage":
        v["raw_usage"]["extra"] = 0
    elif change == "missing-usage":
        del v["raw_usage"]["output_tokens"]
    elif change == "many-reasons":
        v["reason_codes"] = sorted(e.LEGACY_REASONS)[:9]
    elif change == "int64":
        v["observed_at_us"] = 1 << 63
    elif change == "negative-delta-overflow":
        v["python_minus_go"] = -(1 << 63)
    elif change == "wrong-day":
        v["authorization_day"] = "2026-10-10"
    elif change == "winner-rebuilt-delta":
        v.update(classification="requires_review", reason_codes=["winner_polarity"], booked_minus_frozen=None,
                 rebuilt_micro=2, booked_minus_rebuilt=0)
    else:
        v.update(classification="unevaluable", go_micro=None)
    assert_sample_conformance(v, False)


@pytest.mark.parametrize("change", [None, "booking", "same-hash", "same-price", "refund", "rebuild-view"])
def test_v1_v2_catalog_change_invariants(change: str | None) -> None:
    v = fixture("sample")[0]["object"]
    v.update(classification="explained-by-catalog-change", rebuilt_micro=3, booked_micro=3,
             rebuilt_snapshot_hash="1" * 64, booked_minus_frozen=None)
    v["provenance"]["rebuild_matches_booking_view"] = True
    if change == "booking":
        v["booked_micro"] = 4
    elif change == "same-hash":
        v["rebuilt_snapshot_hash"] = v["snapshot_hash"]
    elif change == "same-price":
        v["booked_micro"] = v["rebuilt_micro"] = 2
    elif change == "refund":
        v["booking"].update(attempted_kind="refund", outcome="refunded")
    elif change == "rebuild-view":
        v["provenance"]["rebuild_matches_booking_view"] = False
    assert_sample_conformance(v, change is None)


def parity_paths(value: Any, path: tuple[Any, ...] = ()) -> Any:
    yield path
    if isinstance(value, dict):
        for key, child in value.items():
            yield from parity_paths(child, (*path, key))
    elif isinstance(value, list):
        # Homogeneous arrays: one representative; ordering/duplicates separately.
        for index, child in enumerate(value[:1]):
            yield from parity_paths(child, (*path, index))


def parity_get(value: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        value = value[key]
    return value


def parity_mutations(value: Any) -> Any:
    for item in (None, True, False, 0, 1, "x", [], {}):
        yield "type", item
    for item in (-1, 0, 1, 2**63 - 1, 2**63, 2**64):
        yield "integer-boundary", item
    for length in (0, 1, 40, 41, 64, 65, 128, 129, 512, 513, 2048, 2049):
        yield "string-length", "x" * length
    for code in range(32, 127):
        yield "printable-ascii", chr(code)
    for code in (*range(32), 127):
        yield "control", chr(code)
    for item in ("é", "中", "😀", "\u2028", "x\\y", 'x"y', "x\ty", "a/b", "x y"):
        yield "unicode-escape", item
    for item in ("\ud83d\ude00", "\ud800\udc00", "x\ud800\udc00y"):
        yield "native-pair", item
    for item in ENUM_VALUES:
        yield "enum-vocabulary", item
    if isinstance(value, str):
        for length in (0, 1, 40, 41, 64, 65, 128, 129, 512, 513, 2048, 2049):
            yield "domain-string-length", (value[:1] or "a") * length
    if isinstance(value, dict):
        yield "extra-key", {**value, "extra": None}
        for field in value:
            yield "missing-key", {k: v for k, v in value.items() if k != field}
    if isinstance(value, list):
        yield "duplicate-identity", value * 2
        yield "reordered-array", list(reversed(value))
        if value and isinstance(value[0], dict):
            duplicate = copy.deepcopy(value[0])
            if "count" in duplicate:
                duplicate["count"] += 1
            yield "duplicate-identity-different-count", [*value, duplicate]


def parity_accept(check: Any, value: Any) -> bool:
    try:
        check(value)
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, OSError):
        return False


def retained_parity_shapes() -> list[tuple[str, dict[str, Any], Any, Any]]:
    from scripts.async_settle.shadow_report import validate_counter
    from trusted_router import async_settle_shadow_evidence as olde
    from trusted_router import async_settle_shadow_v2_contract as contract
    from trusted_router import async_settle_shadow_wire as oldw
    from trusted_router.detached_jws import b64encode

    def old_sample(v: dict[str, Any]) -> None:
        old = projection.sample_projection(v)
        olde.validate_sample(old, v["authorization_day"] + "/" + v["authorization_id"])

    def old_counter(v: dict[str, Any]) -> None:
        old = projection.counter_projection(v)
        validate_counter(v["day"] + "/" + old["instance"], old)

    def old_wire(v: dict[str, Any]) -> None:
        old = projection.wire_projection(v)
        oldw.parse_header((b64encode(canonical(old)),))

    sample = copy.deepcopy(fixture("sample")[0]["object"])
    sample["classification"] = "unevaluable"
    sample["reason_codes"] = ["usage_missing"]
    wire = copy.deepcopy(fixture("terminal_compared_full")[0]["object"])
    # Populate every optional DTO field from the retained defaults.
    wire["observed"] = oldw.billing.Eligibility().model_dump(mode="json")
    wire["billing_snapshot"]["candidates"][0]["tiers"] = [{"max_prompt_tokens": None, "rates": wire["billing_snapshot"]["candidates"][0]["rates"]}]
    second = copy.deepcopy(wire["billing_snapshot"]["candidates"][0])
    second["endpoint_id"] += "z"
    wire["billing_snapshot"]["candidates"].append(second)
    counter = copy.deepcopy(fixture("counter")[0]["object"])
    for field in ("exclusions", "rejections", "drops"):
        counter[field] = [dict(phase="worker", adapter="unknown", route_type="unknown", streamed=None, reason="usage_missing", count=1)]
    counter["terminal_counts"] = [dict(phase="settle", adapter="openai", route_type="chat.completions", streamed=False, **dict.fromkeys(e.LEGACY_PHASE_FIELDS, 0))]
    shapes = [("sample", sample, old_sample, e.SAMPLE), ("wire", wire, old_wire, lambda v: contract.parse_terminal(canonical(v))),
              ("counter", counter, old_counter, e.COUNTER)]
    return shapes


@pytest.mark.parametrize("file,vector", [(file, v) for file in ("sample", "counter", "terminal_compared_full")
    for v in fixture(file) if v.get("expected") == "accept" and any(tag in v["name"] for tag in ("-prompt-sum", "-reasoning", "-escaped-", "region-", "-retained-"))], ids=lambda v: v["name"] if isinstance(v, dict) else v)
def test_shared_retained_domain_vectors(file: str, vector: dict[str, Any]) -> None:
    from trusted_router import async_settle_shadow_v2_contract as contract
    shape = {"sample": "sample", "counter": "counter", "terminal_compared_full": "wire"}[file]
    _, _, old, new = next(row for row in retained_parity_shapes() if row[0] == shape)
    obj = vector["object"]
    old(obj)
    new(obj)
    schema = contract.COMPARED_FULL if shape == "wire" else e.SAMPLE if shape == "sample" else e.COUNTER
    assert schema.parse(schema.serialize(obj)) == obj
    if shape == "wire":
        assert contract.parse_terminal(vector["canonical"].encode()) == obj


def test_retained_strings_do_not_weaken_locator_aliases() -> None:
    from trusted_router import async_settle_shadow_v2_contract as contract
    obj = fixture("sample")[0]["object"]
    raw = canonical(obj)
    # A nested v2 locator still has no alternate spelling in a retained wrapper.
    alias = raw.replace(b'"issuer"', b'"\\u0069ssuer"')
    with pytest.raises(ValueError, match="json_encoding"):
        e.SAMPLE.parse(alias)
    wire = fixture("terminal_compared_full")[0]["object"]
    for raw in (b'{"v":2,"v":2}', b'{"observed":{"authority":"\\ud800"}}'):
        with pytest.raises(ValueError):
            contract.parse_terminal(raw)
    wire["terminal"]["invocation_nonce"] = "native/slash"
    with pytest.raises(ValueError):
        contract.COMPARED_FULL(wire)




# Construction audit: no acceptance-difference matcher exists in this harness.

VENDORED = {
    "src/trusted_router/async_settle_ticket.py": ("NativeIdentity", "TicketClaims"),
    "src/trusted_router/storage_models.py": ("generation_id_for_authorization",),
    "src/trusted_router/async_settle_shadow_binding.py": ("LIFETIME", "FIXTURE_SHA256", "ShadowClaims", "binding_valid_at", "validate_claims"),
    "src/trusted_router/async_settle_shadow_evidence.py": ("ADAPTERS", "ROUTES", "DIMENSIONS", "COUNT_FIELDS", "PHASE_FIELDS", "REASONS", "CLASSIFICATIONS", "SAMPLE_FIELDS", "OBJECT_FIELDS", "validate_sample"),
    "scripts/async_settle/shadow_report.py": ("COUNTER_FIELDS", "_uint", "validate_counter", "_validate_counter"),
}


def ast_members(path: Path) -> dict[str, Any]:
    result = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            result[node.name] = node
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            result.setdefault(node.targets[0].id, node)
    return result


@pytest.mark.parametrize("source,member", [(src, member) for src, members in VENDORED.items() for member in members])
def test_verbatim_retained_pins(source: str, member: str) -> None:
    root = Path(__file__).parents[1]
    original = ast_members(root / source)[member]
    frozen = ast_members(root / "src/trusted_router/async_settle_shadow_v2_retained.py")[member]
    assert ast.dump(frozen, include_attributes=False) == ast.dump(original, include_attributes=False)
    if isinstance(original, ast.Assign):
        module = source.removeprefix("src/").removesuffix(".py").replace("/", ".")
        actual = getattr(importlib.import_module(module), member)
        if member == "NativeIdentity":
            from pydantic import TypeAdapter
            assert TypeAdapter(getattr(retained, member)).json_schema() == TypeAdapter(actual).json_schema()
        else:
            assert getattr(retained, member) == actual


def construction_shapes() -> list[tuple[str, str, dict[str, Any]]]:
    seeds = {name: base for name, base, _, _ in retained_parity_shapes()}
    result = []
    for phase in ("settle", "refund"):
        sample = copy.deepcopy(seeds["sample"])
        sample["booking"].update(attempted_kind=phase, outcome="settled" if phase == "settle" else "refunded")
        result.append(("sample-" + phase, "sample", sample))
        for mode in ("full", "hash-only", "failure"):
            wire = copy.deepcopy(seeds["wire"])
            wire["terminal"]["terminal_kind"] = phase
            if mode == "hash-only":
                del wire["billing_snapshot"]
            if mode == "failure":
                wire.update(terminal=None, payload_hash=None, go_error="malformed_usage")
            result.append(("wire-" + phase + "-" + mode, "wire", wire))
    result.append(("counter", "counter", seeds["counter"]))
    from trusted_router.detached_jws import b64decode
    binding = json.loads(b64decode(fixture("purpose_rejections")[0]["binding"].split(".")[1]))
    result.append(("binding", "binding", binding))
    result.extend((name, name, value) for name, value in (
        ("observed", billing.Eligibility().model_dump(mode="json")),
        ("raw-usage", seeds["wire"]["raw_usage"]),
        ("normal-usage", seeds["wire"]["terminal"]["usage"])))
    return result


CONSTRUCTION = {name: (shape, base) for name, shape, base in construction_shapes()}


def schema_paths(schema: dict[str, Any], path: tuple[Any, ...] = (), defs: Any = None) -> set[tuple[Any, ...]]:
    """Independent DTO inventory, never reading the seed or parity_paths."""
    defs = schema.get("$defs", {}) if defs is None else defs
    if "$ref" in schema:
        return schema_paths(defs[schema["$ref"].split("/")[-1]], path, defs)
    result = {path}
    for branch in schema.get("anyOf", []):
        result |= schema_paths(branch, path, defs)
    for key, value in schema.get("properties", {}).items():
        result |= schema_paths(value, (*path, key), defs)
    if "items" in schema:
        result |= schema_paths(schema["items"], (*path, 0), defs)
    return result


def independent_paths(name: str) -> set[tuple[Any, ...]]:
    # Original source constants/DTOs own this inventory, not v2 Schema.fields.
    from scripts.async_settle.shadow_report import COUNTER_FIELDS
    from trusted_router import async_settle_shadow_binding as old_binding
    from trusted_router import async_settle_shadow_evidence as old
    from trusted_router import async_settle_shadow_wire as old_wire
    shape, _ = CONSTRUCTION[name]
    if shape == "sample":
        paths = {(), *((k,) for k in old.SAMPLE_FIELDS)}
        for key, fields in old.OBJECT_FIELDS.items():
            paths.update((key, field) for field in fields.split())
        for key in ("python_usage", "go_usage", "legacy_usage", "raw_usage"):
            model = billing.RawUsage if key == "raw_usage" else billing.NormalizedUsage
            paths |= schema_paths(model.model_json_schema(), (key,))
        paths.add(("reason_codes", 0))
        return paths
    if shape == "counter":
        paths = {(), *((k,) for k in COUNTER_FIELDS)}
        for key, fields in (("counts", {"adapter", "route_type", "streamed", *old.COUNT_FIELDS}),
                            ("terminal_counts", {"adapter", "route_type", "streamed", "phase", *old.PHASE_FIELDS}),
                            *((k, {"phase", "adapter", "route_type", "streamed", "reason", "count"}) for k in ("exclusions", "rejections", "drops"))):
            paths.add((key, 0))
            paths.update((key, 0, field) for field in fields)
        paths.update((key, 0) for key in ("authorize_shadow_hist", "evidence_write_hist"))
        # Extract the retained validator's observer_fields assignment, not a v2 declaration.
        node = ast_members(Path(__file__).parents[1] / "scripts/async_settle/shadow_report.py")["_validate_counter"]
        observer = next(n.value for n in ast.walk(node) if isinstance(n, ast.Assign)
                        and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "observer_fields")
        fields = eval(compile(ast.Expression(observer), "<observer-schema>", "eval"), {"set": set})  # noqa: S307 - repository AST only
        paths.update(("admission_observer", k) for k in fields)
        return paths
    if shape == "wire":
        fields = old_wire.KEYS - ({"billing_snapshot"} if "hash-only" in name else set())
        paths = {(), *((k,) for k in fields)}
        for key, model in (("billing_snapshot", billing.BillingSnapshot), ("terminal", billing.TerminalEnvelope),
                           ("observed", billing.Eligibility), ("raw_usage", billing.RawUsage)):
            if key in fields and not (key == "terminal" and "failure" in name):
                paths |= schema_paths(model.model_json_schema(), (key,))
        return paths
    model = {"binding": old_binding.ShadowClaims, "observed": billing.Eligibility,
             "raw-usage": billing.RawUsage, "normal-usage": billing.NormalizedUsage}[shape]
    return schema_paths(model.model_json_schema())


def exercised_paths(name: str) -> set[tuple[Any, ...]]:
    shape, base = CONSTRUCTION[name]
    dropped = projection.SAMPLE_ADDITIONS if shape == "sample" else projection.COUNTER_ADDITIONS if shape == "counter" else {"variant", "policy_version"} if shape == "wire" else set()
    return {path for path in parity_paths(base) if not path or path[0] not in dropped}


def enum_inventory() -> set[Any]:
    values: set[Any] = set(retained.REASONS) | set(deltas.NEW_COUNTER_REASONS)
    root = Path(__file__).parents[1]
    for path in (*VENDORED, "src/trusted_router/async_settle_shadow_wire.py",
                 "src/trusted_router/async_settle_shadow_v2_deltas.py",
                 "src/trusted_router/async_settle_shadow_v2_evidence.py",
                 "src/trusted_router/async_settle_shadow_v2_certificate.py",
                 "src/trusted_router/async_settle_shadow_v2_contract.py"):
        for node in ast.walk(ast.parse((root / path).read_text())):
            elements = node.elts if isinstance(node, (ast.Set, ast.Tuple, ast.List)) else node.args if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "literal" else ()
            values.update(item.value for item in elements if isinstance(item, ast.Constant)
                          and isinstance(item.value, (str, bool, int)))
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            values.update(value.get("enum", []))
            if "const" in value:
                values.add(value["const"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for model in (billing.BillingSnapshot, billing.TerminalEnvelope, billing.RawUsage,
                  billing.NormalizedUsage, billing.Eligibility, retained.ShadowClaims):
        visit(model.model_json_schema())
    return values


ENUM_VALUES = sorted(enum_inventory(), key=lambda v: (type(v).__name__, str(v)))


def required_mutation_counts(value: Any) -> dict[str, int]:
    counts = {"type": 8, "integer-boundary": 6, "string-length": 12,
              "printable-ascii": 95, "control": 33, "unicode-escape": 9,
              "enum-vocabulary": len(ENUM_VALUES), "native-pair": 3}
    if isinstance(value, str):
        counts["domain-string-length"] = 12
    if isinstance(value, dict):
        counts.update({"extra-key": 1, "missing-key": len(value)})
    if isinstance(value, list):
        counts.update({"duplicate-identity": 1, "reordered-array": 1})
        if value and isinstance(value[0], dict):
            counts["duplicate-identity-different-count"] = 1
    return counts


REQUIRED_CLASSES = {"type", "integer-boundary", "string-length", "printable-ascii", "control", "unicode-escape", "enum-vocabulary", "native-pair"}


@pytest.mark.parametrize("name", list(CONSTRUCTION))
def test_retained_coverage_inventory(name: str) -> None:
    expected = independent_paths(name)
    assert exercised_paths(name) == expected
    _, base = CONSTRUCTION[name]
    for path in expected:
        value = parity_get(base, path)
        classes = {kind for kind, _ in parity_mutations(value)}
        assert REQUIRED_CLASSES <= classes, (name, path, classes)
        assert Counter(kind for kind, _ in parity_mutations(value)) == required_mutation_counts(value)
        if isinstance(value, dict):
            assert {"extra-key", "missing-key"} <= classes
        if isinstance(value, list):
            assert value, ("unpopulated homogeneous array", name, path)
            assert {"duplicate-identity", "reordered-array"} <= classes
    # Assert the actual scheduled work as well as the walk; losing a group fails.
    assert {path for case, path in PARITY_CASES if case == name} == expected


def reference_projection(shape: str, value: Any) -> Any:
    """Test-owned wrapper removal; the old side calls no v2 validation helper."""
    if shape == "sample":
        additions = set("proof_kind original_terminal_receipt health_source health_slot prediction_basis".split())
    elif shape == "counter":
        additions = set("""day boot authorize_attempts authorize_fresh authorize_replay
settle_attempts refund_attempts A B C proof_compared proof_certificate proof_observed
requested_eligible requested_excluded requested_unknown eligible_demand event_seq durable_event_hwm
checkpoint_seq receipt_count receipt_chain_sha256 loss_count reset_count diagnostics buckets publisher
checkpoint_attempts checkpoint_failures checkpoint_recoveries unknown_commits unreconciled_commits irrecoverable_losses""".split())
    elif shape == "wire":
        additions = {"variant", "policy_version"}
    else:
        return value
    old = {**{k: v for k, v in value.items() if k not in additions}, "v": 1}
    if shape in ("sample", "counter"):
        old["policy_version"] = "shadow-v1"
    if shape == "counter":
        for field in ("exclusions", "rejections", "drops"):
            if isinstance(old.get(field), list):
                old[field] = [r for r in old[field] if not isinstance(r, dict)
                              or not isinstance(r.get("reason"), str) or r["reason"] not in APPROVED_NEW_REASONS]
    return old


def v1_delegate(shape: str, value: Any) -> None:
    from scripts.async_settle.shadow_report import validate_counter
    from trusted_router import async_settle_shadow_binding as old_binding
    from trusted_router import async_settle_shadow_evidence as old
    from trusted_router import async_settle_shadow_wire as old_wire
    from trusted_router.detached_jws import b64encode
    if shape == "sample":
        old.validate_sample(reference_projection("sample", value), value["authorization_day"] + "/" + value["authorization_id"])
    elif shape == "counter":
        validate_counter(value["day"] + "/" + value["instance"], reference_projection("counter", value))
    elif shape == "wire":
        old_wire.parse_header((b64encode(canonical(reference_projection("wire", value))),))
    elif shape == "binding":
        old_binding.validate_claims(value, CONSTRUCTION["binding"][1]["iat"])
    else:
        model = {"observed": billing.Eligibility, "raw-usage": billing.RawUsage, "normal-usage": billing.NormalizedUsage}[shape]
        model.model_validate(value)


def v2_acceptor(shape: str, value: Any) -> None:
    if shape in ("sample", "counter", "wire"):
        {"sample": e.SAMPLE, "counter": e.COUNTER, "wire": lambda v: (contract.COMPARED_FULL if isinstance(v, dict) and "billing_snapshot" in v else contract.COMPARED_HASH_ONLY)(v)}[shape](value)
    elif shape == "binding":
        projection.validate_binding_claims(value, CONSTRUCTION["binding"][1]["iat"])
    else:
        {"observed": contract.observed, "raw-usage": contract.wire_raw_usage, "normal-usage": contract.NORMAL_USAGE}[shape](value)


def constructed_acceptor(shape: str, value: Any) -> None:
    v1_delegate(shape, value)
    if shape in ("sample", "counter"):
        schema = e.SAMPLE if shape == "sample" else e.COUNTER
        # Only v2 parts; retained values were already checked by the LIVE v1.
        s.require(type(value) is dict and set(value) == set(schema.fields))
        for key, check in (e.SAMPLE_NEW_FIELDS if shape == "sample" else e.COUNTER_NEW_FIELDS).items():
            check(value[key])
        s.require(type(value["v"]) is int and value["v"] == 2 and value["policy_version"] == "shadow-v2")
        (e._sample if shape == "sample" else e._counter)(value)
    elif shape == "wire":
        s.require(value["variant"] == "compared" and type(value["v"]) is int and value["v"] == 2 and value["policy_version"] == "shadow-v2")
    for delta in deltas.DELTAS:
        if delta.shape in (shape, "retained-object" if shape in ("sample", "counter", "wire") else shape) and delta.direction != "v2-only-broaden":
            assert not delta.predicate(value)


def accepts(check: Any, value: Any) -> bool:
    try:
        check(value)
        return True
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, OSError, AssertionError):
        return False


PARITY_CASES = [(name, path) for name in CONSTRUCTION for path in sorted(exercised_paths(name), key=str)]


def assert_retained_round_trip(shape: str, value: Any) -> None:
    schemas = {"sample": e.SAMPLE, "counter": e.COUNTER, "normal-usage": contract.NORMAL_USAGE}
    if shape == "wire":
        schema = contract.COMPARED_FULL if "billing_snapshot" in value else contract.COMPARED_HASH_ONLY
    else:
        schema = schemas.get(shape)
    if schema is not None:
        assert schema.parse(schema.serialize(value)) == value
    else:
        # Binding/observed/raw-usage expose validators, not Schema codecs.
        decoded = json.loads(canonical(value))
        v2_acceptor(shape, decoded)
        assert decoded == value


@pytest.mark.parametrize("name,path", PARITY_CASES, ids=[name + ":" + ".".join(map(str, path)) for name, path in PARITY_CASES])
def test_systematic_retained_parity(name: str, path: tuple[Any, ...]) -> None:
    shape, base = CONSTRUCTION[name]
    executed: Counter[str] = Counter()
    for mutation, item in parity_mutations(parity_get(base, path)):
        executed[mutation] += 1
        value = copy.deepcopy(base)
        if path:
            parity_get(value, path[:-1])[path[-1]] = item
        else:
            value = item
        actual = accepts(lambda v: v2_acceptor(shape, v), value)
        expected = accepts(lambda v: constructed_acceptor(shape, v), value)
        assert actual == expected, (name, path, mutation, item, actual, expected)
        if shape == "wire":
            schema = contract.COMPARED_FULL if isinstance(value, dict) and "billing_snapshot" in value else contract.COMPARED_HASH_ONLY
            assert accepts(schema.serialize, value) is expected
        if actual:
            assert_retained_round_trip(shape, value)
    assert executed == required_mutation_counts(parity_get(base, path))


@pytest.mark.parametrize("shape", ["sample", "counter", "wire", "binding", "observed", "raw-usage", "normal-usage"])
def test_projection_preserves_every_retained_value(shape: str, monkeypatch: pytest.MonkeyPatch) -> None:
    name = next(name for name, (kind, _) in CONSTRUCTION.items() if kind == shape)
    base = CONSTRUCTION[name][1]
    project = {"sample": projection.sample_projection, "counter": projection.counter_projection,
               "wire": projection.wire_projection, "binding": projection.binding_projection,
               "observed": projection.observed_projection, "raw-usage": projection.usage_projection,
               "normal-usage": projection.usage_projection}[shape]
    dropped = projection.SAMPLE_ADDITIONS if shape == "sample" else projection.COUNTER_ADDITIONS if shape == "counter" else {"variant", "policy_version"} if shape == "wire" else set()
    for path in independent_paths(name) - {(), ("v",), ("policy_version",)}:
        value = copy.deepcopy(base)
        parity_get(value, path[:-1])[path[-1]] = {"sentinel": [None, "untouched", 1]}
        expected = {k: v for k, v in value.items() if k not in dropped}
        if shape in ("sample", "counter", "wire"):
            expected["v"] = 1
        if shape in ("sample", "counter"):
            expected["policy_version"] = "shadow-v1"
        assert project(value) == expected, (shape, path)
        assert parity_get(project(value), path) is parity_get(value, path) or path[0] in ("exclusions", "rejections", "drops")
    assert project({**base, "unknown_retained_field": 17})["unknown_retained_field"] == 17
    # Spy on the actual delegate, not just the projection helper.
    if shape in ("sample", "counter"):
        captured = []
        def spy(*args: Any, **kwargs: Any) -> None:
            captured.append(args[0] if shape == "sample" else args[1])
            raise ValueError("spy reached")
        monkeypatch.setattr(retained, "validate_" + shape, spy)
        for path in independent_paths(name) - {(), ("v",), ("policy_version",)}:
            value = copy.deepcopy(base)
            parity_get(value, path[:-1])[path[-1]] = "mutation-arrives-unchanged"
            # Identity arguments are evaluated before the delegate; use strings.
            with pytest.raises(ValueError):
                (e.SAMPLE if shape == "sample" else e.COUNTER)(value)
            assert parity_get(captured[-1], path) == "mutation-arrives-unchanged"


def clean_sample(classification: str = "exact") -> dict[str, Any]:
    value = copy.deepcopy(fixture("sample")[0]["object"])
    value["classification"] = classification
    if classification == "explained-by-catalog-change":
        value.update(booked_micro=3, rebuilt_micro=3, rebuilt_snapshot_hash="f" * 64,
                     booked_minus_frozen=None, booked_minus_rebuilt=None, rebuilt_minus_frozen=None)
        value["provenance"]["rebuild_matches_booking_view"] = True
    return value


@pytest.mark.parametrize("classification", ["exact", "explained-by-catalog-change"])
@pytest.mark.parametrize("damage", ["excluded", "unknown", "prompt-sum", "reasoning", "null"])
def test_clean_design_assertions(classification: str, damage: str) -> None:
    value = clean_sample(classification)
    if damage in ("excluded", "unknown"):
        value["eligibility"]["observed"] = False if damage == "excluded" else None
        value["admission"]["prediction"] = "no"
    else:
        for field in ("python_usage", "go_usage", "legacy_usage"):
            if damage == "null":
                value[field] = None
            elif damage == "prompt-sum":
                value[field]["total_prompt_tokens"] += 1
            else:
                value[field]["reasoning_tokens"] = value[field]["output_tokens"] + 1
    v1_delegate("sample", value)
    for run in (e.SAMPLE, e.SAMPLE.serialize, lambda v: e.SAMPLE.parse(canonical(v))):
        with pytest.raises(ValueError):
            run(value)
    value["classification"] = "unevaluable"
    e.SAMPLE(value)
    value["deployment"]["instance"] = ""
    with pytest.raises(ValueError):
        e.SAMPLE(value)


# Expectations are hand-written, independent of the production predicates.
DELTA_NAMES = {
    "clean-requires-eligible", "clean-requires-normalized-usage", "clean-winner-conflict",
    "excluded-admission", "strict-dimension", "strict-diagnostic-array", "v2-counter-reasons",
    "wire-wrapper-size", "sample-wrapper-size", "counter-wrapper-size",
    "certificate-issuer-bound", "certificate-origin", "certificate-authority",
    "transport-spelling", "scalar-string",
}


def test_delta_inventory() -> None:
    assert {d.name for d in deltas.DELTAS} == DELTA_NAMES
    assert len(deltas.DELTAS) == len(DELTA_NAMES)
    assert all(d.design.startswith("§") and callable(d.predicate) for d in deltas.DELTAS)


# Independent route inventory, not expanded from production Delta.shape strings.
DELTA_ROUTES = [
    ("clean-requires-eligible", "sample", "sample-settle"),
    ("clean-requires-normalized-usage", "sample", "sample-settle"),
    ("clean-winner-conflict", "sample", "sample-settle"),
    ("excluded-admission", "sample", "sample-settle"),
    ("strict-dimension", "counter", "counter"),
    ("strict-diagnostic-array", "counter", "counter"),
    ("v2-counter-reasons", "counter-row", "counter"),
    ("wire-wrapper-size", "wire", "wire-settle-full"),
    ("sample-wrapper-size", "sample", "sample-settle"),
    ("counter-wrapper-size", "counter", "counter"),
    ("certificate-issuer-bound", "certificate", "certificate"),
    ("certificate-origin", "certificate", "certificate"),
    ("certificate-authority", "certificate", "certificate"),
]


@pytest.mark.parametrize("name,shape,seed", DELTA_ROUTES, ids=[n + ":" + seed for n, _, seed in DELTA_ROUTES])
def test_delta_enforcing_routes(name: str, shape: str, seed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert {n for n, _, _ in DELTA_ROUTES} | {"transport-spelling", "scalar-string"} == DELTA_NAMES
    delta = next(d for d in deltas.DELTAS if d.name == name)
    assert delta.shape == shape
    if seed == "certificate":
        value = proof()["claims"]
        value.update(settle_origin="legacy", billing_authority="federated")
        schema = cert.CLAIMS
    else:
        value = copy.deepcopy(CONSTRUCTION[seed][1])
        schema = {"sample-settle": e.SAMPLE, "counter": e.COUNTER, "wire-settle-full": contract.COMPARED_FULL}[seed]
        if name == "v2-counter-reasons":
            value["drops"][0]["reason"] = "candidate_receipt_size"
    schema(value)
    assert schema.parse(schema.serialize(value)) == value
    calls = []
    def reject(witness: Any) -> bool:
        calls.append(witness)
        # Force a rejection at this particular table entry, including denial
        # of the explicitly approved broadening witnessed above. This detects
        # missing calls even where another schema redundantly rejects defects.
        return name not in {"v2-counter-reasons", "certificate-origin", "certificate-authority"}
    monkeypatch.setattr(deltas, "DELTAS", tuple(replace(d, predicate=reject) if d.name == name else d for d in deltas.DELTAS))
    for check, witness in ((schema, value), (schema.parse, canonical(value)), (schema.serialize, value)):
        calls.clear()
        with pytest.raises(ValueError):
            check(witness)
        assert calls, (name, seed, check)


@pytest.mark.parametrize("name", sorted(DELTA_NAMES))
def test_independent_delta_oracle(name: str) -> None:
    delta = next(d for d in deltas.DELTAS if d.name == name)
    if name == "transport-spelling":
        assert delta.predicate(b'{"x":"\\u0061"}')
        assert not delta.predicate(b'{"x":"a"}')
        return
    if name == "scalar-string":
        assert delta.predicate({"region": "\ud800"})
        assert not delta.predicate({"region": "é\x00\n"})
        return
    if name.endswith("wrapper-size"):
        limit = 65536 if name.startswith("counter") else 8192
        for offset, expected in ((-1, False), (0, False), (1, True)):
            value = {"pad": ""}
            value["pad"] = "x" * (limit + offset - len(canonical(value)))
            assert len(canonical(value)) == limit + offset
            assert delta.predicate(value) is expected
        # Size is also charged on encoded transport, independently of retained
        # validity. Valid near-boundary padding cannot excuse a mixed defect.
        shape = name.split("-", 1)[0]
        seed = {"sample": "sample-settle", "counter": "counter", "wire": "wire-settle-full"}[shape]
        base = copy.deepcopy(CONSTRUCTION[seed][1])
        parser = {"sample": e.SAMPLE.parse, "counter": e.COUNTER.parse, "wire": contract.parse_terminal}[shape]
        for offset in (-1, 0, 1):
            raw = canonical(base)
            padded = raw + b" " * (limit + offset - len(raw))
            assert accepts(parser, padded) is (offset <= 0)
            damaged = copy.deepcopy(base)
            if shape == "sample":
                damaged["deployment"]["instance"] = ""
            elif shape == "counter":
                damaged["region"] = ""
            else:
                damaged["terminal"]["workspace_id"] = "bad space"
            raw = canonical(damaged)
            assert not accepts(parser, raw + b" " * (limit + offset - len(raw)))
        return
    if name.startswith("certificate"):
        value = proof()["claims"]
        field, witness, near = {
            "certificate-issuer-bound": ("iss", "x" * 129, "x" * 128),
            "certificate-origin": ("settle_origin", "legacy", "typed"),
            "certificate-authority": ("billing_authority", "federated", "local"),
        }[name]
        value[field] = witness
        assert delta.predicate(value) is True
        assert accepts(lambda v: cert.validate_claims(v, v["iat"]), value) is (name != "certificate-issuer-bound")
        value[field] = near
        assert delta.predicate(value) is False
        cert.validate_claims(value, value["iat"])
        value["workspace_id"] = "bad space"
        assert delta.predicate(value) is False  # A different failure cannot be called issuer/origin/authority.
        with pytest.raises(ValueError):
            cert.validate_claims(value, value["iat"])
        value[field] = witness
        with pytest.raises(ValueError):
            cert.validate_claims(value, value["iat"])
        return
    if name == "v2-counter-reasons":
        for reason, expected in (("candidate_receipt_size", True), ("usage_missing", False), ("candidate_receipt_sizeX", False)):
            row = dict(phase="worker", adapter="unknown", route_type="unknown", streamed=None, reason=reason, count=1)
            assert delta.predicate(row) is expected
            value = copy.deepcopy(CONSTRUCTION["counter"][1])
            value["drops"] = [row]
            assert accepts(e.COUNTER, value) is (reason != "candidate_receipt_sizeX")
            row["count"] = -1
            assert not accepts(e.COUNTER, value)
        return
    if name.startswith("strict"):
        value = copy.deepcopy(CONSTRUCTION["counter"][1])
        if name == "strict-dimension":
            value["counts"][0]["streamed"] = 0
        else:
            value["drops"] = {}
        assert delta.predicate(value) is True
        v1_delegate("counter", value)
        assert not accepts(e.COUNTER, value)
        if name == "strict-dimension":
            value["counts"][0]["streamed"] = False
        else:
            value["drops"] = []
        assert delta.predicate(value) is False
        e.COUNTER(value)
        value["region"] = ""
        assert delta.predicate(value) is False
        assert not accepts(e.COUNTER, value)
        return
    value = clean_sample()
    if name == "clean-requires-eligible":
        value["eligibility"]["observed"] = None
    elif name == "clean-requires-normalized-usage":
        for key in ("python_usage", "go_usage", "legacy_usage"):
            value[key]["total_prompt_tokens"] += 1
    elif name == "clean-winner-conflict":
        value.update(reason_codes=["winner_polarity"], booked_minus_frozen=None, booked_minus_rebuilt=None)
    else:
        value["classification"] = "unevaluable"
        value["eligibility"]["observed"] = False
    assert delta.predicate(value) is True
    v1_delegate("sample", value)
    assert not accepts(e.SAMPLE, value)
    if name == "excluded-admission":
        value["admission"]["prediction"] = "no"
    else:
        value["classification"] = "unevaluable"
    assert delta.predicate(value) is False
    e.SAMPLE(value)
    value["deployment"]["instance"] = ""
    assert delta.predicate(value) is False
    assert not accepts(e.SAMPLE, value)


@pytest.mark.parametrize("origin", ["typed", "legacy"])
@pytest.mark.parametrize("authority", ["local", "spend_lease", "regional_lease", "federated", "deferred_home"])
def test_certificate_common_constraints_in_every_branch(origin: str, authority: str) -> None:
    base = proof()["claims"]
    base.update(settle_origin=origin, billing_authority=authority)
    cert.validate_claims(base, base["iat"])
    for field in cert.COMMON_FIELDS:
        # Invalid independently of origin/authority and issuer: no safe substitutions.
        damaged = {**base, field: -1 if type(base[field]) is int else 0 if type(base[field]) is bool else "bad space"}
        assert not accepts(lambda v: cert.validate_claims(v, base["iat"]), damaged), (origin, authority, field)
    for field in ("settle_origin", "billing_authority"):
        assert not accepts(lambda v: cert.validate_claims(v, base["iat"]), {**base, field: "unsupported"})


# Policy shadow-v2 §4/§7: independent literal, never sourced from production.
APPROVED_NEW_REASONS = frozenset("""original_receipt_locator_gap
original_receipt_identity_gap original_receipt_digest_gap original_receipt_backing_gap
original_receipt_missing candidate_receipt_size receipt_locator_size exclusion_conflict
missing_signer missing_context transport_failure local_work_overflow evaluator_failed
ambiguous_fallback evidence_schema_gap evidence_identity_gap""".split())


@pytest.mark.parametrize("field", ["exclusions", "rejections", "drops"])
@pytest.mark.parametrize("reason", sorted(retained.REASONS | APPROVED_NEW_REASONS))
def test_all_counter_reason_members(field: str, reason: str, monkeypatch: pytest.MonkeyPatch) -> None:
    value = copy.deepcopy(CONSTRUCTION["counter"][1])
    value[field][0]["reason"] = reason
    seen = []
    delegate = retained.validate_counter
    def spy(key: str, body: Any) -> None:
        seen.append(copy.deepcopy(body))
        delegate(key, body)
    monkeypatch.setattr(retained, "validate_counter", spy)
    e.COUNTER(value)
    assert seen[0][field] == ([] if reason in APPROVED_NEW_REASONS else value[field])
    old = projection.counter_projection(value)
    assert (len(old[field]) == 0) is (reason in APPROVED_NEW_REASONS)
    value[field][0]["count"] = -1
    assert not accepts(e.COUNTER, value)


@pytest.mark.parametrize("date,expected", [("2300-01-01", False), ("2300-01-02", True)])
def test_inherited_calendar_float_boundary(date: str, expected: bool) -> None:
    value = copy.deepcopy(CONSTRUCTION["sample-settle"][1])
    value.update(authorize_at_us=10413878399999999, authorization_day=date,
                 observed_at_us=10413878400000001, booking_observed_at_us=10413878400000002)
    value["original_terminal_receipt"]["day"] = "2300-01-02"
    assert accepts(lambda v: v1_delegate("sample", v), value) is expected
    assert accepts(e.SAMPLE, value) is expected


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_wire_aggregate_byte_boundary(offset: int) -> None:
    value = copy.deepcopy(CONSTRUCTION["wire-settle-full"][1])
    value["observed"]["authority"] = ""
    value["observed"]["authority"] = "x" * (8192 + offset - len(canonical(value)))
    assert len(canonical(value)) == 8192 + offset
    v1_delegate("wire", value)
    assert accepts(lambda v: contract.parse_terminal(canonical(v)), value) is (offset <= 0)
    assert next(d for d in deltas.DELTAS if d.name == "wire-wrapper-size").predicate(value) is (offset > 0)
    value["terminal"]["workspace_id"] = "bad space"
    assert not accepts(lambda v: contract.parse_terminal(canonical(v)), value)


@pytest.mark.parametrize("name,schema,base", CASES, ids=[name for name, _, _ in CASES])
def test_all_sized_objects_encoded_boundary(name: str, schema: s.Schema, base: Any) -> None:
    raw = canonical(base)
    for offset in (-1, 0, 1):
        length = schema.limit + offset
        if length < len(raw):
            continue  # A base exactly at its limit has no whitespace to remove.
        padded = raw + b" " * (length - len(raw))
        if offset <= 0:
            assert schema.parse(padded) == base, name
        else:
            with pytest.raises(ValueError):
                schema.parse(padded)


@pytest.mark.parametrize("shape", ["wire", "binding", "observed", "raw-usage", "normal-usage"])
def test_dto_projection_reaches_delegate_unchanged(shape: str, monkeypatch: pytest.MonkeyPatch) -> None:
    name = next(name for name, (kind, _) in CONSTRUCTION.items() if kind == shape)
    base = CONSTRUCTION[name][1]
    captured: list[Any] = []
    def spy(value: Any, *args: Any, **kwargs: Any) -> Any:
        if shape == "wire":
            from trusted_router.detached_jws import b64decode
            value = json.loads(b64decode(value[0]))
        captured.append(value)
        raise ValueError("captured")
    if shape == "wire":
        monkeypatch.setattr(contract, "retained_header", spy)
    elif shape == "binding":
        monkeypatch.setattr(retained, "validate_claims", spy)
    else:
        model = {"observed": billing.Eligibility, "raw-usage": billing.RawUsage, "normal-usage": billing.NormalizedUsage}[shape]
        monkeypatch.setattr(model, "model_validate", spy)
    for path in independent_paths(name) - {(), ("v",), ("policy_version",)}:
        value = copy.deepcopy(base)
        parity_get(value, path[:-1])[path[-1]] = "unchanged-probe"
        captured.clear()
        with pytest.raises(ValueError):
            v2_acceptor(shape, value)
        assert len(captured) == 1, (shape, path)
        assert parity_get(captured[0], path) == "unchanged-probe", (shape, path)


def test_counter_validation_stays_pure_at_runtime() -> None:
    script = '''
import json, sys
from pathlib import Path
from trusted_router.async_settle_shadow_v2_evidence import COUNTER
counter = json.loads(Path("tests/fixtures/async_settlement/shadow_v2/counter.json").read_bytes())["variants"][0]["object"]
COUNTER(counter)
assert "trusted_router.async_settle_shadow_evidence" not in sys.modules
assert not any(n.startswith(("trusted_router.services", "trusted_router.storage", "trusted_router.routes")) or n == "trusted_router.main" for n in sys.modules)
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parents[1],  # noqa: S603 - fixed local probe
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr


def test_independent_policy_vocabularies() -> None:
    from trusted_router.async_settle_shadow_evidence import REASONS
    assert deltas.NEW_COUNTER_REASONS == APPROVED_NEW_REASONS
    assert not APPROVED_NEW_REASONS & REASONS
    assert not deltas.NEW_COUNTER_REASONS & retained.REASONS
    for value in ("typed", "legacy", "local", "spend_lease", "regional_lease", "federated", "deferred_home", "unsupported", ""):
        assert deltas.broadening("certificate-origin", {"settle_origin": value}) is (value == "legacy")
        assert deltas.broadening("certificate-authority", {"billing_authority": value}) is (value in {"spend_lease", "regional_lease", "federated", "deferred_home"})
    for value in ("exact", "explained-by-catalog-change", "hash", "identity", "normalization", "evaluator_disagreement", "requires_review", "unevaluable", "unsupported", ""):
        assert deltas.clean({"classification": value}) is (value in {"exact", "explained-by-catalog-change"})


@pytest.mark.parametrize("reason", sorted(APPROVED_NEW_REASONS))
@pytest.mark.parametrize("field", ["exclusions", "rejections", "drops"])
def test_new_reason_closed_rows(reason: str, field: str) -> None:
    base = copy.deepcopy(CONSTRUCTION["counter"][1])
    base[field][0]["reason"] = reason
    assert projection.counter_projection(base)[field] == []
    for key, bad in (("extra", 1), ("count", True), ("count", -1), ("phase", "bogus"),
                     ("adapter", "bogus"), ("route_type", "bogus"), ("streamed", 0)):
        value = copy.deepcopy(base)
        value[field][0][key] = bad
        assert not accepts(e.COUNTER, value), (reason, field, key)
    for key in base[field][0]:
        value = copy.deepcopy(base)
        del value[field][0][key]
        assert not accepts(e.COUNTER, value), (reason, field, key)


def v1_raw_wire(raw: bytes) -> None:
    """Independent reference: complete live legacy RAW decoder plus validator."""
    from trusted_router.async_settle_shadow_wire import parse_header
    from trusted_router.detached_jws import b64encode
    parse_header((b64encode(raw),))


def constructed_raw_wire(raw: bytes, legacy_raw: bytes) -> None:
    v1_raw_wire(legacy_raw)
    value = json.loads(raw)  # No v2 retained decoder on the expected side.
    s.require(value["variant"] == "compared" and type(value["v"]) is int
              and value["v"] == 2 and value["policy_version"] == "shadow-v2")
    for delta in deltas.DELTAS:
        if delta.shape == "retained-transport":
            assert not delta.predicate(raw)
        elif delta.shape == "wire":
            assert not delta.predicate(value)
    assert len(raw) <= 8192


def raw_token_variant(value: Any, target: tuple[Any, ...], key_token: bool,
                      mode: str, path: tuple[Any, ...] = ()) -> bytes:
    """Independent JSON emitter: preserve the selected spelling on BOTH sides."""
    def token(text: str, selected: bool) -> bytes:
        if not selected:
            return json.dumps(text, ensure_ascii=True).encode()
        if mode == "alias":
            units = text[0].encode("utf-16-be")
            escaped = "".join(f"\\u{int.from_bytes(units[i:i + 2]):04X}" for i in range(0, len(units), 2))
            return b'"' + escaped.encode() + json.dumps(text[1:])[1:].encode()
        if mode == "control":
            return b'"' + b'\x01' + b'"'
        if mode == "utf8":
            return json.dumps(text, ensure_ascii=False).encode()
        return json.dumps(text, ensure_ascii=True).encode()
    if isinstance(value, dict):
        return b"{" + b",".join(token(k, key_token and path + (k,) == target) + b":" +
            raw_token_variant(v, target, key_token, mode, path + (k,)) for k, v in value.items()) + b"}"
    if isinstance(value, list):
        return b"[" + b",".join(raw_token_variant(v, target, key_token, mode, path + (i,)) for i, v in enumerate(value)) + b"]"
    if isinstance(value, str):
        return token(value, not key_token and path == target)
    return json.dumps(value).encode()


RAW_TOKEN_CASES = []
for _name, (_shape, _base) in CONSTRUCTION.items():
    if _shape != "wire":
        continue
    for _path in sorted(independent_paths(_name), key=str):
        if not _path or _path[0] in ("v", "variant", "policy_version"):
            continue
        if isinstance(_path[-1], str):
            RAW_TOKEN_CASES.append((_name, _path, True))
        if isinstance(parity_get(_base, _path), str):
            RAW_TOKEN_CASES.append((_name, _path, False))


@pytest.mark.parametrize("name,path,key_token", RAW_TOKEN_CASES,
                         ids=[n + ":" + ".".join(map(str, p)) + (":key" if k else ":value") for n, p, k in RAW_TOKEN_CASES])
def test_raw_retained_token_pairs(name: str, path: tuple[Any, ...], key_token: bool) -> None:
    base = copy.deepcopy(CONSTRUCTION[name][1])
    for scalar in (None, "é", "😀", "\n", "\x00") if not key_token else (None,):
        value = copy.deepcopy(base)
        if scalar is not None:
            parity_get(value, path[:-1])[path[-1]] = scalar
        old = reference_projection("wire", value)
        for mode in ("canonical", "utf8", "alias", "control"):
            raw = raw_token_variant(value, path, key_token, mode)
            legacy_raw = raw_token_variant(old, path, key_token, mode)
            if mode != "control":
                assert json.loads(raw) == value
                assert json.loads(legacy_raw) == old
            expected = accepts(lambda pair: constructed_raw_wire(*pair), (raw, legacy_raw))
            assert accepts(contract.parse_terminal, raw) is expected, (path, scalar, mode)
            if scalar is None:
                assert accepts(v1_raw_wire, legacy_raw) is (mode != "control")
                assert expected is (mode in ("canonical", "utf8"))


@pytest.mark.parametrize("shape,path", [("counter", ("region",)), ("sample", ("deployment", "instance"))])
@pytest.mark.parametrize("key_token", [False, True])
@pytest.mark.parametrize("scalar", ["\ud800", "\udfff", "a\ud800b", "\ud83d\ude00", "\ud800\udc00", "x\ud800\udc00y",
                                    "é", "😀", "\U00010000", "x\U00010000y", "\uffff", "\x00", "\n", "\t", "\\", '"'])
def test_retained_object_scalar_edges(shape: str, path: tuple[str, ...], key_token: bool, scalar: str) -> None:
    value = copy.deepcopy(CONSTRUCTION["counter" if shape == "counter" else "sample-settle"][1])
    parent = parity_get(value, path[:-1])
    if key_token:
        # Retained maps have closed keys: scalar key substitutions reject even
        # when the string itself is valid. No arbitrary-key map is retained.
        parent[scalar] = parent.pop(path[-1])
    else:
        parent[path[-1]] = scalar
    old = accepts(lambda v: v1_delegate(shape, v), value)
    assert old is (not key_token and (shape == "counter" or scalar.isascii()))
    expected = old and not any(0xd800 <= ord(c) <= 0xdfff for c in scalar)
    assert accepts(lambda v: constructed_acceptor(shape, v), value) is expected
    assert accepts(lambda v: v2_acceptor(shape, v), value) is expected
    schema = e.COUNTER if shape == "counter" else e.SAMPLE
    assert accepts(schema.serialize, value) is expected
    if expected:
        assert_retained_round_trip(shape, value)
    # Canonical raw surrogate pairs decode to genuine scalars; the raw and
    # native domains intentionally differ. Compute this without v2 helpers.
    decoded = json.loads(canonical(value))
    raw_expected = not key_token and (shape == "counter" or scalar.isascii()) and not any(
        0xd800 <= ord(c) <= 0xdfff for c in parity_get(decoded, path))
    assert accepts(schema.parse, canonical(value)) is raw_expected
    if raw_expected:
        assert schema.parse(canonical(value)) == decoded
        assert_retained_round_trip(shape, decoded)


@pytest.mark.parametrize("scalar", ["\ud83d\ude00", "\ud800\udc00", "x\ud800\udc00y"])
def test_native_counter_surrogate_pair_respects_scalar_delta(scalar: str) -> None:
    # Round-5 review's three failing attacks, preserved as native objects.
    value = copy.deepcopy(CONSTRUCTION["counter"][1])
    value["region"] = scalar
    v1_delegate("counter", value)
    assert deltas.scalar_string(value)
    assert not accepts(lambda v: constructed_acceptor("counter", v), value)
    assert not accepts(e.COUNTER, value)
    assert not accepts(e.COUNTER.serialize, value)


def test_scalar_delta_precedes_serialization(monkeypatch: pytest.MonkeyPatch) -> None:
    value = copy.deepcopy(CONSTRUCTION["counter"][1])
    value["region"] = "\ud83d\ude00"
    def forbidden(value: Any) -> bytes:
        pytest.fail("original native object reached serialization before scalar rejection")
    monkeypatch.setattr(s, "canonical", forbidden)
    for check in (e.COUNTER, e.COUNTER.serialize):
        with pytest.raises(ValueError, match="scalar-string"):
            check(value)


def test_oracle_never_redecodes_retained_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    original = s.decode
    def guard(raw: bytes, limit: int, *, retained_strings: bool = False) -> Any:
        assert not retained_strings, "oracle called the v2 retained decoder"
        return original(raw, limit)
    monkeypatch.setattr(s, "decode", guard)
    for name in ("sample-settle", "counter", "wire-settle-full"):
        shape, value = CONSTRUCTION[name]
        constructed_acceptor(shape, value)
    value = CONSTRUCTION["wire-settle-full"][1]
    constructed_raw_wire(canonical(value), canonical(reference_projection("wire", value)))


OBJECT_TOKEN_CASES = []
for _name in ("sample-settle", "sample-refund", "counter"):
    _shape, _base = CONSTRUCTION[_name]
    _old = reference_projection(_shape, _base)
    for _path in sorted(independent_paths(_name), key=str):
        if not _path or _path[0] not in _old or _path[0] in ("v", "policy_version"):
            continue
        if isinstance(_path[-1], str):
            OBJECT_TOKEN_CASES.append((_name, _path, True))
        if isinstance(parity_get(_base, _path), str):
            OBJECT_TOKEN_CASES.append((_name, _path, False))


def constructed_raw_object(shape: str, raw: bytes) -> None:
    # Sample/counter parity is with standalone object validators, so the raw
    # spelling is decoded by stdlib JSON independently before that reference.
    value = json.loads(raw)
    constructed_acceptor(shape, value)
    for delta in deltas.DELTAS:
        if delta.shape == "retained-transport":
            assert not delta.predicate(raw)
    assert len(raw) <= (8192 if shape == "sample" else 65536)


@pytest.mark.parametrize("name,path,key_token", OBJECT_TOKEN_CASES,
                         ids=[n + ":" + ".".join(map(str, p)) + (":key" if k else ":value") for n, p, k in OBJECT_TOKEN_CASES])
def test_raw_stored_retained_token_pairs(name: str, path: tuple[Any, ...], key_token: bool) -> None:
    shape, base = CONSTRUCTION[name]
    parser = e.SAMPLE.parse if shape == "sample" else e.COUNTER.parse
    for scalar in (None, "é", "\n") if not key_token else (None,):
        value = copy.deepcopy(base)
        if scalar is not None:
            parity_get(value, path[:-1])[path[-1]] = scalar
        for mode in ("canonical", "utf8", "alias", "control"):
            raw = raw_token_variant(value, path, key_token, mode)
            if mode != "control":
                decoded = json.loads(raw)
                assert decoded == value
                assert accepts(lambda v: v1_delegate(shape, v), decoded) == accepts(lambda v: v1_delegate(shape, v), value)
            expected = accepts(lambda data: constructed_raw_object(shape, data), raw)
            assert accepts(parser, raw) is expected, (path, scalar, mode)
            if scalar is None:
                assert expected is (mode in ("canonical", "utf8"))


def discovered_schemas() -> dict[int, tuple[str, s.Schema]]:
    """Discover globals and nested Schema fields, including wrapped validators."""
    found: dict[int, tuple[str, s.Schema]] = {}
    seen: set[int] = set()

    def visit(label: str, value: Any) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, s.Schema):
            found[id(value)] = (label, value)
            for key, child in value.fields.items():
                visit(label + "." + key, child)
        elif callable(value) and getattr(value, "__module__", "").startswith("trusted_router.async_settle_shadow_v2_"):
            for index, cell in enumerate(getattr(value, "__closure__", ()) or ()):
                visit(label + f".closure{index}", cell.cell_contents)

    for module in (s, cert, contract, e):
        for name, value in vars(module).items():
            visit(module.__name__ + "." + name, value)
    return found


def transport_seeds() -> dict[int, list[tuple[str, str, dict[str, Any]]]]:
    seeds: dict[int, list[tuple[str, str, dict[str, Any]]]] = {}
    for name, (shape, value) in CONSTRUCTION.items():
        if shape not in ("sample", "counter", "wire"):
            continue
        schema = e.SAMPLE if shape == "sample" else e.COUNTER if shape == "counter" else (
            contract.COMPARED_FULL if "billing_snapshot" in value else contract.COMPARED_HASH_ONLY)
        seeds.setdefault(id(schema), []).append((name, shape, value))
    wire = CONSTRUCTION["wire-settle-full"][1]
    seeds[id(contract.AUTHORIZE_COMPARED)] = [("authorize-compared", "authorize", {
        "billing_shadow_policy": "shadow-v2", "billing_shadow_binding": wire["billing_shadow_binding"],
        "billing_snapshot": wire["billing_snapshot"],
    })]
    return seeds


def all_string_positions(value: Any, path: tuple[Any, ...] = ()) -> Any:
    # Every array element and nullable slot, not just a homogeneous representative.
    if isinstance(value, dict):
        for key, child in value.items():
            yield (*path, key), True
            yield from all_string_positions(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from all_string_positions(child, (*path, index))
    elif isinstance(value, str) or value is None:
        yield path, False


def replace_string(base: Any, path: tuple[Any, ...], key: bool, text: str) -> Any:
    value = copy.deepcopy(base)
    parent = parity_get(value, path[:-1])
    if key:
        parent[text] = parent.pop(path[-1])
    else:
        parent[path[-1]] = text
    return value


DISCOVERED_SCHEMAS = discovered_schemas()
TRANSPORT_SCHEMAS = [(label, schema) for label, schema in DISCOVERED_SCHEMAS.values() if schema.retained_strings]


def test_transport_schema_seed_inventory() -> None:
    enabled = {key for key, (_, schema) in discovered_schemas().items() if schema.retained_strings}
    disabled = {key for key, (_, schema) in discovered_schemas().items() if not schema.retained_strings}
    assert enabled.isdisjoint(disabled)
    assert enabled | disabled == set(discovered_schemas())
    assert enabled == set(transport_seeds()), "transport-enabled schema missing a seed"
    assert {name for rows in transport_seeds().values() for name, shape, _ in rows if shape == "wire"} == {
        f"wire-{phase}-{mode}" for phase in ("settle", "refund") for mode in ("full", "hash-only", "failure")}


@pytest.mark.parametrize("label,schema", TRANSPORT_SCHEMAS, ids=[label for label, _ in TRANSPORT_SCHEMAS])
def test_transport_schema_enumeration(label: str, schema: s.Schema) -> None:
    seeds = transport_seeds()
    assert id(schema) in seeds, (label, "transport-enabled schema missing a seed")
    for name, shape, base in seeds[id(schema)]:
        schema(base)
        assert schema.parse(schema.serialize(base)) == base
        for path, key in all_string_positions(base):
            for pair in ("\ud83d\ude00", "\ud800\udc00", "x\ud800\udc00y"):
                value = replace_string(base, path, key, pair)
                for check in (schema, schema.serialize):
                    with pytest.raises(ValueError, match="^scalar-string$"):
                        check(value)
            for scalar in ("😀", "\U00010000", "x\U00010000y"):
                value = replace_string(base, path, key, scalar)
                # Authorize's binding and snapshot strings are ASCII-only;
                # all object keys are closed. Other shapes use the live v1
                # delegate plus the independent new-field/delta oracle.
                expected = False if shape == "authorize" else accepts(
                    lambda v, shape=shape: constructed_acceptor(shape, v), value)
                assert accepts(schema, value) is expected, (name, path, key, scalar)
                assert accepts(schema.serialize, value) is expected
                raw = canonical(value)  # supplementary scalars use JSON pairs
                assert accepts(schema.parse, raw) is expected
                if expected:
                    assert schema.parse(raw) == value
                    assert schema.parse(schema.serialize(value)) == value
                    assert schema.serialize(schema.parse(raw)) == raw


TRANSPORT_ROUTES = [(label, schema, stage) for label, schema in TRANSPORT_SCHEMAS
    for stage in ("native", "serialize", "raw-key", "raw-value", "decode", "terminal", "header")
    if stage not in ("terminal", "header") or any(shape == "wire" for _, shape, _ in transport_seeds().get(id(schema), []))]


@pytest.mark.parametrize("label,schema,stage", TRANSPORT_ROUTES, ids=[label + ":" + stage for label, _, stage in TRANSPORT_ROUTES])
def test_transport_enforcing_routes(label: str, schema: s.Schema, stage: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.detached_jws import b64encode

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("next stage reached before original input was rejected")

    for _, _shape, base in transport_seeds()[id(schema)]:
        value = copy.deepcopy(base)
        schema(value)
        raw = canonical(value)
        with monkeypatch.context() as patch:
            calls = []
            if stage in ("native", "serialize"):
                def reject_native(witness: Any, calls: list[Any] = calls, value: Any = value) -> bool:
                    calls.append(witness)
                    assert witness is value, "native input was replaced before scalar check"
                    return True
                patch.setattr(deltas, "DELTAS", tuple(replace(d, predicate=reject_native)
                    if d.name == "scalar-string" else d for d in deltas.DELTAS))
                patch.setattr(s, "canonical", forbidden)
                patch.setattr(contract, "canonical", forbidden)
                with pytest.raises(ValueError, match="^scalar-string$"):
                    (schema if stage == "native" else schema.serialize)(value)
                assert len(calls) == 1 and calls[0] is value
            else:
                # Alias a key or value's first ASCII character; the decoded
                # object is valid and its canonical reserialization differs.
                if stage == "raw-value":
                    match = next(m for m in re.finditer(rb':"([A-Za-z])', raw))
                    pos = match.start(1)
                else:
                    pos = raw.index(b'"') + 1
                original = raw[:pos] + f"\\u{raw[pos]:04x}".encode() + raw[pos + 1:]
                assert json.loads(original) == value and original != raw
                def reject_original(witness: Any, calls: list[Any] = calls, original: bytes = original) -> bool:
                    calls.append(witness)
                    return witness == original
                patch.setattr(deltas, "DELTAS", tuple(replace(d, predicate=reject_original)
                    if d.name == "transport-spelling" else d for d in deltas.DELTAS))
                # Isolate raw enforcement from both decoding and object checks.
                patch.setattr(s, "bounded_json", forbidden)
                patch.setattr(s.Schema, "__call__", forbidden)
                if stage in ("terminal", "header"):
                    patch.setattr(s.Schema, "parse", forbidden)
                check = (contract.parse_terminal if stage == "terminal" else
                         (lambda r: contract.parse_header((b64encode(r),))) if stage == "header" else
                         (lambda r: s.decode(r, schema.limit, retained_strings=True)) if stage == "decode" else schema.parse)
                with pytest.raises(ValueError, match="^json_encoding$"):
                    check(original)
                assert calls == [original], (label, stage)
