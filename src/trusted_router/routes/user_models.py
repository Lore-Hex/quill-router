from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import (
    ManagementPrincipal,
    Principal,
    SettingsDep,
    require_management,
)
from trusted_router.byok_crypto import decrypt_user_model_secret
from trusted_router.config import Settings
from trusted_router.custom_model_billing import (
    HUMAN_PRICE_MAX_MICRODOLLARS_PER_M,
    MACHINE_PRICE_MAX_MICRODOLLARS_PER_M,
    validate_custom_model_price,
)
from trusted_router.custom_model_rules import assert_user_can_create_custom_models
from trusted_router.errors import api_error
from trusted_router.money import format_money_display
from trusted_router.schemas import (
    EarningsTransferRequest,
    UserModelCreateRequest,
    UserModelPatchRequest,
)
from trusted_router.serialization import user_model_owner_shape
from trusted_router.services.user_model_probe import probe_user_model
from trusted_router.services.user_model_secrets import (
    USER_MODEL_SIGNING_PURPOSE,
    encrypt_user_model_endpoint_key,
    encrypt_user_model_signing_secret,
)
from trusted_router.storage import STORE
from trusted_router.storage_custom_models import (
    CUSTOM_MODEL_PREFIX,
    CUSTOM_MODEL_SLUG_PATTERN,
    normalize_custom_model_id,
)
from trusted_router.storage_models import UserProvidedModel
from trusted_router.storage_user_models import USER_PROVIDED_MODEL_LIMIT_PER_USER
from trusted_router.types import ErrorType
from trusted_router.user_model_rules import (
    validate_endpoint_url,
    validate_user_model_display_name,
    validate_user_model_slug,
    verify_clock_signature,
)

# Availability routes intentionally sit outside the normal management-only
# dependency: a model owner can call them with that model's signing secret.
# That also means an anonymous caller can reach the model-id boundary. Keep the
# pre-Store admission state process-wide and fixed-cardinality so changing the
# id on every request cannot grow a limiter map or turn misses into unbounded
# database work.
_CLOCK_ROUTE_MAX_IN_FLIGHT = 16
_CLOCK_ROUTE_MAX_LOOKUPS_PER_WINDOW = 32
_CLOCK_ROUTE_WINDOW_SECONDS = 1.0

# A probe can occupy a socket for up to twenty seconds. Two per process is
# enough to make progress without letting signed retries consume the whole
# outbound pool. Per-model exclusion/cadence uses fixed stripes rather than a
# process-lifetime dict keyed by attacker-controlled model ids.
_CLOCK_PROBE_MAX_IN_FLIGHT = 2
_CLOCK_PROBE_CADENCE_SECONDS = 15.0
_CLOCK_PROBE_STRIPES = 256


class _AdmissionLease:
    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._released = False

    def __enter__(self) -> _AdmissionLease:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._release()


class _ClockRouteAdmission:
    """Bound concurrent and per-second Store admission without keyed state."""

    def __init__(
        self,
        *,
        max_in_flight: int,
        max_per_window: int,
        window_seconds: float,
    ) -> None:
        self._slots = threading.BoundedSemaphore(max_in_flight)
        self._max_per_window = max_per_window
        self._window_seconds = window_seconds
        self._state_lock = threading.Lock()
        self._window_started = time.monotonic()
        self._accepted_in_window = 0

    def acquire(self) -> _AdmissionLease:
        if not self._slots.acquire(blocking=False):
            _raise_clock_rate_limit(1.0)

        try:
            now = time.monotonic()
            retry_after = 0.0
            with self._state_lock:
                elapsed = max(0.0, now - self._window_started)
                if elapsed >= self._window_seconds:
                    self._window_started = now
                    self._accepted_in_window = 0
                    elapsed = 0.0

                if self._accepted_in_window >= self._max_per_window:
                    retry_after = max(0.001, self._window_seconds - elapsed)
                else:
                    self._accepted_in_window += 1

            if retry_after > 0:
                _raise_clock_rate_limit(retry_after)
        except BaseException:
            self._slots.release()
            raise
        return _AdmissionLease(self._slots.release)


