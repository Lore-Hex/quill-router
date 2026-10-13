"""P1 byte contract, immutable facts and three-purpose rejection witnesses."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router import async_settle_shadow_binding as binding
from trusted_router import async_settle_shadow_v2_a1_evidence as a1e
from trusted_router import async_settle_shadow_v2_a1_health as a1h
from trusted_router import async_settle_shadow_v2_a1_reconciliation as a1r
from trusted_router import async_settle_shadow_v2_certificate as cert
from trusted_router import async_settle_shadow_v2_contract as c
from trusted_router import async_settle_shadow_v2_evidence as e
from trusted_router import async_settle_shadow_v2_schema as s
from trusted_router import async_settle_ticket as ticket
from trusted_router.detached_jws import TrustedKey, b64decode, b64encode, canonical

ROOT = Path(__file__).parent / "fixtures/async_settlement"
FIXTURES = ROOT / "shadow_v2"


def strict_equal(left: Any, right: Any) -> bool:
    """JSON structural equality preserving every native type, including keys."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if all(type(k) is str for k in (*left, *right)):
            return left.keys() == right.keys() and all(strict_equal(v, right[k]) for k, v in left.items())
        return len(left) == len(right) and all(
            any(strict_equal(k, other) and strict_equal(v, right[other]) for other in right)
            for k, v in left.items())
    if type(left) in (list, tuple):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def assert_round_trip(schema: s.Schema, value: Any) -> None:
    raw = schema.serialize(value)
    decoded = schema.parse(raw)
    assert strict_equal(decoded, value)
    assert schema.serialize(decoded) == raw


def fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / (name + ".json")).read_bytes())["variants"]


def proof(index: int = 0) -> dict[str, Any]:
    return copy.deepcopy(fixture("exclusion_certificate")[index])


def signer() -> cert.ExclusionSigner:
    p = proof()
    return cert.ExclusionSigner(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(p["seed_hex"])), TrustedKey(**p["key"]))


