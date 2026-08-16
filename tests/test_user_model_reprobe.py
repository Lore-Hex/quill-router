from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from trusted_router.config import Settings
from trusted_router.services import user_model_probe
from trusted_router.services.user_model_reprobe import reprobe_user_models
from trusted_router.services.user_model_secrets import (
    encrypt_user_model_signing_secret,
)
from trusted_router.storage import InMemoryStore


def _online_model(
    store: InMemoryStore,
    settings: Settings,
    *,
    slug: str,
    kind: str = "machine",
) -> Any:
    workspace_id = f"ws-{slug}"
    model = store.create_user_model(
        owner_user_id=f"owner-{slug}",
        owner_workspace_id=workspace_id,
        name=f"Model {slug}",
        kind=kind,
        display_name=slug,
        endpoint_url="https://owner.example/v1",
        encrypted_signing_secret=encrypt_user_model_signing_secret(
            "reprobe-signing-secret",
            settings,
            workspace_id=workspace_id,
        ),
        supports_streaming=False,
        slug=slug,
    )
    return store.set_user_model_online(
        model.id,
        owner_user_id=model.owner_user_id,
        online=True,
    )


@pytest.mark.asyncio
async def test_reprobe_dry_run_filters_online_kind_and_sends_nothing(
    test_settings: Settings,
) -> None:
    store = InMemoryStore()
    machine = _online_model(store, test_settings, slug="reprobe-machine")
    _online_model(store, test_settings, slug="reprobe-agent", kind="agent")
    offline = store.create_user_model(
        owner_user_id="offline-owner",
        owner_workspace_id="offline-workspace",
        name="Offline",
        kind="machine",
        display_name="offline",
        endpoint_url="https://owner.example/v1",
        slug="reprobe-offline",
    )

    report = await reprobe_user_models(
        test_settings,
        store=store,
        kind="machine",
        limit=1,
    )

    assert report.scanned == 1
    assert report.attempted == 0
    assert report.dry_run is True
    assert report.records[0].model_id == machine.id
    assert report.records[0].status == "would_probe"
    assert store.get_user_model(machine.id).probe_status == "unprobed"  # type: ignore[union-attr]
    assert store.get_user_model(offline.id).probe_status == "unprobed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reprobe_records_success_and_failure_without_clocking_model_out(
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    model = _online_model(store, test_settings, slug="reprobe-owner")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))
        ],
    )
    real_client = user_model_probe.httpx.AsyncClient

    def install_transport(handler: Any) -> None:
        def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(user_model_probe.httpx, "AsyncClient", client)

    def success(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "chat.completion",
                "choices": [
                    {"message": {"role": "assistant", "content": "pong"}}
                ],
            },
        )

    install_transport(success)
    passed = await reprobe_user_models(test_settings, store=store, apply=True)
    assert passed.attempted == 1
    assert passed.passed == 1
    assert passed.failed == 0
    stored = store.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "ok"
    assert stored.online is True

    def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    install_transport(malformed)
    failed = await reprobe_user_models(test_settings, store=store, apply=True)
    assert failed.attempted == 1
    assert failed.failed == 1
    stored = store.get_user_model(model.id)
    assert stored is not None
    assert stored.probe_status == "failed"
    assert stored.online is True
