from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from trusted_router.auth import SettingsDep
from trusted_router.catalog import MODELS, endpoint_for_id
from trusted_router.errors import api_error
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.schemas import (
    GatewayVideoJobClaimRequest,
    GatewayVideoJobLookupRequest,
    GatewayVideoJobPrepareRequest,
    GatewayVideoJobQueuedRequest,
    GatewayVideoJobUpdateRequest,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import VideoJob
from trusted_router.types import ErrorType


def _job_payload(job: VideoJob) -> dict[str, Any]:
    return asdict(job)


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
    if body.quoted_microdollars != authorization.additional_cost_reservation_microdollars:
        raise api_error(
            400, "Video quote does not match the authorized reservation", ErrorType.BAD_REQUEST
        )
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


def register(router: APIRouter) -> None:
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
        job = await run_in_threadpool(
            STORE.mark_video_job_queued,
            job_id,
            provider_job_id=body.provider_job_id,
            provider_model=body.provider_model,
            poll_after_seconds=body.poll_after_seconds,
        )
        if job is None:
            raise api_error(404, "Video job not found", ErrorType.NOT_FOUND)
        return {"data": _job_payload(job)}

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
        return {"data": [_job_payload(job) for job in jobs]}

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
        return {"data": _job_payload(job)}

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
        return {"data": _job_payload(job)}