def schema_cases() -> list[tuple[str, s.Schema, dict[str, Any]]]:
    # Discover nested exact objects from committed bytes, not synthesized outputs.
    schemas = {name: value for module in (s, c, e, cert) for name, value in vars(module).items() if isinstance(value, s.Schema)}
    schemas.update({"a1_" + name: value for module in (a1h, a1e, a1r) for name, value in vars(module).items() if isinstance(value, s.Schema) and id(value) not in {id(v) for v in schemas.values()}})
    known = {id(value) for value in schemas.values()}
    traversed: set[int] = set()
    def discover(label: str, check: Any) -> None:
        if id(check) in traversed:
            return
        traversed.add(id(check))
        if isinstance(check, s.Schema):
            if id(check) not in known:
                schemas[label] = check
                known.add(id(check))
            for field, child in check.fields.items():
                discover(label + "." + field, child)
        else:
            for cell in getattr(check, "__closure__", None) or ():
                if callable(cell.cell_contents):
                    discover(label + ".item", cell.cell_contents)
    for label, schema in list(schemas.items()):
        discover(label, schema)
    cases: dict[str, tuple[str, s.Schema, dict[str, Any]]] = {}
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for name, schema in schemas.items():
                if name not in cases and set(value) == set(schema.fields):
                    try:
                        schema(value)
                    except ValueError:
                        continue
                    cases[name] = (name, schema, value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for path in (*FIXTURES.glob("*.json"), *FIXTURES.with_name("shadow_v2_a1").glob("*.json")):
        walk(json.loads(path.read_bytes()))
    return sorted(cases.values(), key=lambda v: v[0])


CASES = schema_cases()


def test_all_named_fixture_files_and_manifest_pin() -> None:
    names = "authorize_compared authorize_excluded exclusion_certificate terminal_compared_full terminal_compared_hash_only terminal_compared_failure terminal_excluded_settle terminal_excluded_refund capability_headers purpose_rejections wire_rejections health health_claim receipt_pages sample certificate_record counter closure controls proof_manifest prerequisite_ledger gate_outputs".split()
    manifest_bytes = (FIXTURES / "manifest.json").read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == c.FIXTURE_MANIFEST_SHA256
    manifest = json.loads(manifest_bytes)
    assert set(manifest["files"]) == {n + ".json" for n in names}
    assert {p.name for p in FIXTURES.iterdir()} == set(manifest["files"]) | {"manifest.json"}
    assert manifest["policy_sha256"] == hashlib.sha256(b"shadow-v2").hexdigest()
    for name, entry in manifest["files"].items():
        raw = (FIXTURES / name).read_bytes()
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        assert [v["name"] for v in json.loads(raw)["variants"]] == entry["variants"]


def test_existing_fixture_bytes_remain_frozen() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_bytes())
    for name, expected in manifest["legacy_files"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
    # Independent existing pins are repeated, never regenerated from the files.
    assert hashlib.sha256((ROOT / "shadow_v1.json").read_bytes()).hexdigest() == binding.FIXTURE_SHA256
    assert hashlib.sha256((ROOT / "authorize_v1.json").read_bytes()).hexdigest() == "3f8f7b08aeb89e35bdcecd85848fc5bb95b181e103f8473459af7065a79458e0"
    assert hashlib.sha256((ROOT / "billing_v1.json").read_bytes()).hexdigest() == "4aedf13e4ba30b4d1f0767f829e790c37ce3f957a39c15d1eb6aff8c8734fd81"


@pytest.mark.parametrize("name,schema,value", CASES, ids=[v[0] for v in CASES])
def test_exact_schema_acceptance_and_missing_extra(name: str, schema: s.Schema, value: dict[str, Any]) -> None:
    raw = canonical(value)
    assert strict_equal(schema.parse(raw), value)
    assert_round_trip(schema, value)
    assert schema.serialize(value) == raw
    for field in value:
        damaged = dict(value)
        del damaged[field]
        with pytest.raises(ValueError):
            schema.parse(canonical(damaged))
    with pytest.raises(ValueError):
        schema.parse(canonical({**value, "extra": None}))


@pytest.mark.parametrize("name,schema,value", CASES, ids=[v[0] for v in CASES])
def test_every_object_byte_limit_and_duplicate_keys(name: str, schema: s.Schema, value: dict[str, Any]) -> None:
    raw = canonical(value)
    # All actual serialized bytes count, including transport whitespace.
    assert strict_equal(schema.parse(raw + b" " * (schema.limit - len(raw))), value)
    with pytest.raises(ValueError, match="evidence_size"):
        schema.parse(raw + b" " * (schema.limit + 1 - len(raw)))
    key = next(iter(value))
    duplicate = b"{" + canonical(key) + b":" + canonical(value[key]) + b"," + raw[1:]
    with pytest.raises(ValueError):
        schema.parse(duplicate)


@pytest.mark.parametrize("name,schema,value", CASES, ids=[v[0] for v in CASES])
def test_every_object_wrong_lexemes(name: str, schema: s.Schema, value: dict[str, Any]) -> None:
    raw = canonical(value)
    key = next(iter(value))
    escaped_key = f'"\\u{ord(key[0]):04x}{key[1:]}"'
    with pytest.raises(ValueError):
        schema.parse(raw.replace(canonical(key), escaped_key.encode(), 1))
    for field, original in value.items():
        if type(original) is bool:
            replacements = [int(original), str(original).lower()]
        elif type(original) is int:
            replacements = [True, str(original), float(original), -1]
        else:
            continue
        for replacement in replacements:
            with pytest.raises(ValueError):
                schema.parse(canonical({**value, field: replacement}))


@pytest.mark.parametrize("lexeme", [b"2.0", b"2e0", b"-0", b"02", b"NaN", b"Infinity", b"9223372036854775808", b"1" * 1000])
def test_numeric_lexemes(lexeme: bytes) -> None:
    with pytest.raises(ValueError):
        s.decode(b'{"n":' + lexeme + b"}", 2048)


@pytest.mark.parametrize("name", ["terminal_compared_full", "terminal_compared_hash_only", "terminal_compared_failure", "terminal_excluded_settle", "terminal_excluded_refund"])
def test_terminal_wire_literal_byte_echo_compatibility(name: str) -> None:
    v = fixture(name)[0]
    assert canonical(v["object"]).decode() == v["canonical"]
    assert b64encode(v["canonical"].encode()) == v["header"]
    assert strict_equal(c.parse_header((v["header"],)), v["object"])
    assert c.parse_header((v["header"],)) == c.parse_terminal(b64decode(v["header"]))
    if v["object"]["variant"] == "excluded":
        assert v["object"]["certificate"] == proof()["certificate"]
        assert "original_receipt" not in v["object"]
    for headers in ((), (v["header"], v["header"]), (v["header"] + "=",)):
        with pytest.raises(ValueError):
            c.parse_header(headers)


def test_frozen_expected_amount_and_negative_controls() -> None:
    v = fixture("terminal_compared_full")[0]
    # Independent integer calculation: half-up each one-token component at 500k.
    expected = (500000 + 500000) // 1000000 + (500000 + 500000) // 1000000
    assert expected == v["expected_micro"] == v["object"]["terminal"]["charge_micro"] == 2
    sample = fixture("sample")[0]["object"]
    for amount in v["negative_controls_micro"]:
        with pytest.raises(ValueError):
            e.SAMPLE({**sample, "go_micro": amount})


def test_cycle_free_signing_and_durable_backing() -> None:
    p = proof()
    ev = p["event"]
    assert ev["decision"] == {k: p["claims"][k] for k in ("reason", "facts_sha256", "candidate_set_sha256")}
    assert "certificate_sha256" not in ev and "original_receipt" not in ev
    assert s.sha256(ev) == p["claims"]["original_receipt"]["event_sha256"]
    assert signer().issue(p["claims"], ev, p["now"], fresh=True, complete_candidates=True).certificate == p["certificate"]
    assert signer().sign(p["claims"], p["now"]) == p["certificate"]
    assert cert.verify_signature(p["certificate"], [signer().trusted], p["expected_identity"], p["now"]) == p["claims"]
    assert cert.verify_original_receipt(p["claims"], returned_key=tuple(p["page_key"]), page=canonical(p["page"])).partition == "B"
    assert cert.certificate_record(p["claims"], p["certificate"], "0" * 64, p["now"] * 1000000) == p["record"]
    assert signer().issue(p["claims"], ev, p["now"], fresh=False, complete_candidates=True).reason == "replay"


def foreign_verify(kind: str, token: str) -> None:
    p = proof()
    matrix = fixture("purpose_rejections")[0]
    # Each validator sees its own purpose descriptor, with the same public material;
    # rejection cannot be attributed to a missing key or different signing secret.
    kid = json.loads(b64decode(token.split(".")[0]))["kid"]
    if kind == "ticket":
        key = replace(signer().trusted, kid=kid, purpose=ticket.PURPOSE, aud="router-settlement")
        ticket.verify_ticket(token, [key], matrix["ticket_claims"], matrix["ticket_claims"]["iat"])
    elif kind == "lookup":
        key = replace(signer().trusted, kid=kid, purpose=ticket.PURPOSE, aud="router-settlement")
        ticket.verify_lookup_ticket(token, [key], p["now"] + 172801)
    elif kind == "binding":
        key = replace(signer().trusted, kid=kid, purpose=binding.PURPOSE, aud=binding.AUDIENCE)
        binding.verify_binding(token, [key], p["now"])
    else:
        key = replace(signer().trusted, kid=kid)
        cert.verify_signature(token, [key], p["expected_identity"], p["now"])


@pytest.mark.parametrize("validator,source", [(v, k) for v in ("ticket", "lookup", "binding", "certificate") for k in ("ticket", "binding", "certificate") if k != ("ticket" if v == "lookup" else v)])
def test_three_purpose_rejection_matrix(validator: str, source: str) -> None:
    with pytest.raises(ValueError):
        foreign_verify(validator, fixture("purpose_rejections")[0][source])


def test_certificate_as_ticket() -> None:
    with pytest.raises(ValueError):
        foreign_verify("ticket", proof()["certificate"])


@pytest.mark.parametrize("change", ["purpose", "audience", "issuer", "kid", "tampered"])
def test_certificate_key_descriptor_and_signature(change: str) -> None:
    p, key = proof(), signer().trusted
    token = p["certificate"]
    if change == "purpose":
        key = replace(key, purpose=ticket.PURPOSE)
    elif change == "audience":
        key = replace(key, aud="router-settlement")
    elif change == "issuer":
        key = replace(key, iss="wrong")
    elif change == "kid":
        key = replace(key, kid="wrong")
    else:
        h, payload, signature = token.split(".")
        signature = b64encode(bytes([b64decode(signature)[0] ^ 1]) + b64decode(signature)[1:])
        token = h + "." + payload + "." + signature
    with pytest.raises(ValueError):
        cert.verify_signature(token, [key], p["expected_identity"], p["now"])


@pytest.mark.parametrize("offset,accept", [(-1, False), (0, True), (172799, True), (172800, False)])
def test_certificate_lifetime(offset: int, accept: bool) -> None:
    p = proof()
    if accept:
        cert.verify_signature(p["certificate"], [signer().trusted], p["expected_identity"], p["now"] + offset)
    else:
        with pytest.raises(ValueError):
            cert.verify_signature(p["certificate"], [signer().trusted], p["expected_identity"], p["now"] + offset)


@pytest.mark.parametrize("field", list(e.IDENTITY_FIELDS))
def test_every_server_identity_is_bound(field: str) -> None:
    p = proof()
    expected = dict(p["expected_identity"])
    expected[field] = expected[field] + 1 if type(expected[field]) is int else expected[field] + "x"
    with pytest.raises(ValueError):
        cert.verify_signature(p["certificate"], [signer().trusted], expected, p["now"])


def ordinary() -> dict[str, Any]:
    facts = proof()["event"]["facts"]
    facts["receipt_requested"] = False
    return facts


def candidates(*adapters: str) -> bytes:
    return canonical([{"endpoint_id": "endpoint-" + str(i), "adapter": a, "features": []} for i, a in enumerate(adapters)])


@pytest.mark.parametrize("reason", sorted(c.COHORT_EXCLUSIONS - {"unknown_parameters"}))
def test_qualifying_reason_table(reason: str) -> None:
    f = ordinary()
    changes = {"untyped": ("settle_origin", "legacy"), "non_credits": ("usage_class", "BYOK"),
        "settlement_authority": ("billing_authority", "spend_lease"), "unsupported_route": ("route", "embeddings"),
        "service_tier": ("service_tier", "flex"), "app_markup": ("app_markup", 1),
        "custom_markup": ("custom_markup", 1), "receipt_fee": ("receipt_requested", True),
        "partner": ("commercial_program", "partner"), "unsupported_adapter": ("commercial_program", "partner")}
    if reason in changes:
        key, value = changes[reason]
        f[key] = value
    else:
        f["features"][reason] = True
    d = c.decide(c.RequestedFacts.from_object(f), reason, None, complete_candidates=False)
    assert d.eligibility == "excluded" and d.reason == reason and d.candidate_set_sha256 is None


@pytest.mark.parametrize("reason", sorted(c.NEVER_EXCLUSIONS))
def test_never_qualifying_reason_table(reason: str) -> None:
    # A failure never qualifies, even in the presence of an independent fact.
    d = c.decide(c.RequestedFacts.from_object(proof()["event"]["facts"]), reason, candidates("other"), complete_candidates=True)
    assert d.eligibility != "excluded"


def test_snapshot_unavailable_is_not_a_reason() -> None:
    assert "snapshot_unavailable" not in c.COHORT_EXCLUSIONS
    assert c.decide(c.RequestedFacts.from_object(ordinary()), "snapshot_unavailable", candidates("other"), complete_candidates=True).eligibility != "excluded"


@pytest.mark.parametrize("reason", ["partner", "unsupported_adapter", *c.DEPENDENT])
def test_candidate_dependent_reason_requires_every_candidate(reason: str) -> None:
    projection = json.loads(candidates("other", "other"))
    if reason in c.DEPENDENT:
        for candidate in projection:
            candidate["features"] = [reason]
    f = c.RequestedFacts.from_object(ordinary())
    d = c.decide(f, reason, canonical(projection), complete_candidates=True)
    assert d.eligibility == "excluded" and d.candidate_dependent and d.candidate_set_sha256 == s.sha256(projection)
    projection[1]["adapter"] = "openai"
    projection[1]["features"] = []
    assert c.decide(f, reason, canonical(projection), complete_candidates=True).eligibility != "excluded"


def test_eligible_fallback_is_never_excludable() -> None:
    f = c.RequestedFacts.from_object(ordinary())
    d = c.decide(f, "unsupported_adapter", candidates("other", "openai"), complete_candidates=True)
    assert d.eligibility == "eligible"
    assert c.decide(f, "unsupported_adapter", candidates("other", "openai"), complete_candidates=True, complete_supported_context=False).eligibility == "unknown"


@pytest.mark.parametrize("projection,complete", [(None, False), (candidates("other"), False), (None, True)])
def test_incomplete_projection_and_trimming_forbidden(projection: bytes | None, complete: bool) -> None:
    d = c.decide(c.RequestedFacts.from_object(ordinary()), "unsupported_adapter", projection, complete_candidates=complete)
    assert d.eligibility == "unknown"


def test_unknown_features_are_unresolved_until_policy_enum_exists() -> None:
    f = ordinary()
    assert c.decide(c.RequestedFacts.from_object(f), "unknown_parameters", candidates("openai"), complete_candidates=True).eligibility == "unknown"
    f["unknown_features"] = ["invented"]
    with pytest.raises(ValueError):
        c.RequestedFacts.from_object(f)


def test_requested_facts_are_immutable_owned_bytes() -> None:
    obj = ordinary()
    frozen = c.RequestedFacts.from_object(obj)
    obj["receipt_requested"] = True
    assert not frozen.object()["receipt_requested"]
    frozen.object()["features"]["fusion"] = True
    assert not frozen.object()["features"]["fusion"]


def test_complete_receipt_limit_2048_2049() -> None:
    at, over = proof(2), proof(3)
    assert len(canonical(at["event"])) == 2048 and len(canonical(over["event"])) == 2049
    assert signer().issue(at["claims"], at["event"], at["now"], fresh=True, complete_candidates=True).certificate == at["certificate"]
    result = signer().issue(over["claims"], over["event"], at["now"], fresh=True, complete_candidates=True)
    assert result.certificate is None and result.reason == "candidate_receipt_size"
    assert result.required_bytes == 2049 and result.candidate_count == 1


def test_32_candidate_4993_byte_projection_cannot_back_exclusion() -> None:
    v = proof(4)
    assert len(v["event"]["candidates"]) == 32
    assert len(canonical(v["event"]["candidates"])) == v["projection_bytes"] == 4993
    result = signer().issue(proof(1)["claims"], v["event"], proof()["now"], fresh=True, complete_candidates=True)
    assert result.reason == "candidate_receipt_size" and result.certificate is None and result.candidate_count == 32
    assert result.required_bytes == len(canonical(v["event"]))


def test_certificate_complete_encoded_size_not_individual_maxima() -> None:
    p = proof()
    # JWS budget counts unpadded base64, signature and both separators.
    for width in (1, 64):
        key = replace(signer().trusted, kid="k" * width)
        sign = cert.ExclusionSigner(signer().private, key)
        header_bytes = canonical({"alg": "EdDSA", "kid": key.kid, "typ": cert.TYP})
        assert len(header_bytes) == (69 if width == 1 else 132)
        claims = copy.deepcopy(p["claims"])
        target = 1401 if width == 1 else 1338
        # Use legitimate bounded identity fields; no padding claim or truncation.
        for field, limit in (("iss", 128), ("journal_region", 128), ("requested_route", 128), ("workspace_id", 64), ("key_id", 64), ("reservation_id", 64), ("invocation_nonce", 64)):
            addition = min(limit - len(claims[field]), target - len(canonical(claims)))
            claims[field] += "x" * max(0, addition)
        sign = cert.ExclusionSigner(sign.private, replace(sign.trusted, iss=claims["iss"]))
        assert len(canonical(claims)) == target
        token = sign.sign(claims, p["now"])
        assert len(token) == 2048
        assert len(b64encode(header_bytes)) + len(b64encode(canonical(claims))) + 86 + 2 == 2048
        claims["invocation_nonce"] += "x"
        with pytest.raises(ValueError):
            sign.sign(claims, p["now"])


def test_comparison_inline_and_local_limits_remain_separate() -> None:
    v = fixture("terminal_compared_full")[0]["object"]
    original = v["billing_snapshot"]
    projection = copy.deepcopy(original)
    model = projection["candidates"][0]
    projection["candidates"] = [{**model, "endpoint_id": f"endpoint-{i:03}"} for i in range(16)]
    assert 6144 < len(canonical(projection)) < 65536
    with pytest.raises(ValueError):
        c.COMPARED_FULL({**v, "billing_snapshot": projection})
    c.AUTHORIZE_COMPARED({"billing_shadow_policy": "shadow-v2", "billing_shadow_binding": v["billing_shadow_binding"], "billing_snapshot": projection})
    assert c.parse_terminal(canonical({k: val for k, val in v.items() if k != "billing_snapshot"}))


def test_candidate_input_count_and_byte_limits() -> None:
    f = c.RequestedFacts.from_object(ordinary())
    projection = json.loads(candidates(*(["other"] * 128)))
    assert c.decide(f, "unsupported_adapter", canonical(projection), complete_candidates=True).eligibility == "excluded"
    projection.append({"endpoint_id": "extra", "adapter": "other", "features": []})
    with pytest.raises(ValueError):
        c.decide(f, "unsupported_adapter", canonical(projection), complete_candidates=True)
    with pytest.raises(ValueError):
        c.decide(f, "unsupported_adapter", candidates("other") + b" " * 65536, complete_candidates=True)


@pytest.mark.parametrize("variant", fixture("wire_rejections"), ids=[v["name"] for v in fixture("wire_rejections")])
def test_committed_wire_rejections(variant: dict[str, Any]) -> None:
    if variant.get("target") == "verify_certificate":
        check_verification_vector(variant)
    elif variant["expected"] == "reject":
        with pytest.raises(ValueError, match="^" + variant["expected_gap"] + "$"):
            c.parse_terminal(variant["raw"].encode())
    else:
        assert c.parse_terminal(variant["raw"].encode())["variant"] == "excluded"
        assert c.parse_header((variant["header"],))["variant"] == "excluded"


def test_issuance_does_not_assume_supplied_candidates_are_complete() -> None:
    p = proof(1)
    result = signer().issue(p["claims"], p["event"], p["now"], fresh=True, complete_candidates=False)
    assert result.certificate is None and result.reason == "missing_context"


def test_purpose_matrix_positive_controls_and_expired_lookup() -> None:
    matrix = fixture("purpose_rejections")[0]
    for kind in ("ticket", "binding", "certificate"):
        token = matrix[kind]
        header, claims_raw, _ = token.split(".")
        claims = json.loads(b64decode(claims_raw))
        kid = json.loads(b64decode(header))["kid"]
        if kind == "ticket":
            key = TrustedKey(kid, ticket.PURPOSE, matrix["ticket_public_key"], claims["iss"], claims["aud"])
            ticket.verify_ticket(token, [key], claims, claims["iat"])
            ticket.verify_lookup_ticket(token, [key], claims["exp"] + 1)
        elif kind == "binding":
            # The historical binding fixture's deterministic TEST key lives in
            # its ticket fixture; it is the same public material used here.
            key = TrustedKey(kid, binding.PURPOSE, matrix["ticket_public_key"], claims["iss"], binding.AUDIENCE)
            binding.verify_binding(token, [key], claims["iat"])
        else:
            cert.verify_signature(token, [signer().trusted], proof()["expected_identity"], claims["iat"])


@pytest.mark.parametrize("damage", ["duplicate", "extra", "missing", "float", "bool", "escape", "lifetime", "generation"])
def test_signed_claims_refuse_malformed_raw_payload(damage: str) -> None:
    p = proof()
    claims = p["claims"]
    raw = canonical(claims)
    if damage == "duplicate":
        raw = b'{"v":2,' + raw[1:]
    elif damage == "extra":
        raw = canonical({**claims, "purpose": cert.PURPOSE})
    elif damage == "missing":
        raw = canonical({k: v for k, v in claims.items() if k != "original_receipt"})
    elif damage == "float":
        raw = raw.replace(b'"v":2', b'"v":2.0')
    elif damage == "bool":
        raw = raw.replace(b'"epoch":1', b'"epoch":true')
    elif damage == "escape":
        raw = raw.replace(b'"boot"', b'"bo\\u006ft"')
    elif damage == "lifetime":
        raw = canonical({**claims, "exp": claims["exp"] + 1})
    else:
        raw = canonical({**claims, "generation_id": "invented"})
    sign = signer()
    header = b64encode(canonical(p["protected_header"]))
    material = header + "." + b64encode(raw)
    token = material + "." + b64encode(sign.private.sign(material.encode()))
    with pytest.raises(ValueError):
        cert.verify_signature(token, [sign.trusted], p["expected_identity"], p["now"])


@pytest.mark.parametrize("variant", [v for v in fixture("exclusion_certificate") if "expected_signature" in v], ids=lambda v: v["name"])
def test_committed_maximum_claims_and_one_byte_over(variant: dict[str, Any]) -> None:
    keys = [TrustedKey(**variant["key"])]
    if variant["expected_signature"] == "accept":
        assert len(variant["certificate"]) == variant["certificate_bytes"] == 2048
        assert len(canonical(variant["claims"])) == variant["claim_bytes"]
        assert cert.verify_signature(variant["certificate"], keys, variant["expected_identity"], variant["now"]) == variant["claims"]
        with pytest.raises(ValueError, match="receipt_locator_size"):
            cert.verify_signature(variant["certificate"] + "A", keys, variant["expected_identity"], variant["now"])
    else:
        with pytest.raises(ValueError, match="^" + variant["expected_gap"] + "$"):
            cert.verify_signature(variant["certificate"], keys, variant["expected_identity"], variant["now"])


@pytest.mark.parametrize("variant", [v for name in ("receipt_pages", "certificate_record") for v in fixture(name) if "raw" in v], ids=lambda v: v["name"])
def test_committed_record_byte_boundaries(variant: dict[str, Any]) -> None:
    schema = getattr(e, variant["schema"])
    raw = variant["raw"].encode()
    assert len(raw) == variant["bytes"]
    if variant["expected"] == "accept":
        schema.parse(raw)
    else:
        with pytest.raises(ValueError, match="evidence_size"):
            schema.parse(raw)


@pytest.mark.parametrize("usage", ["BYOK", "Invoice"])
def test_noncredit_vocabulary_matches_frozen_v1_examples(usage: str) -> None:
    f = ordinary()
    f["usage_class"] = usage
    assert c.decide(c.RequestedFacts.from_object(f), "non_credits", None, complete_candidates=False).eligibility == "excluded"


def check_verification_vector(v: dict[str, Any]) -> None:
    def run() -> cert.ReceiptVerification:
        return cert.verify_certificate(v["certificate"], [TrustedKey(**v["key"])], v["expected_identity"], v["now"],
            returned_key=tuple(v["page_key"]), page=None if v["page"] is None else canonical(v["page"]),
            attempt=v["attempt"], server_attempt=v["server_attempt"], booking=v["booking"])
    if v.get("raises"):
        with pytest.raises(ValueError, match="^" + v["expected_gap"] + "$"):
            run()
    else:
        result = run()
        assert (result.partition, result.reason) == (v["expected_partition"], v["expected_gap"])


def verification_vector(name: str) -> dict[str, Any]:
    return next(v for v in fixture("wire_rejections") if v["name"] == name)


def test_repro_unknown_refund_actuals_cannot_be_represented() -> None:
    check_verification_vector(verification_vector("unknown-refund-actuals"))


def test_repro_server_refund_placeholder_conflicts() -> None:
    # P4 must normalize a known legacy placeholder to None, not claim it as fact.
    check_verification_vector(verification_vector("refund-placeholder-normalized-to-unknown"))
    check_verification_vector(verification_vector("conflicting-actual-stream"))


def test_signed_noncanonical_payload() -> None:
    check_verification_vector(verification_vector("signed-noncanonical-payload"))


@pytest.mark.parametrize("field", ["authorization_id", "workspace_id", "key_id"])
def test_repro_compared_identity_domain_narrowed(field: str) -> None:
    check_wire_domain(field, "native/id", True)


def check_wire_domain(field: str, value: Any, accepted: bool) -> None:
    from trusted_router import async_settle_shadow_wire as old_wire
    v = fixture("terminal_compared_full")[0]["object"]
    v["terminal"][field] = value
    old = {k: val for k, val in v.items() if k not in ("policy_version", "variant")}
    old["v"] = 1
    for run in (lambda: old_wire.parse_header((b64encode(canonical(old)),)), lambda: c.parse_terminal(canonical(v))):
        if accepted:
            assert run() is not None
        else:
            with pytest.raises(ValueError):
                run()


@pytest.mark.parametrize("field,limit,strict", [
    ("authorization_id", 64, False), ("workspace_id", 64, False), ("key_id", 64, False),
    ("invocation_nonce", 64, True), ("generation_id", 128, False), ("journal_region", 128, False),
    ("selected_endpoint", 128, False),
])
@pytest.mark.parametrize("character", list("A0_./:@+-"))
def test_v1_v2_wire_identity_acceptance_domain(field: str, limit: int, strict: bool, character: str) -> None:
    for width in (1, limit, limit + 1):
        check_wire_domain(field, character * width, width <= limit and (not strict or character in "A0_-"))
    check_wire_domain(field, "", False)


def test_repro_v1_valid_binding_allows_workspace_punctuation() -> None:
    matrix = fixture("purpose_rejections")[0]
    header, payload, _ = matrix["binding"].split(".")
    claims = json.loads(b64decode(payload))
    claims["workspace_id"] = "native/id"
    material = header + "." + b64encode(canonical(claims))
    token = material + "." + b64encode(signer().private.sign(material.encode()))
    key = replace(signer().trusted, kid=json.loads(b64decode(header))["kid"], purpose=binding.PURPOSE,
                  iss=claims["iss"], aud=binding.AUDIENCE)
    assert binding.verify_binding(token, [key], claims["iat"]).workspace_id == "native/id"
    check_wire_domain("workspace_id", "native/id", True)


def test_independent_fixture_signing_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import shadow_v2_fixture_generator as generator
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("production signer/serializer used to construct expected bytes")
    monkeypatch.setattr(cert.ExclusionSigner, "sign", forbidden)
    # Fixture generation must survive mutation of the implementation it certifies.
    generated = generator.build()
    assert generated["exclusion_certificate.json"]["variants"] == fixture("exclusion_certificate")
    # A second serializer: recursive minimal ASCII JSON, no json.dumps or
    # production canonicalizer. These fixtures contain only unescaped ASCII.
    def independent(value: Any) -> bytes:
        if value is None:
            return b"null"
        if type(value) is bool:
            return b"true" if value else b"false"
        if type(value) is int:
            return str(value).encode()
        if isinstance(value, str):
            assert all(32 <= ord(c) < 127 and c not in '\\"' for c in value)
            return b'"' + value.encode() + b'"'
        if isinstance(value, list):
            return b"[" + b",".join(independent(v) for v in value) + b"]"
        assert isinstance(value, dict)
        return b"{" + b",".join(independent(k) + b":" + independent(value[k]) for k in sorted(value)) + b"}"
    for v in fixture("exclusion_certificate"):
        if "signing_input" not in v:
            continue
        parts = v["certificate"].split(".")
        assert independent(json.loads(v["header_bytes"])) == v["header_bytes"].encode() == b64decode(parts[0])
        assert independent(v["claims"]) == v["payload_bytes"].encode() == b64decode(parts[1])
        assert ".".join(parts[:2]) == v["signing_input"]
        assert parts[2] == v["signature"]
        signer_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(proof()["seed_hex"])).public_key()
        signer_key.verify(b64decode(parts[2]), v["signing_input"].encode())


