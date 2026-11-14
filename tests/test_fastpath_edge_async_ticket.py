"""Edge signing shares the projection's claims and checks catalog facts first."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import get_args
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.test_async_settle_ticket import endpoint, runtime
from trusted_router import billing_snapshot, catalog
from trusted_router.async_settle_ticket import verify_ticket
from trusted_router.billing_snapshot import Eligibility, build_snapshot, canonical_hash
from trusted_router.config import Settings
from trusted_router.detached_jws import b64decode
from trusted_router.main import create_app
from trusted_router.routes.internal import fastpath_async_ticket as route
from trusted_router.services import async_settle as service
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

NOW = 1_792_368_000
TOKEN = "edge-ticket-test-token"  # noqa: S105 - test credential


def settings(**changes):
    return Settings(_env_file=None, **{
        "environment": "test", "sentry_dsn": None, "storage_backend": "memory",
        "internal_gateway_token": TOKEN, "async_settle_enabled": True,
        "async_settle_protection": True, "fastpath_edge_async_ticket_workspaces": "*",
        **changes,
    })


@pytest.fixture
def facts(monkeypatch):
    candidate = endpoint()
    monkeypatch.setattr(catalog, "endpoint_for_id", lambda value: candidate if value == candidate.id else None)
    snapshot = build_snapshot([catalog.effective_endpoint(candidate, at=datetime.fromtimestamp(NOW, UTC))], Eligibility())
    body = route.EdgeTicketRequest(
        v=1, authorization_id="auth_test/1", workspace_id="ws_test_1", key_hash="key_test_1",
        reservation_id="res_test_1", invocation_nonce="nonce_test_1", route_type="chat.completions",
        streamed=False, region="us-central1", candidate_endpoint_ids=[candidate.id], priced_at=NOW,
        billing_snapshot_hash=canonical_hash(snapshot),
    )
    rt = runtime()
    rt.signer.sign = Mock(wraps=rt.signer.sign)
    return SimpleNamespace(body=body, snapshot=snapshot, runtime=rt, settings=settings(), candidate=candidate)


def call(facts, **changes):
    return service.edge_ticket(**{
        "body": facts.body, "settings": facts.settings, "runtime": facts.runtime, "typed": True, "now": NOW,
        **changes,
    })


def metric(caplog, facts, outcome, reason, eligible):
    lines = [r.getMessage() for r in caplog.records if "metric=async_settle_edge_ticket " in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert f"outcome={outcome} reason={reason} eligible={eligible} elapsed_ms=" in line
    assert line.rsplit("=", 1)[1].isdigit()
    for value in facts.body.model_dump().values():
        if isinstance(value, str):
            assert value not in line
        elif isinstance(value, list):
            assert all(item not in line for item in value)


@pytest.mark.parametrize("admitted", [True, False])
@pytest.mark.parametrize("route_type,streamed", [("chat.completions", False), ("responses", True)])
def test_issued_matches_projection_claim_by_claim(facts, caplog, admitted, route_type, streamed):
    caplog.set_level(logging.INFO, logger="trusted_router.async_settle_timing")
    facts.body = facts.body.model_copy(update={"route_type": route_type, "streamed": streamed})
    if not admitted:
        facts.runtime.admission = None
    result = call(facts)
    auth = GatewayAuthorization(
        id=facts.body.authorization_id, workspace_id=facts.body.workspace_id, key_hash=facts.body.key_hash,
        invocation_nonce=facts.body.invocation_nonce, credit_reservation_id=facts.body.reservation_id,
        region=facts.body.region, model_id=facts.candidate.model_id, provider=facts.candidate.provider,
        usage_type=UsageType.CREDITS, estimated_microdollars=2,
    )
    projection = service.snapshot_projection(
        authorization=auth, snapshot=facts.snapshot, requested=Eligibility(route_type=route_type, streamed=streamed),
        runtime=facts.runtime, settings=facts.settings, now=NOW,
    )
    expected = json.loads(b64decode(projection["settlement_ticket"].split(".")[1]))
    claims = verify_ticket(result["settlement_ticket"], [facts.runtime.signer.trusted], expected, NOW).model_dump()
    assert len(claims) == 19
    for name, value in expected.items():
        assert claims[name] == value, name
    assert result == {name: projection[name] for name in ("async_eligible", "settlement_ticket", "settlement_status_url")}
    assert result["async_eligible"] is claims["async_eligible"] is admitted
    assert result["settlement_status_url"] == "/v1/settlements/" + quote(auth.id + ".settle", safe="")
    metric(caplog, facts, "issued", "none", "yes" if admitted else "no")


@pytest.mark.parametrize("reason", ["workspace", "region", "authority", "records", "price_instant", "endpoint", "snapshot",
                                    "ineligible", "hash", "error"])
def test_refusal_never_signs(facts, monkeypatch, caplog, reason):
    caplog.set_level(logging.INFO, logger="trusted_router.async_settle_timing")
    changes = {}
    if reason == "workspace":
        changes["settings"] = settings(fastpath_edge_async_ticket_workspaces="ws_test_2")
    elif reason == "region":
        changes["settings"] = settings(async_settle_negotiation_regions="europe-west4")
    elif reason == "authority":
        changes["runtime"] = replace(facts.runtime, epoch=0)
    elif reason == "records":
        changes["typed"] = False
    elif reason == "price_instant":
        changes["now"] = NOW + 301
    elif reason == "endpoint":
        monkeypatch.setattr(catalog, "endpoint_for_id", lambda _: None)
    elif reason == "snapshot":
        candidate = replace(facts.candidate, request_price_microdollars=1)
        monkeypatch.setattr(catalog, "endpoint_for_id", lambda _: candidate)
    elif reason == "ineligible":
        # The request's own eligibility is checked once, by build_snapshot.
        monkeypatch.setattr(billing_snapshot, "exclusion", lambda _context: "unsupported_route")
    elif reason == "hash":
        changed = replace(facts.candidate, completion_price_microdollars_per_million_tokens=999_999)
        digest = canonical_hash(build_snapshot([catalog.effective_endpoint(changed, at=datetime.fromtimestamp(NOW, UTC))], Eligibility()))
        changes["body"] = facts.body.model_copy(update={"billing_snapshot_hash": digest})
    elif reason == "error":
        monkeypatch.setattr(catalog, "endpoint_for_id", Mock(side_effect=RuntimeError("test")))
    assert call(facts, **changes) == {"async_eligible": False}
    facts.runtime.signer.sign.assert_not_called()
    metric(caplog, facts, "refused", "snapshot" if reason == "ineligible" else reason, "na")


@pytest.mark.parametrize("authority", ["runtime", "signer", "epoch", "journal"])
def test_missing_authority(facts, authority, caplog):
    caplog.set_level(logging.INFO, logger="trusted_router.async_settle_timing")
    rt = facts.runtime
    missing = {"runtime": None, "signer": replace(rt, signer=None), "epoch": replace(rt, epoch=0),
               "journal": replace(rt, journal_region="")}[authority]
    assert call(facts, runtime=missing) == {"async_eligible": False}
    rt.signer.sign.assert_not_called()
    metric(caplog, facts, "refused", "authority", "na")


def test_signing_exception_is_error(facts, caplog):
    caplog.set_level(logging.INFO, logger="trusted_router.async_settle_timing")
    facts.runtime.signer.sign.side_effect = RuntimeError("test")
    assert call(facts) == {"async_eligible": False}
    metric(caplog, facts, "refused", "error", "na")


@pytest.mark.parametrize("regions,region,issued", [
    # The process serves us-central1 and its journal is there throughout: the
    # rule reads the region the edge names for the authorization, and nothing
    # else.
    ("europe-west4", "europe-west4", True),
    ("us-central1,europe-west4", "europe-west4", True),
    ("us-central1,europe-west4", "us-central1", True),
    ("us-central1", "europe-west4", False),
    ("us-central1,europe-west4", "southamerica-east1", False),
])
def test_region_rule_reads_the_authorization_region(facts, caplog, regions, region, issued):
    caplog.set_level(logging.INFO, logger="trusted_router.async_settle_timing")
    assert facts.runtime.effective_journal_region == "us-central1"
    cfg = settings(async_settle_negotiation_regions=regions, serving_region="us-central1")
    assert cfg.serving_region == cfg.effective_serving_region == "us-central1"
    body = facts.body.model_copy(update={"region": region})
    result = call(facts, body=body, settings=cfg)
    if issued:
        assert set(result) == {"async_eligible", "settlement_ticket", "settlement_status_url"}
        assert result["async_eligible"] is True
        facts.runtime.signer.sign.assert_called_once()
        metric(caplog, facts, "issued", "none", "yes")
    else:
        assert result == {"async_eligible": False}
        facts.runtime.signer.sign.assert_not_called()
        metric(caplog, facts, "refused", "region", "na")


@pytest.mark.parametrize("delta", [-301, -300, 300, 301])
def test_price_instant_boundary(facts, delta):
    result = call(facts, now=NOW + delta)
    assert ("settlement_ticket" in result) is (abs(delta) <= 300)


@pytest.mark.parametrize("switch,allows", [("", False), ("*", True), ("ws_test_1,ws_test_2", True), ("ws_test_2", False)])
def test_workspace_setting(facts, switch, allows):
    cfg = settings(fastpath_edge_async_ticket_workspaces=switch)
    assert cfg.fastpath_edge_async_tickets_enabled is bool(switch)
    assert cfg.fastpath_edge_async_ticket_allows("ws_test_1") is allows
    assert ("settlement_ticket" in call(facts, settings=cfg)) is allows
    if not allows:
        facts.runtime.signer.sign.assert_not_called()


@pytest.mark.parametrize("switch", [
    ",".join(f"ws_test_{i}" for i in range(33)), "ws test", "*,ws_test_1", "w" * 65,
    # Set, and naming no workspace: refused, never read as "off".
    ",", " ", " , ,",
])
def test_invalid_workspace_setting(switch):
    with pytest.raises(ValidationError):
        settings(fastpath_edge_async_ticket_workspaces=switch)


@pytest.mark.parametrize("mode", ["pilot", "reserved", "admission_off"])
def test_star_still_requires_admission(facts, mode):
    changes = {"async_settle_pilot_workspaces": "ws_test_2"} if mode == "pilot" else (
        {"async_settle_enabled": False} if mode == "admission_off" else {})
    cfg = settings(**changes)
    if mode == "reserved":
        # Set the all-mode facts without requiring a Spanner process.
        cfg.fastpath_pilot_all = True
        cfg.async_settle_fastpath_reserved_workspaces = "ws_test_1"
        cfg.parse_fastpath_reserved_workspaces()
    assert call(facts, settings=cfg) == {"async_eligible": False}
    facts.runtime.signer.sign.assert_not_called()


@pytest.fixture
def client(facts, monkeypatch):
    monkeypatch.setattr(route, "typed_billing_store", lambda _: SimpleNamespace(request_record_write_mode="typed"))
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: NOW, monotonic=service.time.monotonic))
    app = create_app(facts.settings, init_observability=False)
    app.state.async_settle = facts.runtime
    with TestClient(app) as client:
        yield client


def post(client, body, token=TOKEN):
    headers = {"X-Trustedrouter-Internal-Token": token} if token is not None else {}
    return client.post(route.PATH, json=body, headers=headers)


def test_registered_route_issues(facts, client):
    response = post(client, facts.body.model_dump())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == call(facts)
    assert "settlement_ticket" in response.json()


@pytest.mark.parametrize("token", [None, "wrong-test-token"])
def test_route_requires_token(facts, client, token):
    assert post(client, facts.body.model_dump(), token).status_code == 401
    facts.runtime.signer.sign.assert_not_called()


@pytest.mark.parametrize("change", ["extra", "missing", "type", "hash", "zero", "sixty_five", "duplicates", "bool_version", "float_version", "string_time", "integer_stream", "candidate_type"])
def test_route_rejects_malformed_body(facts, client, change):
    body = facts.body.model_dump()
    if change == "extra":
        body["extra"] = True
    elif change == "missing":
        del body["reservation_id"]
    else:
        field, value = {
            "type": ("authorization_id", 123), "hash": ("billing_snapshot_hash", "A" * 64),
            "zero": ("candidate_endpoint_ids", []),
            "sixty_five": ("candidate_endpoint_ids", [f"endpoint_test_{i}" for i in range(65)]),
            "duplicates": ("candidate_endpoint_ids", [facts.candidate.id] * 2),
            "bool_version": ("v", True), "float_version": ("v", 1.0),
            "string_time": ("priced_at", str(NOW)), "integer_stream": ("streamed", 1),
            "candidate_type": ("candidate_endpoint_ids", [1]),
        }[change]
        body[field] = value
    assert post(client, body).status_code == 422
    facts.runtime.signer.sign.assert_not_called()


# Every surface the setting can name, so a surface added later is covered.
SURFACES = get_args(Settings.model_fields["service_surface"].annotation)
GATEWAY_SURFACES = {"combined", "internal"}


def test_surface_list_is_the_settings_own():
    assert GATEWAY_SURFACES < set(SURFACES)
    assert len(SURFACES) == len(set(SURFACES)) >= 7


@pytest.mark.parametrize("surface", SURFACES)
def test_route_is_registered_on_the_gateway_surfaces_only(facts, monkeypatch, surface):
    registered = surface in GATEWAY_SURFACES
    monkeypatch.setattr(route, "typed_billing_store", lambda _: SimpleNamespace(request_record_write_mode="typed"))
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: NOW, monotonic=service.time.monotonic))
    cfg = settings(service_surface=surface)
    assert cfg.fastpath_edge_async_tickets_enabled
    app = create_app(cfg, init_observability=False)
    app.state.async_settle = facts.runtime
    with TestClient(app) as surface_client:
        response = post(surface_client, facts.body.model_dump())
    if registered:
        assert response.status_code == 200
        assert "settlement_ticket" in response.json()
    else:
        assert response.status_code == 404
        facts.runtime.signer.sign.assert_not_called()


def test_switch_off_does_not_import_route():
    script = '''
import sys
from trusted_router.config import Settings
from trusted_router.main import create_app
from fastapi.testclient import TestClient
app = create_app(Settings(environment="test", storage_backend="memory", sentry_dsn=None,
                          fastpath_edge_async_ticket_workspaces=""), init_observability=False)
assert "trusted_router.routes.internal.fastpath_async_ticket" not in sys.modules
assert TestClient(app).post("/internal/fastpath/async-ticket", json={}).status_code == 404
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,  # noqa: S603
                            env={**os.environ, "TR_FASTPATH_EDGE_ASYNC_TICKET_WORKSPACES": ""}, timeout=300)
    assert result.returncode == 0, result.stderr


def test_catalog_uses_requested_price_instant(facts, monkeypatch):
    original = catalog.effective_endpoint
    effective = Mock(wraps=original)
    monkeypatch.setattr(catalog, "effective_endpoint", effective)
    assert "settlement_ticket" in call(facts, now=NOW + 100)
    effective.assert_called_once_with(facts.candidate, at=datetime.fromtimestamp(NOW, UTC))


def test_refusal_order(facts, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="trusted_router.async_settle_timing")
    lookup = Mock(side_effect=RuntimeError("test"))
    monkeypatch.setattr(catalog, "endpoint_for_id", lookup)
    changes = {"settings": settings(fastpath_edge_async_ticket_workspaces=""), "runtime": None,
               "typed": False, "now": NOW + 301}
    for reason in ("workspace", "region", "authority", "records", "price_instant", "error"):
        caplog.clear()
        assert call(facts, **changes) == {"async_eligible": False}
        metric(caplog, facts, "refused", reason, "na")
        if reason == "workspace":
            changes["settings"] = settings(async_settle_negotiation_regions="europe-west4")
        elif reason == "region":
            changes["settings"] = facts.settings
        elif reason == "authority":
            changes["runtime"] = facts.runtime
        elif reason == "records":
            changes["typed"] = True
        elif reason == "price_instant":
            changes["now"] = NOW
    lookup.assert_called_once()
    facts.runtime.signer.sign.assert_not_called()


@pytest.mark.parametrize("backend", [None, SimpleNamespace(request_record_write_mode="legacy"), SimpleNamespace()])
def test_route_requires_typed_store(facts, client, monkeypatch, backend):
    monkeypatch.setattr(route, "typed_billing_store", lambda _: backend)
    response = post(client, facts.body.model_dump())
    assert response.status_code == 200
    assert response.json() == {"async_eligible": False}
    facts.runtime.signer.sign.assert_not_called()


def test_workspace_environment_and_normalization(monkeypatch):
    monkeypatch.setenv("TR_FASTPATH_EDGE_ASYNC_TICKET_WORKSPACES", "ws_test_2, ws_test_1,ws_test_2")
    cfg = Settings(environment="test", async_settle_enabled=True, async_settle_protection=True)
    assert cfg.fastpath_edge_async_ticket_workspace_ids == {"ws_test_1", "ws_test_2"}
    assert cfg.fastpath_edge_async_ticket_allows("ws_test_1")
    ids = ",".join(f"ws_test_{i}" for i in range(32))
    assert len(settings(fastpath_edge_async_ticket_workspaces=ids).fastpath_edge_async_ticket_workspace_ids) == 32


def test_sixty_four_candidates_allowed(facts, monkeypatch):
    endpoints = [replace(facts.candidate, id=f"endpoint_test_{i:02}") for i in range(64)]
    by_id = {e.id: e for e in endpoints}
    monkeypatch.setattr(catalog, "endpoint_for_id", by_id.get)
    snapshot = build_snapshot([catalog.effective_endpoint(e, at=datetime.fromtimestamp(NOW, UTC)) for e in endpoints], Eligibility())
    body = route.EdgeTicketRequest.model_validate({**facts.body.model_dump(),
        "candidate_endpoint_ids": list(by_id), "billing_snapshot_hash": canonical_hash(snapshot)})
    assert "settlement_ticket" in call(facts, body=body)
