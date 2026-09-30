"""Frozen cross-language oracles. Never import or execute the fixture generator."""
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from trusted_router import speculation_protocol as protocol

FIXTURES = Path(__file__).parent / "fixtures" / "speculation_v1"
MANIFEST_SHA256 = "ef6cca49eecdea14f47e4419bc1e1543409a22cf715c6cbe1555c8a82701f603"


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
    if category == "input":
        return protocol.sha256(case["value"])
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
        candidate = protocol.verify_grant(case["candidate"], tuple(protocol.TrustedKey(**k) for k in case["candidate_keys"]) if "candidate_keys" in case else KEYS, case["candidate_context"],
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
    except Exception as exc:
        raise AssertionError(f"unexpected runtime exception: {type(exc).__name__}: {exc}") from exc
    assert actual == case["expected"], f"{case['name']}: expected {case['expected']!r}, got {actual!r}"


@pytest.mark.parametrize("vector", read("verdict-vectors.json")["vectors"], ids=lambda v: v["name"])
def test_verdict(vector: dict[str, Any]) -> None:
    actual = protocol.classify_verdict(**vector["input"])
    for field, expected in vector["expected"].items():
        assert actual[field] == expected, f"{vector['name']}.{field}: expected {expected!r}, got {actual[field]!r}"


def test_guard_inventory() -> None:
    import ast
    import inspect

    rules = read("rules.json")["rules"]
    cases = {c["name"] for c in CASES} | {"verdict:" + v["name"] for v in read("verdict-vectors.json")["vectors"]} | {"fixture_pins"}
    cases |= {name for name in globals() if name.startswith("test_")}
    assert all(r["literal_case"] in cases for r in rules)
    assert all(r["before"] != r["after"] for r in rules)
    assert len({r["guard"] for r in rules}) == len(rules)
    assert len({c["name"] for c in CASES}) == len(CASES)
    source = inspect.getsource(protocol)
    # A new require or branch cannot silently escape the executable inventory.
    for function in ast.parse(source).body:
        if not isinstance(function, ast.FunctionDef):
            continue
        anchors = [r["before"] for r in rules if r["function"] == function.name]
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_require":
                assert ast.get_source_segment(source, node) in anchors
            if isinstance(node, (ast.If, ast.IfExp)):
                predicate = ast.get_source_segment(source, node.test)
                assert any(predicate in anchor or anchor in predicate for anchor in anchors), (function.name, predicate)


def test_no_runtime_call_sites() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        if path.name != "speculation_protocol.py":
            assert "speculation_protocol" not in path.read_text(), str(path)


def test_numeric_limits_are_interpreter_independent() -> None:
    import sys

    original = sys.get_int_max_str_digits()
    try:
        for limit in (640, 4300, 0):
            sys.set_int_max_str_digits(limit)
            for name in ('header_number_huge', 'payload_number_huge'):
                test_literal(next(c for c in CASES if c['name'] == name))
    finally:
        sys.set_int_max_str_digits(original)


@contextmanager
def equality_work_budget(limit: int = 100_000) -> Iterator[None]:
    """Bound comparison work deterministically, including under cycle/DAG mutants."""
    previous = sys.gettrace()
    lines = 0
    code = protocol._equal.__code__

    def trace(frame: Any, event: str, arg: Any) -> Any:
        nonlocal lines
        if frame.f_code is code:
            if event == "call":
                lines = 0
            elif event == "line":
                lines += 1
                assert lines <= limit, "comparison exceeded its linear work budget"
            return trace
        return None

    sys.settrace(trace)
    try:
        yield
    finally:
        sys.settrace(previous)


def test_acceptance_depth_5000() -> None:
    left: Any = 0
    right: Any = 0
    for _ in range(5000):
        left, right = {"items": [left]}, {"items": [right]}
    d = descriptor()
    auth = {**BUNDLE["authorization"], "extra": left}
    response_auth = {**BUNDLE["authorization"], "extra": right}
    for marked in (False, True):
        for reversed_order in (False, True):
            response: dict[str, Any] = {"authorization": response_auth}
            if marked:
                response["speculation_accepted"] = BUNDLE["speculation_accepted"]
            assert protocol.verify_acceptance(response, d, auth) == (
                "accepted" if marked else "ordinary")
            response_auth["authorization_id"] = "different"
            with pytest.raises(protocol.ProtocolError, match="^authorization$"):
                protocol.verify_acceptance(response, d, auth)
            response_auth["authorization_id"] = auth["authorization_id"]
            if not reversed_order:
                response_auth = dict(reversed(list(response_auth.items())))


def test_acceptance_shared_subtrees() -> None:
    # Separate DAGs: identity equality cannot substitute for structural equality.
    left: Any = 0
    right: Any = 0
    for _ in range(64):
        left, right = [left, left], [right, right]
        left, right = {"a": left, "b": left}, {"b": right, "a": right}
    d = descriptor()
    auth = {**BUNDLE["authorization"], "extra": left}
    response_auth = {**BUNDLE["authorization"], "extra": right}
    with equality_work_budget(20_000):
        assert protocol.verify_acceptance({"authorization": response_auth}, d, auth) == "ordinary"
        assert protocol.verify_acceptance({"authorization": auth}, d, auth) == "ordinary"
        # Shared left child must still be compared to EACH distinct right child.
        response_auth["extra"] = [{"a": [1], "b": [1]}, right]
        auth["extra"] = [left, left]
        with pytest.raises(protocol.ProtocolError, match="^authorization$"):
            protocol.verify_acceptance({"authorization": response_auth}, d, auth)
        with pytest.raises(protocol.ProtocolError, match="^authorization$"):
            protocol.verify_acceptance({"authorization": auth}, d, response_auth)
    # The budget trace temporarily replaces coverage's tracer; observe the hit too.
    assert protocol._equal(left, right) is True


def test_equality_cycles_and_non_json_values() -> None:
    left: list[Any] = []
    right: list[Any] = []
    left.append(left)
    right.append(right)
    left_dict: dict[str, Any] = {}
    right_dict: dict[str, Any] = {}
    left_dict["self"] = left_dict
    right_dict["self"] = right_dict
    d = descriptor()
    with equality_work_budget():
        for a, b in ((left, right), (left_dict, right_dict), (left, left),
                     (left_dict, left_dict), (left, []), (object(), object())):
            assert protocol._equal(a, b) is False
            auth = {**BUNDLE["authorization"], "extra": a}
            response_auth = {**BUNDLE["authorization"], "extra": b}
            with pytest.raises(protocol.ProtocolError, match="^authorization$"):
                protocol.verify_acceptance({"authorization": response_auth}, d, auth)
        unsupported = object()
        assert protocol._equal(unsupported, unsupported) is False
        assert protocol._equal(None, None) is True
        assert protocol._equal({}, {}) is True
        assert protocol._equal([], []) is True
    # Observe cycle refusal with the coverage tracer restored, after bounding work.
    assert protocol._equal(left, right) is False


def test_public_input_fuzz() -> None:
    """Deterministic, bounded fuzz across every public function and parameter.

    The oracle is only crash freedom: valid return or ProtocolError. Frozen
    literals, never this fuzz corpus, define acceptance and refusal outcomes.
    """
    import copy
    import random

    rng = random.Random(29092026)  # noqa: S311 - reproducible adversarial inputs
    atoms: list[Any] = [None, True, False, 0, -1, 1, 1.0, float('nan'), float('inf'),
                        2**63, '', '\ud800', b'\xff', {}, [], object()]
    values = list(atoms)
    for _ in range(300):
        value = rng.choice(atoms)
        for _ in range(rng.randrange(5)):
            value = [value, rng.choice(atoms)] if rng.randrange(2) else {'x': value}
        values.append(value)
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    values.append(cyclic)
    g, d = grant(), descriptor()
    calls: list[tuple[Any, dict[str, Any]]] = [
        (protocol.sha256, {'raw': WIRE}),
        (protocol.cost_ceiling, dict(zip(('input_bound', 'input_rate_micro_per_m', 'output_limit', 'output_rate_micro_per_m', 'maximum_request_fees_micro'), (8192, 500000, 512, 1000000, 0), strict=True))),
        (protocol.workspace_allowance, {'tier_ceiling_micro': 25000000, 'paid_headroom_micro': 5000000}),
        (protocol.verify_grant, {'token': BUNDLE['real_grant_jws'], 'keys': KEYS, 'context': BUNDLE['context'], 'now': BUNDLE['now'], 'shadow': False}),
        (protocol.verify_descriptor, {'token': BUNDLE['permit_descriptor_jws'], 'keys': KEYS, 'grant': g, 'request_bytes': WIRE, 'execution_id': 'x1', 'invocation_nonce': 'n1'}),
        (protocol.verify_acceptance, {'response': {'authorization': BUNDLE['authorization'], 'speculation_accepted': BUNDLE['speculation_accepted']}, 'descriptor': d, 'authorization': BUNDLE['authorization']}),
        (protocol.renewal_verdict, {'previous': g, 'candidate': g}),
        (protocol.descriptor_replay, {'previous': d, 'candidate': d}),
        (protocol.classify_verdict, {'source': 'authenticated_router', 'status': 403, 'reason': '', 'workspace_id': 'w1', 'key_id': 'k1', 'rate_scope': None}),
    ]
    for function, defaults in calls:
        for field in defaults:
            for value in values:
                try:
                    function(**{**defaults, field: value})
                except protocol.ProtocolError:
                    pass
    for value in values:
        for verified in (protocol.VerifiedGrant('', value, False, 0), protocol.VerifiedDescriptor('', value)):
            try:
                _ = verified.claims
            except protocol.ProtocolError:
                pass
        response = {'authorization': {**BUNDLE['authorization'], 'extra': value}}
        with equality_work_budget():
            try:
                protocol.verify_acceptance(response, d, copy.deepcopy(response['authorization']))
            except protocol.ProtocolError:
                pass


def test_signed_parser_fuzz() -> None:
    import base64
    import random

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    rng = random.Random(30092026)  # noqa: S311 - deterministic fuzz, public test seed
    signer = Ed25519PrivateKey.from_private_bytes(bytes(range(128, 160)))
    header, payload, _ = BUNDLE['real_grant_jws'].split('.')
    raws = [rng.randbytes(rng.randrange(256)) for _ in range(1000)]
    raws += [b'[' * n + b'0' + b']' * n for n in (15, 16, 17, 1000)]
    raws += [b'{"v":' + b'9' * n + b'}' for n in (19, 20, 639, 640, 4300, 5000)]
    for raw in raws:
        segment = base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
        for h, p in ((segment, payload), (header, segment)):
            message = (h + '.' + p).encode('ascii')
            signature = base64.urlsafe_b64encode(signer.sign(message)).rstrip(b'=')
            try:
                protocol.verify_grant((message + b'.' + signature).decode('ascii'), KEYS, BUNDLE['context'], BUNDLE['now'])
            except protocol.ProtocolError:
                pass