class _ClockProbeAdmission:
    """Very small global probe pool plus striped per-model exclusion."""

    def __init__(
        self,
        *,
        max_in_flight: int,
        cadence_seconds: float,
        stripes: int,
    ) -> None:
        self._global_slots = threading.BoundedSemaphore(max_in_flight)
        self._model_slots = [threading.Lock() for _ in range(stripes)]
        self._next_allowed = [-math.inf] * stripes
        self._state_lock = threading.Lock()
        self._cadence_seconds = cadence_seconds

    def acquire(self, model_id: str) -> _AdmissionLease:
        stripe = _clock_stripe(model_id, len(self._model_slots))
        model_slot = self._model_slots[stripe]
        if not model_slot.acquire(blocking=False):
            _raise_clock_rate_limit(1.0)

        global_acquired = False
        try:
            now = time.monotonic()
            with self._state_lock:
                retry_after = self._next_allowed[stripe] - now
            if retry_after > 0:
                _raise_clock_rate_limit(retry_after)
            if not self._global_slots.acquire(blocking=False):
                _raise_clock_rate_limit(1.0)
            global_acquired = True
        except BaseException:
            if global_acquired:
                self._global_slots.release()
            model_slot.release()
            raise

        def release() -> None:
            with self._state_lock:
                self._next_allowed[stripe] = (
                    time.monotonic() + self._cadence_seconds
                )
            self._global_slots.release()
            model_slot.release()

        return _AdmissionLease(release)


_CLOCK_ROUTE_ADMISSION = _ClockRouteAdmission(
    max_in_flight=_CLOCK_ROUTE_MAX_IN_FLIGHT,
    max_per_window=_CLOCK_ROUTE_MAX_LOOKUPS_PER_WINDOW,
    window_seconds=_CLOCK_ROUTE_WINDOW_SECONDS,
)
_CLOCK_PROBE_ADMISSION = _ClockProbeAdmission(
    max_in_flight=_CLOCK_PROBE_MAX_IN_FLIGHT,
    cadence_seconds=_CLOCK_PROBE_CADENCE_SECONDS,
    stripes=_CLOCK_PROBE_STRIPES,
)


