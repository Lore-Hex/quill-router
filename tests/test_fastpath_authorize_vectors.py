"""The authorize golden vectors are what the Python route computes now
(docs/design/fast-admission-e2-decide.md §10.1, §10.2).

fastpath/testdata/authorize_cases_v1.json maps authorize requests, each in
its own world, to Go's answer (Python's exact bytes and rows), Python's 503,
or a pass with its §3 reason and what Python answered;
authorize_fingerprint_v1.json maps request bodies to the idempotency
fingerprint or the route's refusal. The generator runs every case through
the real route in its own process (it installs the snapshot fixture catalog
and pins clocks), so this test runs it in a subprocess and compares the
files; the rest checks that each answer is consistent with itself, which is
what the Go edge will have to reproduce.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from trusted_router.stage_d import ESTIMATED_USAGE_SNAPSHOT_MODELS

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "fastpath" / "testdata" / "authorize_cases_v1.json"
FINGERPRINTS = ROOT / "fastpath" / "testdata" / "authorize_fingerprint_v1.json"
SNAPSHOT = ROOT / "fastpath" / "testdata" / "routing_snapshot_v1.body.json"
GENERATOR = ROOT / "scripts" / "fastpath" / "authorize_vectors.py"

# The pass reasons Python's own pilot shares (pilot_reason, the boot, the
# owner's non-admit answers): Python never leases a request tripping one.
PYTHON_PASSES = {
    "policy.pilot_off", "policy.not_pilot_workspace", "policy.pilot_region", "policy.records_not_durable",
    "policy.digest_not_accepted", "body.stream", "candidates.byok", "boot.signature", "boot.unverified",
    "boot.missing", "key.key_cap", "key.key_window", "key.budget_strict", "owner.busy", "owner.off",
    "owner.invalid", "owner.failed", "owner.unreachable",
}
# Every §3 reason a request, its key and workspace, the policy, the
# evaluation or the insert can trip, each with a case recording Python's
# answer. (cache.*, decide.off, inflight.key, budget.spent, clock.window,
# policy.regions_mismatch and policy.lookup_hash_mismatch are the edge's
# own state, with no Python counterpart to record.)
PASS_REASONS = PYTHON_PASSES | {
    "body.unparseable", "body.duplicate_key", "body.float", "body.lax_type", "body.strip_ambiguous",
    "body.refused", "body.api_key_hash", "body.service_tier", "body.cache_affinity", "body.priced_extra",
    "body.canonical_model", "body.custom_model", "body.tags", "body.referer", "body.evaluator_input",
    "header.idempotency_key", "header.settlement_async", "header.video_providers",
    "key.missing", "key.disabled", "key.expired", "key.scope", "key.app",
    "workspace.paused", "workspace.deleted", "workspace.federated", "workspace.broadcast",
    "policy.async_settle_shadow", "policy.monitor_key", "evaluate.refused",
    "candidates.estimated_usage", "candidates.private_proxy", "insert.scope_taken",
    # Not in §3's list: merged tags Python refuses (over 50, over 4096 bytes,
    # a corrupt stored map) and a key whose workspace row is missing.
    "tags.merged_invalid", "workspace.missing",
}
# Decision 4 (and §3's body reasons): Go passes a body carrying any of these.
PASS_REQUIRED = {
    "float_present", "lax_coercion", "duplicate_key", "strip_ambiguous", "non_ascii_tag",
    "referer_not_strict", "canonical_model_rewrite", "native_batch_key", "partner_key",
    "idempotency_key_too_long",
}
# The generator's two additions to decision 4's list.
GO_PASSES_TOO = {"lone_surrogate", "idempotency_key_non_ascii"}
TIMING = ["total_ms", "spanner_rpcs", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms"]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases(outcome: str | None = None) -> list[dict[str, Any]]:
    return [case for case in _load(CASES)["cases"] if outcome is None or case["expect"]["outcome"] == outcome]


def test_the_vectors_are_what_the_route_computes_now(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed repository generator, no shell or caller input
        [sys.executable, str(GENERATOR), "--out", str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, cwd=ROOT,
        capture_output=True, text=True, timeout=900, check=False,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    for committed in (CASES, FINGERPRINTS):
        fresh = tmp_path / committed.relative_to(ROOT)
        assert committed.read_bytes() == fresh.read_bytes(), (
            f"{committed.relative_to(ROOT)} is stale: run "
            "`uv run python scripts/fastpath/authorize_vectors.py` and check the Go edge against it"
        )


def test_the_cases_name_their_snapshot_and_policy() -> None:
    vectors = _load(CASES)
    assert vectors["snapshot_sha256"] == hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    policy = vectors["policy"]
    assert (policy["v"], policy["kind"], policy["plane"]) == (1, "tr-edge-policy", vectors["plane"])
    assert policy["not_after"] - policy["issued_at"] == 120 and policy["refresh_at"] - policy["issued_at"] == 30
    names = [case["name"] for case in vectors["cases"]]
    assert names == sorted(names) and len(set(names)) == len(names)
    for case in vectors["cases"]:
        assert set(case["policy_patch"]) <= set(policy), case["name"]
        effective = {**policy, **case["policy_patch"]}
        assert effective["issued_at"] * 1_000_000 < case["at_us"] < effective["not_after"] * 1_000_000, case["name"]
        assert case["labels"] == sorted(case["labels"]), case["name"]


def _instant(us: int) -> str:
    return dt.datetime.fromtimestamp(us / 1_000_000, dt.UTC).isoformat().replace("+00:00", "Z")


def test_every_answer_agrees_with_its_rows_and_inputs() -> None:
    answers = _cases("answer")
    assert len(answers) > 150
    token = ["x-trustedrouter-internal-token", _load(CASES)["internal_token"]]
    for case in answers:
        name, expect, ids = case["name"], case["expect"], case["ids"]
        data = json.loads(expect["response"])["data"]
        authorization, reservation = expect["authorization_row"], expect["reservation_row"]
        payload = json.loads(authorization["payload"])
        # The payload is json_body's: sorted keys, compact, ASCII.
        assert authorization["payload"] == json.dumps(payload, sort_keys=True, separators=(",", ":")), name
        # The ids are the case's.
        assert data["authorization_id"] == payload["id"] == authorization["authorization_id"] == ids["authorization_id"]
        assert expect["generation_id"] == data["generation_id"] == "gen-" + uuid.uuid5(
            uuid.NAMESPACE_URL, f"trustedrouter:{ids['authorization_id']}").hex, name
        assert (reservation["reservation_id"] == authorization["reservation_id"] == payload["credit_reservation_id"]
                == data["credit_reservation_id"] == ids["reservation_id"]), name
        assert reservation["authorization_id"] == ids["authorization_id"], name
        # One clock reading: the instant prices, stamps and expires the rows.
        assert authorization["created_at_us"] == reservation["created_at_us"] == case["at_us"], name
        assert payload["created_at"] == _instant(case["at_us"]), name
        assert reservation["expires_at_us"] - case["at_us"] == 7_200_000_000, name
        assert authorization["settled"] is False and authorization["terminal_at_us"] is None, name
        # The scope row holds nothing, unsharded, credits.
        assert (reservation["credit_reserved_micro"], reservation["key_reserved_micro"]) == (0, 0), name
        assert (reservation["ws_shard"], reservation["credit_shard"], reservation["key_shard"]) == (0, 0, 0), name
        assert reservation["hold_usage_type"] == "Credits" == payload["usage_type"] == data["usage_type"], name
        # The fingerprint the route hashed, in the payload, the typed column
        # and the scope row; the scope is the idempotency key's, the minted
        # one when none was presented.
        assert hashlib.sha256(expect["fingerprint_material"].encode()).hexdigest() == expect["fingerprint"], name
        assert (payload["idempotency_fingerprint"] == authorization["idempotency_fingerprint"]
                == reservation["idempotency_fingerprint"] == expect["fingerprint"]), name
        facts = case["facts"]["key"]
        assert expect["idempotency_scope"] == reservation["idempotency_scope"] == (
            f"{facts['workspace_id']}#{facts['hash']}#{hashlib.sha256(payload['idempotency_key'].encode()).hexdigest()}")
        if "idempotency.minted" in case["labels"]:
            assert payload["idempotency_key"] == ids["minted_idempotency_key"], name
        material = json.loads(expect["fingerprint_material"])
        assert (material["workspace_id"], material["key_hash"]) == (facts["workspace_id"], facts["hash"]), name
        assert not {"api_key_hash", "api_key_lookup_hash", "idempotency_key", "invocation_nonce"} & set(material), name
        # The estimate, the envelope, the boot and the candidates as answered.
        assert (expect["estimate_microdollars"] == data["estimated_cost_microdollars"]
                == payload["estimated_microdollars"] == authorization["estimated_microdollars"]), name
        assert payload["fastpath_envelope"] == case["admitted"]["envelope"], name
        assert payload["fastpath_end_of_life"] == _instant(case["admitted"]["end_of_life_us"]), name
        assert payload["stage_d_boot_kid"] == authorization["stage_d_boot_kid"] == case["boot"]["kid"], name
        assert case["boot"]["signature_ok"] and case["boot"]["verified"], name
        assert payload["candidate_endpoint_ids"] == [c["endpoint_id"] for c in data["route_candidates"]], name
        assert payload["endpoint_id"] == data["endpoint_id"] == data["route_candidates"][0]["endpoint_id"], name
        assert expect["decide_region"] == data["region"] == payload["region"], name
        assert payload["stage_d_reason"] == data["stage_d"]["reason"] and not data["stage_d"]["eligible"], name
        assert f"stage_d.{data['stage_d']['reason']}" in case["labels"], name
        assert data["tags"] == payload.get("tags", {}), name
        assert data["broadcast_destinations"] == [] and data["candidate_cost_reporting"] is False, name
        assert data["response_model"] is None and data["idempotent_replay"] is False, name
        assert case["facts"]["key"]["eligible"] and not case["facts"]["workspace"]["pause_causes"], name
        # data.timing is the last key, its shape fixed (its values volatile).
        assert list(data)[-1] == "timing" and list(data["timing"]) == TIMING, name
        assert token in case["request"]["headers"], name


def test_the_answered_float_is_pydantics_spelling_not_python_repr() -> None:
    """FastAPI renders the route's returned dict through pydantic-core:
    estimated_cost is its float text (0.00001, 1e-6), not Python's repr
    (1e-05, 1e-06). Error bodies are JSONResponse's json.dumps."""
    float_json = TypeAdapter(float)
    differs = 0
    for case in _cases("answer"):
        response = case["expect"]["response"]
        micro = case["expect"]["estimate_microdollars"]
        text = float_json.dump_json(micro / 1_000_000).decode()
        assert f'"estimated_cost":{text},' in response, case["name"]
        differs += text != repr(micro / 1_000_000)
    assert differs > 5, "some answered estimates must be ones whose two spellings differ"


