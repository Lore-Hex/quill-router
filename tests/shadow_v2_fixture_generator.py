"""Offline fixture builder. Expectations are hand-derived; never imported by src."""
from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router import async_settle_shadow_v2_certificate as cert
from trusted_router import async_settle_shadow_v2_contract as c
from trusted_router import async_settle_shadow_v2_evidence as e
from trusted_router.detached_jws import TrustedKey


def canonical(value: Any) -> bytes:
    # Independent fixture serialization: no production serializer on signing path.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def independent_signature(private: Ed25519PrivateKey, kid: str, claims: dict[str, Any], *, whitespace: bool = False) -> dict[str, str]:
    header = {"alg": "EdDSA", "kid": kid, "typ": "tr-async-settle-shadow-exclusion-v2"}
    assert set(claims) == set("v policy_version iss aud iat exp authorization_id generation_id workspace_id key_id invocation_nonce reservation_id billing_authority journal_region epoch settle_origin requested_route requested_streamed reason facts_sha256 candidate_set_sha256 router_revision original_receipt async_eligible".split())
    payload = canonical(claims) + (b" " if whitespace else b"")
    material = b64encode(canonical(header)) + "." + b64encode(payload)
    signature = b64encode(private.sign(material.encode("ascii")))
    return {"header_bytes": canonical(header).decode(), "payload_bytes": payload.decode(), "signing_input": material, "signature": signature, "certificate": material + "." + signature}


ROOT = Path(__file__).parent / "fixtures/async_settlement"
OUT = ROOT / "shadow_v2"
NOW = 1791676800
DAY = "2026-10-11"
REV = "a" * 40
ZERO = "0" * 64


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def wrapped(**fields: Any) -> dict[str, Any]:
    return {"v": 2, "policy_version": "shadow-v2", **fields}


