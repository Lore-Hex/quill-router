"""Streams in the fast path's lease cohort (docs/design/fast-admission-streams.md,
increment 4, sections 5.1, 5.2 and 5.4): the rule a stream must meet, its
sub-reasons, the two switches and the edge policy member."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.fakes.spanner import make_fake_store  # noqa: F401 - the pilot fixtures use it
from tests.test_edge_policy_publish import Clock, _publisher, _settings, key  # noqa: F401 - key is a fixture
from tests.test_fastpath_pilot_route import WORKSPACE, _body, _pilot, _request, _seed
from trusted_router.config import Settings
from trusted_router.edge_policy import parse_edge_policy
from trusted_router.gateway_boot import GatewayBoot
from trusted_router.routes.internal import gateway
from trusted_router.services import fastpath_frontdoor as fd
from trusted_router.services.edge_policy_publisher import edge_streams_on

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_792_368_000
OK = {"streams_enabled": True, "stage_d_reason": "ok", "heartbeat_declared": True, "snapshot_booking": True,
      "hold_is_ceiling": True}
COHORT = {"enabled": True, "workspace_id": WORKSPACE, "pilot_workspace_ids": frozenset({WORKSPACE}),
          "region": "us-central1", "pilot_region_ids": frozenset({"us-central1"}), "standard_endpoint_pricing": True,
          "credit_funded": True, "boot_verified": True, "service_tier": None, "key_capped": False,
          "budget_strict": False, "app_markup": False, "records_durable": True}


# -- the rule -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"streams_enabled": False}, "stream.switch"),
        ({"stage_d_reason": "heartbeats_disabled"}, "stream.stage_d:heartbeats_disabled"),
        ({"stage_d_reason": "route"}, "stream.stage_d:route"),
        ({"stage_d_reason": None}, "stream.stage_d:None"),
        ({"heartbeat_declared": False}, "stream.undeclared"),
        ({"snapshot_booking": False}, "stream.snapshot_booking"),
        ({"hold_is_ceiling": False}, "stream.hold"),
        ({"streams_enabled": False, "hold_is_ceiling": False}, "stream.switch"),
        ({"stage_d_reason": "route", "heartbeat_declared": False}, "stream.stage_d:route"),
    ],
)
def test_each_condition_of_the_rule_keeps_a_stream_out_and_names_itself(change: dict[str, Any], reason: str) -> None:
    assert fd.pilot_stream_reason(**{**OK, **change}) == reason
    assert fd.pilot_reason(**COHORT, stream=True, **{**OK, **change}) == "stream"


def test_a_stream_meeting_the_rule_goes_on_to_the_rest_of_the_cohort() -> None:
    assert fd.pilot_stream_reason(**OK) is None
    assert fd.pilot_reason(**COHORT, stream=True, **OK) is None
    assert fd.pilot_reason(**{**COHORT, "service_tier": "flex"}, stream=True, **OK) == "service_tier"
    assert fd.pilot_reason(**{**COHORT, "workspace_id": "other"}, stream=True, **OK) == "workspace"


def test_a_request_that_does_not_stream_ignores_the_rule() -> None:
    assert fd.pilot_reason(**COHORT, stream=False) is None
    assert fd.pilot_reason(**COHORT, stream=False, **{**OK, "streams_enabled": False}) is None


def test_the_rule_is_off_by_default() -> None:
    assert fd.pilot_reason(**COHORT, stream=True) == "stream"
    settings = Settings(environment="test")
    assert settings.fastpath_pilot_streams is False and settings.fastpath_edge_streams is False


# -- the admission ------------------------------------------------------------


class _Client:
    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    def authorize(self, **kwargs: Any) -> fd.Admitted | None:
        self.asked.append(kwargs)
        return None


def _admission(monkeypatch: pytest.MonkeyPatch, settings: Settings, **stream: Any) -> _Client:
    client = _Client()
    monkeypatch.setattr(gateway, "_fastpath_front_door", lambda _settings: client)
    gateway._fastpath_admission(
        settings, workspace_id=WORKSPACE, authorization_id="gwa-s", estimate=300, stream=True,
        standard_endpoint_pricing=True, credit_funded=True, boot_verified=True, boot_kid="boot-pilot",
        service_tier=None, key_capped=False, budget_strict=False, app_markup=False, records_durable=True,
        **stream,
    )
    return client


def test_the_admission_asks_the_front_door_for_a_stream_that_meets_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _pilot(fastpath_pilot_streams=True, reap_snapshot_booking_enabled=True)
    client = _admission(monkeypatch, settings, stage_d_reason="ok", heartbeat_declared=True, hold_is_ceiling=True)
    assert len(client.asked) == 1 and client.asked[0]["stream"] is True and client.asked[0]["estimate"] == 300
    # No stream-open heartbeat is declared: no boot declares one (design 5.1).
    assert "open_heartbeat" not in client.asked[0]


@pytest.mark.parametrize(
    ("settings_change", "stream", "reason"),
    [
        ({"fastpath_pilot_streams": False}, {"stage_d_reason": "ok", "heartbeat_declared": True, "hold_is_ceiling": True},
         "stream.switch"),
        ({}, {"stage_d_reason": "stage_d_disabled", "heartbeat_declared": True, "hold_is_ceiling": True},
         "stream.stage_d:stage_d_disabled"),
        ({}, {"stage_d_reason": "ok", "heartbeat_declared": False, "hold_is_ceiling": True}, "stream.undeclared"),
        ({"reap_snapshot_booking_enabled": False}, {"stage_d_reason": "ok", "heartbeat_declared": True,
                                                    "hold_is_ceiling": True}, "stream.snapshot_booking"),
        ({}, {"stage_d_reason": "ok", "heartbeat_declared": True, "hold_is_ceiling": False}, "stream.hold"),
    ],
)
def test_a_stream_passed_over_logs_its_condition_and_asks_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    settings_change: dict[str, Any], stream: dict[str, Any], reason: str,
) -> None:
    caplog.set_level(logging.INFO)
    settings = _pilot(**{"fastpath_pilot_streams": True, "reap_snapshot_booking_enabled": True, **settings_change})
    client = _admission(monkeypatch, settings, **stream)
    assert client.asked == []
    assert any(r.getMessage() == f"fastpath pilot passed over reason={reason} workspace_id={WORKSPACE}"
               for r in caplog.records)


# -- through the route ---------------------------------------------------------


def _declared_boot(store: Any, *, declares: bool) -> None:
    store.observe_gateway_boot(GatewayBoot(
        kid="boot-pilot", jwk={"kty": "OKP", "crv": "Ed25519", "x": "AA"}, approved=True, verified=True,
        image_digest="sha256:" + "11" * 32, attestation_kind="gcp-cs-jwt", registered_at="2026-10-04T00:00:00Z",
        declares_usage_heartbeat=declares,
    ))


def _stream_settings(**overrides: Any) -> Settings:
    return _pilot(**{"stage_d_eligibility_enabled": True, "stage_d_pilot_workspace_ids": WORKSPACE,
                     "reap_snapshot_booking_enabled": True, "fastpath_pilot_streams": True, **overrides})


@pytest.fixture
def door(monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests.test_fastpath_pilot_route import door as pilot_door

    return pilot_door.__wrapped__(monkeypatch)  # type: ignore[attr-defined]


def test_a_heartbeated_stream_is_leased_through_the_route(door: Any) -> None:
    store, _database, api_key = _seed()
    _declared_boot(store, declares=True)
    data = gateway._authorize_gateway_sync(
        _request(), _body(api_key.hash, stream=True), _stream_settings(),
    )["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope == "sealed-pilot"
    assert data["stage_d"] == {"eligible": True, "reason": "ok"}
    assert data["cap_micro"] == authorization.estimated_microdollars
    path, sent = door.seen[0]
    assert path == "/v1/authorize" and sent["Stream"] is True and "OpenHeartbeat" not in sent
    # The hold is the ceiling: the lease holds the row's estimate.
    assert sent["Estimate"] == authorization.estimated_microdollars


def test_a_stream_whose_reservation_is_lowered_falls_out_of_the_cohort(
    door: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The tripwire (section 5.1 item 5): if a stream's reservation ever
    drops below its estimate (e2-decide's "Streams" follow-up), the stream
    reserves as today rather than being leased and cut at the hold."""
    import dataclasses

    real = gateway.finish_candidates

    def lowered(*args: Any, **kwargs: Any) -> Any:
        outcome = real(*args, **kwargs)
        return dataclasses.replace(outcome, model_reservation=outcome.model_reservation - 1)

    monkeypatch.setattr(gateway, "finish_candidates", lowered)
    caplog.set_level(logging.INFO)
    store, _database, api_key = _seed()
    _declared_boot(store, declares=True)
    data = gateway._authorize_gateway_sync(_request(), _body(api_key.hash, stream=True), _stream_settings())["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert door.seen == []
    assert any("reason=stream.hold " in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("declares", "overrides"),
    [(True, {"fastpath_pilot_streams": False}), (False, {}), (True, {"reap_snapshot_booking_enabled": False}),
     (True, {"stage_d_eligibility_enabled": False})],
    ids=["switch_off", "undeclared_boot", "no_snapshot_booking", "stage_d_off"],
)
def test_a_stream_outside_the_rule_reserves_as_today(door: Any, declares: bool, overrides: dict[str, Any]) -> None:
    store, _database, api_key = _seed()
    _declared_boot(store, declares=declares)
    data = gateway._authorize_gateway_sync(
        _request(), _body(api_key.hash, stream=True), _stream_settings(**overrides),
    )["data"]
    authorization = store.get_gateway_authorization(data["authorization_id"])
    assert authorization is not None and authorization.fastpath_envelope is None
    assert door.seen == []


# -- the edge policy member ----------------------------------------------------


@pytest.mark.parametrize("edge", [False, True])
@pytest.mark.parametrize("pilot", [False, True])
@pytest.mark.parametrize("booking", [False, True])
def test_the_member_is_published_only_with_all_three_on(key: Any, edge: bool, pilot: bool, booking: bool) -> None:  # noqa: F811
    settings = _settings(key, fastpath_edge_streams=edge, fastpath_pilot_streams=pilot,
                         reap_snapshot_booking_enabled=booking)
    body = _publisher(settings, Clock(T0)).current().body
    on = edge and pilot and booking
    assert edge_streams_on(settings) is on
    assert (b'"fastpath_edge_streams":true' in body) is on
    if on:
        policy = parse_edge_policy(body)
        assert policy.fastpath_edge_streams is True
        stripped = json.loads(body)
        stripped.pop("fastpath_edge_streams")
        default = _publisher(_settings(key), Clock(T0)).current().body
        assert json.dumps(stripped, sort_keys=True, separators=(",", ":")).encode("ascii") == default
    else:
        assert b"fastpath_edge_streams" not in body


def test_the_member_has_one_encoding(key: Any) -> None:  # noqa: F811
    on = _publisher(_settings(key, fastpath_edge_streams=True, fastpath_pilot_streams=True,
                              reap_snapshot_booking_enabled=True), Clock(T0)).current().body
    for value in (b"false", b"null", b'"true"', b"1"):
        with pytest.raises((ValidationError, ValueError)):
            parse_edge_policy(on.replace(b'"fastpath_edge_streams":true', b'"fastpath_edge_streams":' + value))


def test_rollout_renders_both_switches_as_literal_false() -> None:
    rollout = (ROOT / "scripts" / "deploy" / "rollout.sh").read_text()
    assert re.findall(r'"TR_FASTPATH_PILOT_STREAMS=[^"]*"', rollout) == ['"TR_FASTPATH_PILOT_STREAMS=false"']
    assert re.findall(r'"TR_FASTPATH_EDGE_STREAMS=[^"]*"', rollout) == ['"TR_FASTPATH_EDGE_STREAMS=false"']


def test_both_switches_read_their_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, env in (("fastpath_pilot_streams", "TR_FASTPATH_PILOT_STREAMS"),
                      ("fastpath_edge_streams", "TR_FASTPATH_EDGE_STREAMS")):
        monkeypatch.delenv(env, raising=False)
        assert getattr(Settings(_env_file=None, environment="test", sentry_dsn=None), name) is False  # type: ignore[call-arg]
        monkeypatch.setenv(env, "true")
        assert getattr(Settings(_env_file=None, environment="test", sentry_dsn=None), name) is True  # type: ignore[call-arg]
        monkeypatch.delenv(env)


def test_the_runbook_names_every_sub_reason_and_the_2b_check() -> None:
    runbook = (ROOT / "docs" / "runbooks" / "fastpath-streams.md").read_text()
    for reason in ("stream.switch", "stream.stage_d:<reason>", "stream.undeclared", "stream.snapshot_booking",
                   "stream.hold"):
        assert f"`{reason}`" in runbook, reason
    assert "Increment 2b on every owner and front door" in runbook
