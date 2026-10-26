"""The authorize golden vectors are what the Python app answers now
(docs/design/fast-admission-e2-decide.md §10.1, §10.2).

fastpath/testdata/authorize_cases_v1.json maps authorize requests, each in
its own world, to Go's answer (Python's exact bytes and rows), Python's 503,
or a pass with its §3 reason and what Python answered;
authorize_fingerprint_v1.json maps requests to the fingerprint and scope
Python wrote, or to Python's refusal. The generator sends every request
through the ASGI app in its own process (it installs the snapshot fixture
catalog and pins clocks), so this test runs it in a subprocess and compares
the files; the rest checks that each answer is consistent with itself, which
is what the Go edge will have to reproduce.
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

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import TypeAdapter

from trusted_router.catalog_data import MONITOR_MODEL_ID
from trusted_router.catalog_registry import MODELS
from trusted_router.gateway_boot import BOOT_AUTH_DOMAIN
from trusted_router.receipt_keys import b64url_encode
from trusted_router.stage_d import ESTIMATED_USAGE_SNAPSHOT_MODELS

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "fastpath" / "testdata" / "authorize_cases_v1.json"
FINGERPRINTS = ROOT / "fastpath" / "testdata" / "authorize_fingerprint_v1.json"
SNAPSHOT = ROOT / "fastpath" / "testdata" / "routing_snapshot_v2.body.json"
GENERATOR = ROOT / "scripts" / "fastpath" / "authorize_vectors.py"
CONTRACT = ROOT / "docs" / "design" / "fast-admission-e2-decide.md"

# §3's pass reasons that have a Python counterpart: each needs a case
# recording what Python answers. The rest of §3 is the edge's own state
# (cache.*, decide.off, inflight.key, budget.spent, clock.window,
# policy.lookup_hash_mismatch, policy.regions_mismatch).
SECTION_3 = {
    "body.content_type", "body.unparseable", "body.duplicate_key", "body.float", "body.lax_type",
    "body.strip_ambiguous", "body.dict_key_strip", "body.refused", "body.api_key_hash", "body.stream",
    "body.service_tier", "body.cache_affinity", "body.priced_extra", "body.canonical_model",
    "body.custom_model", "body.internal_model", "body.tags", "body.referer", "body.evaluator_input",
    "header.idempotency_key", "header.settlement_async", "header.video_providers",
    "key.missing", "key.disabled", "key.expired", "key.scope", "key.federated", "key.app", "key.key_cap",
    "key.key_window", "key.budget_strict",
    "workspace.missing", "workspace.paused", "workspace.in_debt", "workspace.tier", "workspace.federated",
    "workspace.deleted", "workspace.broadcast", "tags.merged_invalid",
    "policy.pilot_off", "policy.not_pilot_workspace", "policy.pilot_region", "policy.records_not_durable",
    "policy.async_settle_shadow", "policy.monitor_key", "policy.digest_not_accepted",
    "evaluate.refused", "candidates.byok", "candidates.estimated_usage", "candidates.private_proxy",
    "owner.off", "owner.busy", "owner.invalid", "owner.failed", "owner.unreachable",
    "insert.scope_taken", "insert.failed",
    # An estimate past int64 (the fake store keeps it, where a real Spanner
    # INT64 column refuses the commit), and the boot E1 or decision 8 refuses.
    "evaluate.estimate_overflow", "boot.signature", "boot.unverified", "boot.missing",
}
# The reasons Python's own pilot shares (pilot_reason, the boot, the owner's
# non-admit answers): Python never leases a request tripping one.
PYTHON_PASSES = {
    "policy.pilot_off", "policy.not_pilot_workspace", "policy.pilot_region", "policy.records_not_durable",
    "policy.digest_not_accepted", "body.stream", "candidates.byok", "key.key_cap", "key.key_window",
    "key.budget_strict", "owner.busy", "owner.off", "owner.invalid", "owner.failed", "owner.unreachable",
    "insert.failed", "boot.signature", "boot.unverified", "boot.missing",
}
# §10.2: Go passes a body carrying any of these.
PASS_REQUIRED = {
    "float_present", "lax_coercion", "duplicate_key", "strip_ambiguous", "dict_key_strip", "non_ascii_tag",
    "referer_not_strict", "canonical_model_rewrite", "native_batch_key", "partner_key",
    "idempotency_key_too_long", "idempotency_header_repeated", "idempotency_header_non_ascii",
    "content_type", "deep_nesting", "int_over_4000_digits", "bom", "lone_surrogate", "estimate_overflow",
}
DESCRIPTIVE = {
    "null_extra", "null_nested", "app_categories_empty", "input_modalities", "extras", "nested_provider",
    "nested_trace", "nested_metadata", "unicode_extra", "int_near_2_63", "int_beyond_2_63", "attribution",
    "tags", "tags_empty", "whitespace", "models_list", "allow_fallbacks_spelling", "idempotency_header",
    "idempotency_body", "invocation_nonce", "defaults",
}
TIMING = ["total_ms", "spanner_rpcs", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms"]


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cases(outcome: str | None = None) -> list[dict[str, Any]]:
    return [case for case in _load(CASES)["cases"] if outcome is None or case["expect"]["outcome"] == outcome]


def test_the_vectors_are_what_the_app_answers_now(tmp_path: Path) -> None:
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


def test_the_header_carries_pythons_internal_and_estimated_usage_models() -> None:
    """Decisions 7 and 19: Go holds its hardcoded sets equal to these, so a
    catalog that gains an internal_only model, or a changed estimated-usage
    set, must change the file first."""
    vectors = _load(CASES)
    internal = sorted({MONITOR_MODEL_ID, *(model.id for model in MODELS.values() if model.internal_only)})
    assert vectors["internal_models"] == internal
    assert vectors["estimated_usage_models"] == sorted(ESTIMATED_USAGE_SNAPSHOT_MODELS) == [
        "abliterate/abliterate-0.3-balanced", "abliterate/abliterate-0.3-clever", "abliterate/abliterate-0.3-fast",
    ]
    covered = {json.loads(case["request"]["body"])["model"] for case in _cases("pass")
               if case["expect"]["reason"] == "candidates.estimated_usage"}
    assert covered == set(ESTIMATED_USAGE_SNAPSHOT_MODELS)


def test_the_cases_name_their_snapshot_and_policy() -> None:
    vectors = _load(CASES)
    assert vectors["snapshot_sha256"] == hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest()
    policy = vectors["policy"]
    assert (policy["v"], policy["kind"], policy["plane"]) == (1, "tr-edge-policy", vectors["plane"])
    assert policy["not_after"] - policy["issued_at"] == 60 and policy["refresh_at"] - policy["issued_at"] == 15
    assert policy["internal_models"] == vectors["internal_models"]
    names = [case["name"] for case in vectors["cases"]]
    assert names == sorted(names) and len(set(names)) == len(names)
    for case in vectors["cases"]:
        assert set(case["policy_patch"]) <= set(policy), case["name"]
        effective = {**policy, **case["policy_patch"]}
        assert effective["issued_at"] * 1_000_000 < case["at_us"] < effective["not_after"] * 1_000_000, case["name"]
        assert case["labels"] == sorted(case["labels"]), case["name"]


def _instant(us: int) -> str:
    return dt.datetime.fromtimestamp(us / 1_000_000, dt.UTC).isoformat().replace("+00:00", "Z")


def _end_of_life(text: str) -> str:
    """§5: the owner's end of life truncated to microseconds, with its own
    offset (+00:00 as Z), no fraction when the microseconds are 0."""
    parsed = dt.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00"))
    return parsed.isoformat().replace("+00:00", "Z")


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
        # The reservation's created_at alone is the commit's timestamp (§5):
        # Spanner refuses a reading later than the commit in that column.
        assert authorization["created_at_us"] == case["at_us"], name
        assert "created_at_us" not in reservation and reservation["created_at"] == "commit_timestamp", name
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
        assert payload["fastpath_end_of_life"] == _end_of_life(case["admitted"]["end_of_life"]), name
        assert payload["stage_d_boot_kid"] == authorization["stage_d_boot_kid"] == case["boot"]["kid"], name
        assert case["boot"]["signature_ok"] and case["boot"]["verified"], name
        assert payload["candidate_endpoint_ids"] == [c["endpoint_id"] for c in data["route_candidates"]], name
        assert payload["endpoint_id"] == data["endpoint_id"] == data["route_candidates"][0]["endpoint_id"], name
        assert expect["decide_region"] == data["region"] == payload["region"], name
        assert payload["stage_d_reason"] == data["stage_d"]["reason"] and not data["stage_d"]["eligible"], name
        assert f"stage_d.{data['stage_d']['reason']}" in case["labels"], name
        assert data["tags"] == payload.get("tags", {}), name
        if case["insert"] == "unknown_landed":
            # Answered from the authorization read back: tags in sorted order.
            assert list(data["tags"]) == sorted(data["tags"]), name
        assert data["broadcast_destinations"] == [] and data["candidate_cost_reporting"] is False, name
        assert data["response_model"] is None and data["idempotent_replay"] is False, name
        assert case["facts"]["key"]["eligible"] and not case["facts"]["workspace"]["pause_causes"], name
        # data.timing is the last key, its keys in a fixed order; EXPECT
        # carries its values (the file's objects are written sorted).
        assert list(data)[-1] == "timing" and list(data["timing"]) == TIMING, name
        assert data["timing"] == expect["timing"], name
        assert token in case["request"]["headers"], name


def test_the_answered_float_is_pydantics_spelling_not_python_repr() -> None:
    """§6: FastAPI renders the route's returned dict through pydantic-core,
    so estimated_cost is its float text (0.00001, 1e-6), not Python's repr
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