def test_the_answers_cover_every_stage_d_reason_and_float_spelling() -> None:
    labels = Counter(label for case in _cases("answer") for label in case["labels"])
    for label in ("stage_d.not_streaming", "stage_d.stage_d_disabled", "stage_d.workspace_not_pilot",
                  "stage_d.heartbeats_disabled", "stage_d.heartbeat_undeclared", "wafer_zdr", "multi_candidate",
                  "tags.key_only", "tags.request_only", "tags.merge_overwrite", "float.exponent",
                  "float.integral", "idempotency.presented_body", "idempotency.presented_header",
                  "idempotency.minted", "invocation_nonce", "region.requested", "region.default",
                  "insert.inserted", "insert.unknown_landed"):
        assert labels[label] > 0, label


def test_every_pass_reason_records_pythons_answer() -> None:
    passes = _cases("pass")
    reasons = Counter(case["expect"]["reason"] for case in passes)
    assert PASS_REASONS <= set(reasons), sorted(PASS_REASONS - set(reasons))
    for case in passes:
        expect = case["expect"]
        assert case["labels"] == [f"pass.{expect['reason']}"], case["name"]
        python = expect["python"]
        assert python["status"] and (python["status"] == 200) == (python["type"] is None), case["name"]
        if expect["reason"] in PYTHON_PASSES:
            assert not python["leased"], case["name"]
        if expect["reason"] == "insert.scope_taken":
            # A replay writes nothing: the stored authorization, or 409.
            assert python["status"] in {200, 409} and not python["leased"], case["name"]
    # Positive control: some passes Python leases, so only the edge's own
    # predicate keeps them out of the cohort.
    leased = {case["expect"]["reason"] for case in passes if case["expect"]["python"]["leased"]}
    assert {"body.float", "body.tags", "candidates.estimated_usage", "body.cache_affinity",
            "workspace.broadcast", "header.settlement_async"} <= leased


