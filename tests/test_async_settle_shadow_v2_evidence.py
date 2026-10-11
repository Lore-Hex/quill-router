"""P1 locator, backing, evidence-bound and dormancy proof witnesses."""
from __future__ import annotations

import ast
import copy
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_async_settle_shadow_v2_contract import CASES, fixture, proof, signer
from trusted_router import async_settle_shadow_v2_certificate as cert
from trusted_router import async_settle_shadow_v2_evidence as e
from trusted_router import async_settle_shadow_v2_schema as s
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
    new = {"async_settle_shadow_v2_" + suffix for suffix in ("schema", "contract", "certificate", "evidence")}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        if path.stem not in new:
            assert not any(name in path.read_text() for name in new), str(path)
        else:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("trusted_router"):
                    assert (node.module == "trusted_router" and all(n.name == "billing_snapshot" for n in node.names)) or node.module.rsplit(".", 1)[-1] in new | {"detached_jws", "async_settle_shadow_wire", "billing_snapshot"}, str(path)
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


# Port of review-2/parity_scan.py: same object, explicit mutation grid, including
# optional/empty-array fields the original 3,556-case scan did not reach.
# This is the ONLY acceptance-difference allowlist. Every entry must be witnessed.
PARITY_ALLOWLIST = {
    "certificate-issuer-bound": {"shape": "binding_claims", "condition": "issuer length 129..512",
        "v1": "accept", "v2": "reject", "design": "§4.1: new certificate issuer is at most 128 bytes"},
    "certificate-origin": {"shape": "binding_claims", "condition": "settle_origin legacy",
        "v1": "reject", "v2": "accept", "design": "§4.1: actual origins, not forced typed"},
    "certificate-authority": {"shape": "binding_claims", "condition": "nonlocal bounded billing authority",
        "v1": "reject", "v2": "accept", "design": "§4.1: actual authorities, not forced local"},
    "clean-winner-conflict": {"shape": "sample", "condition": "clean classification with winner_polarity reason",
        "v1": "accept", "v2": "reject", "design": "§2 / §8 G9: known winner disagreement cannot be clean"},
    "strict-diagnostic-array": {"shape": "counter", "condition": "empty dict/string in a diagnostic array",
        "v1": "accept", "v2": "reject", "design": "§7: existing diagnostics arrays; §8 G1 no coercions"},
    "excluded-admission": {"shape": "sample", "condition": "observed false and admission yes",
        "v1": "accept", "v2": "reject", "design": "§2 / §5: B is never admission yes"},
    "strict-dimension": {"shape": "counter", "condition": "integer 0/1 in streamed dimension",
        "v1": "accept", "v2": "reject", "design": "§7: stream dimension is boolean or null"},
    "clean-usage": {"shape": "sample", "condition": "clean comparison with all usage null",
        "v1": "accept", "v2": "reject", "design": "§2: A requires normalized Python/Go/legacy usage"},
}


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
    from scripts.async_settle.shadow_report import COUNTER_FIELDS, validate_counter
    from trusted_router import async_settle_shadow_evidence as olde
    from trusted_router import async_settle_shadow_v2_contract as contract
    from trusted_router import async_settle_shadow_wire as oldw
    from trusted_router.detached_jws import b64encode

    def old_sample(v: dict[str, Any]) -> None:
        old = {k: val for k, val in v.items() if k not in set(e.SAMPLE.fields) - olde.SAMPLE_FIELDS}
        old.update(v=1, policy_version="shadow-v1")
        olde.validate_sample(old, v["authorization_day"] + "/" + v["authorization_id"])

    def old_counter(v: dict[str, Any]) -> None:
        old = {k: val for k, val in v.items() if k not in set(e.COUNTER.fields) - COUNTER_FIELDS}
        old.update(v=1, policy_version="shadow-v1", sequence=v["checkpoint_seq"], instance="00000000-0000-0000-0000-000000000001")
        validate_counter(v["day"] + "/" + old["instance"], old)

    def old_wire(v: dict[str, Any]) -> None:
        old = {k: val for k, val in v.items() if k not in ("policy_version", "variant")}
        old["v"] = 1
        oldw.parse_header((b64encode(canonical(old)),))

    sample = copy.deepcopy(fixture("sample")[0]["object"])
    sample["classification"] = "unevaluable"
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
    # Signed price-binding claims are not redefined by P1. Audit the common
    # server identity projection against v1 binding validation, holding each
    # purpose's own extra fields fixed. Never import binding from production v2.
    from trusted_router import async_settle_shadow_binding as old_binding
    from trusted_router.detached_jws import b64decode
    old_claims = json.loads(b64decode(fixture("purpose_rejections")[0]["binding"].split(".")[1]))
    p = proof()
    old_claims.update(iat=p["now"], exp=p["now"] + 172800)
    base = {k: old_claims[k] for k in (*e.IDENTITY_FIELDS, "iss", "iat", "exp", "async_eligible")}
    claims = {**p["claims"], **base}
    def old_identity(v: dict[str, Any]) -> None:
        if set(v) != set(base):
            raise ValueError("identity")
        old_binding.validate_claims({**old_claims, **v}, p["now"])
    def new_identity(v: dict[str, Any]) -> None:
        if set(v) != set(base):
            raise ValueError("identity")
        cert.validate_claims({**claims, **v}, p["now"])
    shapes.append(("binding_claims", base, old_identity, new_identity))
    return shapes


