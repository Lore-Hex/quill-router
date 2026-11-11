"""Listed routes the provider refuses to serve: evidence, endpoint and refresh hold."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.pricing import unserved_route_holds as holds
from trusted_router.provider_manifest_policy import (
    UNSERVED_ROUTE_HOLD_REASON,
    provider_manifest_canary_quarantine_valid_until,
)
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic import route_health
from trusted_router.synthetic.route_health import evaluate_unserved_routes

NOW = dt.datetime(2026, 11, 11, 18, 0, tzinfo=dt.UTC)


def _sample(
    provider: str,
    model: str,
    *,
    minutes_ago: int,
    status: str = "unsupported",
    error_type: str | None = "unsupported_route",
    error_status: int | None = 404,
    source: str = "synthetic",
) -> ProviderBenchmarkSample:
    created = NOW - dt.timedelta(minutes=minutes_ago)
    return ProviderBenchmarkSample(
        id=f"{provider}-{model}-{minutes_ago}-{status}",
        model=model,
        provider=provider,
        provider_name=provider,
        status=status,
        usage_type="Credits",
        streamed=True,
        error_type=None if status == "success" else error_type,
        error_status=None if status == "success" else error_status,
        source=source,
        created_at=created.isoformat().replace("+00:00", "Z"),
    )


def _streak(provider: str, model: str, *, count: int = 7, every: int = 35, newest: int = 10,
            **kwargs: Any) -> list[ProviderBenchmarkSample]:
    return [_sample(provider, model, minutes_ago=newest + i * every, **kwargs) for i in range(count)]


def _sibling_success(provider: str = "phala", model: str = "other/model") -> ProviderBenchmarkSample:
    return _sample(provider, model, minutes_ago=20, status="success")


class _Store:
    def __init__(self, samples: list[ProviderBenchmarkSample]) -> None:
        self.samples = samples

    def provider_route_benchmark_samples(
        self, *, cutoff: str, per_route_limit: int, limit: int,
    ) -> list[ProviderBenchmarkSample]:
        rows = sorted(
            (sample for sample in self.samples if sample.created_at >= cutoff),
            key=lambda sample: sample.created_at, reverse=True,
        )
        counts: dict[tuple[str, str], int] = {}
        selected = []
        for sample in rows:
            route = (sample.provider, sample.model)
            if counts.get(route, 0) < per_route_limit:
                counts[route] = counts.get(route, 0) + 1
                selected.append(sample)
        return selected[:limit]


def _evaluate(samples: list[ProviderBenchmarkSample],
              routes: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    found = evaluate_unserved_routes(
        _Store(samples),  # type: ignore[arg-type]
        routes=routes or [("phala", "z-ai/glm-5.1"), ("phala", "other/model")],
        now=NOW,
    )
    return [(route.provider, route.model) for route in found]


# --- evidence ---------------------------------------------------------------


def test_sustained_model_not_served_streak_is_evidence() -> None:
    samples = _streak("phala", "z-ai/glm-5.1") + [_sibling_success()]
    found = evaluate_unserved_routes(
        _Store(samples), routes=[("phala", "z-ai/glm-5.1"), ("phala", "other/model")], now=NOW,  # type: ignore[arg-type]
    )
    assert [(route.provider, route.model) for route in found] == [("phala", "z-ai/glm-5.1")]
    assert found[0].samples == 7
    assert found[0].error_types == ("unsupported_route",)


def test_streak_shorter_than_three_hours_is_not_evidence() -> None:
    # Seven failures in 90 minutes: the route-configuration alert fires, the hold does not.
    assert _evaluate(_streak("phala", "z-ai/glm-5.1", every=15) + [_sibling_success()]) == []


def test_too_few_or_stale_failures_are_not_evidence() -> None:
    assert _evaluate(_streak("phala", "z-ai/glm-5.1", count=5, every=60) + [_sibling_success()]) == []
    assert _evaluate(_streak("phala", "z-ai/glm-5.1", newest=180) + [_sibling_success()]) == []


def test_a_recent_success_breaks_the_streak() -> None:
    samples = _streak("phala", "z-ai/glm-5.1", newest=30) + [
        _sample("phala", "z-ai/glm-5.1", minutes_ago=5, status="success"), _sibling_success(),
    ]
    assert _evaluate(samples) == []


@pytest.mark.parametrize(("error_type", "error_status"), [
    ("ConnectError", None), ("ReadTimeout", None), ("rate_limited", 429),
    ("upstream_error", 502), ("upstream_error", 503), ("probe_config_error", 400),
])
def test_transient_quota_and_probe_config_classes_are_not_evidence(
    error_type: str, error_status: int | None,
) -> None:
    samples = _streak("phala", "z-ai/glm-5.1", status="error", error_type=error_type,
                      error_status=error_status) + [_sibling_success()]
    assert _evaluate(samples) == []
    # One transient failure at the head of an otherwise unserved streak breaks it too.
    mixed = _streak("phala", "z-ai/glm-5.1", newest=30) + [
        _sample("phala", "z-ai/glm-5.1", minutes_ago=5, status="error", error_type=error_type,
                error_status=error_status),
        _sibling_success(),
    ]
    assert _evaluate(mixed) == []


def test_without_a_sibling_success_nothing_is_evidence() -> None:
    # A provider-wide auth or adapter failure must never hold routes one by one.
    assert _evaluate(_streak("phala", "z-ai/glm-5.1")) == []
    auth = _streak("phala", "z-ai/glm-5.1", error_type="provider_auth_config", error_status=403)
    assert _evaluate(auth) == []
    # A sibling success older than the window is not proof the key works now.
    old_sibling = _sample("phala", "other/model", minutes_ago=7 * 60, status="success")
    assert _evaluate(_streak("phala", "z-ai/glm-5.1") + [old_sibling]) == []


def test_a_sibling_success_from_before_the_break_proves_nothing() -> None:
    # Key revoked: every route starts failing; the last sibling success predates it.
    before_break = _sample("phala", "other/model", minutes_ago=240, status="success")
    samples = _streak("phala", "z-ai/glm-5.1", every=35, newest=10) + [before_break]
    assert _evaluate(samples) == []


def test_passed_through_capacity_status_is_not_evidence() -> None:
    # A 503/429 whose body says model_not_available is still capacity.
    for status in (429, 503):
        samples = _streak("phala", "z-ai/glm-5.1", error_status=status) + [_sibling_success()]
        assert _evaluate(samples) == []


def test_a_customer_success_breaks_the_streak() -> None:
    samples = _streak("phala", "z-ai/glm-5.1", newest=30) + [
        _sample("phala", "z-ai/glm-5.1", minutes_ago=5, status="success", source="live"),
        _sibling_success(),
    ]
    assert _evaluate(samples) == []


def test_failures_from_before_a_hold_do_not_rearm_a_lifted_route() -> None:
    # Six old failures, a 26h gap while the route was held, then one new failure.
    old = _streak("phala", "z-ai/glm-5.1", newest=27 * 60)
    samples = [*old, _sample("phala", "z-ai/glm-5.1", minutes_ago=10), _sibling_success()]
    assert _evaluate(samples) == []


def test_per_model_403_counts_when_the_same_key_serves_a_sibling() -> None:
    samples = _streak("lyceum", "moonshotai/kimi-k3.1-pro", error_type="provider_auth_config",
                      error_status=403) + [_sibling_success("lyceum", "z-ai/glm-5.3")]
    routes = [("lyceum", "moonshotai/kimi-k3.1-pro"), ("lyceum", "z-ai/glm-5.3")]
    assert _evaluate(samples, routes) == [("lyceum", "moonshotai/kimi-k3.1-pro")]


def test_non_synthetic_samples_and_unconfigured_routes_are_ignored() -> None:
    samples = _streak("phala", "z-ai/glm-5.1", source="live") + [_sibling_success()]
    assert _evaluate(samples) == []
    assert _evaluate(_streak("phala", "z-ai/glm-5.1") + [_sibling_success()],
                     routes=[("phala", "other/model")]) == []


# --- public endpoint --------------------------------------------------------


def test_endpoint_publishes_evidence(client, monkeypatch) -> None:
    from trusted_router.storage import InMemoryStore

    now = dt.datetime.now(dt.UTC)
    offset = now - NOW
    samples = [
        dataclasses.replace(
            sample,
            created_at=(dt.datetime.fromisoformat(sample.created_at.replace("Z", "+00:00")) + offset)
            .isoformat().replace("+00:00", "Z"),
        )
        for sample in _streak("phala", "z-ai/glm-5.1") + [_sibling_success()]
    ]
    monkeypatch.setattr(route_health, "rotation_candidates",
                        lambda: {"phala": ["z-ai/glm-5.1", "other/model"]})
    monkeypatch.setattr(InMemoryStore, "provider_route_benchmark_samples",
                        lambda self, **kwargs: _Store(samples).provider_route_benchmark_samples(**kwargs),
                        raising=False)
    response = client.get("/route-health/unserved.json")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert [(row["provider"], row["model"]) for row in body["routes"]] == [("phala", "z-ai/glm-5.1")]
    assert "error_message" not in json.dumps(body)
    assert holds.parse_evidence(body, now=now) == {("phala", "z-ai/glm-5.1")}


def test_endpoint_answers_503_when_analytics_is_unavailable(client, monkeypatch) -> None:
    from trusted_router.storage import InMemoryStore

    def unavailable(self, **kwargs):
        raise RuntimeError("operational analytics unavailable")

    monkeypatch.setattr(route_health, "rotation_candidates", lambda: {"phala": ["z-ai/glm-5.1"]})
    monkeypatch.setattr(InMemoryStore, "provider_route_benchmark_samples", unavailable, raising=False)
    response = client.get("/route-health/unserved.json")
    assert response.status_code == 503
    assert "routes" not in response.json()


# --- refresh hold -----------------------------------------------------------


def _row(model_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": model_id, "model_type": "chat", "endpoints": ["chat/completions"],
        "input_token_price_per_m": 1_000_000, "output_token_price_per_m": 2_000_000, **extra,
    }


def _manifest(tmp_path: Path, provider: str, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"{provider}.json"
    path.write_text(json.dumps({"provider": provider, "generated_at": "2026-11-11T17:00:00Z",
                                "models": rows}, indent=2) + "\n")
    return path


def _rows(path: Path) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in json.loads(path.read_text())["models"]}


def _payload(routes: list[tuple[str, str]], *, generated: dt.datetime = NOW) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "routes": [{"provider": provider, "model": model} for provider, model in routes],
    }


def test_listed_but_unserved_route_is_held_after_the_threshold(tmp_path: Path) -> None:
    phala = _manifest(tmp_path, "phala", [_row("z-ai/glm-5.1"), _row("z-ai/glm-5.2"), _row("qwen/qwen3")])
    makora = _manifest(tmp_path, "makora", [_row("meta-models/muse"), _row("z-ai/glm-5.2")])
    makora_before = makora.read_bytes()
    evidence = holds.parse_evidence(_payload([("phala", "z-ai/glm-5.1")]), now=NOW)

    notes = holds.apply_unserved_route_holds(evidence, manifest_dir=tmp_path, now=NOW)

    rows = _rows(phala)
    assert rows["z-ai/glm-5.1"]["routable"] is False
    assert rows["z-ai/glm-5.1"]["routable_reason"] == UNSERVED_ROUTE_HOLD_REASON
    assert rows["z-ai/glm-5.1"][holds.HELD_AT_FIELD] == "2026-11-11T18:00:00Z"
    # Scoped to the exact provider route: siblings and other providers untouched.
    assert "routable" not in rows["z-ai/glm-5.2"] and "routable" not in rows["qwen/qwen3"]
    assert makora.read_bytes() == makora_before
    assert any("held z-ai/glm-5.1" in note for note in notes)


def test_hold_survives_the_next_refresh_until_it_expires(tmp_path: Path) -> None:
    held = _row("z-ai/glm-5.1", routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
                **{holds.HELD_AT_FIELD: "2026-11-11T06:00:00Z"})
    phala = _manifest(tmp_path, "phala", [held, _row("z-ai/glm-5.2"), _row("qwen/qwen3")])
    before = phala.read_bytes()
    # Held routes are not probed, so they drop out of the evidence; the hold stays.
    holds.apply_unserved_route_holds(set(), manifest_dir=tmp_path, now=NOW)
    assert phala.read_bytes() == before


def test_recovered_route_is_lifted_when_the_hold_expires(tmp_path: Path) -> None:
    held = _row("z-ai/glm-5.1", routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
                **{holds.HELD_AT_FIELD: "2026-11-10T17:00:00Z"})
    other = _row("z-ai/glm-5.0", routable=False, routable_reason="provider-access-unavailable")
    phala = _manifest(tmp_path, "phala", [held, other, _row("z-ai/glm-5.2"), _row("qwen/qwen3")])

    notes = holds.apply_unserved_route_holds(set(), manifest_dir=tmp_path, now=NOW)

    rows = _rows(phala)
    assert {"routable", "routable_reason", holds.HELD_AT_FIELD}.isdisjoint(rows["z-ai/glm-5.1"])
    # Other hold reasons are never this step's to lift.
    assert rows["z-ai/glm-5.0"]["routable_reason"] == "provider-access-unavailable"
    assert any("lifted expired" in note for note in notes)


def test_a_route_still_unserved_at_expiry_is_held_again(tmp_path: Path) -> None:
    held = _row("z-ai/glm-5.1", routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
                **{holds.HELD_AT_FIELD: "2026-11-10T17:00:00Z"})
    phala = _manifest(tmp_path, "phala", [held, _row("z-ai/glm-5.2"), _row("qwen/qwen3")])
    holds.apply_unserved_route_holds({("phala", "z-ai/glm-5.1")}, manifest_dir=tmp_path, now=NOW)
    row = _rows(phala)["z-ai/glm-5.1"]
    assert (row["routable"], row["routable_reason"], row[holds.HELD_AT_FIELD]) == (
        False, UNSERVED_ROUTE_HOLD_REASON, "2026-11-11T18:00:00Z",
    )


def test_analytics_unavailable_changes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    held = _row("z-ai/glm-5.1", routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
                **{holds.HELD_AT_FIELD: "2026-11-11T12:00:00Z"})
    phala = _manifest(tmp_path, "phala", [held, _row("z-ai/glm-5.2"), _row("qwen/qwen3")])
    makora = _manifest(tmp_path, "makora", [_row("meta-models/muse"), _row("z-ai/glm-5.2")])
    before = {path: path.read_bytes() for path in (phala, makora)}

    def unavailable(source: str) -> set[tuple[str, str]]:
        raise RuntimeError("503 route health unavailable")

    monkeypatch.setattr(holds, "fetch_evidence", unavailable)
    assert holds.main(["--manifest-dir", str(tmp_path)]) == 0
    assert {path: path.read_bytes() for path in before} == before
    assert "holding nothing" in capsys.readouterr().out


@pytest.mark.parametrize("payload", [
    _payload([("phala", "z-ai/glm-5.1")], generated=NOW - dt.timedelta(hours=3)),
    _payload([("phala", "z-ai/glm-5.1")], generated=NOW + dt.timedelta(minutes=10)),
    {"version": 2, "generated_at": "2026-11-11T18:00:00Z", "routes": []},
    {"version": 1, "generated_at": "2026-11-11T18:00:00Z", "routes": [{"provider": "phala"}]},
    {"version": 1, "generated_at": "2026-11-11T18:00:00Z"},
    {"error": "route health unavailable"},
])
def test_stale_or_malformed_evidence_is_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        holds.parse_evidence(payload, now=NOW)


def test_a_provider_is_never_held_wholesale(tmp_path: Path) -> None:
    single = _manifest(tmp_path, "lonely", [_row("only/model")])
    pair = _manifest(tmp_path, "pair", [_row("a/one"), _row("b/two"), _row("c/held", routable=False,
                                                                             routable_reason="price-unavailable")])
    before = {path: path.read_bytes() for path in (single, pair)}
    notes = holds.apply_unserved_route_holds(
        {("lonely", "only/model"), ("pair", "a/one"), ("pair", "b/two")}, manifest_dir=tmp_path, now=NOW,
    )
    assert {path: path.read_bytes() for path in before} == before
    assert sum("provider-wide problem" in note for note in notes) == 2


def test_the_provider_cap_cannot_ratchet_across_runs(tmp_path: Path) -> None:
    held = [_row(f"m/{i}", routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
                 **{holds.HELD_AT_FIELD: "2026-11-11T12:00:00Z"}) for i in range(5)]
    path = _manifest(tmp_path, "wide", held + [_row(f"m/{i}") for i in range(5, 10)])
    before = path.read_bytes()
    # Five held of ten already; one more would make a majority.
    notes = holds.apply_unserved_route_holds({("wide", "m/5")}, manifest_dir=tmp_path, now=NOW)
    assert path.read_bytes() == before
    assert any("6 of 10 rows unserved" in note for note in notes)


def test_lift_never_publishes_an_unpriced_or_delisted_row(tmp_path: Path) -> None:
    expired = {"routable": False, "routable_reason": UNSERVED_ROUTE_HOLD_REASON,
               holds.HELD_AT_FIELD: "2026-11-10T12:00:00Z"}
    unpriced = {"id": "z-ai/unpriced", "model_type": "chat", "endpoints": ["chat/completions"], **expired}
    delisted = _row("z-ai/gone", missing_since="2026-11-11", **expired)
    path = _manifest(tmp_path, "phala", [unpriced, delisted, _row("z-ai/glm-5.2"), _row("qwen/qwen3")])
    holds.apply_unserved_route_holds(None, manifest_dir=tmp_path, now=NOW)
    rows = _rows(path)
    assert (rows["z-ai/unpriced"]["routable"], rows["z-ai/unpriced"]["routable_reason"]) == (False, "price-unavailable")
    assert (rows["z-ai/gone"]["routable"], rows["z-ai/gone"]["routable_reason"]) == (False, "delisted-upstream")
    assert holds.HELD_AT_FIELD not in rows["z-ai/unpriced"] and holds.HELD_AT_FIELD not in rows["z-ai/gone"]


def test_evidence_built_just_after_the_runner_clock_is_accepted() -> None:
    payload = _payload([("phala", "z-ai/glm-5.1")], generated=NOW + dt.timedelta(seconds=40))
    assert holds.parse_evidence(payload, now=NOW) == {("phala", "z-ai/glm-5.1")}


def test_more_than_the_route_cap_holds_nothing(tmp_path: Path) -> None:
    rows = [_row(f"m/{i}") for i in range(100)]
    path = _manifest(tmp_path, "wide", rows)
    before = path.read_bytes()
    evidence = {("wide", f"m/{i}") for i in range(holds.MAX_ROUTES + 1)}
    notes = holds.apply_unserved_route_holds(evidence, manifest_dir=tmp_path, now=NOW)
    assert path.read_bytes() == before
    assert any("cap" in note for note in notes)


def test_routes_without_a_manifest_row_are_reported_not_invented(tmp_path: Path) -> None:
    path = _manifest(tmp_path, "phala", [_row("z-ai/glm-5.2"), _row("qwen/qwen3")])
    before = path.read_bytes()
    notes = holds.apply_unserved_route_holds(
        {("phala", "z-ai/glm-9"), ("ghost", "x/y")}, manifest_dir=tmp_path, now=NOW,
    )
    assert path.read_bytes() == before
    assert any("ghost: no provider manifest" in note for note in notes)
    assert any("z-ai/glm-9 is not a routable manifest row" in note for note in notes)


def test_hold_reason_is_a_valid_fail_closed_quarantine() -> None:
    raw = {
        "generated_at": "2026-11-11T17:00:00Z",
        "models": [_row("a/one", routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON)],
    }
    deadline = provider_manifest_canary_quarantine_valid_until("lyceum", raw)
    assert deadline is not None and deadline > NOW


def test_shared_writer_carries_the_hold_through_discovery(tmp_path: Path) -> None:
    from scripts.pricing.base import ModelPrice, ProviderPricingResult
    from scripts.pricing.manifest import write_discovered_chat_manifest

    held = _row("z-ai/glm-5.1", routable=False, routable_reason=UNSERVED_ROUTE_HOLD_REASON,
                **{holds.HELD_AT_FIELD: "2026-11-11T12:00:00Z"})
    path = _manifest(tmp_path, "phala", [held, _row("z-ai/glm-5.2"), _row("qwen/qwen3")])
    prices = {model: ModelPrice(1_000_000, 2_000_000) for model in ("z-ai/glm-5.1", "z-ai/glm-5.2", "qwen/qwen3")}
    write_discovered_chat_manifest(
        ProviderPricingResult(slug="phala", prices=prices, source="api", fetched_url="https://example.test"),
        manifest_path=path,
        # The provider still lists the model, even claims it routable.
        discovered_rows={model: {"id": model, "upstream_id": model, "routable": True} for model in prices},
        source_url="https://example.test",
    )
    row = _rows(path)["z-ai/glm-5.1"]
    assert (row["routable"], row["routable_reason"]) == (False, UNSERVED_ROUTE_HOLD_REASON)
    assert row[holds.HELD_AT_FIELD] == "2026-11-11T12:00:00Z"
