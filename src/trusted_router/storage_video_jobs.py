from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from trusted_router.storage_models import VideoJob, iso_now

VIDEO_CONTENT_RETENTION_SECONDS = 24 * 60 * 60
VIDEO_SUBMISSION_TIMEOUT_SECONDS = 5 * 60


class InMemoryVideoJobs:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.jobs: dict[str, VideoJob] = {}
        self.by_authorization: dict[str, str] = {}

    def reset(self) -> None:
        self.jobs.clear()
        self.by_authorization.clear()

    def prepare(self, job: VideoJob) -> tuple[VideoJob, bool]:
        with self._lock:
            existing_id = self.by_authorization.get(job.authorization_id)
            existing = self.jobs.get(existing_id or job.id)
            if existing is not None:
                return existing, False
            job.next_poll_at = _iso_after_seconds(VIDEO_SUBMISSION_TIMEOUT_SECONDS)
            self.jobs[job.id] = job
            self.by_authorization[job.authorization_id] = job.id
            return job, True

    def get(self, job_id: str) -> VideoJob | None:
        with self._lock:
            return self.jobs.get(job_id)

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
        with self._lock:
            job = self.jobs.get(job_id)
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
            job.provider_job_id = provider_job_id
            job.provider = provider
            job.endpoint_id = endpoint_id
            job.provider_model = provider_model
            job.quoted_microdollars = quoted_microdollars
            if job.status == "submitting":
                job.status = "pending"
            job.next_poll_at = _iso_after_seconds(poll_after_seconds)
            job.updated_at = iso_now()
            return job

    def claim_due(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[VideoJob]:
        now = iso_now()
        lease_until = _iso_after_seconds(lease_seconds)
        with self._lock:
            jobs = [job for job in self.jobs.values() if _is_due(job, now)]
            jobs.sort(key=lambda job: (job.next_poll_at, job.created_at, job.id))
            claimed = jobs[:limit]
            for job in claimed:
                job.lease_owner = lease_owner
                job.leased_until = lease_until
                job.updated_at = now
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
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if lease_owner is not None and job.lease_owner not in {None, lease_owner}:
                return job
            if job.status in {"completed", "failed"}:
                # Two regional pollers can observe provider completion at the
                # same time. A settlement replay may finish the job before the
                # winning poller attaches its generation ID. Permit that one
                # monotonic repair without reopening or otherwise mutating the
                # terminal job.
                if (
                    job.status == "completed"
                    and status == "completed"
                    and generation_id
                    and not job.generation_id
                ):
                    job.generation_id = generation_id
                    job.updated_at = iso_now()
                return job
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
            return job

    def mark_cleaned(self, job_id: str) -> VideoJob | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job.cleaned_at is None:
                job.cleaned_at = iso_now()
                job.updated_at = job.cleaned_at
            job.lease_owner = None
            job.leased_until = None
            return job


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
