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
              snapshot_of: affinity_rule.SnapshotOf | None = None) -> str:
    a = case["expect"]["affinity"]
    return affinity_rule.affinity_class(
        case["name"], json.loads(case["request"]["body"]), a["cold_response"], warm_response,
        [tuple(s) for s in (a["sessions"] if sessions is None else sessions)], _model_of, snapshot_of,
        a.get("async_facts"))


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


def _async_affinity() -> tuple[dict[str, Any], affinity_rule.SnapshotOf]:
    """The async golden's affinity case, and a snapshot rebuild for it: None
    where the embedded routing snapshot shows build_snapshot refusing a
    candidate (a provider Candidate refuses, or billing_valid false at the
    case's instant), the cold answer's own snapshot for the cold set."""
    vectors = json.loads(ASYNC_CASES.read_text(encoding="utf-8"))
    (case,) = [c for c in vectors["cases"] if "affinity" in c["expect"]]
    endpoints = {e["id"]: e for e in json.loads(vectors["snapshot_body"])["endpoints"]}
    cold = json.loads(case["expect"]["affinity"]["cold_response"])["data"]

    def billing_valid(endpoint_id: str) -> bool:
        epochs = [e for e in endpoints[endpoint_id]["epochs"] if e["starts_at"] * 1_000_000 <= case["at_us"]]
        return bool(epochs[-1]["billing_valid"])

    def snapshot_of(ids: Any) -> dict[str, Any] | None:
        if any(endpoints[i]["provider"] not in ("openai", "anthropic") or not billing_valid(i) for i in ids):
            return None
        assert sorted(ids) == sorted(c["endpoint_id"] for c in cold["route_candidates"]), ids
        return dict(cold["billing_snapshot"])

    return case, snapshot_of


def _with_async(response: str, members: dict[str, Any]) -> str:
    """The answer with its async members replaced, in their place before tags."""
    data = {k: v for k, v in json.loads(response)["data"].items() if k not in affinity_rule.ASYNC_MEMBERS}
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "tags":
            out.update(members)
        out[key] = value
    return json.dumps({"data": out}, separators=(",", ":"))


def test_async_members_change_only_with_the_candidate_set() -> None:
    """The async members are built from the candidates kept, sorted: the
    async golden's warm answer keeps another endpoint (fallbacks off) and
    changes them in kind (the G15 design's §4.4); a re-rank of the same set
    may not change them."""
    case, snapshot_of = _async_affinity()
    a = case["expect"]["affinity"]
    assert set(a["candidates"]) != set(a["cold_candidates"])
    assert _classify(case, a["response"], snapshot_of=snapshot_of) == "affinity_warm"
    with pytest.raises(AssertionError, match="no way to rebuild"):
        _classify(case, a["response"])
    same_set = _affinity_cases()["pass:body.cache_affinity:warm:fallback_models"]
    warm = json.loads(same_set["expect"]["affinity"]["response"])
    warm["data"]["async_eligible"] = False
    with pytest.raises(AssertionError, match="changed \\['async_eligible'\\]"):
        _classify(same_set, json.dumps(warm, separators=(",", ":")))


def test_a_warm_answers_async_members_are_its_own_candidates() -> None:
    """With the set changed, the warm members must be Python's for the warm
    candidates: async_eligible false, and here shape A (the remembered azure
    endpoint is one Candidate refuses), so neither the cold snapshot nor any
    hash may ride along."""
    case, snapshot_of = _async_affinity()
    a = case["expect"]["affinity"]
    cold = json.loads(a["cold_response"])["data"]
    snapshot, digest = cold["billing_snapshot"], cold["billing_snapshot_hash"]
    for members, message in (
        ({"async_eligible": True}, "not shape A or B"),
        ({"async_eligible": False, "billing_snapshot_hash": "0" * 64}, "not shape A or B"),
        ({"async_eligible": False, "billing_snapshot": snapshot, "billing_snapshot_hash": digest},
         "warm answer is shape B, where Python answers shape A"),
    ):
        with pytest.raises(AssertionError, match=message):
            _classify(case, _with_async(a["response"], members), snapshot_of=snapshot_of)
    # The warm answer itself, rebuilt by _with_async, is accepted.
    assert _classify(case, _with_async(a["response"], {"async_eligible": False}), snapshot_of=snapshot_of) == (
        "affinity_warm")