def test_the_answers_cover_every_label_section_10_1_names() -> None:
    labels = Counter(label for case in _cases("answer") for label in case["labels"])
    for label in ("stage_d.not_streaming", "stage_d.stage_d_disabled", "stage_d.workspace_not_pilot",
                  "stage_d.heartbeats_disabled", "stage_d.heartbeat_undeclared", "wafer_zdr", "multi_candidate",
                  "tags.key_only", "tags.request_only", "tags.merge_overwrite", "float.exponent",
                  "float.integral", "idempotency.presented_body", "idempotency.presented_header",
                  "idempotency.minted", "invocation_nonce", "region.requested", "region.default",
                  "insert.inserted", "insert.unknown_landed", "read_back.sorted_tags", "end_of_life.offset"):
        assert labels[label] > 0, label


def test_every_section_3_reason_records_pythons_answer() -> None:
    passes = _cases("pass")
    reasons = Counter(case["expect"]["reason"] for case in passes)
    assert set(reasons) == SECTION_3, (sorted(SECTION_3 - set(reasons)), sorted(set(reasons) - SECTION_3))
    for case in passes:
        expect = case["expect"]
        if "affinity" in expect:
            # G13: labelled by the class Python's warm bytes compare as.
            assert expect["reason"] == "body.cache_affinity", case["name"]
            assert case["labels"] == sorted(["pass.body.cache_affinity",
                                             expect["affinity"]["class"].replace("_", ".", 1)]), case["name"]
        else:
            assert case["labels"] == [f"pass.{expect['reason']}"], case["name"]
        python = expect["python"]
        assert python["status"] >= 200, case["name"]
        assert (python["status"] == 200) <= (python["type"] is None), case["name"]
        if expect["reason"] in PYTHON_PASSES:
            assert not python["leased"], case["name"]
        if expect["reason"] == "insert.scope_taken":
            # A replay writes nothing: the stored authorization, or 409.
            assert python["status"] in {200, 409} and not python["leased"], case["name"]
    # Each cause of body.unparseable and each tags.merged_invalid has a case.
    names = {case["name"] for case in passes}
    for name in ("body.unparseable:bom", "body.unparseable:depth_129", "body.unparseable:int_4001_digits",
                 "body.unparseable:over_4_mib", "header.idempotency_key:repeated", "header.idempotency_key:non_ascii",
                 "tags.merged_invalid:over_50", "tags.merged_invalid:over_4096_bytes",
                 "tags.merged_invalid:corrupt_stored"):
        assert f"pass:{name}" in names, name
    # Positive control: some passes Python leases, so only the edge's own
    # predicate keeps them out of the cohort.
    leased = {case["expect"]["reason"] for case in passes if case["expect"]["python"]["leased"]}
    assert {"body.float", "body.tags", "body.dict_key_strip", "candidates.estimated_usage", "body.cache_affinity",
            "workspace.broadcast", "header.settlement_async", "body.unparseable"} <= leased


