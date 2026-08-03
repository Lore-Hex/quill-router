"""The shadow workspace a federated key needs, and what it may NOT contain.

Two separate risks are pinned here.

The BUG: a federated key resolved fine and then 403'd on every request, because
`upsert_federated_api_key` wrote the key, its lookup index and a typed key-limit
row — but no workspace. `_authorize_gateway_sync` reads
`STORE.get_workspace(api_key.workspace_id)` and rejects a missing one BEFORE
credits are ever consulted, so federation was end-to-end broken in a way that
looked like a billing problem.

The RISK the fix introduces: a shadow is a second copy of somebody else's
record. Every field carried can drift, and a field carried carelessly is an
entitlement this plane granted itself on another plane's behalf. So the tests
below assert what is ABSENT at least as hard as what is present.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.fakes.postgres import (
    SqlitePostgresConn,
    postgres_store_on,
    sqlite_postgres_conn,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.services import federation
from trusted_router.services.federation import FederationClient
from trusted_router.storage import STORE, InMemoryStore, configure_store
from trusted_router.storage_errors import StoreConflict
from trusted_router.storage_models import (
    FEDERATED_WORKSPACE_NAME,
    federated_workspace_from_record,
)

HOME_RECORD = {
    "lookup_hash": "lh-fed-1",
    "key_hash": "kh-fed-1",
    "workspace_id": "ws-home-1",
    "name": "prod key",
    "disabled": False,
    "limit_microdollars": 5_000_000,
    "include_byok_in_limit": True,
    "workspace_billing_paused": False,
    "revision": "2026-08-03T00:00:00Z",
}


# --------------------------------------------------------------------------
# What the shadow carries, and what it must not
# --------------------------------------------------------------------------


class TestShadowContents:
    def test_the_workspace_id_is_carried(self) -> None:
        """The join key back to the home plane, and what every workspace-scoped
        read on the authorize path keys on."""
        assert federated_workspace_from_record(HOME_RECORD).id == "ws-home-1"

    def test_it_is_marked_as_federated(self) -> None:
        """A reconciliation must be able to tell a shadow from a workspace
        somebody created here. A shadow deleted as 'orphaned' would take a
        paying customer's access with it."""
        assert federated_workspace_from_record(HOME_RECORD).federated_home != ""

    def test_it_has_no_owner(self) -> None:
        """This plane has no user directory. An invented owner id would either
        be meaningless or — worse — collide with a real local user and hand
        them a stranger's workspace."""
        assert federated_workspace_from_record(HOME_RECORD).owner_user_id == ""

    def test_no_member_row_is_written(self) -> None:
        """A shadow must never surface in a local console listing: nobody
        here is a member of it."""
        store = InMemoryStore()
        store.upsert_federated_api_key(HOME_RECORD)
        assert store.members == {}

    def test_content_storage_stays_off(self) -> None:
        """An ENTITLEMENT the home plane does not serve. Defaulting it off
        means a federated workspace's request content is never stored in a
        second jurisdiction because of a copied flag."""
        assert federated_workspace_from_record(HOME_RECORD).content_storage_enabled is False

    def test_the_name_is_not_fabricated(self) -> None:
        """The home plane serves no workspace name. A real-looking one would be
        a field nobody sent, free to drift, and easy to mistake for the
        customer's actual workspace name in an operator console."""
        assert federated_workspace_from_record(HOME_RECORD).name == FEDERATED_WORKSPACE_NAME

    def test_a_home_plane_pause_is_carried(self) -> None:
        """The one workspace field the home plane's allow-list serves, and the
        only one authorize reads. It is a RESTRICTION, so copying it can only
        ever refuse work."""
        workspace = federated_workspace_from_record(
            {**HOME_RECORD, "workspace_billing_paused": True}
        )
        assert workspace.billing_paused is True
        assert "home plane" in workspace.billing_pause_reason

    def test_no_credits_arrive_with_the_workspace(self) -> None:
        """Identity federates; money does not. This is the conservation law:
        a shadow that arrived pre-funded would have minted that money."""
        store = InMemoryStore()
        store.upsert_federated_api_key(HOME_RECORD)
        money = store.credit_money["ws-home-1"]
        assert money.total_credits_microdollars == 0
        assert money.reserved_microdollars == 0
        assert money.total_usage_microdollars == 0


# --------------------------------------------------------------------------
# Materialization: atomic, and non-destructive
# --------------------------------------------------------------------------


