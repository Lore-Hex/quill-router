"""Both monitor admission paths must enforce the same internal-key allowlist."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import HTTPException

from trusted_router.auth import Principal
from trusted_router.config import Settings
from trusted_router.routes import inference
from trusted_router.routes.internal import gateway
from trusted_router.security import lookup_hash_api_key
from trusted_router.storage import STORE

MONITOR_KEY = "sk-tr-monitor-access-test"
PROBE_KEY = "sk-tr-stage-d-access-test"
REFUSAL = "trustedrouter/monitor is restricted to the synthetic monitor key"


@pytest.fixture(params=["gateway", "inference"])
def require_monitor_key(request: pytest.FixtureRequest) -> Callable[..., None]:
    if request.param == "gateway":
        return gateway._require_monitor_model_key

    user = STORE.ensure_user("monitor-access-test")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    _, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="Monitor access test",
        creator_user_id=user.id,
    )

    def require(body: dict[str, Any], caller_hash: str | None, settings: Settings) -> None:
        key.lookup_hash = caller_hash or ""
        principal = Principal(
            user=user,
            workspace=workspace,
            api_key=key if caller_hash is not None else None,
            is_management=False,
            scopes=frozenset(),
        )
        inference._require_monitor_model_key(body, principal, settings)

    return require


@pytest.fixture(
    params=[{"model": "trustedrouter/monitor"}, {"models": ["other", " trustedrouter/monitor "]}]
)
def monitor_body(request: pytest.FixtureRequest) -> dict[str, Any]:
    return request.param


@pytest.mark.parametrize("caller_key", [MONITOR_KEY, PROBE_KEY], ids=["synthetic", "probe"])
def test_monitor_accepts_either_configured_key(
    require_monitor_key: Callable[..., None],
    monitor_body: dict[str, Any],
    caller_key: str,
) -> None:
    settings = Settings(
        environment="test",
        synthetic_monitor_api_key=MONITOR_KEY,
        stage_d_probe_api_key=PROBE_KEY,
    )
    require_monitor_key(monitor_body, lookup_hash_api_key(caller_key), settings)


@pytest.mark.parametrize("caller_key", [MONITOR_KEY, PROBE_KEY], ids=["synthetic", "probe"])
def test_monitor_refuses_when_matching_setting_is_empty(
    require_monitor_key: Callable[..., None],
    monitor_body: dict[str, Any],
    caller_key: str,
) -> None:
    settings = Settings(
        environment="test",
        synthetic_monitor_api_key="" if caller_key == MONITOR_KEY else MONITOR_KEY,
        stage_d_probe_api_key="" if caller_key == PROBE_KEY else PROBE_KEY,
    )
    with pytest.raises(HTTPException) as caught:
        require_monitor_key(monitor_body, lookup_hash_api_key(caller_key), settings)
    assert caught.value.status_code == 403
    assert caught.value.detail["error"]["message"] == REFUSAL


@pytest.mark.parametrize(
    ("monitor_key", "probe_key"),
    [(MONITOR_KEY, PROBE_KEY), (MONITOR_KEY, ""), ("", PROBE_KEY), ("", ""), (None, "")],
    ids=["both", "no-probe", "no-monitor", "empty", "unset"],
)
@pytest.mark.parametrize(
    "caller_hash",
    [lookup_hash_api_key("sk-tr-other"), lookup_hash_api_key(""), "", None],
    ids=["other-key", "hash-of-empty-key", "empty-hash", "absent-hash"],
)
def test_monitor_refuses_other_keys_and_empty_settings_never_match(
    require_monitor_key: Callable[..., None],
    monitor_body: dict[str, Any],
    monitor_key: str | None,
    probe_key: str,
    caller_hash: str | None,
) -> None:
    settings = Settings(
        environment="test",
        synthetic_monitor_api_key=monitor_key,
        stage_d_probe_api_key=probe_key,
    )
    with pytest.raises(HTTPException) as caught:
        require_monitor_key(monitor_body, caller_hash, settings)
    assert caught.value.status_code == 403
    assert caught.value.detail["error"]["message"] == REFUSAL


def test_monitor_setting_defaults_to_disabled() -> None:
    assert Settings(environment="test").stage_d_probe_api_key == ""


def test_non_monitor_model_does_not_require_internal_key(
    require_monitor_key: Callable[..., None],
) -> None:
    require_monitor_key({"model": "other"}, None, Settings(environment="test"))