def test_affinity_cases_record_pythons_cold_and_warm_orders() -> None:
    """G13 (§1 item 5): Python's order for a cache_affinity_key comes from
    a per-process session table only settle seeds. Each affinity case
    records Python's 200 with no session (the cold order, which Go
    composes) and with the case's sessions remembered: the same bytes
    (affinity_cold: no session, an expired one, one for another endpoint,
    a provider.order, the remembered endpoint already first) or the
    remembered endpoint moved to the front of its model's run, or kept as
    the one candidate with fallbacks off, and nothing else changed
    (affinity_warm)."""
    cases = [case for case in _cases("pass") if "affinity" in case["expect"]]
    classes = Counter(case["expect"]["affinity"]["class"] for case in cases)
    assert classes["affinity_cold"] >= 6 and classes["affinity_warm"] >= 10, classes
    # The 200's members written from the first candidate (gwauth/response.go, responseData).
    first_candidate_members = {"model", "upstream_model", "endpoint_id", "provider", "provider_name",
                               "inference_location", "wafer_zdr_required", "route_candidates"}
    missing = object()
    for case in cases:
        name, a, python = case["name"], case["expect"]["affinity"], case["expect"]["python"]
        body = json.loads(case["request"]["body"])
        assert re.fullmatch(r"[0-9a-f]{64}", body["cache_affinity_key"]), name
        assert python == {"status": 200, "type": None, "leased": True}, name
        cold, warm = json.loads(a["cold_response"])["data"], json.loads(a["response"])["data"]
        cold_ids = [c["endpoint_id"] for c in cold["route_candidates"]]
        warm_ids = [c["endpoint_id"] for c in warm["route_candidates"]]
        assert (cold_ids, warm_ids) == (a["cold_candidates"], a["candidates"]), name
        assert (cold["estimated_cost_microdollars"], warm["estimated_cost_microdollars"]) == (
            a["cold_estimate_microdollars"], a["estimate_microdollars"]), name
        # wafer_zdr_required is written only under a Wafer first candidate.
        members = [k for k in cold if k != "wafer_zdr_required"]
        assert members == [k for k in warm if k != "wafer_zdr_required"] and list(warm)[-1] == "timing", name
        # The key is in neither response nor the payload's material.
        assert "cache_affinity" not in a["response"] and "cache_affinity" not in a["cold_response"], name
        seeded = {endpoint for endpoint, _age in a["sessions"]}
        if a["class"] == "affinity_cold":
            assert a["response"] == a["cold_response"], name
            continue
        assert a["class"] == "affinity_warm" and warm_ids != cold_ids and seeded, name
        changed = {k for k in set(cold) | set(warm) if cold.get(k, missing) != warm.get(k, missing)}
        if len(cold_ids) == 1:
            # Fallbacks off: the remembered endpoint alone, at its own price.
            assert warm_ids[0] in seeded and warm_ids[0] != cold_ids[0], name
            assert changed <= first_candidate_members | {"estimated_cost", "estimated_cost_microdollars"}, name
            continue
        assert sorted(warm_ids) == sorted(cold_ids) and cold["estimated_cost_microdollars"] == warm[
            "estimated_cost_microdollars"], name
        # routing_state.rank's order: a stable sort by the model's first-seen
        # rank (unless provider.sort.partition is none), the remembered
        # endpoint first; provider.order names no provider in a warm case.
        provider = body.get("provider") or {}
        assert not provider.get("order"), name
        by_model = not (isinstance(provider.get("sort"), dict) and provider["sort"].get("partition") == "none")
        model_of = {c["endpoint_id"]: c["model"] for c in cold["route_candidates"]}
        ranks: dict[str, int] = {}
        for endpoint_id in cold_ids:
            ranks.setdefault(model_of[endpoint_id], len(ranks))
        expected = [sorted(cold_ids, key=lambda e: (ranks[model_of[e]] if by_model else 0, e != sticky))
                    for sticky in seeded]
        assert warm_ids in expected, (name, warm_ids, expected)
        assert changed <= first_candidate_members, name
    # Positive controls: a promotion within the second model, and a session
    # kept in place of the first with fallbacks off.
    by_name = {case["name"]: case["expect"]["affinity"] for case in cases}
    fallback = by_name["pass:body.cache_affinity:warm:fallback_models"]
    assert fallback["candidates"][0] == fallback["cold_candidates"][0] and fallback["class"] == "affinity_warm"
    assert by_name["pass:body.cache_affinity:warm:no_fallbacks"]["candidates"] != by_name[
        "pass:body.cache_affinity:warm:no_fallbacks"]["cold_candidates"]
    assert by_name["pass:body.cache_affinity:cold:expired"]["sessions"][0][1] > 600
    # Partition none moves the remembered endpoint of the second model to
    # the front of the list; the default partition keeps it in its run; an
    # empty provider.order is no order.
    none, model = (by_name[f"pass:body.cache_affinity:warm:partition_{p}:fallback_models"] for p in ("none", "model"))
    assert none["candidates"][0] != none["cold_candidates"][0] and none["class"] == "affinity_warm"
    assert model["candidates"][0] == model["cold_candidates"][0] and model["class"] == "affinity_warm"
    for case in ("warm:empty_order", "warm:no_fallbacks:empty_order", "warm:top_level_no_fallbacks:empty_order"):
        assert by_name[f"pass:body.cache_affinity:{case}"]["class"] == "affinity_warm", case
    assert by_name["pass:body.cache_affinity:cold:order"]["class"] == "affinity_cold"