def test_the_estimated_usage_models_are_the_three_go_names() -> None:
    """Decision 7: Go hardcodes these ids and passes a request naming one."""
    names = {"abliterate/abliterate-0.3-fast", "abliterate/abliterate-0.3-balanced",
             "abliterate/abliterate-0.3-clever"}
    assert names == ESTIMATED_USAGE_SNAPSHOT_MODELS
    covered = {json.loads(case["request"]["body"])["model"] for case in _cases("pass")
               if case["expect"]["reason"] == "candidates.estimated_usage"}
    assert covered == names


def test_an_insert_whose_outcome_is_unknown_and_not_landed_is_pythons_exact_503() -> None:
    (case,) = _cases("unavailable")
    expect = case["expect"]
    assert case["insert"] == "unknown_not_landed" and expect["status"] == 503
    assert expect["headers"] == [["retry-after", "1"]]
    response = json.loads(expect["response"])
    error = response["error"]
    assert (error["code"], error["message"], error["type"]) == (503, "Authorization unavailable; retry",
                                                                "service_unavailable")
    assert list(response["data"]) == ["timing"] and list(response["data"]["timing"]) == TIMING


def test_the_fingerprint_bodies_cover_what_go_must_pass() -> None:
    bodies = _load(FINGERPRINTS)["cases"]
    assert len(bodies) >= 14_000 and FINGERPRINTS.stat().st_size < 10_000_000
    labels = Counter(label for body in bodies for label in body["labels"])
    assert all(labels[label] >= 20 for label in PASS_REQUIRED), labels
    outcomes = Counter(body["expect"]["outcome"] for body in bodies)
    assert outcomes["fingerprint"] > 6_000 and outcomes["refused"] > 2_000
    for body in bodies:
        expect = body["expect"]
        if expect["outcome"] == "refused":
            assert expect["status"] >= 400 and expect["type"], body["name"]
            continue
        assert re.fullmatch(r"[0-9a-f]{64}", expect["fingerprint"]), body["name"]
        presented = expect["idempotency_key"]
        assert expect["idempotency_scope"] == (
            f"{body['workspace_id']}#{body['key_hash']}#{hashlib.sha256(presented.encode()).hexdigest()}"
            if presented else None), body["name"]
    # The same request spelled differently hashes the same: some fingerprint
    # is shared by bodies whose text differs, among bodies Go answers.
    by_fingerprint: dict[str, set[str]] = {}
    for body in bodies:
        if body["expect"]["outcome"] == "fingerprint" and not (PASS_REQUIRED | GO_PASSES_TOO) & set(body["labels"]):
            by_fingerprint.setdefault(body["expect"]["fingerprint"], set()).add(body["body"])
    assert sum(len(texts) > 1 for texts in by_fingerprint.values()) > 50
