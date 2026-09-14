import hashlib
import logging
import os
import re
import ssl
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .credentials import Credentials
from .lnd import Lnd
from .rates import Rates
from .service import Funding
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / "web"
logger = logging.getLogger("lightning_router")


class CreateInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_account: StrictBool = False
    usd_cents: StrictInt = Field(ge=1, le=100_000)


class Catalog:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.models: list[dict[str, Any]] = []
        self.loaded = 0.0
        self.lock = threading.Lock()

    def current(self) -> list[dict[str, Any]]:
        with self.lock:
            if time.monotonic() - self.loaded < 300 and self.models:
                return self.models
            response = self.client.get("https://trustedrouter.com/v1/models")
            response.raise_for_status()
            models = []
            for item in response.json()["data"]:
                model_id = item.get("id", "")
                if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,180}", model_id):
                    continue
                policy = item.get("trustedrouter") or {}
                if (policy.get("prepaid_available") is not True or
                        policy.get("supports_chat") is not True or
                        policy.get("internal_only") or policy.get("configuration_hidden")):
                    continue
                architecture = item.get("architecture") or {}
                if "text" not in architecture.get("output_modalities", ["text"]):
                    continue
                if "tools" not in item.get("supported_parameters", []):
                    continue
                models.append({
                    "id": model_id, "name": str(item.get("name") or model_id)[:180],
                    "context": item.get("context_length") or 32768,
                    "output": (item.get("top_provider") or {}).get("max_completion_tokens") or 4096,
                })
            self.models = sorted(models, key=lambda row: row["name"].lower())
            self.loaded = time.monotonic()
            return self.models


