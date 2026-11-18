"""LightningRouter, the retired Bitcoin funding product, has no credit ingress.

The product was retired on 2026-10-22 (docs/lightning-retirement.md) and its
credit bridge was later deleted. Credits it granted stay readable as ordinary
ledger rows. The unrelated Lightning AI inference provider is out of scope.
"""

from datetime import UTC, datetime
from typing import get_args
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from tests.route_inventory import route_paths
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import InMemoryStore
from trusted_router.storage_gcp import SpannerStore
from trusted_router.storage_models import CreditProvenance
from trusted_router.storage_postgres import PostgresStore
from trusted_router.store_protocol import Store

SURFACES = get_args(Settings.model_fields["service_surface"].annotation)
# The funding worker's credit, account and readiness calls, and the funded-key
# feedback form.
RETIRED_PATHS = tuple(
    prefix + path
    for prefix in ("", "/v1")
    for path in (
        "/internal/lightning/account",
        "/internal/lightning/balance",
        "/internal/lightning/credit",
        "/internal/lightning/health",
        "/internal/lightning/resolve",
        "/lightning/feedback",
    )
)


@pytest.mark.parametrize("surface", SURFACES)
def test_no_surface_serves_a_lightning_funding_route(surface: str) -> None:
    app = create_app(
        Settings(environment="test", service_surface=surface),
        configure_store_arg=False,
        init_observability=False,
    )
    assert not [path for path in route_paths(app) if "lightning" in path.lower()]
    client = TestClient(app)
    body = {"account_id": "workspace", "payment_hash": "a" * 64, "amount_microdollars": 1}
    for path in RETIRED_PATHS:
        assert client.post(path, json=body).status_code == 404, path


@pytest.mark.parametrize("store", [Store, InMemoryStore, SpannerStore, PostgresStore])
def test_no_store_can_create_lightning_identities_or_payments(store: type) -> None:
    assert not [name for name in dir(store) if "lightning" in name.lower()]


def test_postgres_ledger_still_rejects_noncanonical_lightning_event_before_sql() -> None:
    # Historical Lightning credits keep their provider in tr_trust_event. The
    # write-side guard stays so no other path can record one under a new ID.
    conn = Mock()
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="canonical event ID"):
        PostgresStore._insert_credit_trust_event_tx(
            conn, workspace_id="workspace", event_id="arbitrary-id",
            amount_microdollars=1,
            provenance=CreditProvenance("invoice", "lightning", "a" * 64, now),
            recorded_at=now, payment_amount_microdollars=1, currency="USD",
        )
    conn.execute.assert_not_called()
