from __future__ import annotations

import logging
from typing import Any

from trusted_router.byok_crypto import encrypted_secret_payload
from trusted_router.config import Settings
from trusted_router.services.broadcast_adapters import adapter_for
from trusted_router.storage import STORE, BroadcastDestination, Generation

log = logging.getLogger(__name__)


def broadcast_secret_context(destination_id: str, kind: str) -> str:
    return f"broadcast:{destination_id}:{kind}"


def public_destination_shape(destination: BroadcastDestination) -> dict[str, Any]:
    return {
        "id": destination.id,
        "workspace_id": destination.workspace_id,
        "type": destination.type,
        "name": destination.name,
        "endpoint": destination.endpoint,
        "enabled": destination.enabled,
        "include_content": destination.include_content,
        "method": destination.method,
        "api_key_configured": destination.encrypted_api_key is not None,
        "header_names": list(destination.header_names),
        "headers_configured": destination.encrypted_headers is not None,
        "created_at": destination.created_at,
        "updated_at": destination.updated_at,
    }


def gateway_destination_payload(destination: BroadcastDestination) -> dict[str, Any] | None:
    if not destination.enabled or not destination.include_content:
        return None
    return {
        "id": destination.id,
        "type": destination.type,
        "endpoint": destination.endpoint,
        "method": destination.method,
        "include_content": True,
        "api_key_context": broadcast_secret_context(destination.id, "api_key"),
        "headers_context": broadcast_secret_context(destination.id, "headers"),
        "encrypted_api_key": encrypted_secret_payload(destination.encrypted_api_key),
        "encrypted_headers": encrypted_secret_payload(destination.encrypted_headers),
    }


async def test_destination(destination: BroadcastDestination, settings: Settings) -> tuple[bool, str]:
    try:
        adapter = adapter_for(destination.type)
        if adapter is None:
            return False, "unknown destination type"
        return await adapter.test(destination, settings)
    except Exception as exc:  # noqa: BLE001 - connection test returns message.
        return False, str(exc)


def enqueue_metadata_broadcast(
    generation: Generation,
    *,
    settle_body: dict[str, Any],
) -> None:
    for destination in STORE.list_broadcast_destinations(generation.workspace_id):
        if not destination.enabled or destination.include_content:
            continue
        STORE.enqueue_broadcast_delivery(
            workspace_id=generation.workspace_id,
            destination_id=destination.id,
            generation_id=generation.id,
            settle_body=settle_body,
        )


def drain_broadcast_queue(*, settings: Settings, limit: int = 100) -> None:
    for job in STORE.due_broadcast_deliveries(limit=limit):
        generation = STORE.get_generation(job.generation_id)
        destination = STORE.get_broadcast_destination(job.workspace_id, job.destination_id)
        if generation is None or destination is None or not destination.enabled or destination.include_content:
            STORE.mark_broadcast_delivery(job.id, success=True)
            continue
        try:
            deliver_metadata_broadcast(
                destination,
                generation,
                settle_body=job.settle_body,
                settings=settings,
            )
        except Exception as exc:
            STORE.mark_broadcast_delivery(job.id, success=False, error=str(exc))
            log.exception("broadcast_metadata_delivery_failed destination=%s job=%s", destination.id, job.id)
            continue
        STORE.mark_broadcast_delivery(job.id, success=True)


def deliver_metadata_broadcast(
    destination: BroadcastDestination,
    generation: Generation,
    *,
    settle_body: dict[str, Any],
    settings: Settings,
) -> None:
    adapter = adapter_for(destination.type)
    if adapter is None:
        return
    adapter.deliver_metadata(destination, generation, settle_body=settle_body, settings=settings)