def test_an_insert_whose_outcome_is_unknown_and_not_landed_is_pythons_exact_503() -> None:
    # A lost answer, and the refusals that prove nothing about a commit that
    # may have applied (§8.4: FailedPrecondition, Aborted once the client's
    # re-runs are spent): the same 503 bytes.
    cases = {case["insert"]: case for case in _cases("unavailable")}
    assert set(cases) == {"unknown_not_landed", "failed_precondition", "aborted"}
    for insert, case in cases.items():
        expect = case["expect"]
        assert expect["status"] == 503 and expect["headers"] == [["retry-after", "1"]], insert
        assert expect["response"] == cases["unknown_not_landed"]["expect"]["response"], insert
        response = json.loads(expect["response"])
        error = response["error"]
        assert (error["code"], error["message"], error["type"]) == (503, "Authorization unavailable; retry",
                                                                    "service_unavailable"), insert
        assert list(response["data"]) == ["timing"] and response["data"]["timing"] == expect["timing"], insert
        assert list(response["data"]["timing"]) == TIMING, insert
    # Python fails a FailedPrecondition outright, where Go keeps the hold.
    assert cases["failed_precondition"]["expect"]["python"]["status"] == 500
    assert "python" not in cases["aborted"]["expect"]


def test_the_fingerprint_bodies_cover_what_go_must_pass() -> None:
    bodies = _load(FINGERPRINTS)["cases"]
    assert len(bodies) >= 14_000 and FINGERPRINTS.stat().st_size < 10_000_000
    labels = Counter(label for body in bodies for label in body["labels"])
    assert set(labels) <= PASS_REQUIRED | DESCRIPTIVE, set(labels) - PASS_REQUIRED - DESCRIPTIVE
    # Two hand-written bodies carry a lone surrogate; every other label is
    # drawn at least three times.
    assert all(labels[label] >= 2 for label in PASS_REQUIRED | DESCRIPTIVE), labels
    outcomes = Counter(body["expect"]["outcome"] for body in bodies)
    assert outcomes["fingerprint"] > 5_000 and outcomes["refused"] > 2_000
    for body in bodies:
        expect = body["expect"]
        assert body["labels"] == sorted(body["labels"]), body["name"]
        if expect["outcome"] == "refused":
            assert expect["status"] >= 400, body["name"]
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
        if body["expect"]["outcome"] == "fingerprint" and not PASS_REQUIRED & set(body["labels"]):
            by_fingerprint.setdefault(body["expect"]["fingerprint"], set()).add(body["body"])
    assert sum(len(texts) > 1 for texts in by_fingerprint.values()) > 50


