from __future__ import annotations

from typing import Any

import pytest

from trusted_router.gcs_singleflight import (
    GCSGenerationLease,
    GCSLease,
    GCSLeaseConfig,
)


def _singleflight() -> GCSGenerationLease:
    return GCSGenerationLease(
        GCSLeaseConfig(
            bucket="private-worker-state",
            object_name="worker/lease.json",
            lease_seconds=240,
            min_interval_seconds=50,
            failure_cooldown_seconds=30,
        )
    )


def test_acquire_creates_generation_guarded_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    singleflight = _singleflight()
    writes: list[tuple[dict[str, object], int]] = []
    monkeypatch.setattr(singleflight, "_access_token", lambda: "token")
    monkeypatch.setattr(singleflight, "_read", lambda _token: None)

    def write(
        _token: str,
        payload: dict[str, object],
        *,
        if_generation_match: int,
    ) -> int:
        writes.append((payload, if_generation_match))
        return 17

    monkeypatch.setattr(singleflight, "_write", write)

    lease = singleflight.acquire(now=100.0, owner="execution-1")

    assert lease == GCSLease(owner="execution-1", generation=17, acquired_at=100.0)
    assert writes == [
        (
            {
                "owner": "execution-1",
                "state": "running",
                "acquired_at": 100.0,
                "expires_at": 340.0,
            },
            0,
        )
    ]


def test_acquire_skips_active_lease_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    singleflight = _singleflight()
    monkeypatch.setattr(singleflight, "_access_token", lambda: "token")
    monkeypatch.setattr(
        singleflight,
        "_read",
        lambda _token: ({"owner": "leader", "expires_at": 101.0}, 8),
    )
    monkeypatch.setattr(
        singleflight,
        "_write",
        lambda *_args, **_kwargs: pytest.fail("active lease was overwritten"),
    )

    assert singleflight.acquire(now=100.0, owner="follower") is None


def test_acquire_replaces_expired_lease_conditionally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    singleflight = _singleflight()
    deletes: list[int] = []
    monkeypatch.setattr(singleflight, "_access_token", lambda: "token")
    monkeypatch.setattr(
        singleflight,
        "_read",
        lambda _token: ({"owner": "expired", "expires_at": 99.0}, 8),
    )
    monkeypatch.setattr(
        singleflight,
        "_delete",
        lambda _token, generation: deletes.append(generation) or True,
    )
    monkeypatch.setattr(singleflight, "_write", lambda *_args, **_kwargs: 9)

    lease = singleflight.acquire(now=100.0, owner="replacement")

    assert lease == GCSLease(owner="replacement", generation=9, acquired_at=100.0)
    assert deletes == [8]


def test_acquire_loses_concurrent_creation_without_guessing_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    singleflight = _singleflight()
    monkeypatch.setattr(singleflight, "_access_token", lambda: "token")
    monkeypatch.setattr(singleflight, "_read", lambda _token: None)
    monkeypatch.setattr(singleflight, "_write", lambda *_args, **_kwargs: None)

    assert singleflight.acquire(now=100.0, owner="loser") is None


@pytest.mark.parametrize(
    ("succeeded", "expected_state", "expected_expiry", "expected_cooldown"),
    [
        (True, "cooldown", 150.0, 10.0),
        (False, "failed", 170.0, 30.0),
    ],
)
def test_finish_updates_only_owned_generation(
    monkeypatch: pytest.MonkeyPatch,
    succeeded: bool,
    expected_state: str,
    expected_expiry: float,
    expected_cooldown: float,
) -> None:
    singleflight = _singleflight()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(singleflight, "_access_token", lambda: "token")

    def write(
        _token: str,
        payload: dict[str, object],
        *,
        if_generation_match: int,
    ) -> int:
        captured["payload"] = payload
        captured["generation"] = if_generation_match
        return 18

    monkeypatch.setattr(singleflight, "_write", write)
    lease = GCSLease(owner="execution-1", generation=17, acquired_at=100.0)

    cooldown = singleflight.finish(lease, succeeded=succeeded, now=140.0)

    assert captured["generation"] == 17
    assert captured["payload"] == {
        "owner": "execution-1",
        "state": expected_state,
        "acquired_at": 100.0,
        "completed_at": 140.0,
        "expires_at": expected_expiry,
    }
    assert cooldown == expected_cooldown


def test_finish_fails_when_generation_ownership_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    singleflight = _singleflight()
    monkeypatch.setattr(singleflight, "_access_token", lambda: "token")
    monkeypatch.setattr(singleflight, "_write", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="ownership changed"):
        singleflight.finish(
            GCSLease(owner="old", generation=3, acquired_at=100.0),
            succeeded=True,
            now=120.0,
        )


@pytest.mark.parametrize(
    "config",
    [
        GCSLeaseConfig("", "object", 1, 0, 0),
        GCSLeaseConfig("bucket", "", 1, 0, 0),
        GCSLeaseConfig("bucket", "object", 0, 0, 0),
        GCSLeaseConfig("bucket", "object", 1, -1, 0),
        GCSLeaseConfig("bucket", "object", 1, 0, -1),
        GCSLeaseConfig("bucket", "object", 1, 0, 0, 0),
    ],
)
def test_invalid_config_fails_closed(config: GCSLeaseConfig) -> None:
    with pytest.raises(ValueError):
        GCSGenerationLease(config)