class TestMaterialization:
    def test_the_key_and_its_workspace_appear_together(self) -> None:
        store = InMemoryStore()
        store.upsert_federated_api_key(HOME_RECORD)

        assert store.get_key_by_lookup_hash("lh-fed-1") is not None
        assert store.get_workspace("ws-home-1") is not None

    def test_it_refuses_to_overwrite_a_real_local_workspace(self) -> None:
        """A directory collision must be loud. Overwriting would replace a
        tenant's workspace with an ownerless shadow — unrecoverable, and
        silent. Refusing to federate one key is the cheap failure."""
        store = InMemoryStore()
        user = store.ensure_user("local", "local@example.com")
        local = store.create_workspace(user.id, "real", trial_credit_microdollars=500)

        with pytest.raises(StoreConflict, match="not federated"):
            store.upsert_federated_api_key({**HOME_RECORD, "workspace_id": local.id})

        assert store.get_workspace(local.id).name == "real"
        assert store.credit_money[local.id].total_credits_microdollars == 500

    def test_re_federating_does_not_reset_a_transferred_balance(self) -> None:
        """The money bug hiding in an upsert. A key re-resolved after a cache
        miss must not zero a balance a completed credit transfer funded."""
        store = InMemoryStore()
        store.upsert_federated_api_key(HOME_RECORD)
        store.claim_credit_transfer(
            transfer_id="t-fund",
            workspace_id="ws-home-1",
            amount_microdollars=750_000,
            source="home",
            accept=True,
        )

        store.upsert_federated_api_key(HOME_RECORD)

        assert store.credit_money["ws-home-1"].total_credits_microdollars == 750_000


# --------------------------------------------------------------------------
# Atomicity, against the REAL Postgres statements
# --------------------------------------------------------------------------
#
# Postgres is the backend the AWS plane actually runs, and atomicity is a claim
# an InMemory twin cannot test — a dict-and-lock store has no way to fail
# half-way. See tests/fakes/postgres.py for what this harness does and does not
# cover.


@pytest.fixture
def pg_conn() -> SqlitePostgresConn:
    return sqlite_postgres_conn()


class TestPostgresAtomicity:
    def test_the_workspace_and_key_commit_together(self, pg_conn: SqlitePostgresConn) -> None:
        postgres_store_on(pg_conn).upsert_federated_api_key(HOME_RECORD)

        assert pg_conn.count_entities("workspace") == 1
        assert pg_conn.count_entities("api_key") == 1
        assert pg_conn.count_entities("api_key_lookup") == 1
        assert pg_conn.balance_row_count() == 1

    def test_a_failed_key_write_leaves_no_workspace(self, pg_conn: SqlitePostgresConn) -> None:
        """The atomicity claim, driven.

        The workspace is written FIRST, so a naive implementation would leave a
        committed shadow workspace behind when the key write fails — an
        ownerless workspace with a credit-balance row and no key, i.e. a
        durable place for a later transfer to deposit money that nothing can
        ever spend.
        """
        pg_conn.fail_on = "tr_key_limit"

        with pytest.raises(RuntimeError, match="connection reset"):
            postgres_store_on(pg_conn).upsert_federated_api_key(HOME_RECORD)

        assert pg_conn.count_entities("workspace") == 0, "workspace survived a failed key write"
        assert pg_conn.count_entities("api_key") == 0
        assert pg_conn.balance_row_count() == 0, "an orphan credit-balance row was left behind"

    def test_the_balance_row_is_seeded_at_zero(self, pg_conn: SqlitePostgresConn) -> None:
        """It must EXIST (a transfer credits it with a conditional UPDATE that
        would otherwise match no rows) and it must be EMPTY."""
        postgres_store_on(pg_conn).upsert_federated_api_key(HOME_RECORD)

        assert pg_conn.balance(HOME_RECORD["workspace_id"]) == (0, 0, 0)

    def test_re_federating_does_not_reset_a_funded_balance(
        self, pg_conn: SqlitePostgresConn
    ) -> None:
        """The money bug hiding in an upsert, on the backend that ships it.

        The balance seed is `ON CONFLICT DO NOTHING` for exactly this: a key
        re-resolved after a cache miss must not zero a balance a completed
        credit transfer funded. An `ON CONFLICT DO UPDATE` here would silently
        delete transferred money on the next federation refresh.
        """
        store = postgres_store_on(pg_conn)
        store.upsert_federated_api_key(HOME_RECORD)
        store.claim_credit_transfer(
            transfer_id="t-pg-fund",
            workspace_id="ws-home-1",
            amount_microdollars=900_000,
            source="home",
            accept=True,
        )

        store.upsert_federated_api_key(HOME_RECORD)

        assert pg_conn.spendable("ws-home-1") == 900_000


