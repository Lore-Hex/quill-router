"""Production wiring. No mock adapters, schema creation, or wallet authority."""

import logging
import os
import re
import ssl
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from .app import create_app
from .credentials import Credentials
from .lexe import Lexe
from .lnd import Lnd
from .rates import Rates
from .service import Funding
from .store import Store, invoices
from .trustedrouter import TrustedRouterCredits

logger = logging.getLogger("lightning_router")


class Readiness:
    def __init__(self, funding: Funding, credits: TrustedRouterCredits) -> None:
        self.funding = funding
        self.credits = credits
        self.checked: float | None = None
        self.ready = False
        self.lock = threading.Lock()

    def __call__(self) -> bool:
        with self.lock:
            if self.checked is not None and time.monotonic() - self.checked < 5:
                return self.ready
            try:
                with self.funding.store.transaction() as connection:
                    connection.execute(select(invoices.c.id).limit(1)).first()
                self.credits.health()
                self.ready = self.funding.receiving_ready()
            except Exception as exc:
                self.ready = False
                logger.error("lightning.readiness_failed error_type=%s", type(exc).__name__)
            self.checked = time.monotonic()
            return self.ready


def production_app() -> FastAPI:
    database_url = os.environ["LR_DATABASE_URL"]
    if not database_url.startswith("postgresql+psycopg://"):
        raise ValueError("Production checkout storage requires PostgreSQL")
    # Fixed private endpoint, certificate verification mandatory. Secret mounts
    # cannot select a different network peer or an arbitrary funding authority.
    endpoint = os.environ["LR_CREDITS_ENDPOINT"]
    if endpoint != "https://trustedrouter.com":
        raise ValueError("Unrecognized funding authority")
    macaroon = Path(os.environ["LR_LND_MACAROON_FILE"]).read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64,8192}", macaroon):
        raise ValueError("Invalid invoice macaroon")
    context = ssl.create_default_context(cafile=os.environ["LR_LND_CERT_FILE"])
    lnd_client = httpx.Client(base_url="https://10.92.0.2:8080", verify=context,
                            headers={"Grpc-Metadata-macaroon": macaroon},
                            timeout=8, follow_redirects=False, trust_env=False)
    credit_client = httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
    rate_client = httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
    credits = TrustedRouterCredits(endpoint, os.environ["LR_CREDITS_TOKEN"], credit_client)
    store = Store(database_url)
    lexe = None
    if os.environ.get("LR_LEXE_WALLET_ID"):
        lexe = Lexe(httpx.Client(base_url="http://127.0.0.1:5393", timeout=20, follow_redirects=False, trust_env=False),
                    os.environ["LR_LEXE_WALLET_ID"])
    funding = Funding(store, Credentials(bytes.fromhex(os.environ["LR_CHECKOUT_SECRET"])),
                      Lnd(lnd_client), Rates(rate_client), credits, check_capacity=True,
                      lexe=lexe, new_invoice_backend=os.environ.get("LR_INVOICE_BACKEND", "lnd"))
    store.pin_credentials(funding.credentials)
    readiness = Readiness(funding, credits)
    if not readiness():
        raise RuntimeError("Funding startup preflight failed; previous revision must remain serving")
    return create_app(funding, rates=funding.rates, readiness=readiness, edge_rate_limited=True)


def migrate() -> None:
    """Run only as the separately invoked migration job, never at web startup."""
    url = os.environ["LR_DATABASE_URL"]
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("Production migration requires PostgreSQL")
    store = Store(url)
    store.migrate()
    password = os.environ["LR_DATABASE_APP_PASSWORD"]
    if not re.fullmatch(r"[0-9a-f]{64}", password):
        raise ValueError("Invalid application database credential")
    # The migration identity owns schema objects; the runtime role can only
    # read/write funding records and cannot change schema or create roles.
    from psycopg import sql
    with store.engine.begin() as connection:
        driver = connection.connection.driver_connection
        assert driver is not None
        cursor = driver.cursor()
        cursor.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls FROM pg_roles WHERE rolname = 'lr_app'")
        attributes = cursor.fetchone()
        if attributes is not None:
            if any(attributes):
                raise ValueError("Existing runtime database role has elevated privileges")
            # Cloud SQL admins cannot restate even NOSUPERUSER on ALTER ROLE.
            # Verify the privilege boundary, then change only login credentials.
            statement = "ALTER ROLE lr_app LOGIN PASSWORD {}"
        else:
            statement = "CREATE ROLE lr_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
        cursor.execute(sql.SQL(statement).format(sql.Literal(password)))
        assert store.engine.url.database is not None
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO lr_app").format(sql.Identifier(store.engine.url.database)))
        cursor.execute("GRANT USAGE ON SCHEMA public TO lr_app")
        cursor.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON lr_checkouts, lr_invoices, lr_deposits, lr_rate_limits, lr_settings TO lr_app")


if __name__ == "__main__":
    migrate()
