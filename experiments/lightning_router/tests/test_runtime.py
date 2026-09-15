from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from lightning_router.app import create_app, from_environment
from lightning_router.lnd import Lnd
from lightning_router.runtime import Readiness, migrate, production_app


def node(*, active=True, synced=True, network="mainnet", balance="712128", pending=None):
    def handle(request):
        if request.url.path == "/v1/getinfo":
            return httpx.Response(200, json={"synced_to_chain": synced, "synced_to_graph": synced,
                                           "chains": [{"chain": "bitcoin", "network": network}]})
        assert request.url.path == "/v1/channels"
        return httpx.Response(200, json={"channels": [{
            "active": active, "remote_balance": balance, "pending_htlcs": pending or [],
            "remote_constraints": {"chan_reserve_sat": "7500", "max_pending_amt_msat": "742500000"},
            "local_constraints": {"max_pending_amt_msat": "337500000"},
        }]})
    return Lnd(httpx.Client(base_url="https://node.test", transport=httpx.MockTransport(handle)))


def test_inbound_capacity_uses_receiving_not_sending_limit():
    assert node().receiving_capacity() == 704628000
    assert node(pending=[{"amount": "10000"}]).receiving_capacity() == 694628000
    assert node(balance="900000").receiving_capacity() == 742500000


@pytest.mark.parametrize("kwargs", [{"active": False}, {"synced": False}, {"balance": "7400"}])
def test_no_capacity_when_node_or_channel_unavailable(kwargs):
    assert node(**kwargs).receiving_capacity() == 0


def test_wrong_network_fails_closed():
    with pytest.raises(ValueError, match="wrong network"):
        node(network="testnet").receiving_capacity()


def test_production_never_falls_back_to_sqlite(monkeypatch):
    monkeypatch.setenv("LR_PAYMENTS_ENABLED", "true")
    monkeypatch.setenv("LR_DATABASE_URL", "sqlite:///tmp.db")
    with pytest.raises(ValueError, match="PostgreSQL"):
        from_environment()
    with pytest.raises(ValueError, match="PostgreSQL"):
        migrate()


def test_untrusted_credit_endpoint_rejected_before_loading_credentials(monkeypatch):
    monkeypatch.setenv("LR_DATABASE_URL", "postgresql+psycopg://host/db")
    monkeypatch.setenv("LR_CREDITS_ENDPOINT", "https://evil.test")
    with pytest.raises(ValueError, match="Unrecognized funding"):
        production_app()


def test_readiness_failure_is_closed_and_redacted(funding, caplog):
    credits = Mock()
    credits.health.side_effect = RuntimeError("secret-key-do-not-log")
    check = Readiness(funding, credits)
    assert check() is False
    assert check() is False
    assert credits.health.call_count == 1
    assert "secret-key-do-not-log" not in caplog.text


def test_readiness_recovers_after_cache_expiry(funding):
    credits = Mock()
    funding.lnd = node()
    check = Readiness(funding, credits)
    assert check() is True
    check.checked = 0
    funding.lnd = node(active=False)
    assert check() is False


def test_disabled_readiness_blocks_new_invoice_but_keeps_recovery(funding, raw_key):
    ready = [True]
    with TestClient(create_app(funding, rates=funding.rates, readiness=lambda: ready[0], start_worker=False),
                    base_url="https://lightningrouter.ai") as client:
        headers = {"Authorization": "Bearer " + raw_key, "Idempotency-Key": "a" * 32,
                   "Origin": "https://lightningrouter.ai"}
        created = client.post("/api/invoices", headers=headers, json={"usd_cents": 100, "new_account": True})
        assert created.status_code == 200
        invoice = created.json()
        record = funding.store.invoice(invoice["id"], funding.credentials.fingerprint(raw_key))
        funding.lnd.pay(record["payment_hash"])
        ready[0] = False
        assert client.get("/health").json()["payments_ready"] is False
        assert client.post("/api/invoices", headers=headers, json={"usd_cents": 100, "new_account": True}).status_code == 503
        recovered = client.post(f"/api/invoices/{invoice['id']}/refresh", headers=headers, json={})
        assert recovered.json()["credited"] is True


def test_https_origin_works_behind_http_cloud_run_socket(funding, raw_key):
    with TestClient(create_app(funding, rates=funding.rates, network="regtest", start_worker=False),
                    base_url="http://lightningrouter.ai") as client:
        headers = {"Authorization": "Bearer " + raw_key, "Idempotency-Key": "b" * 32,
                   "Origin": "https://lightningrouter.ai"}
        assert client.post("/api/invoices", headers=headers, json={"usd_cents": 100, "new_account": True}).status_code == 200
        headers["Origin"] = "https://evil.test"
        headers["X-Forwarded-Host"] = "evil.test"
        assert client.post("/api/invoices", headers=headers, json={"usd_cents": 100}).status_code == 403