def _signature(vectors: dict[str, Any], body: bytes) -> str:
    """The boot signature as the file's header says to make it, from its seed alone."""
    key, rule = vectors["boot_test_key"], vectors["boot_auth"]
    seed = bytes.fromhex(key["seed_hex"])
    assert seed == hashlib.sha256(key["seed_preimage"].encode()).digest()
    digest = hashlib.sha256(
        rule["domain"].encode() + rule["method"].encode() + rule["path"].encode() + hashlib.sha256(body).digest(),
    ).digest()
    return b64url_encode(Ed25519PrivateKey.from_private_bytes(seed).sign(digest))


def test_the_contract_names_every_reason_and_label_the_vectors_use() -> None:
    """Every pass reason and fingerprint label here is one the contract
    names (§3, §8.1, §10.2), so the vectors and the doc cannot drift."""
    contract = CONTRACT.read_text(encoding="utf-8")
    named = set(re.findall(r"`([a-z0-9_.<>]+)`", contract))
    for reason in SECTION_3:
        group, _, name = reason.partition(".")
        if group == "key":  # `key.<reason>` for §8.1's reasons
            assert {"key.<reason>", name} <= named, reason
        elif reason == "evaluate.refused":  # `evaluate.<outcome>`
            assert "evaluate.<outcome>" in named, reason
        else:
            assert reason in named, reason
    assert PASS_REQUIRED | DESCRIPTIVE <= named, sorted((PASS_REQUIRED | DESCRIPTIVE) - named)


