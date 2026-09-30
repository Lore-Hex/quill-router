"""Frozen cross-language oracles. Never import or execute the fixture generator."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from trusted_router import speculation_protocol as protocol

FIXTURES = Path(__file__).parent / "fixtures" / "speculation_v1"
MANIFEST_SHA256 = "cf4961055cc4d7944a16b6160860b34fab05d4c8966ce3b19cfd041a601da648"


def read(name: str) -> Any:
    return json.loads((FIXTURES / name).read_bytes())


BUNDLE = read("grant-permit-tokens.json")
CASES = read("protocol-vectors.json")["cases"]
KEYS = tuple(protocol.TrustedKey(**key) for key in BUNDLE["trusted_test_keys"])
WIRE = (FIXTURES / "provider-wire.json").read_bytes()[:-1]


def grant(shadow: bool = False) -> protocol.VerifiedGrant:
    return protocol.verify_grant(BUNDLE["shadow_grant_jws" if shadow else "real_grant_jws"],
                                 KEYS, BUNDLE["context"], BUNDLE["now"], shadow=shadow)


def descriptor() -> protocol.VerifiedDescriptor:
    return protocol.verify_descriptor(BUNDLE["permit_descriptor_jws"], KEYS, grant(), WIRE, "x1", "n1")


def test_fixture_pins() -> None:
    assert hashlib.sha256((FIXTURES / "manifest.json").read_bytes()).hexdigest() == MANIFEST_SHA256, "manifest pin mismatch"
    for name, digest in read("manifest.json")["files"].items():
        raw = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest, f"fixture pin mismatch: {name}"
        assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")


def test_seed_contract() -> None:
    real, shadow, d = grant(), grant(True), descriptor()
    assert real.claims == BUNDLE["grant_claims"]
    assert shadow.claims == BUNDLE["grant_claims"]
    assert d.claims == BUNDLE["permit_descriptor_claims"]
    expected = BUNDLE["expected"]
    assert expected == {
        "b_micro": 4608, "customer_settlement_mode": "ordinary",
        "descriptor_signature_valid": True, "real_signature_valid": True,
        "real_start_allowed": True, "shadow_dispatch_allowed": False,
        "shadow_signature_valid": True, "start_at_1700000027": True,
        "start_at_1700000028": False, "workspace_allowance_micro": 250000,
    }
    assert protocol.cost_ceiling(8192, 500000, 512, 1000000, 0) == expected["b_micro"]
    assert protocol.workspace_allowance(25000000, 5000000) == expected["workspace_allowance_micro"]
    assert protocol.sha256(WIRE) == d.claims["request_sha256"]
    assert protocol.sha256(real.compact.encode()) == d.claims["grant_sha256"]
    assert protocol.sha256(d.compact.encode()) == BUNDLE["speculation_accepted"]["descriptor_sha256"]
    assert real.start_deadline == 1700000028
    real.claims["key_id"] = "mutated"
    assert real.claims["key_id"] == "k1"


def evaluate(case: dict[str, Any]) -> Any:
    category = case["category"]
    if category == "verdict_extra":
        return protocol.classify_verdict(**case["input"])
    if category == "grant":
        protocol.verify_grant(case["token"], tuple(protocol.TrustedKey(**k) for k in case["keys"]) if "keys" in case else KEYS, case["context"], case["now"], shadow=case["shadow"])
        return "allowed"
    if category == "descriptor":
        protocol.verify_descriptor(case["token"], KEYS, grant(case["grant"] == "shadow"),
                                   case["wire"].encode(), case["execution"], case["nonce"])
        return "allowed"
    if category == "marker":
        return protocol.verify_acceptance(case["response"], descriptor(), case["authorization"])
    if category == "cost":
        return protocol.cost_ceiling(*case["args"])
    if category == "allowance":
        return protocol.workspace_allowance(*case["args"])
    if category == "renewal":
        previous = protocol.verify_grant(case["previous"], KEYS, case["previous_context"], 1700000000)
        candidate = protocol.verify_grant(case["candidate"], KEYS, case["candidate_context"],
                                          1700000000, shadow=case["shadow"])
        return protocol.renewal_verdict(previous, candidate)
    if category == "replay":
        candidate_d = protocol.verify_descriptor(case["candidate"], KEYS, grant(), WIRE,
                                                 case["execution"], case["nonce"])
        return protocol.descriptor_replay(descriptor(), candidate_d)
    raise AssertionError(f"unknown fixture category {category}")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_literal(case: dict[str, Any]) -> None:
    try:
        actual = evaluate(case)
    except protocol.ProtocolError as exc:
        actual = str(exc)
    assert actual == case["expected"], f"{case['name']}: expected {case['expected']!r}, got {actual!r}"


@pytest.mark.parametrize("vector", read("verdict-vectors.json")["vectors"], ids=lambda v: v["name"])
def test_verdict(vector: dict[str, Any]) -> None:
    actual = protocol.classify_verdict(**vector["input"])
    for field, expected in vector["expected"].items():
        assert actual[field] == expected, f"{vector['name']}.{field}: expected {expected!r}, got {actual[field]!r}"


def test_rules_cover_every_literal() -> None:
    rules = read("rules.json")["rules"]
    named = {r["literal_case"] for r in rules}
    assert {c["name"] for c in CASES} <= named
    assert {v["name"] for v in read("verdict-vectors.json")["vectors"]} <= named
    assert "fixture_pins" in named
    assert all(r["guard"] and r["mutation"] for r in rules)
    assert len({c["name"] for c in CASES}) == len(CASES)


def test_no_runtime_call_sites() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        if path.name != "speculation_protocol.py":
            assert "speculation_protocol" not in path.read_text(), str(path)