def _swapped(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
    """The golden's answers the other way round: cold over the azure
    endpoint (shape A), warm over the openai one (shape B), as a request
    whose cheaper cold choice were azure would have them."""
    a = case["expect"]["affinity"]
    azure, openai = json.loads(a["response"])["data"], json.loads(a["cold_response"])["data"]
    return azure, openai, a["candidates"], a["cold_candidates"]


def test_a_warm_snapshot_is_compared_as_canonical_bytes_under_its_own_hash() -> None:
    """Cold A over azure, warm B over openai: the warm snapshot is accepted
    under the hash of its own canonical bytes, and refused when any value
    differs in its JSON (true or 1.0 for the version 1, which compare equal
    as Python values) or when its hash is another's."""
    case, snapshot_of = _async_affinity()
    body, facts = json.loads(case["request"]["body"]), case["expect"]["affinity"]["async_facts"]
    azure, openai, azure_ids, openai_ids = _swapped(case)
    affinity_rule.check_warm_async("b", body, azure, openai, azure_ids, openai_ids, snapshot_of, facts)
    for version in (True, 1.0):
        retyped = {**openai, "billing_snapshot": {**openai["billing_snapshot"], "v": version}}
        assert retyped["billing_snapshot"] == openai["billing_snapshot"]  # equal as Python values
        with pytest.raises(AssertionError, match="warm billing_snapshot is not its candidates' snapshot"):
            affinity_rule.check_warm_async("b", body, azure, retyped, azure_ids, openai_ids, snapshot_of, facts)
    with pytest.raises(AssertionError, match="hash is not its snapshot's"):
        affinity_rule.check_warm_async("b", body, azure, {**openai, "billing_snapshot_hash": "0" * 64}, azure_ids,
                                       openai_ids, snapshot_of, facts)


def test_request_eligibility_comes_from_the_request_not_the_cold_answer() -> None:
    """Cold azure (no snapshot), warm openai, fallbacks off, and tools
    requested: Python answers shape A to both, whatever the candidates, so
    the genuine warm A is accepted and a warm B with a rebuilt snapshot and
    its correct hash is refused. The cold answer alone (shape A over
    candidates that freeze nothing) cannot tell the two apart."""
    case, snapshot_of = _async_affinity()
    facts = case["expect"]["affinity"]["async_facts"]
    body = {**json.loads(case["request"]["body"]), "requested_parameters": ["tools"]}
    assert affinity_rule.request_exclusion(body, facts) == "requested_parameters"
    azure, openai, azure_ids, openai_ids = _swapped(case)
    genuine = {k: v for k, v in openai.items() if k not in ("billing_snapshot", "billing_snapshot_hash")}
    affinity_rule.check_warm_async("tools", body, azure, genuine, azure_ids, openai_ids, snapshot_of, facts)
    with pytest.raises(AssertionError, match="warm answer is shape B, where Python answers shape A"):
        affinity_rule.check_warm_async("tools", body, azure, openai, azure_ids, openai_ids, snapshot_of, facts)
    # The golden request is eligible; each other fact can make it not.
    assert affinity_rule.request_exclusion(json.loads(case["request"]["body"]), facts) is None
    for fact, value, reason in (("replay", True, "replay"), ("typed", False, "untyped"),
                                ("federated", True, "settlement_authority"), ("receipt_fee_basis_points", 100,
                                                                             "receipt_fee"),
                                ("ticket_authority", False, "ticket_authority")):
        assert affinity_rule.request_exclusion(json.loads(case["request"]["body"]), {**facts, fact: value}) == reason
    with pytest.raises(AssertionError, match="admission on"):
        affinity_rule.request_exclusion(json.loads(case["request"]["body"]), {**facts, "admission_enabled": True})


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