def test_the_headers_say_how_to_sign_and_expand_a_request() -> None:
    """Go can remake every boot signature from the header alone, and
    expand a body_expand body to what was sent."""
    vectors = _load(CASES)
    assert vectors["boot_auth"]["domain"].encode() == BOOT_AUTH_DOMAIN
    assert vectors["boot_auth"] == _load(FINGERPRINTS)["boot_auth"]
    assert vectors["boot_test_key"] == _load(FINGERPRINTS)["boot_test_key"]
    kid = vectors["boot_test_key"]["kid"]
    signed = expanded = 0
    for case in vectors["cases"]:
        request = case["request"]
        body = request["body"]
        if "body_expand" in request:
            expand = request["body_expand"]
            assert body.count(expand["marker"]) == 1, case["name"]
            body = body.replace(expand["marker"], expand["repeat"] * expand["count"])
            assert len(body.encode()) > 4 * 1024 * 1024, case["name"]
            expanded += 1
        auth = [value for name, value in request["headers"] if name == "x-tr-boot-auth"]
        if case["boot"] is not None and case["boot"]["signature_ok"]:
            assert auth == [f"kid={kid},sig={_signature(vectors, body.encode())}"], case["name"]
            signed += 1
    assert expanded == 1 and signed > 200


def test_the_facts_are_the_stored_rows_in_section_10_3s_shapes() -> None:
    """KEY_FACTS.tags is the stored key body's tags in its order (json_body
    sorts them; Python merges what it decodes), and the facts carry
    §10.3's fields."""
    key_fields = {"lookup_hash", "eligible", "reason", "hash", "workspace_id", "expires_at_us", "tags", "tags_usable"}
    workspace_fields = {"workspace_id", "missing", "pause_causes", "in_debt", "trust_tier", "federated", "deleted",
                        "byok_slugs", "broadcast_pointer"}
    reordered = 0
    for case in _cases():
        facts = case["facts"]
        assert set(facts["key"]) == key_fields and set(facts["workspace"]) == workspace_fields, case["name"]
        assert facts["workspace"]["missing"] == (case["workspace"]["body"] is None), case["name"]
        stored = json.loads(case["key"]["key_body"]).get("tags", {})
        stored = stored if isinstance(stored, dict) else {}
        assert facts["key"]["tags"] == [[k, v] for k, v in stored.items()], case["name"]
        # Stored tags are sorted, whatever order the key was given them in.
        assert list(stored) == sorted(stored), case["name"]
        reordered += len(stored) > 1
    # Positive control: the answered tag cases and the read-back case give
    # their keys tags out of sorted order, and the facts still list them sorted.
    assert reordered >= 4
    names = {"answer:tags:key_only", "answer:tags:overlap", "answer:tags:stored_sorted_then_request_order",
             "answer:insert_unknown_landed_tags"}
    by_name = {case["name"]: case for case in _cases()}
    for name in names:
        tags = by_name[name]["facts"]["key"]["tags"]
        assert len(tags) == 2 and tags == sorted(tags), name


def test_a_streams_reservation_is_its_estimate_for_now() -> None:
    """Go passes a stream to Python (body.stream); Python's typed store
    reserved the whole budget's price, where the same non-stream request
    reserves at most HOLD_OUTPUT_TOKENS of it (§5, "The two figures")."""
    by_name = {case["name"]: case["expect"] for case in [*_cases("answer"), *_cases("pass")]}
    flash = "deepseek/deepseek-v4-flash:1000+131072"
    answered = by_name[f"answer:estimate:hold_cap:{flash}"]
    streamed = by_name[f"pass:body.stream:hold_cap:{flash}"]["python"]
    assert streamed["status"] == 200 and not streamed["leased"]
    assert streamed["reservation_microdollars"] == answered["estimate_microdollars"] > answered["reservation_microdollars"]
    at_the_bound = by_name["pass:body.stream:hold_cap:deepseek/deepseek-v4-flash:1000+16384"]["python"]
    assert at_the_bound["reservation_microdollars"] == answered["reservation_microdollars"]