def test_capacity_checked_before_new_invoice_not_settlement(funding, raw_key):
    funding.check_capacity = True
    funding.lnd.receiving_capacity = lambda: 1
    with pytest.raises(ValueError, match="receiving capacity"):
        funding.create(raw_key, "c" * 32, 100, new=True)
    assert funding.store.pending() == []
    assert funding.credits.balances == {}


def test_postgres_migration_creates_restricted_runtime_role(monkeypatch):
    import os

    import psycopg
    from sqlalchemy.engine import make_url

    dsn = os.environ.get("LR_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("requires isolated local PostgreSQL")
    url = make_url(dsn)
    assert url.host in {"127.0.0.1", "localhost"} and url.database == "lightning_router_test"
    monkeypatch.setenv("LR_DATABASE_URL", dsn)
    monkeypatch.setenv("LR_DATABASE_APP_PASSWORD", "f" * 64)
    migrate()
    migrate()
    app_url = url.set(drivername="postgresql", username="lr_app", password="f" * 64)
    with psycopg.connect(app_url.render_as_string(hide_password=False), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolcreatedb, rolcreaterole, rolsuper FROM pg_roles WHERE rolname = current_user")
            assert cursor.fetchone() == (False, False, False)
            cursor.execute("SELECT 1 FROM lr_checkouts LIMIT 1")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("CREATE TABLE public.lr_unwanted (id integer)")


def test_postgres_migration_retries_without_superuser(monkeypatch):
    import os

    import psycopg
    from lightning_router.store import Store, metadata
    from sqlalchemy.engine import make_url

    dsn = os.environ.get("LR_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("requires isolated local PostgreSQL")
    url = make_url(dsn)
    assert url.host == "127.0.0.1" and url.database == "lightning_router_test"
    store = Store(dsn)
    metadata.drop_all(store.engine)
    store.engine.dispose()
    admin_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            for role in ("lr_app", "lr_migrator_test"):
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if cursor.fetchone():
                    cursor.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(psycopg.sql.Identifier(role)))
                    cursor.execute(psycopg.sql.SQL("DROP ROLE {}").format(psycopg.sql.Identifier(role)))
            cursor.execute("CREATE ROLE lr_migrator_test LOGIN CREATEROLE PASSWORD 'local-test-only'")
            cursor.execute("GRANT USAGE, CREATE ON SCHEMA public TO lr_migrator_test")
    deployer_url = url.set(username="lr_migrator_test")
    monkeypatch.setenv("LR_DATABASE_URL", deployer_url.render_as_string(hide_password=False))
    monkeypatch.setenv("LR_DATABASE_APP_PASSWORD", "e" * 64)
    migrate()
    migrate()
    with psycopg.connect(admin_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = 'lr_app'")
            assert cursor.fetchone() == (False, False, False, False, False)
    app_url = url.set(drivername="postgresql", username="lr_app", password="e" * 64)
    with psycopg.connect(app_url.render_as_string(hide_password=False), autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM lr_checkouts LIMIT 1")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cursor.execute("CREATE TABLE public.lr_unwanted (id integer)")


@pytest.mark.parametrize("privilege", range(5))
def test_migration_refuses_elevated_existing_runtime_role(monkeypatch, privilege):
    from unittest.mock import MagicMock

    store = MagicMock()
    cursor = store.engine.begin.return_value.__enter__.return_value.connection.driver_connection.cursor.return_value
    attributes = [False] * 5
    attributes[privilege] = True
    cursor.fetchone.return_value = tuple(attributes)
    monkeypatch.setattr("lightning_router.runtime.Store", lambda _: store)
    monkeypatch.setenv("LR_DATABASE_URL", "postgresql+psycopg://local.test/funding")
    monkeypatch.setenv("LR_DATABASE_APP_PASSWORD", "f" * 64)
    with pytest.raises(ValueError, match="elevated privileges"):
        migrate()
    assert cursor.execute.call_count == 1


def test_edge_limits_do_not_group_all_customers_under_proxy_address(funding, raw_key, monkeypatch):
    calls = []
    original = funding.store.rate_limit

    def rate_limit(identity, now, maximum=10):
        calls.append(identity)
        return original(identity, now, maximum)

    monkeypatch.setattr(funding.store, "rate_limit", rate_limit)
    with TestClient(create_app(funding, rates=funding.rates, network="regtest", edge_rate_limited=True, start_worker=False)) as client:
        result = client.post("/api/invoices", json={"usd_cents": 100, "new_account": True},
                             headers={"Authorization": "Bearer " + raw_key, "Idempotency-Key": "d" * 32})
        assert result.status_code == 200
    assert len(calls) == 1 and calls[0].startswith("key:")
