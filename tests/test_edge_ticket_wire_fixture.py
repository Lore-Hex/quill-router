# ruff: noqa: F811 - shared fixtures
"""The edge ticket wire, one literal fixture read by Python and by the Go edge.

fastpath/testdata/edge_ticket_wire_v1.json holds the body the edge posts to the
sign-only route and the two answers the edge accepts. The Go edge's
TestEdgeTicketWireFixture (fastpath/internal/edge/ticket_test.go) marshals its
request type to the same request and parses the same answers, so a field one
side renames, adds or drops fails on that side's own tests.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tests.test_fastpath_edge_async_ticket import NOW, TOKEN, client, facts, settings  # noqa: F401
from trusted_router.billing_snapshot import canonical_hash
from trusted_router.routes.internal import fastpath_async_ticket as route
from trusted_router.services import async_settle as service

FIXTURE = Path(__file__).resolve().parents[1] / "fastpath" / "testdata" / "edge_ticket_wire_v1.json"
WIRE = json.loads(FIXTURE.read_text(encoding="utf-8"))
# The edge's own check of a ticket's form (compactTicket in ticket.go).
COMPACT = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def test_the_request_is_exactly_the_routes_model():
    body = route.EdgeTicketRequest.model_validate(WIRE["request"])
    assert body.model_dump() == WIRE["request"]
    assert list(route.EdgeTicketRequest.model_fields) == list(WIRE["request"])


def test_the_fixture_prices_the_facts_the_route_tests_use(facts):
    request = WIRE["request"]
    assert request["candidate_endpoint_ids"] == [facts.candidate.id]
    assert request["priced_at"] == NOW
    assert request["billing_snapshot_hash"] == canonical_hash(facts.snapshot)


def test_the_route_answers_the_fixture_request_in_the_issued_shape(client):
    response = client.post(route.PATH, content=json.dumps(WIRE["request"]),
                           headers={"X-Trustedrouter-Internal-Token": TOKEN, "Content-Type": "application/json"})
    assert response.status_code == 200
    answer = response.json()
    assert list(answer) == list(WIRE["issued"])
    assert answer["async_eligible"] is WIRE["issued"]["async_eligible"] is True
    assert answer["settlement_status_url"] == WIRE["issued"]["settlement_status_url"]
    assert COMPACT.fullmatch(answer["settlement_ticket"])
    assert COMPACT.fullmatch(WIRE["issued"]["settlement_ticket"])


def test_a_refusal_is_the_declined_answer(facts):
    body = route.EdgeTicketRequest.model_validate(WIRE["request"])
    refused = service.edge_ticket(body=body, settings=settings(fastpath_edge_async_ticket_workspaces="ws_other"),
                                  runtime=facts.runtime, typed=True, now=NOW)
    assert refused == WIRE["declined"]