def parity_difference(shape: str, value: dict[str, Any], old: bool, new: bool) -> str | None:
    if shape == "binding_claims":
        if old and not new and isinstance(value.get("iss"), str) and 128 < len(value["iss"]) <= 512:
            return "certificate-issuer-bound"
        if new and not old and value.get("settle_origin") == "legacy":
            return "certificate-origin"
        if new and not old and value.get("billing_authority") in ("spend_lease", "regional_lease", "federated", "deferred_home"):
            return "certificate-authority"
    if not old or new:
        return None
    if shape == "sample":
        if value.get("classification") in ("exact", "explained-by-catalog-change") and "winner_polarity" in value.get("reason_codes", []):
            return "clean-winner-conflict"
        if value.get("eligibility", {}).get("observed") is False and value.get("admission", {}).get("prediction") == "yes":
            return "excluded-admission"
        if value.get("classification") in ("exact", "explained-by-catalog-change") and all(value[k] is None for k in ("python_usage", "go_usage", "legacy_usage")):
            return "clean-usage"
    if shape == "counter":
        if any(type(value[field]) is not list for field in ("exclusions", "rejections", "drops")):
            return "strict-diagnostic-array"
        for field in ("counts", "terminal_counts", "exclusions", "rejections", "drops"):
            if any(type(row.get("streamed")) is int for row in value[field]):
                return "strict-dimension"
    return None


def test_systematic_retained_parity(tmp_path: Path) -> None:
    from scripts.async_settle.shadow_report import COUNTER_FIELDS
    from trusted_router.async_settle_shadow_evidence import SAMPLE_FIELDS
    rows: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    witnessed: set[str] = set()
    def compare(shape: str, path: tuple[Any, ...], mutation: str, value: dict[str, Any], old: Any, new: Any) -> None:
        a, b = parity_accept(old, value), parity_accept(new, value)
        row = dict(shape=shape, path=list(path), mutation=mutation, old=a, new=b)
        rows.append(row)
        if a != b:
            allowed = parity_difference(shape, value, a, b)
            row["allowlist"] = allowed
            differences.append(row)
            row["changed"] = parity_get(value, path) if path else value
            if allowed in PARITY_ALLOWLIST:
                witnessed.add(allowed)
    for shape, base, old, new in retained_parity_shapes():
        assert parity_accept(old, base) and parity_accept(new, base), shape
        for path in parity_paths(base):
            if path and path[0] in ("v", "policy_version", "variant"):
                continue
            retained = SAMPLE_FIELDS if shape == "sample" else COUNTER_FIELDS | {"checkpoint_seq"} if shape == "counter" else base
            if path and path[0] not in retained:
                continue
            for mutation, item in parity_mutations(parity_get(base, path)):
                if not path:
                    if mutation not in ("missing-key", "extra-key"):
                        continue
                    removed = set(base) - set(item)
                    if removed and (not removed <= set(retained) or removed & {"v", "policy_version", "variant"}):
                        continue
                    value = copy.deepcopy(item)
                else:
                    value = copy.deepcopy(base)
                    parity_get(value, path[:-1])[path[-1]] = item
                compare(shape, path, mutation, value, old, new)
        if shape == "sample":
            import itertools
            for classification, observed, admission, usage in itertools.product(
                ("unevaluable", "normalization", "exact", "explained-by-catalog-change"), (True, False, None), ("yes", "no", "unknown"), ("equal", "null", "inconsistent", "disagree")):
                value = copy.deepcopy(base)
                value.update(classification=classification)
                value["eligibility"]["observed"] = observed
                value["admission"]["prediction"] = admission
                if usage == "null":
                    value.update(python_usage=None, go_usage=None, legacy_usage=None)
                elif usage == "inconsistent":
                    for field in ("python_usage", "go_usage", "legacy_usage"):
                        value[field]["reasoning_tokens"] = 2
                elif usage == "disagree":
                    value["go_usage"]["total_prompt_tokens"] += 1
                compare(shape, (), "cross-clean-usage-provenance", value, old, new)
        if shape == "sample":
            import itertools
            for clean, bound, raw, rebuilt, phase, outcome, winner in itertools.product(
                ("exact", "explained-by-catalog-change"), (False, True), (False, True), (False, True),
                ("settle", "refund"), ("settled", "refunded", "unknown"), (False, True)):
                value = copy.deepcopy(base)
                value.update(classification=clean, reason_codes=["winner_polarity"] if winner else [])
                value["provenance"].update(binding_verified=bound, raw_matches_body=raw, rebuild_matches_booking_view=rebuilt)
                value["booking"].update(attempted_kind=phase, outcome=outcome)
                for delta in ("python_minus_go", "booked_minus_frozen", "rebuilt_minus_frozen", "booked_minus_rebuilt"):
                    value[delta] = None
                if clean == "explained-by-catalog-change":
                    value.update(booked_micro=3, rebuilt_micro=3, rebuilt_snapshot_hash="f" * 64)
                compare(shape, (), "cross-clean-provenance-booking-winner", value, old, new)
        if shape == "wire":
            import itertools
            for error, terminal, raw, payload in itertools.product((None, "malformed_usage"), (False, True), (False, True), (False, True)):
                value = copy.deepcopy(base)
                value.update(go_error=error)
                for field, present in (("terminal", terminal), ("raw_usage", raw), ("payload_hash", payload)):
                    if not present:
                        value[field] = None
                compare(shape, (), "cross-error-presence", value, old, new)
        if shape == "counter":
            for field in ("exclusions", "rejections", "drops", "terminal_counts"):
                for altered in ("count", "reason", "phase", "adapter"):
                    value = copy.deepcopy(base)
                    row = copy.deepcopy(value[field][0])
                    if altered == "count":
                        row["count" if field != "terminal_counts" else e.LEGACY_PHASE_FIELDS[0]] += 1
                    elif altered == "reason":
                        if field == "terminal_counts":
                            continue
                        row["reason"] = "raw_usage"
                    else:
                        row[altered] = "refund" if altered == "phase" else "other"
                    value[field].append(row)
                    compare(shape, (), "cross-diagnostic-identity-" + altered, value, old, new)
            for count in (127, 128, 129):
                value = copy.deepcopy(base)
                unique = [dict(phase=phase, adapter=a, route_type=r, streamed=stream, reason="usage_missing", count=1)
                          for phase in ("authorize", "settle", "refund", "worker") for a, r, stream in e.FIXED_DIMENSIONS]
                value.update(exclusions=unique[:64], rejections=unique[64:count], drops=[])
                compare(shape, (), "cross-combined-diagnostic-bound", value, old, new)
        if shape == "binding_claims":
            for field, values in (("settle_origin", ("typed", "legacy")), ("billing_authority", ("local", "spend_lease", "regional_lease", "federated", "deferred_home"))):
                for item in values:
                    value = {**base, field: item}
                    compare(shape, (field,), "certificate-enum", value, old, new)
            for delta in (-172801, -172800, -1, 0, 1):
                for duration in (172799, 172800, 172801):
                    value = {**base, "iat": base["iat"] + delta, "exp": base["iat"] + delta + duration}
                    compare(shape, (), "cross-validity-window", value, old, new)
            for authorization in ("x", "x" * 64, "x" * 65):
                import uuid
                value = {**base, "authorization_id": authorization,
                    "generation_id": "gen-" + uuid.uuid5(uuid.NAMESPACE_URL, "trustedrouter:" + authorization).hex}
                compare(shape, (), "cross-generation-identity", value, old, new)
    report = dict(comparisons=len(rows), differences=differences, rows=rows)
    (tmp_path / "parity.json").write_text(json.dumps(report))
    if destination := os.environ.get("SHADOW_V2_PARITY_REPORT"):
        Path(destination).write_text(json.dumps(report))
    assert not [r for r in differences if r["allowlist"] not in PARITY_ALLOWLIST], differences
    assert witnessed == set(PARITY_ALLOWLIST), (witnessed, PARITY_ALLOWLIST)