def create_app(service: Funding | None = None, *, rates: Rates | None = None,
               catalog: Catalog | None = None, network: str = "mainnet",
               start_worker: bool = True) -> FastAPI:
    # Mainnet funding is intentionally not launchable until the attested gateway
    # can enforce this BTC ledger. Never take real deposits for unusable keys.
    if service is not None and network != "regtest":
        raise RuntimeError("Mainnet launch blocked: BTC inference billing is not integrated")
    client = httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
    rates = rates or Rates(client)
    catalog = catalog or Catalog(client)
    stop = threading.Event()

    def reconcile() -> None:
        while not stop.is_set():
            if service:
                try:
                    service.reconcile()
                except Exception as exc:
                    logger.error("lightning.worker_failed error_type=%s", type(exc).__name__)
            stop.wait(15)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        worker = threading.Thread(target=reconcile, daemon=True)
        if service and start_worker:
            worker.start()
        yield
        stop.set()
        if worker.is_alive():
            worker.join(timeout=10)
        client.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
        "lightningrouter.ai", "www.lightningrouter.ai", "lightning.trustedrouter.com",
        "localhost", "127.0.0.1", "testserver",
    ])

    @app.middleware("http")
    async def secure_headers(request: Request, call_next: Any) -> Any:
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdigit() or int(length) > 1024:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "cross_origin_request"}, status_code=403)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    @app.exception_handler(Exception)
    async def failure(_: Request, exc: Exception) -> JSONResponse:
        logger.error("lightning.request_failed error_type=%s", type(exc).__name__)
        return JSONResponse({"error": "temporarily_unavailable"}, status_code=503,
                            headers={"Cache-Control": "no-store", "Retry-After": "10"})

    def key(request: Request) -> tuple[str, str]:
        assert service is not None
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise ValueError("API key required")
        raw = header[7:]
        return raw, service.credentials.fingerprint(raw)

    def unavailable() -> JSONResponse:
        return JSONResponse({"error": "payments_not_ready"}, status_code=503)

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "up", "payments_ready": service is not None, "inference_ready": False}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {"payments_ready": service is not None, "network": network,
                "inference_ready": False, "api_base": "https://api.lightningrouter.ai/v1"}

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        assert catalog is not None
        return {"data": catalog.current(), "inference_ready": False}

    @app.get("/api/quote")
    def quote(usd_cents: int = 1000) -> Any:
        assert rates is not None
        rate = rates.current()
        try:
            amount = rate.invoice_msats(usd_cents)
        except ValueError:
            return JSONResponse({"error": "invalid_amount"}, status_code=400)
        from .money import btc
        return {"usd_cents": str(usd_cents), "btc": btc(amount), "amount_msat": str(amount),
                "usd_per_btc": str(rate.usd_per_btc), "as_of": rate.as_of}

    @app.get("/api/account")
    def account(request: Request) -> Any:
        if service is None:
            return unavailable()
        try:
            _, hashed = key(request)
            balance: dict[str, Any] = service.store.balance(hashed)
            balance["active_invoice"] = service.store.active(hashed)
        except (ValueError, KeyError):
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        assert rates is not None
        # Rate outages must not hide a customer's exact BTC balance.
        try:
            rate = rates.current()
            balance["usd_estimate"] = rate.usd_estimate(int(balance["balance_msat"]))
            balance["rate_as_of"] = rate.as_of
        except Exception:
            balance["usd_estimate"] = None
        return balance

    @app.post("/api/invoices")
    def create(request: Request, body: CreateInvoice) -> Any:
        if service is None:
            return unavailable()
        try:
            raw, hashed = key(request)
        except ValueError:
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        request_id = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            return JSONResponse({"error": "idempotency_key_required"}, status_code=400)
        # Use the socket peer, not an untrusted X-Forwarded-For. Deployment must
        # also enforce per-source limits at ingress before any paid operation.
        peer = request.client.host if request.client else "unknown"
        identity = hashlib.sha256(peer.encode()).hexdigest()
        now = int(time.time())
        if not service.store.rate_limit("peer:" + identity, now, 40) or not service.store.rate_limit("key:" + hashed, now):
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "900"})
        try:
            return service.create(raw, request_id, body.usd_cents, new=body.new_account)
        except KeyError:
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        except ValueError:
            return JSONResponse({"error": "invoice_conflict"}, status_code=409)

    def invoice_action(invoice_id: str, request: Request, cancel: bool) -> Any:
        if service is None:
            return unavailable()
        try:
            _, hashed = key(request)
            row = service.store.invoice(invoice_id, hashed)
        except (ValueError, KeyError):
            return JSONResponse({"error": "invoice_not_found"}, status_code=404)
        return service.refresh(row, cancel=cancel)

    @app.post("/api/invoices/{invoice_id}/refresh")
    def refresh(invoice_id: str, request: Request) -> Any:
        return invoice_action(invoice_id, request, False)

    @app.post("/api/invoices/{invoice_id}/cancel")
    def cancel(invoice_id: str, request: Request) -> Any:
        return invoice_action(invoice_id, request, True)

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app


def from_environment() -> FastAPI:
    if os.environ.get("LR_PAYMENTS_ENABLED") != "true":
        return create_app()
    network = os.environ.get("LR_NETWORK", "mainnet")
    if network != "regtest":
        raise RuntimeError("Mainnet payments remain blocked pending BTC inference integration")
    store = Store(os.environ["LR_DATABASE_URL"])
    credentials = Credentials(bytes.fromhex(Path(os.environ["LR_SECRET_FILE"]).read_text().strip()))
    cert = ssl.create_default_context(cafile=os.environ["LR_LND_TLS_CERT"])
    macaroon = Path(os.environ["LR_LND_INVOICE_MACAROON"]).read_bytes().hex()
    base_url = os.environ["LR_LND_URL"]
    if not base_url.startswith("https://"):
        raise ValueError("LND TLS verification is mandatory")
    lnd = Lnd(httpx.Client(base_url=base_url, verify=cert, timeout=8, trust_env=False,
                          headers={"Grpc-Metadata-macaroon": macaroon}), network=network)
    rates = Rates(httpx.Client(timeout=8, follow_redirects=False, trust_env=False))
    service = Funding(store, credentials, lnd, rates)
    return create_app(service, rates=rates, network=network)
