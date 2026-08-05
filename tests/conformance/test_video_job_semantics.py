"""Behavioural contract for asynchronous video jobs and the gateway reads.

Why this file exists
--------------------
Every enclave dialled the GCP (Spanner) control plane, while the AWS and Azure
control planes run `TR_STORAGE_BACKEND=postgres`. So the Postgres
implementations of these methods had never served a real request, and eleven
of them raised `NotImplementedError` — four reachable from
`/internal/gateway/*` and seven from `/internal/gateway/video/jobs/*`. Cutting
a peer enclave over to its own control plane would have 500'd every one of
those calls, and one of the four fires *after* the credit escrow commits, so
each attempt would have stranded a reservation.

The structural guard in `tests/test_store_protocol_conformance.py` stops a
method from *refusing*. It cannot tell whether the implementation is correct.
These tests do, and because they are written against the `Store` Protocol they
run on every registered backend — so the Postgres implementation is checked
against the same properties as the in-memory reference rather than against a
fake that drifts kinder than production.

Run against a real server-backed Postgres with:

    docker run -d --rm --name tr-conf-pg -e POSTGRES_PASSWORD=conf \\
      -e POSTGRES_DB=trconf -p 55433:5432 postgres:17-alpine
    export TR_CONFORMANCE_POSTGRES_DSN=postgresql://postgres:conf@127.0.0.1:55433/trconf
"""

from __future__ import annotations

import pytest

from trusted_router.storage_models import VideoJob
from trusted_router.store_protocol import Store

from .conftest import BACKENDS

pytestmark = pytest.mark.parametrize("store", list(BACKENDS), indirect=True)


def _job(workspace_id: str, unique: str, *, suffix: str = "") -> VideoJob:
    return VideoJob(
        id=f"job-{unique}{suffix}",
        workspace_id=workspace_id,
        key_hash=f"hash-{unique}",
        authorization_id=f"auth-{unique}{suffix}",
        model="sora-2",
        provider="openai",
        endpoint_id="openai/sora-2",
        provider_model="sora-2",
        quoted_microdollars=1_000_000,
    )


# --------------------------------------------------------------------------
# The four gateway-reachable reads: a miss is a value, never an exception
# --------------------------------------------------------------------------


def test_gateway_reachable_reads_return_a_miss_instead_of_raising(
    store: Store, workspace_id: str, unique: str
) -> None:
    """These four are called during `authorize`. A raise is a 500 on the whole
    request — and `list_broadcast_destinations` is called after the escrow
    commits, so a raise there strands credits with no authorization id
    returned to settle or refund against."""
    assert store.get_custom_model(f"missing-{unique}") is None
    assert store.get_byok_provider(workspace_id, f"missing-{unique}") is None
    assert store.get_generation(f"missing-{unique}") is None
    assert store.list_broadcast_destinations(workspace_id) == []


def test_broadcast_destination_lookup_does_not_leak_across_workspaces(
    store: Store, workspace_id: str, unique: str
) -> None:
    """A workspace id is a LIKE prefix on some backends, and `_` is a
    single-character wildcard there. Unescaped, one tenant's scan can return
    another tenant's rows. Querying a neighbouring id must stay empty."""
    assert store.list_broadcast_destinations(f"{workspace_id}-other") == []
    assert store.list_broadcast_destinations(f"{workspace_id}_x") == []


# --------------------------------------------------------------------------
# Video jobs
# --------------------------------------------------------------------------


def test_prepare_video_job_is_idempotent_per_authorization(
    store: Store, workspace_id: str, unique: str
) -> None:
    """One authorization must map to exactly one job. Creating a second on
    replay would submit the same generation to the provider twice and bill it
    twice."""
    job = _job(workspace_id, unique)
    first, created = store.prepare_video_job(job)
    assert created is True

    replay = _job(workspace_id, unique)
    replay.id = f"different-{unique}"  # same authorization_id, new job id
    second, created_again = store.prepare_video_job(replay)
    assert created_again is False
    assert second.id == first.id


def test_get_video_job_for_key_is_the_tenant_boundary(
    store: Store, workspace_id: str, unique: str
) -> None:
    """The public video endpoints authorise by key hash. A caller who guesses
    a job id must not read it with the wrong key."""
    job, _ = store.prepare_video_job(_job(workspace_id, unique))
    assert store.get_video_job_for_key(job.id, job.key_hash) is not None
    assert store.get_video_job_for_key(job.id, "wrong-hash") is None