def register_user_model_routes(router: APIRouter) -> None:
    @router.get("/user-models")
    async def list_user_models(principal: ManagementPrincipal) -> dict[str, Any]:
        models = STORE.list_user_models_for_user(_owner_user_id(principal))
        return {"data": [user_model_owner_shape(model) for model in models]}

    @router.get("/user-models/earnings")
    async def get_user_model_earnings(
        principal: ManagementPrincipal,
        before: str | None = None,
    ) -> dict[str, Any]:
        return {"data": _earnings_data(_owner_user_id(principal), before=before)}

    @router.post("/user-models/earnings/transfer")
    async def transfer_user_model_earnings(
        body: EarningsTransferRequest,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        owner_user_id = _owner_user_id(principal)
        if body.amount_microdollars <= 0:
            raise api_error(
                400,
                "Transfer amount must be positive",
                ErrorType.BAD_REQUEST,
            )
        workspace = next(
            (
                candidate
                for candidate in STORE.list_workspaces_for_user(owner_user_id)
                if candidate.id == body.workspace_id
            ),
            None,
        )
        if workspace is None:
            # Membership and existence deliberately collapse to the same answer.
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        if workspace.federated_home:
            raise api_error(
                400,
                "Earnings cannot be transferred to a federated workspace",
                ErrorType.BAD_REQUEST,
            )
        event_suffix = body.idempotency_key or str(uuid.uuid4())
        outcome = STORE.transfer_earnings_to_workspace(
            owner_user_id,
            workspace.id,
            body.amount_microdollars,
            f"earnings_transfer:{owner_user_id}:{event_suffix}",
        )
        if outcome == "insufficient":
            available = STORE.earnings_summary(owner_user_id)["available"]
            raise api_error(
                402,
                "Insufficient earnings",
                ErrorType.INSUFFICIENT_CREDITS,
                extra={"available": available},
            )
        summary = _earnings_summary_shape(STORE.earnings_summary(owner_user_id))
        return {
            "data": {
                "deduplicated": outcome == "duplicate",
                "summary": summary,
            }
        }

    @router.post("/user-models")
    async def create_user_model(
        body: UserModelCreateRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        owner_user_id = _owner_user_id(principal)
        assert_user_can_create_custom_models(STORE.get_user(owner_user_id), settings)
        slug = validate_user_model_slug(body.slug) if body.slug is not None else None
        display_name = validate_user_model_display_name(body.display_name)
        endpoint_url = await validate_endpoint_url(body.endpoint_url, settings)
        _validate_price(
            body.prompt_price_microdollars_per_million_tokens,
            body.completion_price_microdollars_per_million_tokens,
            kind=body.kind,
        )
        signing_secret = secrets.token_urlsafe(32)
        encrypted_signing_secret = encrypt_user_model_signing_secret(
            signing_secret,
            settings,
            workspace_id=principal.workspace.id,
        )
        encrypted_endpoint_api_key = (
            encrypt_user_model_endpoint_key(
                body.endpoint_api_key,
                settings,
                workspace_id=principal.workspace.id,
            )
            if body.endpoint_api_key is not None
            else None
        )
        try:
            model = STORE.create_user_model(
                owner_user_id=owner_user_id,
                owner_workspace_id=principal.workspace.id,
                name=body.name,
                kind=body.kind,
                description=body.description,
                display_identity=body.display_identity,
                display_name=display_name,
                endpoint_url=endpoint_url,
                upstream_model_id=body.upstream_model_id,
                encrypted_endpoint_api_key=encrypted_endpoint_api_key,
                endpoint_key_hint=_secret_hint(body.endpoint_api_key),
                encrypted_signing_secret=encrypted_signing_secret,
                supports_streaming=body.supports_streaming,
                heartbeat_interval_seconds=body.heartbeat_interval_seconds,
                max_concurrency=body.max_concurrency,
                prompt_price_microdollars_per_million_tokens=(
                    body.prompt_price_microdollars_per_million_tokens
                ),
                completion_price_microdollars_per_million_tokens=(
                    body.completion_price_microdollars_per_million_tokens
                ),
                human_verified=body.kind == "human",
                slug=slug,
            )
        except ValueError as exc:
            _raise_store_error(exc)
        data = user_model_owner_shape(model)
        data["signing_secret"] = signing_secret
        return JSONResponse({"data": data}, status_code=201)

    @router.get("/user-models/{model_id:path}")
    async def get_user_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        return {
            "data": user_model_owner_shape(
                _require_owner_user_model(model_id, principal)
            )
        }

    @router.patch("/user-models/{model_id:path}")
    async def patch_user_model(
        model_id: str,
        body: UserModelPatchRequest,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        owner_user_id = _owner_user_id(principal)
        assert_user_can_create_custom_models(STORE.get_user(owner_user_id), settings)
        existing = _require_owner_user_model(model_id, principal)
        patch = body.model_dump(exclude_unset=True)
        if "slug" in patch:
            patch["slug"] = validate_user_model_slug(str(patch["slug"]))
        if "display_name" in patch:
            patch["display_name"] = validate_user_model_display_name(
                str(patch["display_name"])
            )
        if "endpoint_url" in patch:
            patch["endpoint_url"] = await validate_endpoint_url(
                str(patch["endpoint_url"]), settings
            )
        kind = str(patch.get("kind", existing.kind))
        prompt_price = int(
            patch.get(
                "prompt_price_microdollars_per_million_tokens",
                existing.prompt_price_microdollars_per_million_tokens,
            )
        )
        completion_price = int(
            patch.get(
                "completion_price_microdollars_per_million_tokens",
                existing.completion_price_microdollars_per_million_tokens,
            )
        )
        _validate_price(prompt_price, completion_price, kind=kind)
        if "kind" in patch:
            patch["human_verified"] = kind == "human"
        if "endpoint_api_key" in body.model_fields_set:
            endpoint_api_key = patch.pop("endpoint_api_key", None)
            patch["encrypted_endpoint_api_key"] = (
                encrypt_user_model_endpoint_key(
                    str(endpoint_api_key),
                    settings,
                    workspace_id=existing.owner_workspace_id,
                )
                if endpoint_api_key is not None
                else None
            )
            patch["endpoint_key_hint"] = _secret_hint(
                None if endpoint_api_key is None else str(endpoint_api_key)
            )
        try:
            updated = STORE.update_user_model(
                existing.id,
                owner_user_id=existing.owner_user_id,
                patch=patch,
            )
        except ValueError as exc:
            _raise_store_error(exc)
        return {"data": user_model_owner_shape(updated)}

    @router.delete("/user-models/{model_id:path}")
    async def delete_user_model(
        model_id: str,
        principal: ManagementPrincipal,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        if not STORE.delete_user_model(existing.id, owner_user_id=existing.owner_user_id):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        return {"data": {"deleted": True, "id": existing.id}}

    @router.post("/user-models/{model_id:path}/clock-in")
    async def clock_in_user_model(
        model_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        canonical_model_id = _persisted_user_model_id(model_id)
        with _CLOCK_ROUTE_ADMISSION.acquire():
            existing = await _clock_target(canonical_model_id, request, settings)
            with _CLOCK_PROBE_ADMISSION.acquire(_clock_probe_subject(existing)):
                result = await probe_user_model(existing, settings)
            if not result.ok:
                await run_in_threadpool(
                    STORE.set_user_model_online,
                    existing.id,
                    owner_user_id=existing.owner_user_id,
                    online=False,
                )
                raise api_error(409, result.detail, ErrorType.CONFLICT)
            updated = await run_in_threadpool(
                STORE.set_user_model_online,
                existing.id,
                owner_user_id=existing.owner_user_id,
                online=True,
            )
        return {"data": user_model_owner_shape(updated)}

    @router.post("/user-models/{model_id:path}/clock-out")
    async def clock_out_user_model(
        model_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        canonical_model_id = _persisted_user_model_id(model_id)
        with _CLOCK_ROUTE_ADMISSION.acquire():
            existing = await _clock_target(canonical_model_id, request, settings)
            updated = await run_in_threadpool(
                STORE.set_user_model_online,
                existing.id,
                owner_user_id=existing.owner_user_id,
                online=False,
            )
        return {"data": user_model_owner_shape(updated)}

    @router.post("/user-models/{model_id:path}/heartbeat")
    async def heartbeat_user_model(
        model_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        canonical_model_id = _persisted_user_model_id(model_id)
        with _CLOCK_ROUTE_ADMISSION.acquire():
            existing = await _clock_target(canonical_model_id, request, settings)
            interval = existing.heartbeat_interval_seconds
            if interval is None:
                raise api_error(
                    400,
                    "No heartbeat interval is configured for this model",
                    ErrorType.BAD_REQUEST,
                )
            expires_at = datetime.now(UTC) + timedelta(seconds=2 * interval)
            updated = await run_in_threadpool(
                STORE.record_user_model_heartbeat,
                existing.id,
                expires_at=expires_at.isoformat().replace("+00:00", "Z"),
            )
        return {"data": user_model_owner_shape(updated)}

    @router.post("/user-models/{model_id:path}/probe")
    async def probe_user_model_route(
        model_id: str,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        result = await probe_user_model(existing, settings)
        updated = _require_owner_user_model(model_id, principal)
        return {
            "data": {
                **user_model_owner_shape(updated),
                "probe_detail": result.detail,
            }
        }

    @router.post("/user-models/{model_id:path}/rotate-secrets")
    async def rotate_user_model_secrets(
        model_id: str,
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        existing = _require_owner_user_model(model_id, principal)
        signing_secret = secrets.token_urlsafe(32)
        encrypted = encrypt_user_model_signing_secret(
            signing_secret,
            settings,
            workspace_id=existing.owner_workspace_id,
        )
        updated = STORE.update_user_model(
            existing.id,
            owner_user_id=existing.owner_user_id,
            patch={"encrypted_signing_secret": encrypted},
        )
        return {
            "data": {
                **user_model_owner_shape(updated),
                "signing_secret": signing_secret,
            }
        }


def _owner_user_id(principal: Principal) -> str:
    if principal.user is not None:
        return principal.user.id
    if principal.api_key is not None and principal.api_key.creator_user_id:
        return principal.api_key.creator_user_id
    raise api_error(
        403,
        "A user-owned management session or key is required",
        ErrorType.FORBIDDEN,
    )


def _earnings_data(user_id: str, *, before: str | None = None) -> dict[str, Any]:
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    by_model = STORE.custom_model_earnings_by_model(user_id, since=since)
    recent = STORE.list_credit_movements(
        f"user:{user_id}",
        limit=50,
        before=before,
    )
    return {
        # GET response only; transfer POSTs keep the strong default.
        "summary": _earnings_summary_shape(STORE.earnings_summary(user_id, allow_stale=True)),
        "by_model_30d": [
            {"model_id": model_id, "earned_microdollars": amount}
            for model_id, amount in sorted(
                by_model.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ],
        "recent": [
            {
                "account_id": movement.account_id,
                "movement_id": movement.movement_id,
                "kind": movement.kind,
                "amount_microdollars": movement.amount_microdollars,
                "amount_display": format_money_display(
                    movement.amount_microdollars
                ),
                "counterparty_account_id": movement.counterparty_account_id,
                "custom_model_id": movement.custom_model_id,
                "authorization_id": movement.authorization_id,
                "created_at": movement.created_at,
            }
            for movement in recent
        ],
    }


def _earnings_summary_shape(summary: dict[str, int]) -> dict[str, int | str]:
    return {
        "total_earned": summary["total_earned"],
        "total_earned_display": format_money_display(summary["total_earned"]),
        "total_transferred": summary["total_transferred"],
        "total_transferred_display": format_money_display(
            summary["total_transferred"]
        ),
        "available": summary["available"],
        "available_display": format_money_display(summary["available"]),
    }


async def _clock_target(
    model_id: str,
    request: Request,
    settings: Settings,
) -> UserProvidedModel:
    """Resolve the model for an availability call.

    Two ways in. A management principal (a session or a management key) is the
    console path. The other is a signature made with the model's OWN signing
    secret — the credential the owner already holds and already uses to check
    the requests TrustedRouter sends them. That is what the harness uses: a
    laptop that can clock one model in and out should not also be able to mint
    API keys or spend the workspace's credits, which is everything a management
    key can do. It deliberately does NOT extend to editing the model: changing
    endpoint_url decides where prompts go, so it stays with the owner's account.
    """
    # The route validates and admits the canonical persisted id before this
    # function. Keep the synchronous backend entirely off the event loop.
    model = await run_in_threadpool(STORE.get_user_model, model_id)
    signature = request.headers.get("tr-signature")
    if signature and model is not None and model.encrypted_signing_secret is not None:
        try:
            await run_in_threadpool(
                _verify_model_clock_signature,
                model,
                settings,
                signature,
                await request.body(),
            )
        except ValueError as exc:
            raise api_error(
                401,
                "Invalid model signature",
                ErrorType.UNAUTHORIZED,
            ) from exc
        return model
    principal = await run_in_threadpool(require_management, request, settings)
    if model is None or model.owner_user_id != _owner_user_id(principal):
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return model


def _persisted_user_model_id(model_id: str) -> str:
    """Validate the bounded persisted id grammar without touching Store."""

    # The canonical prefix plus the 64-character slug is well below this cap.
    # Check raw length first so normalization never does attacker-sized work.
    if len(model_id) > 128:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    canonical = normalize_custom_model_id(model_id)
    if not canonical.startswith(CUSTOM_MODEL_PREFIX):
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    slug = canonical.removeprefix(CUSTOM_MODEL_PREFIX)
    if not CUSTOM_MODEL_SLUG_PATTERN.fullmatch(slug):
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return canonical


def _verify_model_clock_signature(
    model: UserProvidedModel,
    settings: Settings,
    signature: str,
    body: bytes,
) -> None:
    encrypted_secret = model.encrypted_signing_secret
    if encrypted_secret is None:
        raise ValueError("model signing secret is unavailable")
    secret = decrypt_user_model_secret(
        encrypted_secret,
        settings,
        workspace_id=model.owner_workspace_id,
        purpose=USER_MODEL_SIGNING_PURPOSE,
    )
    verify_clock_signature(
        secret,
        signature,
        body,
        now=datetime.now(UTC),
    )


def _clock_stripe(value: str, stripe_count: int) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % stripe_count


def _clock_probe_subject(model: UserProvidedModel) -> str:
    # Recreating a deleted model with the same slug is a new persisted model,
    # so it must not inherit the old incarnation's cooldown. The encrypted
    # signing secret is unique per creation/rotation; hash it before building
    # the fixed-stripe subject so limiter state never retains ciphertext.
    encrypted_secret = model.encrypted_signing_secret
    incarnation = (
        f"{encrypted_secret.ciphertext}:{encrypted_secret.nonce}"
        if encrypted_secret is not None
        else model.created_at
    )
    incarnation_hash = hashlib.sha256(incarnation.encode("utf-8")).hexdigest()
    return f"{model.id}:{incarnation_hash}"


def _raise_clock_rate_limit(retry_after_seconds: float) -> NoReturn:
    raise api_error(
        429,
        "Model availability endpoint is busy; retry",
        ErrorType.RATE_LIMITED,
        headers={"Retry-After": str(max(1, math.ceil(retry_after_seconds)))},
    )


def _require_owner_user_model(
    model_id: str,
    principal: Principal,
) -> UserProvidedModel:
    owner_user_id = _owner_user_id(principal)
    model = STORE.get_user_model(normalize_custom_model_id(model_id))
    if model is None or model.owner_user_id != owner_user_id:
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
    return model


def _validate_price(prompt_price: int, completion_price: int, *, kind: str) -> None:
    try:
        validate_custom_model_price(prompt_price, completion_price, kind=kind)
    except ValueError as exc:
        cap = (
            HUMAN_PRICE_MAX_MICRODOLLARS_PER_M
            if kind == "human"
            else MACHINE_PRICE_MAX_MICRODOLLARS_PER_M
        )
        raise api_error(
            400,
            f"A {kind} model prices between 0 and {cap} microdollars per million "
            f"tokens; got {prompt_price} prompt and {completion_price} completion",
            ErrorType.BAD_REQUEST,
        ) from exc


def _raise_store_error(exc: ValueError) -> NoReturn:
    error = str(exc)
    if error == "custom_model_limit_exceeded":
        raise api_error(
            400,
            f"User-provided model limit reached ({USER_PROVIDED_MODEL_LIMIT_PER_USER})",
            ErrorType.BAD_REQUEST,
        ) from exc
    if error == "invalid_custom_model_slug":
        raise api_error(
            400,
            "Model slug must be 3-64 lowercase letters, numbers, or hyphens",
            ErrorType.BAD_REQUEST,
        ) from exc
    if error == "custom_model_slug_taken":
        raise api_error(409, "Model slug is already in use", ErrorType.CONFLICT) from exc
    if error == "custom_model_not_found":
        raise api_error(404, "Resource not found", ErrorType.NOT_FOUND) from exc
    raise exc


def _secret_hint(secret: str | None) -> str | None:
    if secret is None:
        return None
    stripped = secret.strip()
    if not stripped:
        return None
    return f"...{stripped[-4:]}"
