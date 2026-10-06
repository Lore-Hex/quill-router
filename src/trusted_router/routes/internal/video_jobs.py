from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.catalog import MODELS, PROVIDERS, endpoint_for_id
from trusted_router.errors import api_error
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.schemas import (
    GatewayAuthorizeRequest,
    GatewayVideoJobClaimRequest,
    GatewayVideoJobLookupRequest,
    GatewayVideoJobPrepareRequest,
    GatewayVideoJobQueuedRequest,
    GatewayVideoJobUpdateRequest,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import ProviderBenchmarkSample, VideoJob
from trusted_router.types import ErrorType
from trusted_router.video_billing import video_cost_microdollars, video_token_billed


def _job_payload(job: VideoJob) -> dict[str, Any]:
    payload = asdict(job)
    if job.status == "completed" and job.output_token_limit:
        # Read the authoritative billing outcome, not a quote or analytics row.
        authorization = STORE.get_gateway_authorization(job.authorization_id)
        if authorization is not None and authorization.settled:
            payload["settled_microdollars"] = authorization.finalized_cost_microdollars
            payload["output_tokens"] = authorization.finalized_output_tokens
    return payload


def _validate_video_quote(authorization: Any, endpoint: Any, quoted: int, token_limit: int) -> None:
    try:
        token_billed = video_token_billed(authorization.video_pricing_snapshot, endpoint)
    except ValueError as exc:
        raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc
    if token_billed:
        if quoted != 0 or token_limit <= 0:
            raise api_error(400, "Token-billed video requires a token reservation, not a fixed quote", ErrorType.BAD_REQUEST)
        try:
            video_cost_microdollars(
                authorization.video_pricing_snapshot, endpoint.id,
                output_tokens=token_limit, quoted_microdollars=quoted,
            )
        except ValueError as exc:
            raise api_error(400, str(exc), ErrorType.BAD_REQUEST) from exc
    elif quoted <= 0:
        raise api_error(400, "Fixed-price video requires a positive quote", ErrorType.BAD_REQUEST)


def _prepare(
    request: Request,
    body: GatewayVideoJobPrepareRequest,
    settings: Any,
) -> dict[str, Any]:
    require_internal_gateway(request, settings)
    authorization = STORE.get_gateway_authorization(body.authorization_id)
    if authorization is None:
        raise api_error(404, "Authorization not found", ErrorType.NOT_FOUND)
    if authorization.settled:
        # Finalization must forbid new work, not retrieval of an already paid
        # (or refunded) job. Never call prepare here: it can insert on a miss.
        existing = STORE.get_video_job_for_key(body.job_id, authorization.key_hash)
        if (
            existing is not None
            and existing.authorization_id == authorization.id
            and existing.workspace_id == authorization.workspace_id
            and existing.model == authorization.model_id == body.model
        ):
            return {"data": {**_job_payload(existing), "created": False}}
        raise api_error(409, "Authorization is already finalized", ErrorType.CONFLICT)
    model = MODELS.get(body.model)
    if model is None or not model.supports_video:
        raise api_error(
            400, "Model does not support video generation", ErrorType.MODEL_NOT_SUPPORTED
        )
    if authorization.model_id != body.model:
        raise api_error(400, "Video model does not match the authorization", ErrorType.BAD_REQUEST)
    allowed_endpoint_ids = set(authorization.candidate_endpoint_ids)
    if authorization.endpoint_id:
        allowed_endpoint_ids.add(authorization.endpoint_id)
    if body.endpoint_id not in allowed_endpoint_ids:
        raise api_error(400, "Video endpoint was not authorized", ErrorType.BAD_REQUEST)
    endpoint = endpoint_for_id(body.endpoint_id)
    if endpoint is None or endpoint.model_id != body.model or endpoint.provider != body.provider:
        raise api_error(
            400, "Video provider does not match the authorized endpoint", ErrorType.BAD_REQUEST
        )
    if body.quoted_microdollars > authorization.additional_cost_reservation_microdollars:
        raise api_error(
            400, "Video quote exceeds the authorized reservation", ErrorType.BAD_REQUEST
        )
    _validate_video_quote(authorization, endpoint, body.quoted_microdollars, body.output_token_limit)
    job = VideoJob(
        id=body.job_id,
        workspace_id=authorization.workspace_id,
        key_hash=authorization.key_hash,
        authorization_id=authorization.id,
        model=body.model,
        provider=body.provider,
        endpoint_id=body.endpoint_id,
        provider_model=body.provider_model,
        quoted_microdollars=body.quoted_microdollars,
        output_token_limit=body.output_token_limit,
        input_mode=body.input_mode,
        duration_seconds=body.duration_seconds,
        resolution=body.resolution,
        aspect_ratio=body.aspect_ratio,
        generate_audio=body.generate_audio,
        region=body.region,
    )
    stored, created = STORE.prepare_video_job(job)
    return {"data": {**_job_payload(stored), "created": created}}


def _lookup(
    request: Request,
    body: GatewayVideoJobLookupRequest,
    settings: Any,
    job_id: str,
) -> dict[str, Any]:
    require_internal_gateway(request, settings)
    api_key = STORE.get_key_by_lookup_hash(body.api_key_lookup_hash)
    if api_key is None or api_key.disabled:
        raise api_error(401, "Invalid API key", ErrorType.UNAUTHORIZED)
    job = STORE.get_video_job_for_key(job_id, api_key.hash)
    if job is None:
        raise api_error(404, "Video job not found", ErrorType.NOT_FOUND)
    return {"data": _job_payload(job)}


def _replay_lookup(
    request: Request,
    body: GatewayAuthorizeRequest,
    settings: Any,
) -> dict[str, Any]:
    """Lookup only: a miss must never reach authorization, pricing or reserve."""
    from trusted_router.auth import is_api_key_expired
    from trusted_router.routes.internal.gateway import (
        _api_key_for_gateway_lookup,
        _assert_gateway_key_scope,
        _video_cross_region_replay_matches,
    )
    from trusted_router.storage import typed_billing_store
    from trusted_router.storage_legacy_trust import BillingPausedError

    require_internal_gateway(request, settings)
    if (
        body.route_type != "videos"
        or not body.idempotency_key
        or not body.request_fingerprint
        or not body.api_key_lookup_hash
        or body.api_key_hash
        or body.additional_cost_reservation_microdollars
        or body.invocation_nonce
    ):
        raise api_error(400, "Invalid read-only video replay lookup", ErrorType.BAD_REQUEST)
    api_key = _api_key_for_gateway_lookup(
        api_key_hash=None, api_key_lookup_hash=body.api_key_lookup_hash,
    )
    if api_key is None or api_key.disabled or is_api_key_expired(api_key.expires_at):
        raise api_error(401, "Invalid API key", ErrorType.INVALID_API_KEY)
    _assert_gateway_key_scope(api_key)
    workspace = STORE.get_workspace(api_key.workspace_id)
    if workspace is None:
        raise api_error(401, "Invalid API key", ErrorType.INVALID_API_KEY)
    # Same normalization as authorize before its logical fingerprint. Dynamic
    # quotes and invocation identity have never been part of video identity.
    original = body.model_dump(exclude_none=True)
    original.pop("additional_cost_reservation_microdollars", None)
    original.pop("invocation_nonce", None)
    if not body.inference_receipt:
        original.pop("inference_receipt", None)
    if body.tags is None:
        original.pop("tags", None)
    typed_store = typed_billing_store(STORE)
    try:
        authorization = (
            typed_store.get_typed_authorization_by_idempotency(
                workspace.id, api_key.hash, body.idempotency_key,
            )
            if typed_store is not None
            else STORE.get_gateway_authorization_by_idempotency_key(
                workspace.id, api_key.hash, body.idempotency_key,
            )
        )
    except BillingPausedError as exc:
        raise api_error(403, "billing_paused", ErrorType.FORBIDDEN) from exc
    if authorization is None:
        return {"data": {"found": False}}
    if not _video_cross_region_replay_matches(
        authorization, workspace.id, api_key.hash, original, body.idempotency_key,
    ):
        raise api_error(
            409, "Idempotency key was already used for a different gateway request",
            ErrorType.CONFLICT,
        )
    # Identity only. Never return routes, credentials, pricing, or a dispatch
    # nonce. The enclave must fetch the existing job at this same authority.
    return {"data": {"found": True, "authorization": {
        "authorization_id": authorization.id,
        "workspace_id": authorization.workspace_id,
        "api_key_hash": authorization.key_hash,
        "model": authorization.model_id,
        "idempotent_replay": True,
    }}}


def register(router: APIRouter) -> None:
    @router.post("/internal/gateway/video/replay-lookup")
    async def lookup_video_replay(
        request: Request,
        body: GatewayAuthorizeRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await run_in_threadpool(_replay_lookup, request, body, settings)


    @router.post("/internal/gateway/video/jobs/prepare")
    async def prepare_video_job(
        request: Request,
        body: GatewayVideoJobPrepareRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await run_in_threadpool(_prepare, request, body, settings)

    @router.post("/internal/gateway/video/jobs/{job_id}/queued")
    async def queued_video_job(
        job_id: str,
        request: Request,
        body: GatewayVideoJobQueuedRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        existing = await run_in_threadpool(STORE.get_video_job, job_id)
        if existing is None:
            raise api_error(404, "Video job not found", ErrorType.NOT_FOUND)
        authorization = await run_in_threadpool(
            STORE.get_gateway_authorization, existing.authorization_id
        )
        if authorization is None:
            raise api_error(404, "Authorization not found", ErrorType.NOT_FOUND)
        allowed_endpoint_ids = set(authorization.candidate_endpoint_ids)
        if authorization.endpoint_id:
            allowed_endpoint_ids.add(authorization.endpoint_id)
        provider = body.provider or existing.provider
        endpoint_id = body.endpoint_id or existing.endpoint_id
        provider_model = body.provider_model or existing.provider_model
        quoted_microdollars = existing.quoted_microdollars if body.quoted_microdollars is None else body.quoted_microdollars
        endpoint = endpoint_for_id(endpoint_id)
        if (
            endpoint_id not in allowed_endpoint_ids
            or endpoint is None
            or endpoint.model_id != existing.model
            or endpoint.provider != provider
        ):
            raise api_error(400, "Queued video route was not authorized", ErrorType.BAD_REQUEST)
        if quoted_microdollars > authorization.additional_cost_reservation_microdollars:
            raise api_error(
                400, "Video quote exceeds the authorized reservation", ErrorType.BAD_REQUEST
            )
        _validate_video_quote(authorization, endpoint, quoted_microdollars, existing.output_token_limit)
        job = await run_in_threadpool(
            STORE.mark_video_job_queued,
            job_id,
            provider_job_id=body.provider_job_id,
            provider=provider,
            endpoint_id=endpoint_id,
            provider_model=provider_model,
            quoted_microdollars=quoted_microdollars,
            poll_after_seconds=body.poll_after_seconds,
        )
        if job is None:
            raise api_error(404, "Video job not found", ErrorType.NOT_FOUND)
        return {"data": await run_in_threadpool(_job_payload, job)}

    @router.post("/internal/gateway/video/jobs/{job_id}/lookup")
    async def lookup_video_job(
        job_id: str,
        request: Request,
        body: GatewayVideoJobLookupRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        return await run_in_threadpool(_lookup, request, body, settings, job_id)

    @router.post("/internal/gateway/video/jobs/claim")
    async def claim_video_jobs(
        request: Request,
        body: GatewayVideoJobClaimRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        jobs = await run_in_threadpool(
            STORE.claim_video_jobs,
            lease_owner=body.lease_owner,
            limit=body.limit,
            lease_seconds=body.lease_seconds,
        )
        return {"data": await run_in_threadpool(lambda: [_job_payload(job) for job in jobs])}

    @router.post("/internal/gateway/video/jobs/{job_id}/update")
    async def update_video_job(
        job_id: str,
        request: Request,
        body: GatewayVideoJobUpdateRequest,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        job = await run_in_threadpool(
            STORE.update_video_job,
            job_id,
            status=body.status,
            lease_owner=body.lease_owner,
            provider_status=body.provider_status,
            generation_id=body.generation_id,
            error=body.error,
            poll_after_seconds=body.poll_after_seconds,
        )
        if job is None:
            raise api_error(404, "Video job not found", ErrorType.NOT_FOUND)
        if job.status == "failed":
            provider = PROVIDERS.get(job.provider)
            await run_in_threadpool(
                STORE.record_provider_benchmark,
                ProviderBenchmarkSample.from_video_job_failure(
                    job,
                    provider_name=provider.name if provider is not None else job.provider,
                ),
            )
        return {"data": await run_in_threadpool(_job_payload, job)}

    @router.post("/internal/gateway/video/jobs/{job_id}/cleaned")
    async def cleaned_video_job(
        job_id: str,
        request: Request,
        settings: SettingsDep,
    ) -> dict[str, Any]:
        require_internal_gateway(request, settings)
        job = await run_in_threadpool(STORE.mark_video_job_cleaned, job_id)
        if job is None:
            raise api_error(404, "Video job not found", ErrorType.NOT_FOUND)
        return {"data": await run_in_threadpool(_job_payload, job)}
