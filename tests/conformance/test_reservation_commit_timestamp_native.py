"""tr_reservation.created_at is the commit's timestamp, on the native emulator.

The column is allow_commit_timestamp=true, and Spanner refuses a value in such
a column later than the commit's own timestamp (FAILED_PRECONDITION, "Cannot
write timestamps in the future"). A node whose clock runs ahead of TrueTime by
more than the time from its reading to the commit would fail every authorize
that bound its reading there. Both of Python's reservation writers, the leased
authorize and today's atomic authorize, are driven here with a clock reading
an hour ahead: each commits, its reservation's created_at is the commit's
timestamp, and the authorization row keeps the reading. The control shows the
emulator refuses the same reading bound into the column, so the two passes
mean what they say.
"""
from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from google.api_core.exceptions import FailedPrecondition
from google.cloud.spanner_v1 import param_types as pt

from tests.conformance.spanner_emulator import emulator_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome, authorize_atomic
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")

AHEAD = dt.timedelta(hours=1)


def _authorization(workspace: str, key_hash: str):
    def build(authorization_id: str, reservation_id: str) -> GatewayAuthorization:
        return GatewayAuthorization(
            id=authorization_id, workspace_id=workspace, key_hash=key_hash, model_id="model",
            provider="provider", usage_type=UsageType.CREDITS, estimated_microdollars=1,
            credit_reservation_id=reservation_id,
        )
    return build


def _rows(database, reservation_id: str, authorization_id: str) -> tuple[dt.datetime, dt.datetime]:
    with database.snapshot(multi_use=True) as snapshot:
        (reservation_created,), = list(snapshot.execute_sql(
            "SELECT created_at FROM tr_reservation WHERE reservation_id=@rid",
            params={"rid": reservation_id}, param_types={"rid": pt.STRING},
        ))
        (authorization_created,), = list(snapshot.execute_sql(
            "SELECT created_at FROM tr_gateway_authorization WHERE authorization_id=@aid",
            params={"aid": authorization_id}, param_types={"aid": pt.STRING},
        ))
    return reservation_created, authorization_created


def _assert_commit_timestamp(reservation_created: dt.datetime, before: dt.datetime) -> None:
    # The commit's timestamp: after the test began, and not the reading.
    after = dt.datetime.now(dt.UTC)
    assert before - dt.timedelta(seconds=5) <= reservation_created <= after + dt.timedelta(seconds=5)


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_a_client_clock_ahead_is_refused_in_the_column(native_emulator_resources, backend):
    """The control: the emulator enforces the rule the fix sidesteps."""
    database, _ = native_emulator_resources
    ahead = dt.datetime.now(dt.UTC) + AHEAD

    def insert(transaction):
        transaction.execute_update(
            "INSERT INTO tr_reservation (reservation_id, created_at) VALUES (@rid, @created_at)",
            params={"rid": "commit-ts-control-" + uuid4().hex, "created_at": ahead},
            param_types={"rid": pt.STRING, "created_at": pt.TIMESTAMP},
        )

    with pytest.raises(FailedPrecondition, match="(?i)future"):
        database.run_in_transaction(insert)


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_the_leased_authorize_commits_with_a_clock_ahead(native_emulator_resources, backend):
    _, instance_id = native_emulator_resources
    suffix = uuid4().hex
    workspace, key_hash = "commit-ts-ws-" + suffix, "commit-ts-kh-" + suffix
    authorization_id = "gwa-" + suffix
    before = dt.datetime.now(dt.UTC)
    ahead = before + AHEAD
    with emulator_store(instance_id) as store:
        store.request_record_write_mode = "typed"
        verdict, authorization = store._authorize_gateway_leased(
            build_authorization=_authorization(workspace, key_hash),
            authorization_id=authorization_id, workspace_id=workspace, key_hash=key_hash,
            idempotency_key="idem-" + suffix, idempotency_fingerprint="fp-" + suffix,
            reservation_usage_type="Credits", expires_at=ahead + dt.timedelta(hours=2),
            fastpath_envelope="envelope", fastpath_end_of_life=None, created_at=ahead,
            window_decision=None,
        )
        assert verdict == AuthorizeOutcome.ACCEPTED
        assert authorization is not None
        reservation_created, authorization_created = _rows(
            store._database, authorization.credit_reservation_id, authorization_id)
    _assert_commit_timestamp(reservation_created, before)
    # The authorization row and its payload keep the reading.
    assert authorization_created == ahead
    assert authorization.created_at == ahead.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_the_atomic_authorize_commits_with_a_clock_ahead(native_emulator_resources, backend):
    database, _ = native_emulator_resources
    suffix = uuid4().hex
    workspace, key_hash = "commit-ts-ws-" + suffix, "commit-ts-kh-" + suffix
    with database.batch() as batch:
        batch.insert("tr_credit_balance", columns=("workspace_id", "shard", "total_credits", "total_usage", "reserved"),
                     values=[(workspace, 0, 1_000_000, 0, 0)])
    before = dt.datetime.now(dt.UTC)
    ahead = before + AHEAD
    result = authorize_atomic(
        database, pt, workspace_id=workspace, key_hash=key_hash, estimate=10,
        has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope="scope-" + suffix, idempotency_fingerprint="fp-" + suffix,
        expires_at=ahead + dt.timedelta(hours=2), build_authorization=_authorization(workspace, key_hash),
        request_record_write_mode="typed", skip_key_limit=True, created_at=ahead,
    )
    assert result["outcome"] == AuthorizeOutcome.ACCEPTED, result
    reservation_created, authorization_created = _rows(
        database, result["reservation_id"], result["authorization_id"])
    _assert_commit_timestamp(reservation_created, before)
    assert authorization_created == ahead