# --------------------------------------------------------------------------
# End to end: the exact bug
# --------------------------------------------------------------------------


def _federating_client(monkeypatch: pytest.MonkeyPatch, record: dict[str, Any]) -> TestClient:
    """An app whose home plane serves `record` for one lookup hash."""
    from trusted_router.routes.internal import gateway as gateway_routes

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-trustedrouter-federation-token"] == "home-token"
        return httpx.Response(200, json={"data": record})

    federation_client = FederationClient(
        home_base_url="https://trustedrouter.com",
        peer_token="home-token",  # noqa: S106 - test fixture.
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        gateway_routes, "_federation_client", lambda *_a, **_k: federation_client
    )
    # `_federate_api_key` reads `get_settings()`, which rebuilds Settings from
    # the ENVIRONMENT rather than taking the request's injected Settings — so
    # the app-level object below does not reach it. Setting both is deliberate:
    # if that ever gets fixed to use the injected settings, this fixture keeps
    # working instead of silently testing nothing.
    monkeypatch.setenv("TR_FEDERATION_HOME_BASE_URL", "https://trustedrouter.com")
    monkeypatch.setenv("TR_FEDERATION_HOME_TOKEN", "home-token")
    settings = Settings(
        environment="test",
        sentry_dsn=None,
        internal_gateway_token=None,
        stripe_secret_key=None,
        stripe_webhook_secret=None,
        federation_home_base_url="https://trustedrouter.com",
        federation_home_token="home-token",  # noqa: S106 - test fixture.
    )
    return TestClient(create_app(settings, init_observability=False))


def _authorize(client: TestClient, lookup_hash: str) -> httpx.Response:
    return client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": lookup_hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": 20,
            "max_output_tokens": 4,
        },
    )


class TestFederatedRequestReachesAuthorize:
    def test_a_federated_key_no_longer_403s_on_a_missing_workspace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE BUG. Before the shadow workspace existed, this returned 403
        "Workspace is unavailable" — a federated key could never spend, and the
        credit path below was unreachable.

        Asserted positively as well as negatively: `!= 403` alone would pass on
        a 401 too, which is a DIFFERENT federation failure (the key never
        resolved) and would leave this test green while proving nothing.
        """
        client = _federating_client(monkeypatch, HOME_RECORD)

        response = _authorize(client, "lh-fed-1")

        assert response.status_code != 403, response.text
        assert "Workspace is unavailable" not in response.text
        assert response.status_code != 401, "the key did not federate at all"
        assert STORE.get_workspace("ws-home-1") is not None
        assert STORE.get_key_by_lookup_hash("lh-fed-1") is not None

    def test_it_reaches_the_credit_check_and_says_where_the_money_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Having reached authorize, a federated key with no transferred
        credits must not look like a customer who is out of money.

        A bare 402 "Insufficient credits" would send someone with a healthy
        home-plane balance to top up an account that is already funded, and
        would tell the operator nothing about the actual fix (run a transfer).
        """
        client = _federating_client(monkeypatch, HOME_RECORD)

        response = _authorize(client, "lh-fed-1")

        assert response.status_code == 402, response.text
        error = response.json()["error"]
        assert error["type"] == "credits_not_on_this_plane"
        assert "transferred to this plane" in error["message"]

    def test_credits_transferred_in_make_the_same_key_spend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point of the two blockers together: identity federates,
        money moves explicitly, and then the key works."""
        client = _federating_client(monkeypatch, HOME_RECORD)
        assert _authorize(client, "lh-fed-1").status_code == 402

        STORE.claim_credit_transfer(
            transfer_id="t-e2e",
            workspace_id="ws-home-1",
            amount_microdollars=5_000_000,
            source="https://trustedrouter.com/v1",
            accept=True,
        )

        response = _authorize(client, "lh-fed-1")

        assert response.status_code == 200, response.text
        assert response.json()["data"]["credit_reservation_id"]

    def test_a_home_plane_pause_still_stops_the_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The carried restriction has to actually restrict, otherwise copying
        it bought nothing."""
        client = _federating_client(
            monkeypatch, {**HOME_RECORD, "workspace_billing_paused": True}
        )

        response = _authorize(client, "lh-fed-1")

        assert response.status_code == 503, response.text
        assert "billing is paused" in response.text.lower()

    def test_a_non_federated_workspace_still_gets_the_plain_402(self) -> None:
        """The new error must not leak onto ordinary local customers, who
        really are out of money."""
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            internal_gateway_token=None,
            stripe_secret_key=None,
            stripe_webhook_secret=None,
        )
        configure_store(InMemoryStore())
        client = TestClient(create_app(settings, init_observability=False))
        created = client.post(
            "/v1/keys", headers={"x-trustedrouter-user": "bob@example.com"}, json={"name": "k"}
        ).json()
        workspace_id = created["data"]["workspace_id"]
        STORE.credit_money[workspace_id].total_credits_microdollars = 0

        response = client.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": created["data"]["hash"],
                "model": "anthropic/claude-opus-4.7",
                "estimated_input_tokens": 20,
                "max_output_tokens": 4,
            },
        )

        assert response.status_code == 402, response.text
        assert response.json()["error"]["type"] == "insufficient_credits"