@pytest.mark.parametrize("change", ["observed-strings", "binding-empty", "binding-opaque", "usage-optional", "refund-charge"])
def test_v1_v2_remaining_wire_domains(change: str) -> None:
    from trusted_router import async_settle_shadow_wire as old_wire
    v = fixture("terminal_compared_full")[0]["object"]
    if change == "observed-strings":
        v["observed"].update(usage_type="", authority="not local", route_type="x" * 129, service_tier="")
    elif change.startswith("binding-"):
        v["billing_shadow_binding"] = "" if change == "binding-empty" else "opaque proof diagnostic"
    elif change == "usage-optional":
        v["raw_usage"] = {"input_tokens": 1, "output_tokens": 1}
    else:
        v["terminal"].update(terminal_kind="refund", charge_micro=2)
    old = {k: val for k, val in v.items() if k not in ("policy_version", "variant")}
    old["v"] = 1
    parsed = old_wire.parse_header((b64encode(canonical(old)),))
    assert c.parse_terminal(canonical(v)) == v
    if change == "refund-charge":
        assert parsed.invalid_refund_charge == 2


@pytest.mark.parametrize("field", ["workspace_id", "key_id", "reservation_id", "invocation_nonce", "authorization_id"])
@pytest.mark.parametrize("character", list("A0_./:@+-"))
def test_v1_binding_v2_claim_identity_domains(field: str, character: str) -> None:
    old = json.loads(b64decode(fixture("purpose_rejections")[0]["binding"].split(".")[1]))
    new = proof()["claims"]
    for width in (1, 64, 65):
        value = character * width
        old[field] = new[field] = value
        accepted = width <= 64 and (field not in ("invocation_nonce", "authorization_id") or character in "A0_-")
        if field == "authorization_id" and accepted:
            old["generation_id"] = new["generation_id"] = c.generation_id(value)
        for run in (lambda: binding.validate_claims(old, old["iat"]), lambda: cert.CLAIMS(new)):
            if accepted:
                run()
            else:
                with pytest.raises(ValueError):
                    run()


