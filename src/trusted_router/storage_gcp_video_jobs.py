from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from trusted_router.storage_gcp_io import SpannerIO, run_in_transaction_with_retry
from trusted_router.storage_models import VideoJob, iso_now
from trusted_router.storage_video_jobs import (
    VIDEO_CONTENT_RETENTION_SECONDS,
    VIDEO_SUBMISSION_TIMEOUT_SECONDS,
)


class SpannerVideoJobs:
    """Metadata-only asynchronous video jobs on the existing entity table.

    Video QPS is expected to remain far below token-request QPS. Keeping the
    launch queue here avoids a schema-coupled rollout while still providing
    cross-region durability and transactional lease claims. A dedicated typed
    table can replace this adapter without changing the Store contract.
    """

    def __init__(self, io: SpannerIO) -> None:
        self._io = io

    def prepare(self, job: VideoJob) -> tuple[VideoJob, bool]:
        def txn(transaction: Any) -> tuple[VideoJob, bool]:
            pointer = self._io.read_entity_tx(
                transaction, "video_job_by_authorization", job.authorization_id, dict
            )
            if pointer is not None:
                existing = self._io.read_entity_tx(
                    transaction, "video_job", str(pointer.get("job_id", "")), VideoJob
                )
                if existing is not None:
                    return existing, False
            existing = self._io.read_entity_tx(transaction, "video_job", job.id, VideoJob)
            if existing is not None:
                return existing, False
            job.next_poll_at = _iso_after_seconds(VIDEO_SUBMISSION_TIMEOUT_SECONDS)
            self._io.write_entity_tx(transaction, "video_job", job.id, job)
            self._io.write_entity_tx(
                transaction,
                "video_job_by_authorization",
                job.authorization_id,
                {"job_id": job.id},
            )
            self._io.write_entity_tx(
                transaction,
                "video_job_due",
                _due_id(job),
                {"job_id": job.id, "next_poll_at": job.next_poll_at},
            )
            return job, True

        return run_in_transaction_with_retry(self._io.database, txn)

    def get(self, job_id: str) -> VideoJob | None:
        return self._io.read_entity("video_job", job_id, VideoJob)

    def get_for_key(self, job_id: str, key_hash: str) -> VideoJob | None:
        job = self.get(job_id)
        return job if job is not None and job.key_hash == key_hash else None

    def mark_queued(
        self,
        job_id: str,
        *,
        provider_job_id: str,
        provider: str,
        endpoint_id: str,
        provider_model: str,
        quoted_microdollars: int,
        poll_after_seconds: int,
    ) -> VideoJob | None:
        def txn(transaction: Any) -> VideoJob | None:
            job = self._io.read_entity_tx(transaction, "video_job", job_id, VideoJob)
            if job is None:
                return None
            if job.provider_job_id and job.provider_job_id != provider_job_id:
                raise ValueError("video job was already queued with a different provider id")
            if job.provider_job_id and (
                job.provider != provider
                or job.endpoint_id != endpoint_id
                or job.provider_model != provider_model
                or job.quoted_microdollars != quoted_microdollars
            ):
                raise ValueError("video job was already queued with different route metadata")
            old_due = _due_id(job)
            job.provider_job_id = provider_job_id
            job.provider = provider
            job.endpoint_id = endpoint_id
            job.provider_model = provider_model
            job.quoted_microdollars = quoted_microdollars
            if job.status == "submitting":
                job.status = "pending"
            job.next_poll_at = _iso_after_seconds(poll_after_seconds)
            job.updated_at = iso_now()
            self._io.write_entity_tx(transaction, "video_job", job.id, job)
            self._io.delete_entities_tx(transaction, "video_job_due", [old_due])
            self._io.write_entity_tx(
                transaction,
                "video_job_due",
                _due_id(job),
                {"job_id": job.id, "next_poll_at": job.next_poll_at},
            )
            return job

        return run_in_transaction_with_retry(self._io.database, txn)

    def claim_due(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[VideoJob]:
        pointers = self._io.list_entities("video_job_due", cls=dict, limit=max(limit * 10, limit))
        now = iso_now()
        claimed: list[VideoJob] = []
        lease_until = _iso_after_seconds(lease_seconds)
        for pointer in pointers:
            if len(claimed) >= limit:
                break
            if str(pointer.get("next_poll_at", "")) > now:
                break
            job_id = str(pointer.get("job_id", ""))
            if not job_id:
                continue

            def txn(transaction: Any, *, candidate_id: str = job_id) -> VideoJob | None:
                job = self._io.read_entity_tx(transaction, "video_job", candidate_id, VideoJob)
                if job is None or not _is_due(job, now):
                    return None
                job.lease_owner = lease_owner
                job.leased_until = lease_until
                job.updated_at = now
                self._io.write_entity_tx(transaction, "video_job", job.id, job)
                return job

            job = run_in_transaction_with_retry(self._io.database, txn)
            if job is not None:
                claimed.append(job)
        return claimed

    def update(
        self,
        job_id: str,
        *,
        status: str,
        lease_owner: str | None = None,
        provider_status: str | None = None,
        generation_id: str | None = None,
        error: str | None = None,
        poll_after_seconds: int = 5,
    ) -> VideoJob | None:
        def txn(transaction: Any) -> VideoJob | None:
            job = self._io.read_entity_tx(transaction, "video_job", job_id, VideoJob)
            if job is None:
                return None
            if lease_owner is not None and job.lease_owner not in {None, lease_owner}:
                return job
            if job.status in {"completed", "failed"}:
                return job
            old_due = _due_id(job)
            job.status = status
            job.provider_status = provider_status
            if generation_id:
                job.generation_id = generation_id
            job.last_error = error[:500] if error else None
            job.attempts += 1
            job.lease_owner = None
            job.leased_until = None
            job.updated_at = iso_now()
            if status in {"pending", "in_progress"}:
                job.next_poll_at = _iso_after_seconds(poll_after_seconds)
            elif status == "completed" and job.cleaned_at is None:
                if job.content_expires_at is None:
                    job.content_expires_at = _iso_after_seconds(VIDEO_CONTENT_RETENTION_SECONDS)
                job.next_poll_at = job.content_expires_at
            self._io.write_entity_tx(transaction, "video_job", job.id, job)
            self._io.delete_entities_tx(transaction, "video_job_due", [old_due])
            if status in {"pending", "in_progress", "completed"} and job.cleaned_at is None:
                self._io.write_entity_tx(
                    transaction,
                    "video_job_due",
                    _due_id(job),
                    {"job_id": job.id, "next_poll_at": job.next_poll_at},
                )
            return job

        return run_in_transaction_with_retry(self._io.database, txn)

    def mark_cleaned(self, job_id: str) -> VideoJob | None:
        def txn(transaction: Any) -> VideoJob | None:
            job = self._io.read_entity_tx(transaction, "video_job", job_id, VideoJob)
            if job is None:
                return None
            if job.cleaned_at is None:
                old_due = _due_id(job)
                job.cleaned_at = iso_now()
                job.updated_at = job.cleaned_at
                job.lease_owner = None
                job.leased_until = None
                self._io.write_entity_tx(transaction, "video_job", job.id, job)
                self._io.delete_entities_tx(transaction, "video_job_due", [old_due])
            return job

        return run_in_transaction_with_retry(self._io.database, txn)


def _due_id(job: VideoJob) -> str:
    return f"{job.next_poll_at}#{job.id}"


def _iso_after_seconds(seconds: int) -> str:
    return (
        (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=max(seconds, 0)))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _is_due(job: VideoJob, now: str) -> bool:
    pollable = job.status in {"submitting", "pending", "in_progress"}
    cleanup_due = job.status == "completed" and job.cleaned_at is None
    if not (pollable or cleanup_due) or job.next_poll_at > now:
        return False
    return not job.leased_until or job.leased_until <= now