# --------------------------------------------------------------------------
# Staleness: the only revocation mechanism a peer plane has
# --------------------------------------------------------------------------


class TestAFederatedKeyStopsServingWhenItGoesStale:
    """A shadow is a CACHED COPY of a credential, and nothing invalidates it.

    `upsert_federated_api_key` runs on a cache MISS only. There is no
    revocation feed, no push, no refresh — so before the age check, a key the
    customer deleted at home kept authorizing here forever, spending whatever
    credits had been transferred to this plane, with a manual per-region row
    delete as the only remedy. The same holds for `workspace_billing_paused`:
    a pause that never arrives cannot quiesce anything.

    This was inert until the shadow workspace landed, because every federated
    request 403'd before it. Making federation work is what armed it.
    """

    def _revocable_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        answers: list[httpx.Response],
    ) -> tuple[TestClient, list[int]]:
        """An app whose home plane replies from `answers`, one per call."""
        from trusted_router.routes.internal import gateway as gateway_routes

        calls = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            index = min(calls[0], len(answers) - 1)
            calls[0] += 1
            return answers[index]

        federation_client = FederationClient(
            home_base_url="https://trustedrouter.com",
            peer_token="home-token",  # noqa: S106 - test fixture.
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        monkeypatch.setattr(
            gateway_routes, "_federation_client", lambda *_a, **_k: federation_client
        )
        monkeypatch.setenv("TR_FEDERATION_HOME_BASE_URL", "https://trustedrouter.com")
        monkeypatch.setenv("TR_FEDERATION_HOME_TOKEN", "home-token")
        settings = Settings(
            environment="test",
            sentry_dsn=None,
            internal_gateway_token=None,
            stripe_secret_key=None,
            stripe_webhook_secret=None,
            federation_home_base_url="https://trustedrouter.com",
            federation_home_token="home-token",  # noqa: S106 - test fixture.
        )
        return TestClient(create_app(settings, init_observability=False)), calls

    def _age_the_cached_key(self, seconds: int) -> None:
        """Backdate the shadow, which is what "time passed" means here."""
        import datetime as dt

        stored = STORE.get_key_by_lookup_hash(HOME_RECORD["lookup_hash"])
        assert stored is not None
        stale = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)
        stored.created_at = stale.isoformat().replace("+00:00", "Z")
        STORE.api_keys.keys[stored.hash] = stored

    def test_a_fresh_record_is_served_without_calling_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The availability property this whole workstream exists to buy.

        If the age check turned every request into a home-plane call, the
        coupling would be back and worse than before.
        """
        client, calls = self._revocable_client(
            monkeypatch, answers=[httpx.Response(200, json={"data": HOME_RECORD})]
        )

        for _ in range(3):
            _authorize(client, HOME_RECORD["lookup_hash"])

        assert calls[0] == 1

    def test_a_key_revoked_at_home_stops_working_here(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 404 from home is a VERDICT, not an outage: revoke immediately."""
        client, _calls = self._revocable_client(
            monkeypatch,
            answers=[
                httpx.Response(200, json={"data": HOME_RECORD}),
                httpx.Response(404, json={"error": {"message": "Unknown API key"}}),
            ],
        )
        first = _authorize(client, HOME_RECORD["lookup_hash"])
        assert first.status_code != 401, first.text

        # The customer deletes the key at home; time passes past the soft TTL.
        self._age_the_cached_key(federation.SOFT_TTL_SECONDS + 60)
        after = _authorize(client, HOME_RECORD["lookup_hash"])

        assert after.status_code == 401, after.text

    def test_a_home_outage_does_not_revoke_a_recently_seen_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """STALE-WHILE-REVALIDATE. Between the soft and hard TTLs, a home plane
        that cannot answer must not take this plane's requests down with it —
        that is the exact coupling being removed."""
        client, _calls = self._revocable_client(
            monkeypatch,
            answers=[
                httpx.Response(200, json={"data": HOME_RECORD}),
                httpx.Response(503, json={"error": {"message": "down"}}),
            ],
        )
        assert _authorize(client, HOME_RECORD["lookup_hash"]).status_code != 401

        self._age_the_cached_key(federation.SOFT_TTL_SECONDS + 60)
        during_outage = _authorize(client, HOME_RECORD["lookup_hash"])

        # Served from the stale copy: not a 401 (wrong — blames the customer)
        # and not a 503 (wrong — the record is still young enough to trust).
        assert during_outage.status_code not in (401, 503), during_outage.text

    def test_past_the_hard_ttl_an_unreachable_home_refuses_rather_than_serves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """503, never 401, and never "serve it anyway".

        At some age "probably still valid" stops being good enough for a
        credential, because a revocation may have been waiting the whole time.
        503 + Retry-After is the honest answer: it is our outage, not a bad key.
        """
        client, _calls = self._revocable_client(
            monkeypatch,
            answers=[
                httpx.Response(200, json={"data": HOME_RECORD}),
                httpx.Response(503, json={"error": {"message": "down"}}),
            ],
        )
        assert _authorize(client, HOME_RECORD["lookup_hash"]).status_code != 401

        self._age_the_cached_key(federation.HARD_TTL_SECONDS + 60)
        expired = _authorize(client, HOME_RECORD["lookup_hash"])

        assert expired.status_code == 503, expired.text
        assert expired.headers.get("Retry-After") == "5"

    def test_a_locally_issued_key_is_never_age_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a SHADOW expires. A key this plane issued is authoritative, and
        ageing it out would be inventing an expiry the customer never set."""
        client, calls = self._revocable_client(
            monkeypatch, answers=[httpx.Response(404, json={"error": {}})]
        )
        user = STORE.ensure_user("local", "local@example.com")
        workspace = STORE.create_workspace(
            user.id, "local", trial_credit_microdollars=1_000_000
        )
        _raw, created = STORE.create_api_key(
            workspace_id=workspace.id, name="local key", creator_user_id=user.id
        )
        # Older than any TTL. A locally issued key must not care.
        created.created_at = "2000-01-01T00:00:00Z"
        STORE.api_keys.keys[created.hash] = created

        response = _authorize(client, created.lookup_hash)

        assert response.status_code != 401, response.text
        assert calls[0] == 0


class TestTheNegativeCacheIsBounded:
    def test_unknown_keys_cannot_grow_the_cache_without_limit(self) -> None:
        """The entries are keyed on an ATTACKER-CHOSEN lookup hash.

        N requests with N random bearer tokens produce N distinct misses on a
        client that lives for the process lifetime. Expired entries were read
        past rather than deleted and nothing else evicted them, so the dict was
        an unbounded memory leak driven by unauthenticated traffic.
        """
        calls = [0]

        def handler(_request: httpx.Request) -> httpx.Response:
            calls[0] += 1
            return httpx.Response(404, json={"error": {"message": "Unknown API key"}})

        client = FederationClient(
            home_base_url="https://trustedrouter.com",
            peer_token="home-token",  # noqa: S106 - test fixture.
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

        for index in range(federation.NEGATIVE_MAX_ENTRIES * 2):
            assert client.resolve(f"random-lookup-{index}") is None

        assert len(client._negative) <= federation.NEGATIVE_MAX_ENTRIES
        # Still a cache, not a bypass: a repeat of a remembered miss is free.
        before = calls[0]
        last = f"random-lookup-{federation.NEGATIVE_MAX_ENTRIES * 2 - 1}"
        assert client.resolve(last) is None
        assert calls[0] == before
