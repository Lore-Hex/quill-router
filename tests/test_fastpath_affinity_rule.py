"""The generator's affinity rule (scripts/fastpath/affinity_rule.py, G13,
§10.1): a warm answer the goldens record must be routing_state.rank's
re-rank of Python's cold order, provider.order and the sort partition
respected, with nothing but the first candidate's members changed. The
goldens' own answers pass; forbidden answers, built from a golden's cold
answer with every first-candidate member rendered consistently, are
refused."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.fastpath import affinity_rule

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "fastpath" / "testdata" / "authorize_cases_v1.json"
ASYNC_CASES = ROOT / "fastpath" / "testdata" / "authorize_async_cases_v1.json"


def _affinity_cases() -> dict[str, dict[str, Any]]:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    return {c["name"]: c for c in cases if "affinity" in c["expect"]}


def _model_of(endpoint_id: str) -> str:
    """An endpoint id is <model>@<provider>/<tier> (the catalog's); the
    generator reads the catalog, which a remembered endpoint outside the
    cold candidates (fallbacks off) needs."""
    return endpoint_id.split("@", 1)[0]


def _classify(case: dict[str, Any], warm_response: str, sessions: list[list[Any]] | None = None,
              expected_async: tuple[dict[str, Any], dict[str, Any]] | None = None) -> str:
    a = case["expect"]["affinity"]
    return affinity_rule.affinity_class(
        case["name"], json.loads(case["request"]["body"]), a["cold_response"], warm_response,
        [tuple(s) for s in (a["sessions"] if sessions is None else sessions)], _model_of, expected_async)


def _reordered(case: dict[str, Any], order: list[str]) -> str:
    """The cold answer in another candidate order, every first-candidate
    member rendered from the new first candidate, as the 200 writes them."""
    cold = json.loads(case["expect"]["affinity"]["cold_response"])
    by_id = {c["endpoint_id"]: c for c in cold["data"]["route_candidates"]}
    data = copy.deepcopy(cold["data"])
    data["route_candidates"] = [by_id[endpoint_id] for endpoint_id in order]
    first = by_id[order[0]]
    for member in ("model", "upstream_model", "endpoint_id", "provider", "provider_name", "inference_location"):
        data[member] = first[member]
    data.pop("wafer_zdr_required", None)
    if first["provider"] == "wafer":
        data["wafer_zdr_required"] = True
    return json.dumps({"data": data}, separators=(",", ":"))


def test_every_golden_affinity_answer_is_ranks_order() -> None:
    cases = _affinity_cases()
    assert len(cases) >= 18
    for case in cases.values():
        assert _classify(case, case["expect"]["affinity"]["response"]) == case["expect"]["affinity"]["class"], case["name"]


def test_a_promotion_under_a_provider_order_is_refused() -> None:
    case = _affinity_cases()["pass:body.cache_affinity:cold:order"]
    cold = case["expect"]["affinity"]["cold_candidates"]
    forbidden = _reordered(case, [cold[1], cold[0], cold[2]])
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, forbidden)
    assert affinity_rule.provider_orders(json.loads(case["request"]["body"]))


def test_a_move_across_models_under_the_default_partition_is_refused() -> None:
    cases = _affinity_cases()
    case = cases["pass:body.cache_affinity:warm:fallback_models"]
    cold = case["expect"]["affinity"]["cold_candidates"]
    across = _reordered(case, [cold[3], *cold[:3], *cold[4:]])
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, across)
    # Under partition none the same move is rank's order (the golden), and
    # the within-run order is then refused.
    none = cases["pass:body.cache_affinity:warm:partition_none:fallback_models"]
    assert _classify(none, none["expect"]["affinity"]["response"]) == "affinity_warm"
    cold = none["expect"]["affinity"]["cold_candidates"]
    within = _reordered(none, [cold[0], cold[3], cold[1], cold[2], cold[4]])
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(none, within)


def test_an_expired_or_overwritten_session_keeps_the_cold_order() -> None:
    case = _affinity_cases()["pass:body.cache_affinity:warm"]
    a = case["expect"]["affinity"]
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, a["response"], sessions=[[a["sessions"][0][0], 600.0]])
    assert _classify(case, a["cold_response"], sessions=[[a["sessions"][0][0], 600.0]]) == "affinity_cold"
    # remember overwrites the session: the last remembered endpoint wins.
    other = next(e for e in a["cold_candidates"] if e not in (a["sessions"][0][0], a["cold_candidates"][0]))
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, a["response"], sessions=[[a["sessions"][0][0], 1.0], [other, 1.0]])


def test_anything_but_the_first_candidates_members_is_refused() -> None:
    case = _affinity_cases()["pass:body.cache_affinity:warm"]
    warm = json.loads(case["expect"]["affinity"]["response"])
    warm["data"]["tags"] = {"x": "y"}
    with pytest.raises(AssertionError, match="changed \\['tags'\\]"):
        _classify(case, json.dumps(warm, separators=(",", ":")))
    # With fallbacks on the estimate is the same largest over the same set.
    warm = json.loads(case["expect"]["affinity"]["response"])
    warm["data"]["estimated_cost_microdollars"] += 1
    with pytest.raises(AssertionError, match="estimated_cost_microdollars"):
        _classify(case, json.dumps(warm, separators=(",", ":")))


def _async_affinity(shapes: str) -> tuple[dict[str, Any], tuple[dict[str, Any], dict[str, Any]]]:
    """An async golden affinity case by its shapes (warm_A.cold_B, say),
    and Python's additions for its cold and warm answers: the generator
    held each answer to authorize_additions over the authorization Python
    wrote and that answer's candidates, so the recorded members are them."""
    vectors = json.loads(ASYNC_CASES.read_text(encoding="utf-8"))
    (case,) = [c for c in vectors["cases"] if "affinity" in c["expect"] and f":async.{shapes}:" in c["name"]]
    a = case["expect"]["affinity"]
    return case, (affinity_rule.async_members(json.loads(a["cold_response"])["data"]),
                  affinity_rule.async_members(json.loads(a["response"])["data"]))


def _with_async(response: str, members: dict[str, Any]) -> str:
    """The answer with its async members replaced, in their place before tags."""
    return json.dumps({"data": _data_with(json.loads(response)["data"], members)}, separators=(",", ":"))


def _data_with(data: dict[str, Any], members: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "tags":
            out.update(members)
        if key not in affinity_rule.ASYNC_MEMBERS:
            out[key] = value
    return out


def _hashed(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(affinity_rule.canonical(snapshot)).hexdigest()


def test_async_members_change_only_with_the_candidate_set() -> None:
    """The async members are built from the candidates kept, sorted: the
    async golden's warm answer keeps another endpoint (fallbacks off) and
    changes them in kind (the G15 design's §4.4); a re-rank of the same set
    keeps them."""
    case, expected = _async_affinity("warm_A.cold_B")
    a = case["expect"]["affinity"]
    assert set(a["candidates"]) != set(a["cold_candidates"])
    assert _classify(case, a["response"], expected_async=expected) == "affinity_warm"
    with pytest.raises(AssertionError, match="no way to rebuild"):
        _classify(case, a["response"])
    same, same_expected = _async_affinity("warm_B.cold_B")
    b = same["expect"]["affinity"]
    assert set(b["candidates"]) == set(b["cold_candidates"]) and b["candidates"] != b["cold_candidates"]
    assert _classify(same, b["response"], expected_async=same_expected) == "affinity_warm"
    plain = _affinity_cases()["pass:body.cache_affinity:warm:fallback_models"]
    warm = json.loads(plain["expect"]["affinity"]["response"])
    warm["data"]["async_eligible"] = False
    with pytest.raises(AssertionError, match="async members, and no way to rebuild them"):
        _classify(plain, json.dumps(warm, separators=(",", ":")))
    with pytest.raises(AssertionError, match="warm answer's async members are not Python's"):
        _classify(plain, json.dumps(warm, separators=(",", ":")), expected_async=({}, {}))


def test_a_warm_answers_async_members_are_pythons_for_its_candidates() -> None:
    """The warm answer over the remembered azure endpoint, which Candidate
    refuses: Python's additions are shape A, so neither async_eligible true,
    nor a stray hash, nor the cold snapshot riding along passes."""
    case, expected = _async_affinity("warm_A.cold_B")
    a = case["expect"]["affinity"]
    cold = json.loads(a["cold_response"])["data"]
    snapshot, digest = cold["billing_snapshot"], cold["billing_snapshot_hash"]
    for members in ({"async_eligible": True}, {"async_eligible": False, "billing_snapshot_hash": "0" * 64},
                    {"async_eligible": False, "billing_snapshot": snapshot, "billing_snapshot_hash": digest}):
        with pytest.raises(AssertionError, match="warm answer's async members are not Python's"):
            _classify(case, _with_async(a["response"], members), expected_async=expected)


@pytest.mark.parametrize("version", [True, 1.0])
def test_a_snapshot_is_compared_as_canonical_bytes_even_when_the_set_is_kept(version: Any) -> None:
    """v true or 1.0 equals v 1 as a Python value, so a same-set re-rank sees
    nothing changed; the canonical bytes differ, and the hash is recomputed
    from them, so the retained hash fails and a recomputed one is still not
    Python's snapshot."""
    for shapes in ("warm_B.cold_B", "warm_A.cold_B"):
        case, expected = _async_affinity(shapes)
        a = case["expect"]["affinity"]
        cold = json.loads(a["cold_response"])["data"]
        # Over the set-keeping case, the warm answer; over the other, Python's
        # B answer swapped in as the cold one checked against its expectation.
        target = json.loads(a["response"])["data"] if shapes == "warm_B.cold_B" else cold
        retyped = {**target["billing_snapshot"], "v": version}
        assert retyped == target["billing_snapshot"]
        label, want = ("warm", expected[1]) if shapes == "warm_B.cold_B" else ("cold", expected[0])
        with pytest.raises(AssertionError, match=f"the {label} billing_snapshot_hash is not its snapshot's"):
            affinity_rule.check_async(case["name"], label, {**target, "billing_snapshot": retyped}, want)
        with pytest.raises(AssertionError, match=f"the {label} answer's async members are not Python's"):
            affinity_rule.check_async(case["name"], label, {**target, "billing_snapshot": retyped,
                                                            "billing_snapshot_hash": _hashed(retyped)}, want)
    case, expected = _async_affinity("warm_B.cold_B")
    a = case["expect"]["affinity"]
    warm = json.loads(a["response"])["data"]
    with pytest.raises(AssertionError, match="warm billing_snapshot_hash is not its snapshot's"):
        _classify(case, _with_async(a["response"], {
            "async_eligible": False, "billing_snapshot": {**warm["billing_snapshot"], "v": True},
            "billing_snapshot_hash": warm["billing_snapshot_hash"]}), expected_async=expected)
    with pytest.raises(AssertionError, match="warm billing_snapshot_hash is not its snapshot's"):
        _classify(case, _with_async(a["response"], {**affinity_rule.async_members(warm),
                                                     "billing_snapshot_hash": "0" * 64}), expected_async=expected)


def _python_additions(*, tools: bool = False, **authorization_changes: Any) -> dict[str, Any]:
    """Python's own authorize_additions (admission off) over the async-settle
    tests' authorization, body and endpoint, with these changes."""
    from tests.test_async_settle_admission import body, request
    from tests.test_async_settle_ticket import authorization, endpoint, runtime, settings
    from trusted_router.services.async_settle import authorize_additions

    auth = authorization()
    for name, value in authorization_changes.items():
        setattr(auth, name, value)
    additions: dict[str, Any] = authorize_additions(
        request=request(runtime()), body=body(requested_parameters=["tools"]) if tools else body(),
        authorization=auth, endpoints=[endpoint()],
        settings=settings(async_settle_enabled=False, async_settle_protection=False),
        typed=True, federated=False, replay=False, effective_at=None)
    return additions


def _answer(members: dict[str, Any]) -> dict[str, Any]:
    return {"stage_d": {}, **members, "tags": {}}


def test_the_check_follows_pythons_additions_not_a_reimplementation() -> None:
    """Whatever Python's own code answers is what an answer must carry:
    tools requested make both answers shape A (the genuine A accepted, a
    forged B with a correct hash refused); a markup of -1, which Eligibility
    refuses to validate, is caught by Python and answers false only; a
    markup of false is whatever Python makes of it."""
    eligible = _python_additions()
    assert list(eligible) == ["async_eligible", "billing_snapshot", "billing_snapshot_hash"]
    forged_b, false_only = _answer(eligible), _answer({"async_eligible": False})
    for label, expected in (("tools", _python_additions(tools=True)),
                            ("markup -1", _python_additions(app_markup_basis_points=-1))):
        assert expected == {"async_eligible": False}, label
        affinity_rule.check_async(label, "warm", false_only, expected)
        with pytest.raises(AssertionError, match="warm answer's async members are not Python's"):
            affinity_rule.check_async(label, "warm", forged_b, expected)
    as_false = _python_additions(app_markup_basis_points=False)
    affinity_rule.check_async("markup false", "warm", _answer(as_false), as_false)
    other = false_only if "billing_snapshot" in as_false else forged_b
    with pytest.raises(AssertionError, match="warm answer's async members are not Python's"):
        affinity_rule.check_async("markup false", "warm", other, as_false)
    # The genuine A/A pair over the golden's candidates, swapped round (cold
    # over azure, warm over openai), with tools requested: accepted; the
    # warm answer forged to B, refused.
    case, _ = _async_affinity("warm_A.cold_B")
    a = case["expect"]["affinity"]
    azure, openai = json.loads(a["response"])["data"], json.loads(a["cold_response"])["data"]
    tools = _python_additions(tools=True)
    affinity_rule.check_async("tools", "cold", azure, tools)
    affinity_rule.check_async("tools", "warm", _data_with(openai, {"async_eligible": False}), tools)
    with pytest.raises(AssertionError, match="warm answer's async members are not Python's"):
        affinity_rule.check_async("tools", "warm", openai, tools)


def test_the_rules_inputs() -> None:
    assert not affinity_rule.provider_orders({"provider": {"order": []}})
    assert not affinity_rule.provider_orders({"provider": {"order": ""}})
    assert affinity_rule.provider_orders({"provider": {"order": "a, b"}})
    assert affinity_rule.provider_orders({"provider": {"order": ["fireworks"]}})
    assert affinity_rule.partition_by_model({"provider": {"sort": "price"}})
    assert not affinity_rule.partition_by_model({"provider": {"sort": {"sort": "price", "partition": "none"}}})
    assert affinity_rule.fallbacks_off({"allow_fallbacks": False}) and affinity_rule.fallbacks_off(
        {"provider": {"allow_fallbacks": False}})
    model_of = {"a1": "a", "a2": "a", "b1": "b"}.__getitem__
    assert affinity_rule.rerank(["a1", "a2", "b1"], "b1", True, model_of) == ["a1", "a2", "b1"]
    assert affinity_rule.rerank(["a1", "a2", "b1"], "b1", False, model_of) == ["b1", "a1", "a2"]
    assert affinity_rule.rerank(["a1", "a2", "b1"], "a2", True, model_of) == ["a2", "a1", "b1"]