def build() -> dict[str, Any]:
    old = json.loads((ROOT / "authorize_v1.json").read_bytes())
    shadow = json.loads((ROOT / "shadow_v1.json").read_bytes())
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(old["seed_hex"]))
    key = TrustedKey("exclusion-test", cert.PURPOSE, b64encode(private.public_key().public_bytes_raw()), "router-fixture", cert.AUDIENCE)
    ids = {k: old["claims"][k] for k in e.IDENTITY_FIELDS}
    facts = {"settle_origin": "typed", "usage_class": "Credits", "billing_authority": "local",
        "route": "chat.completions", "streamed": False, "service_tier": None, "app_markup": 0,
        "custom_markup": 0, "receipt_requested": True, "receipt_fee": 0, "commercial_program": "ordinary",
        "features": dict.fromkeys(c.FEATURES + c.DEPENDENT, False), "unknown_features": []}
    coords = {"day": DAY, "boot": "issuer", "first_seq": f"{1:020}", "event_seq": f"{1:020}"}
    committed = {k: ids[k] for k in e.IDENTITY_FIELDS if k not in e.COMMITTED_IDENTITIES}
    committed.update({k + "_sha256": digest(ids[k]) for k in e.COMMITTED_IDENTITIES})
    event = wrapped(seq=f"{1:020}", event_at_us=NOW * 1000000, receipt_at_us=NOW * 1000000,
        phase="authorize", authorization_day=DAY, authorization_id=ids["authorization_id"],
        identity=committed, router_revision=REV,
        dimensions={"adapter": None, "route_type": "chat.completions", "streamed": False},
        decision={"reason": "receipt_fee", "facts_sha256": digest({"facts": facts, "reason": "receipt_fee"}), "candidate_set_sha256": None},
        facts=facts, candidates=None, receipt_locator=coords, partition="C", eligibility="excluded", diagnostic=None)

    def signed(ev: dict[str, Any], identity: dict[str, Any] = ids) -> dict[str, Any]:
        claims = wrapped(**identity, iss=key.iss, aud=key.aud, iat=NOW, exp=NOW + 172800,
            requested_route=ev["facts"]["route"], requested_streamed=ev["facts"]["streamed"],
            **ev["decision"], router_revision=REV,
            original_receipt={**ev["receipt_locator"], "event_sha256": digest(ev)}, async_eligible=False)
        signing = independent_signature(private, key.kid, claims)
        token = signing["certificate"]
        projection = {k: v for k, v in claims.items() if k not in e.COMMITTED_IDENTITIES}
        projection.update({k + "_sha256": digest(claims[k]) for k in e.COMMITTED_IDENTITIES})
        row = wrapped(claims=projection, certificate_sha256=hashlib.sha256(token.encode()).hexdigest(),
            issuer_keyring_sha256=ZERO, verified_at_us=NOW * 1000000, verification_result="verified", authorization_day=DAY)
        page = wrapped(boot=ev["receipt_locator"]["boot"], day=DAY,
            first_seq=ev["receipt_locator"]["first_seq"], last_seq=ev["seq"], previous_page_sha256=None, events=[ev])
        return {**signing, "claims": claims, "certificate": token, "event": ev, "page": page, "record": row,
            "page_key": [e.KINDS[2], DAY + "/" + ev["receipt_locator"]["boot"] + "/" + ev["receipt_locator"]["first_seq"]],
            "expected_identity": identity, "now": NOW, "expected_partition": "B", "key": key.__dict__}

    proof = signed(event)
    candidate_event = copy.deepcopy(event)
    candidate_event["facts"]["receipt_requested"] = False
    candidate_event["candidates"] = [{"endpoint_id": "partner/endpoint", "adapter": "other", "features": []}]
    candidate_event["decision"] = {"reason": "unsupported_adapter", "facts_sha256": digest({"facts": candidate_event["facts"], "reason": "unsupported_adapter"}), "candidate_set_sha256": digest(candidate_event["candidates"])}
    candidate = signed(candidate_event)
    boundary = copy.deepcopy(candidate_event)
    boundary_ids = dict(ids)
    # Fill only represented immutable metadata, never an invented padding member.
    for target, field, maximum in ((boundary["identity"], "journal_region", 128), (boundary["receipt_locator"], "boot", 64), (boundary["candidates"][0], "endpoint_id", 128)):
        count = min(maximum - len(target[field]), 2048 - len(canonical(boundary)))
        target[field] += "x" * max(0, count)
    boundary_ids["journal_region"] = boundary["identity"]["journal_region"]
    boundary["decision"]["candidate_set_sha256"] = digest(boundary["candidates"])
    assert len(canonical(boundary)) == 2048
    at_limit = signed(boundary, boundary_ids)
    over = copy.deepcopy(boundary)
    over["candidates"][0]["endpoint_id"] += "x"
    over["decision"]["candidate_set_sha256"] = digest(over["candidates"])
    assert len(canonical(over)) == 2049
    # Complete ordered projection is independently fixed at exactly 4,993 bytes.
    projection = [{"endpoint_id": f"partner/{i:02}/" + "x" * 89, "adapter": "other", "features": []} for i in range(32)]
    delta = 4993 - len(canonical(projection))
    assert delta >= 0
    for projected in projection:
        extra = min(delta, 128 - len(projected["endpoint_id"]))
        projected["endpoint_id"] += "x" * extra
        delta -= extra
    assert delta == 0
    large = copy.deepcopy(candidate_event)
    large["candidates"] = projection
    large["decision"]["candidate_set_sha256"] = digest(projection)
    assert len(canonical(projection)) == 4993

    def terminal(kind: str) -> dict[str, Any]:
        attempt = {"authorization_id": ids["authorization_id"], "terminal_kind": kind,
            "selected_endpoint": "openai/billing-v1@openai/prepaid" if kind == "settle" else None,
            "adapter": "openai" if kind == "settle" else None, "route_type": "chat.completions", "streamed": False}
        return wrapped(variant="excluded", certificate=proof["certificate"], attempt=attempt, go_revision=REV)

    def wire(value: dict[str, Any], expected: str) -> dict[str, Any]:
        raw = canonical(value)
        return {"object": value, "canonical": raw.decode(), "header": b64encode(raw), "expected": expected, "description": "Literal byte-echo compatibility vector only; real retention/retry-seam proof belongs to P2/P4."}

    compared = {**shadow, "v": 2, "policy_version": "shadow-v2", "variant": "compared"}
    hashed = {k: v for k, v in compared.items() if k != "billing_snapshot"}
    failure = {**hashed, "raw_usage": None, "terminal": None, "payload_hash": None, "go_error": "usage_missing"}
    health = wrapped(authority="local", source="shadow_observation", publisher_boot_id="issuer",
        publisher_region="us-central1", router_revision=REV, complete=True, slot=NOW // 2,
        observed_at_us=NOW * 1000000, publisher_heartbeat_us=NOW * 1000000 + 100,
        sample_count=0, backlog_count=0, frozen_micro=0, p50_age_us=0, p95_age_us=0,
        oldest_unresolved_age_us=0, dead_count=0)
    health_claim = wrapped(authority="local", slot=NOW // 2, claim_at_us=NOW * 1000000, owner_boot_id="issuer")
    hist = {"bounds_us": [100, 500, 1000, 2000, 5000, 10000, 50000, 200000], "counts": [0] * 9}
    counter = wrapped(day=DAY, boot="issuer", router_revision=REV, region="us-central1",
        **dict.fromkeys(e.COUNTS + e.CHECKPOINT_FIELDS, 0), eligible_demand=0, event_seq=0,
        durable_event_hwm=0, checkpoint_seq=1, receipt_count=0, receipt_chain_sha256=None,
        loss_count=0, reset_count=0, diagnostics={}, buckets=[],
        started_at_us=NOW * 1000000, flushed_at_us=NOW * 1000000, closed=True,
        counts=[dict(adapter=a, route_type=r, streamed=stream, **dict.fromkeys(e.LEGACY_COUNT_FIELDS, 0)) for a, r, stream in e.FIXED_DIMENSIONS],
        terminal_counts=[], exclusions=[], rejections=[], drops=[], dimension_overflow=0, counter_overflow=False,
        **dict.fromkeys("comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown".split(), 0),
        first_evidence_at_us=None, last_mismatch_at_us=None, first_gap_at_us=None,
        authorize_shadow_hist=[0] * 9, evidence_write_hist=[0] * 9,
        admission_observer=dict.fromkeys("workspace_reads health_reads read_failures missed_ticks late_installs max_consecutive_failures degraded_seconds prediction_yes prediction_no prediction_unknown".split(), 0),
        publisher={**dict.fromkeys("claim_attempts claims scans publications failures missed_slots".split(), 0), "cycle_us": hist, "rpc_us": hist})
    closure = wrapped(day=DAY, boot="issuer", router_revision=REV, region="us-central1", reason="shutdown",
        interval_end_us=NOW * 1000000, final_event_seq=0, final_checkpoint_seq=1, durable_event_hwm=0,
        receipt_chain_sha256=ZERO, counter_sha256=digest(counter), retained_work=0, pending_comparisons=0,
        unresolved_commits=0, loss_count=0, observer_fold_through_us=NOW * 1000000, previous_day_closure_sha256=None)
    interval = {"start_us": NOW * 1000000, "end_us": NOW * 1000000}
    roster = {"issuer": {"revision": REV, "region": "us-central1", "lifetime": interval}}
    manifest = wrapped(day=DAY, config_sha256=ZERO, fixture_sha256=ZERO, router_writers=roster,
        enclave_writers={}, revisions={"router": [REV], "enclave": []},
        **dict.fromkeys("counter_root closure_root receipts_root prerequisite_ledger_root independent_inventory_sha256".split(), ZERO),
        first_evidence_at_us=None, proof_manifest_sha256=None,
        disabled_flag_intervals=[{"interval": interval, "admission": False, "protection": False, "negotiation": False, "fast_drain": False}], complete=True, gaps=[])
    ledger = wrapped(role="router", boot="issuer", revision=REV, region="us-central1", policy_sha256=ZERO,
        fixture_sha256=ZERO, config_sha256=ZERO, boot_at_us=NOW * 1000000, collection_interval=interval,
        traffic_ready_at_us=NOW * 1000000, previous_ledger_sha256=None,
        **{k: {"sha256": ZERO, "completed_at_us": NOW * 1000000} for k in ("transport", "size", "regional_load", "boot_transport", "observer_freshness", "marker_keys")})
    proof_manifest = wrapped(source_shas={"router": REV, "enclave": REV},
        **dict.fromkeys("evaluator_fixture_sha256 binding_fixture_sha256 wire_fixture_sha256 ledger_root shutdown_inventory_sha256 cohort_seed_policy_sha256 pre_budget_sha256 post_budget_sha256 publisher_slot_trace_sha256 external_proofs_sha256 resolution_dag_sha256".split(), ZERO),
        rosters_by_day={DAY: {"router": roster, "enclave": {}}}, instance_regions_by_day={DAY: {"issuer": "us-central1"}},
        attachments=[], explicit_keys=[], complete=True)
    # Amount: ceil-half-up(1 * .5 + 1 * .5) = 1 for fixture rates, but v1
    # applies per-component rounding: round(.5) + round(.5) = 2 micro.
    sample = wrapped(authorization_id=ids["authorization_id"], authorization_day=DAY,
        observed_at_us=NOW * 1000000, authorize_at_us=NOW * 1000000, booking_observed_at_us=NOW * 1000000,
        workspace_fingerprint=ZERO, adapter="openai", route_type="chat.completions", streamed=False,
        model_id="openai/billing-v1", endpoint_id="openai/billing-v1@openai/prepaid",
        snapshot_hash=shadow["terminal"]["snapshot_hash"], payload_hash=shadow["payload_hash"], rebuilt_snapshot_hash=None,
        raw_usage=shadow["raw_usage"], python_usage=shadow["terminal"]["usage"], go_usage=shadow["terminal"]["usage"], legacy_usage=shadow["terminal"]["usage"],
        frozen_micro=2, python_micro=2, go_micro=2, booked_micro=2, rebuilt_micro=None, legacy_frozen_micro=2,
        python_minus_go=0, booked_minus_frozen=0, rebuilt_minus_frozen=None, booked_minus_rebuilt=None,
        classification="exact", reason_codes=[], eligibility={"requested": True, "observed": True, "exclusion": None},
        booking={"attempted_kind": "settle", "outcome": "settled", "source": "finalized_authorization", "price_source": "catalog_at_authorize_time"},
        admission={"prediction": "yes", "reason": "eligible", **dict.fromkeys("tier pending_micro cap_micro workspace_age_us health_age_us health_p95_us".split(), 0)},
        timing={**dict.fromkeys("authorize_shadow_us handoff_prepare_us router_settle_us booking_confirm_us comparator_us".split(), 0), "evidence_write_us": None},
        deployment={"region": "us-central1", "instance": "issuer", "router_revision": REV, "go_revision": REV, "python_evaluator": "billing-v1", "go_evaluator": "billing-v1"},
        provenance={"binding_verified": True, "raw_matches_body": True, "rebuild_matches_booking_view": False,
            "snapshot_transport": "full", "s0_reconstruction": "not_needed", "legacy_oracle": "stage_d_candidate_v1", "fixture_sha256": hashlib.sha256((ROOT / "shadow_v1.json").read_bytes()).hexdigest()},
        proof_kind="compared", original_terminal_receipt={"day": DAY, "boot": "issuer", "event_seq": f"{2:020}"},
        health_source="shadow_observation", health_slot=NOW // 2, prediction_basis="counterfactual_backlog_v2")
    def variant(n: str, obj: Any, schema: str) -> dict[str, Any]:
        return {"name": n, "schema": schema, "object": obj, "expected": "accept"}

    terminal_receipt = {**event, "phase": "settle", "receipt_locator": None, "facts": None, "candidates": None,
        "original_receipt": proof["claims"]["original_receipt"], "certificate_sha256": proof["record"]["certificate_sha256"],
        "payload_sha256": None, "proof_kind": "certificate", "partition": "B", "comparison": None,
        "booking": {"outcome": "settled", "reference": ZERO, "winner": "settle"}, "prediction": None,
        "retry_original": None, "completion_of": None}
    files = {
        "authorize_compared.json": [variant("compared", {"billing_shadow_policy": "shadow-v2", "billing_shadow_binding": shadow["billing_shadow_binding"], "billing_snapshot": shadow["billing_snapshot"]}, "AUTHORIZE_COMPARED")],
        "authorize_excluded.json": [variant("excluded", {"billing_shadow_policy": "shadow-v2", "billing_shadow_exclusion": proof["certificate"]}, "AUTHORIZE_EXCLUDED")],
        "exclusion_certificate.json": [{"name": "request-level", **proof, "seed_hex": old["seed_hex"], "key": key.__dict__, "protected_header": {"alg": "EdDSA", "kid": key.kid, "typ": cert.TYP}}, {"name": "candidate-dependent", **candidate}, {"name": "event-2048", **at_limit}, {"name": "event-2049", **signed(over, boundary_ids), "expected": "candidate_receipt_size", "expected_partition": "C"}, {"name": "candidate-32-4993", **signed(large), "projection_bytes": 4993, "candidate_count": 32, "expected": "candidate_receipt_size", "expected_partition": "C"}],
        "terminal_compared_full.json": [{"name": "full", **wire(compared, "accept"), "expected_micro": 2, "negative_controls_micro": [1, 3]}],
        "terminal_compared_hash_only.json": [{"name": "hash-only", **wire(hashed, "accept"), "expected_micro": 2}],
        "terminal_compared_failure.json": [{"name": "usage-less-refund", **wire(failure, "C")}],
        "terminal_excluded_settle.json": [{"name": "settle", **wire(terminal("settle"), "B")}],
        "terminal_excluded_refund.json": [{"name": "pre-invoke-refund", **wire(terminal("refund"), "B")}],
        "capability_headers.json": [variant("authorize-marker", {"X-TR-Settlement-Shadow-Policy": "shadow-v2"}, "CAPABILITY_HEADERS")],
        "purpose_rejections.json": [{"name": "three-purpose-matrix", "ticket": old["response"]["data"]["settlement_ticket"], "ticket_claims": old["claims"], "ticket_public_key": old["public_key"], "binding": shadow["billing_shadow_binding"], "certificate": proof["certificate"], "expected_foreign": "reject"}],
        "wire_rejections.json": [{"name": "duplicate-version", "raw": '{"v":2,"v":2}', "expected": "reject"}, {"name": "float-version", "raw": '{"v":2.0}', "expected": "reject"}, {"name": "bool-version", "raw": '{"v":true}', "expected": "reject"}],
        "health.json": [variant("empty", health, "HEALTH")], "health_claim.json": [variant("claim", health_claim, "HEALTH_CLAIM")],
        "receipt_pages.json": [variant("authorize-page", proof["page"], "PAGE"), variant("authorize-event", event, "AUTHORIZE_EVENT"), variant("terminal-event", terminal_receipt, "TERMINAL_EVENT")],
        "sample.json": [variant("exact-two-micro", sample, "SAMPLE")], "certificate_record.json": [variant("verified", proof["record"], "CERTIFICATE_RECORD")],
        "counter.json": [variant("closed-idle", counter, "COUNTER")], "closure.json": [variant("closed-idle", closure, "CLOSURE"), variant("sealed-export", wrapped(closure_sha256=digest(closure), physical_commit_ack_at_us=NOW * 1000000 + 1), "SEALED_CLOSURE")],
        "controls.json": [variant("manifest", manifest, "MANIFEST"), variant("cap", wrapped(day=DAY, terminal_reserved=100, event_reserved=100), "CAP"), variant("retention", wrapped(retired_through="2026-09-10", kinds=list(e.KINDS)), "RETENTION")],
        "proof_manifest.json": [variant("proof", proof_manifest, "PROOF_MANIFEST")], "prerequisite_ledger.json": [variant("boot", ledger, "PREREQUISITE_LEDGER")],
        "gate_outputs.json": [variant("no-seed", wrapped(gate="shadow-serving", verdict="BLOCKED", completed_at_us=NOW * 1000000, proof_manifest_sha256=ZERO, prerequisite_ledger_root=ZERO, gaps=["positive_seed_gap"], clock_start_us=None, elapsed_seconds=0, coverage_sha256=ZERO), "GATE_OUTPUT")],
    }
    # Nested record examples make every exact sub-object independently portable.
    dims = {"adapter": "openai", "route_type": "chat.completions", "streamed": False}
    files["counter.json"].extend([
        variant("partition-bucket", {"phase": "authorize", "dimensions": dims, **dict.fromkeys(e.COUNTS, 0), "eligible_demand": 0}, "BUCKET"),
        variant("finite-dimensions", dims, "COUNTER_DIMENSIONS"),
        variant("diagnostic-bucket", {**dims, "phase": "worker", "reason": "original_receipt_missing", "count": 1}, "COUNT_DIAGNOSTIC"),
        variant("legacy-phase-bucket", {**dims, "phase": "settle", **dict.fromkeys(e.LEGACY_PHASE_FIELDS, 0)}, "LEGACY_PHASE_COUNT"),
    ])
    files["sample.json"].append(variant("counterfactual-prediction", {"prediction": "yes", "reason": "eligible", "health_source": "shadow_observation", "health_slot": NOW // 2, "prediction_basis": "counterfactual_backlog_v2", "basis_sha256": ZERO}, "PREDICTION"))
    files["controls.json"].append(variant("gap-interval", {"interval": interval, "reason": "writer_closure_gap"}, "GAP_INTERVAL"))
    files["terminal_compared_full.json"].append(variant("billing-v1-tier", {"max_prompt_tokens": None, "rates": shadow["billing_snapshot"]["candidates"][0]["rates"]}, "TIER"))
    for width, target in ((1, 1401), (64, 1338)):
        max_claims = copy.deepcopy(proof["claims"])
        for field, maximum in (("iss", 128), ("journal_region", 128), ("requested_route", 128), ("workspace_id", 64), ("key_id", 64), ("reservation_id", 64), ("invocation_nonce", 64)):
            extra = min(maximum - len(max_claims[field]), target - len(canonical(max_claims)))
            max_claims[field] += "x" * max(0, extra)
        max_key = TrustedKey("k" * width, cert.PURPOSE, key.public_key_b64url, max_claims["iss"], cert.AUDIENCE)
        max_header = {"alg": "EdDSA", "kid": max_key.kid, "typ": cert.TYP}
        material = b64encode(canonical(max_header)) + "." + b64encode(canonical(max_claims))
        token = material + "." + b64encode(private.sign(material.encode()))
        assert len(canonical(max_claims)) == target and len(token) == 2048
        files["exclusion_certificate.json"].append({"name": f"maximum-claims-kid-{width}", "claims": max_claims,
            **independent_signature(private, max_key.kid, max_claims), "key": max_key.__dict__, "claim_bytes": target, "certificate_bytes": 2048,
            "expected_signature": "accept", "target": "verify_signature", "expected_gap": None, "expected_identity": {k: max_claims[k] for k in e.IDENTITY_FIELDS}, "now": NOW})
        larger = {**max_claims, "invocation_nonce": max_claims["invocation_nonce"] + "x"}
        larger_material = b64encode(canonical(max_header)) + "." + b64encode(canonical(larger))
        files["exclusion_certificate.json"].append({"name": f"one-byte-over-claims-kid-{width}", "claims": larger,
            "certificate": larger_material + "." + b64encode(private.sign(larger_material.encode())),
            "key": max_key.__dict__, "expected_signature": "reject", "now": NOW,
            "target": "verify_signature", "expected_gap": "receipt_locator_size",
            **independent_signature(private, max_key.kid, larger),
            "expected_identity": {k: larger[k] for k in e.IDENTITY_FIELDS}})
    for label, obj, schema_name, limit in (("verified-row", proof["record"], "CERTIFICATE_RECORD", 4096),
                                         ("receipt-page", proof["page"], "PAGE", 65536)):
        filename = "certificate_record.json" if label == "verified-row" else "receipt_pages.json"
        for extra in (0, 1):
            raw = canonical(obj)
            raw += b" " * (limit + extra - len(raw))
            files[filename].append({"name": label + ("-at-limit" if extra == 0 else "-one-byte-over"), "raw": raw.decode(),
                "schema": schema_name, "target": schema_name, "expected": "accept" if extra == 0 else "reject", "bytes": limit + extra,
                "expected_gap": None if extra == 0 else "evidence_size"})
    # Commit transport rejects rather than relying only on Python-generated cases.
    excluded = terminal("settle")
    for label, obj in (("extra-field", {**excluded, "extra": None}),
                       ("missing-field", {k: v for k, v in excluded.items() if k != "certificate"}),
                       ("wrong-boolean", {**excluded, "attempt": {**excluded["attempt"], "streamed": 0}})):
        files["wire_rejections.json"].append({"name": label, "raw": canonical(obj).decode(), "expected": "reject"})
    for label, size in (("excluded-exact-4096", 4096), ("excluded-over-4097", 4097)):
        raw = canonical(excluded)
        raw += b" " * (size - len(raw))
        files["wire_rejections.json"].append({"name": label, "raw": raw.decode(), "header": b64encode(raw), "expected": "accept" if size == 4096 else "reject"})
    locator = proof["claims"]["original_receipt"]
    for label, change in (("locator-missing-boot", {k: v for k, v in locator.items() if k != "boot"}),
                          ("locator-extra", {**locator, "extra": None}),
                          ("locator-invalid-day", {**locator, "day": "2026-02-30"}),
                          ("locator-wrong-boot", {**locator, "boot": "wrong"}),
                          ("locator-wrong-page", {**locator, "first_seq": f"{2:020}", "event_seq": f"{2:020}"}),
                          ("locator-wrong-event", {**locator, "event_seq": f"{2:020}"}),
                          ("locator-wrong-digest", {**locator, "event_sha256": "f" * 64})):
        files["wire_rejections.json"].append({"name": label, "locator": change, "expected": "C"})
    files["exclusion_certificate.json"].append({"name": "maximum-locator", "locator": {
        "day": DAY, "boot": "b" * 64, "first_seq": "18446744073709551615", "event_seq": "18446744073709551615", "event_sha256": "f" * 64},
        "coordinate_bytes": 164, "locator_bytes": 246, "authorize_member_bytes": 183, "signed_member_bytes": 266,
        "maximum_claim_bytes_minimum_kid": 1401, "maximum_claim_bytes_maximum_kid": 1338,
        "certificate_limit": 2048, "record_limit": 4096, "event_limit": 2048, "wire_limit": 4096, "header_limit": 5462})
    # Self-contained verifier vectors; expected results are explicit contract facts.
    def verification(label: str, source: dict[str, Any] = proof, **updates: Any) -> dict[str, Any]:
        at = terminal("settle")["attempt"]
        if source["claims"]["candidate_set_sha256"] is not None:
            at.update(selected_endpoint="partner/endpoint", adapter="other")
        vector = {"name": label, "target": "verify_certificate", "certificate": source["certificate"],
            "claims": copy.deepcopy(source["claims"]), "key": key.__dict__, "now": NOW,
            "expected_identity": copy.deepcopy(source["expected_identity"]), "page_key": source["page_key"],
            "page": copy.deepcopy(source["page"]), "attempt": at, "server_attempt": copy.deepcopy(at),
            "booking": "settled", "expected_partition": "B", "expected_gap": None}
        vector.update(updates)
        return vector

    rejects = files["wire_rejections.json"]
    old_locators = [v for v in rejects if "locator" in v]
    rejects[:] = [v for v in rejects if "locator" not in v]
    for v in old_locators:
        item = verification(v["name"])
        item["claims"]["original_receipt"] = v["locator"]
        item.update(independent_signature(private, key.kid, item["claims"]))
        item["expected_partition"] = "C"
        item["expected_gap"] = ("original_receipt_locator_gap" if v["name"] in
            ("locator-missing-boot", "locator-extra", "locator-invalid-day") else
            "original_receipt_digest_gap" if v["name"] == "locator-wrong-digest" else "original_receipt_identity_gap")
        item["raises"] = item["expected_gap"] == "original_receipt_locator_gap"
        rejects.append(item)
    for label, when, gap in (("before-iat", NOW - 1, "proof_expired"), ("at-iat", NOW, None),
                             ("last-valid-second", NOW + 172799, None), ("at-exp", NOW + 172800, "proof_expired")):
        rejects.append(verification(label, now=when, expected_partition="C" if gap else "B", expected_gap=gap, raises=bool(gap)))
    for field in ids:
        item = verification("server-identity-" + field, expected_partition="C", expected_gap="identity", raises=True)
        item["expected_identity"][field] = ids[field] + "x" if isinstance(ids[field], str) else ids[field] + 1
        rejects.append(item)
    item = verification("signed-noncanonical-payload", expected_partition="C", expected_gap="proof_signature", raises=True)
    item.update(independent_signature(private, key.kid, item["claims"], whitespace=True))
    rejects.append(item)
    # Correctly signed complete backing that contains an eligible fallback is C.
    fallback = copy.deepcopy(candidate_event)
    fallback["candidates"].append({"endpoint_id": "openai/fallback", "adapter": "openai", "features": []})
    fallback["decision"]["candidate_set_sha256"] = digest(fallback["candidates"])
    rejects.append(verification("eligible-fallback", signed(fallback), expected_partition="C", expected_gap="exclusion_conflict"))
    refund = terminal("refund")["attempt"]
    unknown = {**refund, "route_type": None, "streamed": None}
    for label, server, gap in (("unknown-refund-actuals", unknown, None),
                               ("refund-placeholder-normalized-to-unknown", unknown, None),
                               ("conflicting-actual-stream", {**unknown, "streamed": True}, "exclusion_conflict"),
                               ("conflicting-actual-route", {**unknown, "route_type": "responses"}, "exclusion_conflict"),
                               ("known-endpoint-unknown-adapter", {**unknown, "selected_endpoint": "unexpected"}, "exclusion_conflict")):
        rejects.append(verification(label, attempt=refund, server_attempt=server, booking="refunded",
            expected_partition="C" if gap else "B", expected_gap=gap))
    missing_actual = verification("candidate-needs-actuals", candidate, expected_partition="C", expected_gap="exclusion_conflict")
    missing_actual["server_attempt"].update(selected_endpoint=None, adapter=None)
    rejects.append(missing_actual)
    for label, mutate, gap in (
        ("page-missing", lambda v: v.update(page=None), "original_receipt_missing"),
        ("page-key-conflict", lambda v: v.update(page_key=[e.KINDS[2], DAY + "/other/" + f"{1:020}"]), "original_receipt_identity_gap"),
        ("page-body-conflict", lambda v: v["page"].update(boot="other"), "original_receipt_identity_gap"),
        ("page-sequence-conflict", lambda v: v["page"].update(last_seq=f"{2:020}"), "original_receipt_identity_gap"),
        ("page-extra-field", lambda v: v["page"].update(extra=None), "original_receipt_backing_gap"),
    ):
        item = verification(label, expected_partition="C", expected_gap=gap)
        mutate(item)
        rejects.append(item)

    for label, token in (("ticket-as-certificate", old["response"]["data"]["settlement_ticket"]),
                         ("binding-as-certificate", shadow["billing_shadow_binding"])):
        rejects.append(verification(label, certificate=token, expected_partition="C", expected_gap="type", raises=True))
    for label, gap in (("signed-duplicate-payload", "duplicate_key"), ("signed-duplicate-header", "duplicate_key"),
                       ("signed-noncanonical-header", "canonical_header"), ("signature-tamper", "signature")):
        item = verification(label, expected_partition="C", expected_gap=gap, raises=True)
        header = canonical({"alg": "EdDSA", "kid": key.kid, "typ": "tr-async-settle-shadow-exclusion-v2"})
        payload = canonical(item["claims"])
        if label == "signed-duplicate-payload":
            payload = b'{"v":2,' + payload[1:]
        elif label == "signed-duplicate-header":
            header = b'{"alg":"EdDSA",' + header[1:]
        elif label == "signed-noncanonical-header":
            header += b" "
        material = b64encode(header) + "." + b64encode(payload)
        signature = private.sign(material.encode())
        if label == "signature-tamper":
            signature = bytes([signature[0] ^ 1]) + signature[1:]
        item["certificate"] = material + "." + b64encode(signature)
        rejects.append(item)
    for item in rejects:
        if "target" not in item:
            item["target"] = "parse_terminal"
            item["expected_gap"] = (None if item["expected"] == "accept" else
                "duplicate_key" if item["name"] == "duplicate-version" else
                "integer" if item["name"] == "float-version" else
                "evidence_size" if item["name"] == "excluded-over-4097" else "evidence_schema_gap")

    def row_case(filename: str, label: str, obj: Any, schema: str, accept: bool) -> None:
        files[filename].append({"name": label, "schema": schema, "target": schema,
            "object": copy.deepcopy(obj), "expected": "accept" if accept else "reject",
            "expected_gap": None if accept else "evidence_schema_gap"})
    failed = copy.deepcopy(terminal_receipt)
    failed.update(partition="C", eligibility="eligible", proof_kind="compared", original_receipt=None,
        certificate_sha256=None, comparison="unevaluable", prediction=files["sample.json"][1]["object"])
    failed["booking"].update(outcome="unknown", winner=None, reference=None)
    row_case("receipt_pages.json", "eligible-failure", failed, "TERMINAL_EVENT", True)
    row_case("receipt_pages.json", "eligible-failure-missing-prediction", {**failed, "prediction": None}, "TERMINAL_EVENT", False)
    for phase, outcome, winner in (("settle", "refunded", "refund"), ("refund", "settled", "settle")):
        obj = copy.deepcopy(terminal_receipt)
        obj.update(phase=phase)
        obj["booking"].update(outcome=outcome, winner=winner)
        row_case("receipt_pages.json", phase + "-opposite-winner", obj, "TERMINAL_EVENT", False)
    row_case("receipt_pages.json", "B-computed-mismatch", {**terminal_receipt, "comparison": "mismatch"}, "TERMINAL_EVENT", False)
    for label, change in (("epoch-zero", {"epoch": 0}), ("invented-generation", {"generation_id": "invented"})):
        obj = copy.deepcopy(proof["record"])
        obj["claims"].update(change)
        row_case("certificate_record.json", label, obj, "CERTIFICATE_RECORD", False)
        files["certificate_record.json"][-1].update(target="validate_row", kind=e.KINDS[1],
            row_key=DAY + "/" + ids["authorization_id"], expected_gap="identity")
    rows = [dict(phase=phase, adapter=a, route_type=r, streamed=stream, reason="usage_missing", count=1)
            for phase, (a, r, stream) in itertools.product(("authorize", "settle", "refund", "worker"), e.FIXED_DIMENSIONS)]
    for count in (128, 129):
        obj = copy.deepcopy(counter)
        obj.update(exclusions=rows[:64], rejections=rows[64:128], drops=rows[128:count])
        row_case("counter.json", "combined-diagnostics-" + str(count), obj, "COUNTER", count == 128)
    for field in ("exclusions", "rejections", "drops", "terminal_counts"):
        obj = copy.deepcopy(counter)
        row = files["counter.json"][4]["object"] if field == "terminal_counts" else rows[0]
        obj[field] = [row, copy.deepcopy(row)]
        row_case("counter.json", "duplicate-" + field, obj, "COUNTER", False)
    obj = copy.deepcopy(sample)
    obj["reason_codes"] = ["winner_polarity"]
    row_case("sample.json", "winner-deltas", obj, "SAMPLE", False)
    obj = copy.deepcopy(sample)
    obj.update(proof_kind="observed_binding", classification="unevaluable", reason_codes=["service_tier"])
    obj["eligibility"].update(observed=False, exclusion="service_tier")
    row_case("sample.json", "excluded-admission-yes", obj, "SAMPLE", False)
    obj["admission"]["prediction"] = "no"
    row_case("sample.json", "excluded-admission-diagnostic-only", obj, "SAMPLE", True)

    matrix = files["purpose_rejections.json"][0]
    matrix.update(target="purpose_matrix", expected="reject", expected_gap="per-case", cases=[])
    for target in ("ticket", "lookup", "binding", "certificate"):
        for source in ("ticket", "binding", "certificate"):
            if source == ("ticket" if target == "lookup" else target):
                continue
            token = matrix[source]
            kid = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "==="))["kid"]
            own_key = {**key.__dict__, "kid": kid}
            if target in ("ticket", "lookup"):
                own_key.update(purpose="async-settle-ticket", aud="router-settlement")
            elif target == "binding":
                own_key.update(purpose="async-settle-shadow", aud="router-shadow")
            matrix["cases"].append(dict(name=target + "-rejects-" + source, target="foreign_" + target,
                certificate=token, signing_input=token.rsplit(".", 1)[0], key=own_key,
                expected_identity=old["claims"] if target == "ticket" else ids,
                now=old["claims"]["iat"] if target == "ticket" else NOW + 172801 if target == "lookup" else NOW,
                expected_gap="proof_signature" if target == "binding" else "type"))

    for item in files["exclusion_certificate.json"]:
        if item.get("expected") == "candidate_receipt_size":
            item.update(target="issue", seed_hex=old["seed_hex"], fresh=True,
                        complete_candidates=True, expected_gap="candidate_receipt_size")
    for field in ("python_usage", "go_usage", "legacy_usage"):
        for damage in ("prompt-sum", "reasoning"):
            obj = copy.deepcopy(sample)
            obj.update(classification="normalization", reason_codes=["malformed_usage"])
            obj[field]["total_prompt_tokens" if damage == "prompt-sum" else "reasoning_tokens"] += 3
            row_case("sample.json", field + "-" + damage, obj, "SAMPLE", True)
    for value in ("us west", "us=west", "us(west)", "é"):
        row_case("counter.json", "region-" + value, {**counter, "region": value}, "COUNTER", True)
    for field in ("region", "instance"):
        for index, value in enumerate(('quoted"', 'back\\slash', 'tab\t', '\x00')):
            obj = copy.deepcopy(sample)
            obj["deployment"][field] = value
            row_case("sample.json", field + "-escaped-" + str(index), obj, "SAMPLE", True)
    for field in ("authority", "usage_type", "route_type", "service_tier", "billing_shadow_binding"):
        for index, value in enumerate(('quoted"', 'back\\slash', 'tab\t', 'é') if field != "billing_shadow_binding" else ('quoted"', 'back\\slash', 'tab\t')):
            obj = copy.deepcopy(compared)
            (obj if field == "billing_shadow_binding" else obj["observed"])[field] = value
            files["terminal_compared_full.json"].append({"name": field + "-retained-" + str(index), **wire(obj, "accept")})

    return {filename: wrapped(variants=variants) for filename, variants in files.items()}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    files = build()
    manifest = wrapped(policy_sha256=hashlib.sha256(b"shadow-v2").hexdigest(), files={}, legacy_files={})
    for filename, content in files.items():
        raw = canonical(content) + b"\n"
        (OUT / filename).write_bytes(raw)
        manifest["files"][filename] = {"sha256": hashlib.sha256(raw).hexdigest(), "variants": [v["name"] for v in content["variants"]]}
    for path in sorted(ROOT.glob("*.json")):
        manifest["legacy_files"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    raw = canonical(manifest) + b"\n"
    (OUT / "manifest.json").write_bytes(raw)
    print("Manifest SHA-256:", hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    main()
