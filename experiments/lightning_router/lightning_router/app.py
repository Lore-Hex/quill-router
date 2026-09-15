import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable
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

from .catalog import limits, pricing
from .errors import FundingReviewRequired
from .lookup import LookupGate
from .pages import public_page
from .rates import Rates
from .reasoning import reasoning_profile
from .service import Funding

STATIC = Path(__file__).resolve().parent.parent / "web"
logger = logging.getLogger("lightning_router")


class CreateInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_account: StrictBool = False
    usd_cents: StrictInt = Field(ge=1, le=100_000)


class Feedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, repr=False)
    message: str = Field(min_length=1, max_length=3000, repr=False)


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
                    **limits(item), "pricing": pricing(item),
                    "reasoning": reasoning_profile(item),
                })
            self.models = sorted(models, key=lambda row: row["name"].lower())
            self.loaded = time.monotonic()
            return self.models


def create_app(service: Funding | None = None, *, rates: Rates | None = None,
               catalog: Catalog | None = None, network: str = "mainnet",
               start_worker: bool = True, readiness: Callable[[], bool] | None = None,
               api_base: str = "https://api.trustedrouter.com/v1", edge_rate_limited: bool = False) -> FastAPI:
    # Mainnet funding needs a live USD credit backend and funded Lightning
    # receiving capacity. Inference continues to use the existing USD ledger.
    if service is not None and network != "regtest" and readiness is None:
        raise RuntimeError("Mainnet launch blocked: Lightning settlement and USD credit delivery are not verified")
    if api_base not in {"https://api.trustedrouter.com/v1", "https://api.lightningrouter.ai/v1"}:
        raise ValueError("Unrecognized attested inference endpoint")
    client = httpx.Client(timeout=8, follow_redirects=False, trust_env=False)
    rates = rates or Rates(client)
    catalog = catalog or Catalog(client)
    stop = threading.Event()
    lookup_gate = LookupGate()

    def payments_ready() -> bool:
        return service is not None and (readiness is None or readiness())

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
            maximum = 16_384 if request.url.path == "/api/feedback" else 1024
            if not length.isdigit() or int(length) > maximum:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            expected_origin = "https://" + host
            if host.split(":")[0] in {"localhost", "127.0.0.1", "testserver"}:
                expected_origin = str(request.base_url).rstrip("/")
            if origin and origin != expected_origin:
                return JSONResponse({"error": "cross_origin_request"}, status_code=403)
        gated = request.url.path in {"/api/account", "/api/usage", "/api/feedback"}
        if gated and not lookup_gate.slots.acquire(blocking=False):
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "10"})
        try:
            if gated:
                await lookup_gate.delay()
            response = await call_next(request)
        except Exception as exc:
            # Catch before Starlette's outer ServerErrorMiddleware re-raises to
            # Uvicorn, which otherwise logs traceback/SQL parameter contents.
            logger.error("lightning.request_failed error_type=%s", type(exc).__name__)
            response = JSONResponse({"error": "temporarily_unavailable"}, status_code=503,
                                    headers={"Retry-After": "10"})
        finally:
            if gated:
                lookup_gate.slots.release()
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

    def lookup_limited(request: Request) -> JSONResponse | None:
        assert service is not None
        peer = request.client.host if request.client else "unknown"
        # Production's source-IP limit is enforced by Cloud Armor. Never trust
        # forwarded headers here or use a proxy's shared IP as a customer ID.
        raw = request.headers.get("authorization", "")[:512]
        now = int(time.time())
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        identity = hashlib.sha256(peer.encode()).hexdigest()
        if (not edge_rate_limited and not service.store.rate_limit("lookup-peer:" + identity, now, 120)) or not service.store.rate_limit("lookup-key:" + hashed, now, 60):
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "900"})
        return None

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health() -> JSONResponse:
        delivery = service.store.delivery_health(int(time.time())) if service else {}
        ready = payments_ready()
        degraded = not ready or delivery.get("review_required", 0) > 0 or delivery.get("oldest_uncredited_seconds", 0) >= 120
        return JSONResponse({"status": "degraded" if degraded else "up", "payments_ready": ready,
                             "inference_configured": service is not None, "delivery": delivery}, status_code=503 if degraded else 200)

    @app.get("/usage")
    def usage_page() -> Any:
        return public_page("usage")

    @app.get("/pricing")
    def pricing_page() -> Any:
        return public_page("pricing")

    @app.get("/terms")
    def terms_page() -> Any:
        return public_page("terms")

    @app.get("/docs")
    def docs_page() -> Any:
        return public_page("docs")

    @app.get("/privacy")
    def privacy_page() -> Any:
        return public_page("privacy")

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {"payments_ready": payments_ready(), "network": network,
                "inference_configured": service is not None, "api_base": api_base}

    @app.get("/api/models")
    def models() -> dict[str, Any]:
        assert catalog is not None
        return {"data": catalog.current(), "inference_configured": service is not None}

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
                **rate.quote_fields(amount), "as_of": rate.as_of}

    @app.get("/api/account")
    def account(request: Request) -> Any:
        if service is None:
            return unavailable()
        limited = lookup_limited(request)
        if limited is not None:
            return limited
        try:
            raw, _ = key(request)
            balance = service.account(raw)
        except (ValueError, KeyError):
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        return balance

    @app.get("/api/usage")
    def usage(request: Request) -> Any:
        if service is None:
            return unavailable()
        limited = lookup_limited(request)
        if limited is not None:
            return limited
        try:
            raw, _ = key(request)
        except ValueError:
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        try:
            return service.credits.usage(raw)
        except KeyError:
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)

    @app.post("/api/feedback")
    def feedback(request: Request, body: Feedback) -> Any:
        if service is None:
            return unavailable()
        limited = lookup_limited(request)
        if limited is not None:
            return limited
        email, message = body.email.strip(), body.message.strip()
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", email) or not message:
            return JSONResponse({"error": "invalid_feedback"}, status_code=400)
        if re.search(r"sk-tr-v1-", email + message, re.IGNORECASE):
            return JSONResponse({"error": "remove_api_keys"}, status_code=400)
        try:
            raw, _ = key(request)
        except ValueError:
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        try:
            account = service.credits.account(raw)
            if not account.support_eligible:
                return JSONResponse({"error": "funded_key_required"}, status_code=403)
            # Shared database, scoped to account rather than key: rotating keys
            # or hitting a different web replica cannot multiply the allowance.
            if not service.store.rate_limit("feedback:" + account.account_id, int(time.time()), 3):
                return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "900"})
            service.credits.feedback(raw, email, message)
        except KeyError:
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        return {"sent": True}

    @app.post("/api/invoices")
    def create(request: Request, body: CreateInvoice) -> Any:
        if service is None or not payments_ready():
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
        if (not edge_rate_limited and not service.store.rate_limit("peer:" + identity, now, 40)) or not service.store.rate_limit("key:" + hashed, now):
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "900"})
        try:
            return service.create(raw, request_id, body.usd_cents, new=body.new_account)
        except FundingReviewRequired:
            row = service.store.by_request(hashed, request_id)
            if row is None:
                return unavailable()
            return service.public(row)
        except KeyError:
            return JSONResponse({"error": "invalid_api_key"}, status_code=401)
        except ValueError:
            return JSONResponse({"error": "invoice_conflict"}, status_code=409)

    def invoice_action(invoice_id: str, request: Request, cancel: bool) -> Any:
        if service is None:
            return unavailable()
        try:
            raw, hashed = key(request)
            row = service.store.invoice(invoice_id, hashed)
            # An unpaid checkout has only a secret ownership capability. Once
            # it is bound to a real account, still enforce key revocation.
            if service.store.credit_account(hashed) is not None:
                service.account(raw)
        except (ValueError, KeyError):
            return JSONResponse({"error": "invoice_not_found"}, status_code=404)
        try:
            return service.refresh(row, cancel=cancel)
        except FundingReviewRequired:
            return service.public(service.store.invoice(invoice_id, hashed))

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
    from .runtime import production_app
    return production_app()
