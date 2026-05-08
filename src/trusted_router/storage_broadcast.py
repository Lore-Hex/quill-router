from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import (
    BroadcastDeliveryJob,
    BroadcastDestination,
    EncryptedSecretEnvelope,
    iso_now,
)


class InMemoryBroadcastDestinations:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.destinations: dict[str, BroadcastDestination] = {}
        self.delivery_jobs: dict[str, BroadcastDeliveryJob] = {}

    def reset(self) -> None:
        self.destinations.clear()
        self.delivery_jobs.clear()

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
        with self._lock:
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
            self.destinations[destination.id] = destination
            return destination

    def list_for_workspace(self, workspace_id: str) -> list[BroadcastDestination]:
        with self._lock:
            rows = [
                destination
                for destination in self.destinations.values()
                if destination.workspace_id == workspace_id
            ]
        rows.sort(key=lambda item: item.created_at)
        return rows

    def get(self, workspace_id: str, destination_id: str) -> BroadcastDestination | None:
        with self._lock:
            destination = self.destinations.get(destination_id)
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
        with self._lock:
            destination = self.destinations.get(destination_id)
            if destination is None or destination.workspace_id != workspace_id:
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
            return destination

    def delete(self, workspace_id: str, destination_id: str) -> bool:
        with self._lock:
            destination = self.destinations.get(destination_id)
            if destination is None or destination.workspace_id != workspace_id:
                return False
            self.destinations.pop(destination_id, None)
            return True

    def enqueue_delivery(
        self,
        *,
        workspace_id: str,
        destination_id: str,
        generation_id: str,
        settle_body: dict[str, object],
    ) -> BroadcastDeliveryJob:
        with self._lock:
            job = BroadcastDeliveryJob(
                id=f"bdel_{uuid.uuid4().hex}",
                workspace_id=workspace_id,
                destination_id=destination_id,
                generation_id=generation_id,
                settle_body=dict(settle_body),
            )
            self.delivery_jobs[job.id] = job
            return job

    def due_deliveries(self, *, limit: int = 100) -> list[BroadcastDeliveryJob]:
        now = iso_now()
        with self._lock:
            jobs = [
                job
                for job in self.delivery_jobs.values()
                if _is_due(job, now)
            ]
        jobs.sort(key=lambda job: (job.next_attempt_at, job.created_at, job.id))
        return jobs[:limit]

    def claim_deliveries(
        self,
        *,
        limit: int = 100,
        lease_seconds: int = 60,
    ) -> list[BroadcastDeliveryJob]:
        now = iso_now()
        owner = f"bworker_{uuid.uuid4().hex}"
        lease_until = _iso_after_seconds(lease_seconds)
        with self._lock:
            jobs = [
                job
                for job in self.delivery_jobs.values()
                if _is_due(job, now)
            ]
            jobs.sort(key=lambda job: (job.next_attempt_at, job.created_at, job.id))
            claimed = jobs[:limit]
            for job in claimed:
                job.lease_owner = owner
                job.leased_until = lease_until
                job.updated_at = now
            return claimed

    def mark_delivery(
        self,
        job_id: str,
        *,
        success: bool,
        error: str | None = None,
        lease_owner: str | None = None,
        max_attempts: int = 8,
    ) -> BroadcastDeliveryJob | None:
        with self._lock:
            job = self.delivery_jobs.get(job_id)
            if job is None:
                return None
            if lease_owner is not None and job.lease_owner not in {None, lease_owner}:
                return job
            job.attempts += 1
            job.updated_at = iso_now()
            job.lease_owner = None
            job.leased_until = None
            if success:
                job.status = "sent"
                job.last_error = None
                return job
            job.last_error = (error or "delivery failed")[:500]
            if job.attempts >= max_attempts:
                job.status = "dead"
            else:
                job.status = "pending"
                job.next_attempt_at = _iso_after_seconds(_backoff_seconds(job.attempts))
            return job


def _backoff_seconds(attempts: int) -> int:
    return min(60 * 60, 2 ** max(attempts - 1, 0))


def _iso_after_seconds(seconds: int) -> str:
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _is_due(job: BroadcastDeliveryJob, now: str) -> bool:
    if job.status != "pending" or job.next_attempt_at > now:
        return False
    return not job.leased_until or job.leased_until <= now
