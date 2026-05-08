from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import (
    BroadcastDeliveryJob,
    BroadcastDestination,
    EncryptedSecretEnvelope,
    iso_now,
)


class SpannerBroadcastDestinations:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def create(
        self,
        *,
        workspace_id: str,
        type: str,
        name: str,
        endpoint: str,
        enabled: bool = True,
        include_content: bool = False,
        method: str = "POST",
        encrypted_api_key: EncryptedSecretEnvelope | None = None,
        encrypted_headers: EncryptedSecretEnvelope | None = None,
        header_names: list[str] | None = None,
    ) -> BroadcastDestination:
        destination = BroadcastDestination(
            id=f"bdst_{uuid.uuid4().hex}",
            workspace_id=workspace_id,
            type=type,
            name=name,
            endpoint=endpoint,
            enabled=enabled,
            include_content=include_content,
            method=method,
            encrypted_api_key=encrypted_api_key,
            encrypted_headers=encrypted_headers,
            header_names=list(header_names or []),
        )
        self._io.write_entity("broadcast_destination", destination.id, destination)
        self._io.write_entity(
            "broadcast_destination_by_workspace",
            _workspace_destination_id(workspace_id, destination.id),
            {"destination_id": destination.id},
        )
        return destination

    def list_for_workspace(self, workspace_id: str) -> list[BroadcastDestination]:
        pointers = self._io.list_entities(
            "broadcast_destination_by_workspace",
            prefix=f"{workspace_id}#",
            cls=dict,
        )
        destinations: list[BroadcastDestination] = []
        for pointer in pointers:
            destination_id = str(pointer.get("destination_id", ""))
            if not destination_id:
                continue
            destination = self.get(workspace_id, destination_id)
            if destination is not None:
                destinations.append(destination)
        destinations.sort(key=lambda item: item.created_at)
        return destinations

    def get(self, workspace_id: str, destination_id: str) -> BroadcastDestination | None:
        destination = self._io.read_entity(
            "broadcast_destination", destination_id, BroadcastDestination
        )
        if destination is None or destination.workspace_id != workspace_id:
            return None
        return destination

    def update(
        self,
        workspace_id: str,
        destination_id: str,
        *,
        name: str | None = None,
        endpoint: str | None = None,
        enabled: bool | None = None,
        include_content: bool | None = None,
        method: str | None = None,
        encrypted_api_key: EncryptedSecretEnvelope | None = None,
        replace_api_key: bool = False,
        encrypted_headers: EncryptedSecretEnvelope | None = None,
        header_names: list[str] | None = None,
        replace_headers: bool = False,
    ) -> BroadcastDestination | None:
        destination = self.get(workspace_id, destination_id)
        if destination is None:
            return None
        if name is not None:
            destination.name = name
        if endpoint is not None:
            destination.endpoint = endpoint
        if enabled is not None:
            destination.enabled = enabled
        if include_content is not None:
            destination.include_content = include_content
        if method is not None:
            destination.method = method
        if replace_api_key:
            destination.encrypted_api_key = encrypted_api_key
        if replace_headers:
            destination.encrypted_headers = encrypted_headers
            destination.header_names = list(header_names or [])
        destination.updated_at = iso_now()
        self._io.write_entity("broadcast_destination", destination.id, destination)
        return destination

    def delete(self, workspace_id: str, destination_id: str) -> bool:
        destination = self.get(workspace_id, destination_id)
        if destination is None:
            return False
        self._io.delete_entities("broadcast_destination", [destination.id])
        self._io.delete_entities(
            "broadcast_destination_by_workspace",
            [_workspace_destination_id(workspace_id, destination.id)],
        )
        return True

    def enqueue_delivery(
        self,
        *,
        workspace_id: str,
        destination_id: str,
        generation_id: str,
        settle_body: dict[str, Any],
    ) -> BroadcastDeliveryJob:
        job = BroadcastDeliveryJob(
            id=f"bdel_{uuid.uuid4().hex}",
            workspace_id=workspace_id,
            destination_id=destination_id,
            generation_id=generation_id,
            settle_body=dict(settle_body),
        )
        self._write_delivery(job)
        return job

    def due_deliveries(self, *, limit: int = 100) -> list[BroadcastDeliveryJob]:
        pointers = self._io.list_entities(
            "broadcast_delivery_due",
            prefix="pending#",
            cls=dict,
            limit=max(limit * 10, limit),
        )
        due_ids: list[str] = []
        now = iso_now()
        for pointer in pointers:
            next_attempt_at = str(pointer.get("next_attempt_at", ""))
            if next_attempt_at and next_attempt_at <= now:
                job_id = str(pointer.get("job_id", ""))
                if job_id:
                    due_ids.append(job_id)
        jobs: list[BroadcastDeliveryJob] = []
        for job_id in due_ids:
            job = self._io.read_entity("broadcast_delivery", job_id, BroadcastDeliveryJob)
            if job is not None and _is_due(job, now):
                jobs.append(job)
        jobs.sort(key=lambda job: (job.next_attempt_at, job.created_at, job.id))
        return jobs[:limit]

    def claim_deliveries(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[BroadcastDeliveryJob]:
        candidates = self.due_deliveries(limit=max(limit * 2, limit))
        owner = f"bworker_{uuid.uuid4().hex}"
        lease_until = _iso_after_seconds(lease_seconds)
        claimed: list[BroadcastDeliveryJob] = []
        for candidate in candidates:
            if len(claimed) >= limit:
                break
            claimed_job = self._claim_delivery(
                candidate.id,
                owner=owner,
                lease_until=lease_until,
            )
            if claimed_job is not None:
                claimed.append(claimed_job)
        return claimed

    def _claim_delivery(
        self,
        job_id: str,
        *,
        owner: str,
        lease_until: str,
    ) -> BroadcastDeliveryJob | None:
        now = iso_now()

        def txn(transaction: Any) -> BroadcastDeliveryJob | None:
            job = self._io.read_entity_tx(transaction, "broadcast_delivery", job_id, BroadcastDeliveryJob)
            if job is None or not _is_due(job, now):
                return None
            job.lease_owner = owner
            job.leased_until = lease_until
            job.updated_at = now
            self._io.write_entity_tx(transaction, "broadcast_delivery", job.id, job)
            return job

        return self._io.database.run_in_transaction(txn)

    def mark_delivery(
        self,
        job_id: str,
        *,
        success: bool,
        error: str | None = None,
        lease_owner: str | None = None,
        max_attempts: int = 8,
    ) -> BroadcastDeliveryJob | None:
        job = self._io.read_entity("broadcast_delivery", job_id, BroadcastDeliveryJob)
        if job is None:
            return None
        if lease_owner is not None and job.lease_owner not in {None, lease_owner}:
            return job
        old_index = _delivery_due_id(job)
        job.attempts += 1
        job.updated_at = iso_now()
        job.lease_owner = None
        job.leased_until = None
        if success:
            job.status = "sent"
            job.last_error = None
        else:
            job.last_error = (error or "delivery failed")[:500]
            if job.attempts >= max_attempts:
                job.status = "dead"
            else:
                job.status = "pending"
                job.next_attempt_at = _iso_after_seconds(_backoff_seconds(job.attempts))
        self._io.write_entity("broadcast_delivery", job.id, job)
        self._io.delete_entities("broadcast_delivery_due", [old_index])
        if job.status == "pending":
            self._io.write_entity(
                "broadcast_delivery_due",
                _delivery_due_id(job),
                {
                    "job_id": job.id,
                    "next_attempt_at": job.next_attempt_at,
                    "workspace_id": job.workspace_id,
                },
            )
        return job

    def _write_delivery(self, job: BroadcastDeliveryJob) -> None:
        self._io.write_entity("broadcast_delivery", job.id, job)
        self._io.write_entity(
            "broadcast_delivery_due",
            _delivery_due_id(job),
            {
                "job_id": job.id,
                "next_attempt_at": job.next_attempt_at,
                "workspace_id": job.workspace_id,
            },
        )


def _workspace_destination_id(workspace_id: str, destination_id: str) -> str:
    return f"{workspace_id}#{destination_id}"


def _delivery_due_id(job: BroadcastDeliveryJob) -> str:
    return f"{job.status}#{job.next_attempt_at}#{job.id}"


def _backoff_seconds(attempts: int) -> int:
    return min(60 * 60, 2 ** max(attempts - 1, 0))


def _iso_after_seconds(seconds: int) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _is_due(job: BroadcastDeliveryJob, now: str) -> bool:
    if job.status != "pending" or job.next_attempt_at > now:
        return False
    return not job.leased_until or job.leased_until <= now