def test_a_job_is_claimed_by_exactly_one_lease_owner(
    store: Store, workspace_id: str, unique: str
) -> None:
    """The property the whole queue rests on. Two pollers claiming the same
    job poll the provider twice and can bill the completion twice."""
    job, _ = store.prepare_video_job(_job(workspace_id, unique))
    store.mark_video_job_queued(
        job.id,
        provider_job_id=f"p-{unique}",
        provider="openai",
        endpoint_id="openai/sora-2",
        provider_model="sora-2",
        quoted_microdollars=1_000_000,
        poll_after_seconds=0,
    )

    first = store.claim_video_jobs(lease_owner="poller-a", limit=10, lease_seconds=300)
    second = store.claim_video_jobs(lease_owner="poller-b", limit=10, lease_seconds=300)

    claimed_once = [j.id for j in first if j.id == job.id]
    claimed_twice = [j.id for j in second if j.id == job.id]
    assert claimed_once == [job.id], "the first poller should have claimed the due job"
    assert claimed_twice == [], "a leased job must not be claimable by a second poller"


def test_terminal_video_job_state_is_immutable(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Once completed, a job must not be reopened — a later poller observing a
    stale provider status could otherwise re-bill or resurrect it."""
    job, _ = store.prepare_video_job(_job(workspace_id, unique))
    store.update_video_job(job.id, status="completed", generation_id=f"gen-{unique}")
    reopened = store.update_video_job(job.id, status="in_progress")
    assert reopened is not None
    assert reopened.status == "completed"


def test_completed_job_accepts_a_missing_generation_link_once(
    store: Store, workspace_id: str, unique: str
) -> None:
    """The one permitted mutation of a terminal job: filling in a generation
    link that concurrent regional settlement left empty. Monotonic — it may
    fill an absent link, never replace a present one."""
    job, _ = store.prepare_video_job(_job(workspace_id, unique))
    store.update_video_job(job.id, status="completed")
    repaired = store.update_video_job(
        job.id, status="completed", generation_id=f"gen-{unique}"
    )
    assert repaired is not None
    assert repaired.generation_id == f"gen-{unique}"

    kept = store.update_video_job(job.id, status="completed", generation_id="other-gen")
    assert kept is not None
    assert kept.generation_id == f"gen-{unique}"


def test_cleaned_job_leaves_the_due_queue(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Cleanup must remove the job from the polling index. A job left in the
    queue is claimed forever, burning poller capacity on every cycle."""
    job, _ = store.prepare_video_job(_job(workspace_id, unique))
    store.update_video_job(job.id, status="completed", poll_after_seconds=0)
    cleaned = store.mark_video_job_cleaned(job.id)
    assert cleaned is not None
    assert cleaned.cleaned_at is not None

    claimed = store.claim_video_jobs(lease_owner="poller-c", limit=50, lease_seconds=300)
    assert job.id not in [j.id for j in claimed]


def test_requeueing_with_a_conflicting_provider_id_is_refused(
    store: Store, workspace_id: str, unique: str
) -> None:
    """Two different provider job ids for one job means one provider
    generation is orphaned and unbilled. Refuse rather than overwrite."""
    job, _ = store.prepare_video_job(_job(workspace_id, unique))
    kwargs = dict(
        provider="openai",
        endpoint_id="openai/sora-2",
        provider_model="sora-2",
        quoted_microdollars=1_000_000,
        poll_after_seconds=30,
    )
    store.mark_video_job_queued(job.id, provider_job_id=f"p-{unique}", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="different provider id"):
        store.mark_video_job_queued(job.id, provider_job_id="other-provider-id", **kwargs)  # type: ignore[arg-type]


def test_missing_video_job_reads_are_none_not_errors(store: Store, unique: str) -> None:
    assert store.get_video_job(f"absent-{unique}") is None
    assert store.get_video_job_for_key(f"absent-{unique}", "any") is None
    assert store.update_video_job(f"absent-{unique}", status="completed") is None
    assert store.mark_video_job_cleaned(f"absent-{unique}") is None