@pytest.mark.parametrize("file,vector", [(file, v) for file in ("sample", "counter", "terminal_compared_full")
    for v in fixture(file) if any(tag in v["name"] for tag in ("-prompt-sum", "-reasoning", "-escaped-", "region-", "-retained-"))], ids=lambda v: v["name"] if isinstance(v, dict) else v)
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


def test_parity_allowlist_has_live_witnesses() -> None:
    shapes = {name: (base, old, new) for name, base, old, new in retained_parity_shapes()}
    seen = set()
    for shape, damage in (("sample", "exclusion"), ("sample", "usage"), ("sample", "winner"), ("counter", "dimension"), ("counter", "array"), ("binding_claims", "issuer"), ("binding_claims", "origin"), ("binding_claims", "authority")):
        base, old, new = shapes[shape]
        value = copy.deepcopy(base)
        if damage == "exclusion":
            value["eligibility"]["observed"] = False
        elif damage == "usage":
            value.update(classification="exact", python_usage=None, go_usage=None, legacy_usage=None)
        elif damage == "winner":
            value.update(classification="exact", reason_codes=["winner_polarity"], booked_minus_frozen=None, booked_minus_rebuilt=None)
        elif damage == "dimension":
            value["counts"][0]["streamed"] = 0
        elif damage == "issuer":
            value["iss"] = "x" * 129
        elif damage == "origin":
            value["settle_origin"] = "legacy"
        elif damage == "authority":
            value["billing_authority"] = "federated"
        else:
            value["drops"] = {}
        a, b = parity_accept(old, value), parity_accept(new, value)
        assert a != b
        key = parity_difference(shape, value, a, b)
        assert key in PARITY_ALLOWLIST
        seen.add(key)
    assert seen == set(PARITY_ALLOWLIST)
