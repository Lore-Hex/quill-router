"""The generator's affinity rule (scripts/fastpath/affinity_rule.py, G13,
§10.1): a warm answer the goldens record must be routing_state.rank's
re-rank of Python's cold order, provider.order and the sort partition
respected, with nothing but the first candidate's members changed. The
goldens' own answers pass; forbidden answers, built from a golden's cold
answer with every first-candidate member rendered consistently, are
refused."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.fastpath import affinity_rule

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "fastpath" / "testdata" / "authorize_cases_v1.json"


def _affinity_cases() -> dict[str, dict[str, Any]]:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    return {c["name"]: c for c in cases if "affinity" in c["expect"]}


def _model_of(endpoint_id: str) -> str:
    """An endpoint id is <model>@<provider>/<tier> (the catalog's); the
    generator reads the catalog, which a remembered endpoint outside the
    cold candidates (fallbacks off) needs."""
    return endpoint_id.split("@", 1)[0]


def _classify(case: dict[str, Any], warm_response: str, sessions: list[list[Any]] | None = None) -> str:
    a = case["expect"]["affinity"]
    return affinity_rule.affinity_class(
        case["name"], json.loads(case["request"]["body"]), a["cold_response"], warm_response,
        [tuple(s) for s in (a["sessions"] if sessions is None else sessions)], _model_of)


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
    case = _affinity_cases()["answer:cache_affinity:cold:order"]
    cold = case["expect"]["affinity"]["cold_candidates"]
    forbidden = _reordered(case, [cold[1], cold[0], cold[2]])
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, forbidden)
    assert affinity_rule.provider_orders(json.loads(case["request"]["body"]))


def test_a_move_across_models_under_the_default_partition_is_refused() -> None:
    cases = _affinity_cases()
    case = cases["answer:cache_affinity:warm:fallback_models"]
    cold = case["expect"]["affinity"]["cold_candidates"]
    across = _reordered(case, [cold[3], *cold[:3], *cold[4:]])
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, across)
    # Under partition none the same move is rank's order (the golden), and
    # the within-run order is then refused.
    none = cases["answer:cache_affinity:warm:partition_none:fallback_models"]
    assert _classify(none, none["expect"]["affinity"]["response"]) == "affinity_warm"
    cold = none["expect"]["affinity"]["cold_candidates"]
    within = _reordered(none, [cold[0], cold[3], cold[1], cold[2], cold[4]])
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(none, within)


def test_an_expired_or_overwritten_session_keeps_the_cold_order() -> None:
    case = _affinity_cases()["answer:cache_affinity:warm"]
    a = case["expect"]["affinity"]
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, a["response"], sessions=[[a["sessions"][0][0], 600.0]])
    assert _classify(case, a["cold_response"], sessions=[[a["sessions"][0][0], 600.0]]) == "affinity_cold"
    # remember overwrites the session: the last remembered endpoint wins.
    other = next(e for e in a["cold_candidates"] if e not in (a["sessions"][0][0], a["cold_candidates"][0]))
    with pytest.raises(AssertionError, match="not rank's order"):
        _classify(case, a["response"], sessions=[[a["sessions"][0][0], 1.0], [other, 1.0]])


def test_anything_but_the_first_candidates_members_is_refused() -> None:
    case = _affinity_cases()["answer:cache_affinity:warm"]
    warm = json.loads(case["expect"]["affinity"]["response"])
    warm["data"]["tags"] = {"x": "y"}
    with pytest.raises(AssertionError, match="changed \\['tags'\\]"):
        _classify(case, json.dumps(warm, separators=(",", ":")))
    # With fallbacks on the estimate is the same largest over the same set.
    warm = json.loads(case["expect"]["affinity"]["response"])
    warm["data"]["estimated_cost_microdollars"] += 1
    with pytest.raises(AssertionError, match="estimated_cost_microdollars"):
        _classify(case, json.dumps(warm, separators=(",", ":")))


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