def test_required_shared_regression_vectors_are_present() -> None:
    names = {v["name"] for v in fixture("wire_rejections")}
    assert {"before-iat", "at-iat", "last-valid-second", "at-exp", "eligible-fallback",
            "signed-noncanonical-payload", "unknown-refund-actuals", "conflicting-actual-stream",
            "candidate-needs-actuals", "page-missing", "page-key-conflict", "page-body-conflict",
            "page-sequence-conflict", "signed-duplicate-header", "signed-duplicate-payload"} <= names
    assert {"server-identity-" + field for field in e.IDENTITY_FIELDS} <= names
    for v in fixture("wire_rejections"):
        assert "target" in v and "expected_gap" in v
        if v["target"] == "verify_certificate":
            assert {"now", "key", "expected_identity", "certificate", "page_key", "page", "attempt", "server_attempt", "booking"} <= set(v)
            assert set(v["expected_identity"]) == set(e.IDENTITY_FIELDS)


def run_isolated_vector(v: dict[str, Any]) -> None:
    """Only the supplied JSON object is available: no fixture/sibling lookup."""
    assert {"target", "expected_gap"} <= set(v)
    target = v["target"]
    gap = v["expected_gap"]
    if target == "purpose_matrix":
        assert gap == "per-case" and len(v["cases"]) == 8
        for case in v["cases"]:
            run_isolated_vector(case)
        return
    if target == "issue":
        local = cert.ExclusionSigner(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(v["seed_hex"])), TrustedKey(**v["key"]))
        assert v["signing_input"] + "." + v["signature"] == v["certificate"]
        result = local.issue(v["claims"], v["event"], v["now"], fresh=v["fresh"], complete_candidates=v["complete_candidates"])
        assert result.certificate is None and result.reason == gap
        return
    if target == "verify_certificate":
        check_verification_vector(v)
        return
    def run() -> Any:
        if target.startswith("foreign_"):
            keys = [TrustedKey(**v["key"])]
            token = v["certificate"]
            assert token.rsplit(".", 1)[0] == v["signing_input"]
            if target == "foreign_ticket":
                return ticket.verify_ticket(token, keys, v["expected_identity"], v["now"])
            if target == "foreign_lookup":
                return ticket.verify_lookup_ticket(token, keys, v["now"])
            if target == "foreign_binding":
                return binding.verify_binding(token, keys, v["now"])
            return cert.verify_signature(token, keys, v["expected_identity"], v["now"])
        if target == "verify_signature":
            assert v["certificate"].rsplit(".", 1)[0] == v["signing_input"]
            return cert.verify_signature(v["certificate"], [TrustedKey(**v["key"])], v["expected_identity"], v["now"])
        if target == "parse_terminal":
            return c.parse_terminal(v["raw"].encode())
        if target == "parse_header":
            return c.parse_header(tuple(v["headers"]))
        if target == "validate_row":
            return e.validate_row(v["kind"], v["row_key"], canonical(v["object"]))
        schema = getattr(e, target)
        return schema.parse(v["raw"].encode() if "raw" in v else canonical(v["object"]))
    with pytest.raises(ValueError) as caught:
        run()
    assert str(caught.value) == gap


ISOLATED_NEGATIVES = [v for path in sorted(FIXTURES.glob("*.json")) if path.name != "manifest.json"
                      for v in json.loads(path.read_bytes())["variants"]
                      if v.get("expected") in ("reject", "candidate_receipt_size") or v.get("expected_signature") == "reject"
                      or v.get("expected_gap") is not None]


@pytest.mark.parametrize("vector", ISOLATED_NEGATIVES, ids=lambda v: v["name"])
def test_isolated_negative_vector(vector: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "one-vector.json"
    path.write_bytes(canonical(vector))
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("isolated runner consulted sibling fixtures")
    monkeypatch.setattr(__import__(__name__, fromlist=["fixture"]), "fixture", forbidden)
    monkeypatch.setattr(__import__(__name__, fromlist=["proof"]), "proof", forbidden)
    monkeypatch.setattr(__import__(__name__, fromlist=["signer"]), "signer", forbidden)
    run_isolated_vector(json.loads(path.read_bytes()))
