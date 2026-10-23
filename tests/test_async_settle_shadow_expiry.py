"""Runtime expiry of shadow pins: a canary stops itself without a deploy."""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import NOW_US, fake_store
from trusted_router import async_settle_shadow_lifecycle as m
from trusted_router import config
from trusted_router.config import (
    SHADOW_CANARY_MAX_SECONDS,
    Settings,
    in_v2_cohort,
    parse_shadow_expiry,
)
from trusted_router.services import async_settle_shadow_health as shell

WORKSPACE = "ws"


def iso(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).isoformat()


def pins(**extra: Any) -> Settings:
    return Settings(environment="test", async_settle_shadow_policy="shadow-v2",
                    async_settle_shadow_workspaces=WORKSPACE, source_revision="a"*40, **extra)


def deployed(settings: Settings) -> Settings:
    # Deployed-environment validation needs unrelated production secrets; the
    # copy keeps the parsed expiry and changes only the environment name.
    return settings.model_copy(update={"environment": "production"})


def test_absent_expiry_is_off_when_deployed_and_unbounded_locally():
    settings = pins()
    assert settings.async_settle_shadow_live()
    assert settings.effective_async_settle_shadow_policy() == "shadow-v2"
    for environment in ("production", "staging", "worker"):
        copy = settings.model_copy(update={"environment": environment})
        assert not copy.async_settle_shadow_live()
        assert copy.effective_async_settle_shadow_policy() == ""


@pytest.mark.parametrize("environment", ["test", "production"])
def test_expiry_turns_pins_off_at_the_instant(environment):
    expiry = float(int(time.time()) + 600)  # Whole seconds survive the ISO round trip.
    settings = pins(async_settle_shadow_expires_at=iso(expiry))
    if environment == "production":
        settings = deployed(settings)
    assert settings.async_settle_shadow_live(expiry - 1)
    assert settings.effective_async_settle_shadow_policy(expiry - 1) == "shadow-v2"
    assert settings.async_settle_shadow_live()  # Real clock, ten minutes early.
    assert not settings.async_settle_shadow_live(expiry)
    assert settings.effective_async_settle_shadow_policy(expiry) == ""
    assert not in_v2_cohort(settings.effective_async_settle_shadow_policy(expiry), WORKSPACE,
                            settings.async_settle_enabled, settings.async_settle_shadow_workspace_ids)
    # The raw pin is untouched: v1 never re-arms from an expired v2 canary.
    assert settings.async_settle_shadow_policy == "shadow-v2"


def test_past_expiry_and_z_suffix_are_accepted():
    settings = pins(async_settle_shadow_expires_at="2026-10-23T11:00:00Z")
    assert not settings.async_settle_shadow_live()
    assert settings.async_settle_shadow_live(dt.datetime(2026, 10, 23, 10, 59, tzinfo=dt.UTC).timestamp())


@pytest.mark.parametrize("value,message", [
    ("2026-10-23T11:00:00", "explicit UTC offset"),
    ("tomorrow", "ISO-8601"),
    (None, "canary maximum"),
])
def test_invalid_or_unbounded_expiry_fails_closed_without_failing_settings(value, message, caplog):
    if value is None:
        value = iso(time.time() + SHADOW_CANARY_MAX_SECONDS + 3600)
    # Strict for deploy tests and the activation tool ...
    with pytest.raises(ValueError, match=message):
        parse_shadow_expiry(value)
    # ... but a shadow-only typo never fails a revision's Settings: it is off.
    config._LOGGED_SHADOW_EXPIRY_ERRORS.clear()
    with caplog.at_level(logging.WARNING, logger="trusted_router.config"):
        for _ in range(3):
            settings = pins(async_settle_shadow_expires_at=value)
            assert not settings.async_settle_shadow_live(0)
            assert settings.effective_async_settle_shadow_policy() == ""
    logged = [r.getMessage() for r in caplog.records if "shadow expiry invalid" in r.getMessage()]
    assert len(logged) == 1 and message in logged[0]
    # The longest allowed canary still parses and is live.
    assert pins(async_settle_shadow_expires_at=iso(time.time() + SHADOW_CANARY_MAX_SECONDS - 60)).async_settle_shadow_live()


def test_rollout_expiry_pin_is_strictly_valid_or_empty():
    """Deploy-time strictness: rollout.sh may carry only an empty or valid expiry."""
    source = (Path(__file__).resolve().parents[1]/"scripts/deploy/rollout.sh").read_text()
    values = re.findall(r'^  "TR_ASYNC_SETTLE_SHADOW_EXPIRES_AT=([^"\n]*)"$', source, re.MULTILINE)
    assert len(values) == 1
    if values[0]:
        assert values[0].endswith("Z")
        parse_shadow_expiry(values[0])
    policy = re.findall(r'^  "TR_ASYNC_SETTLE_SHADOW_POLICY=([^"\n]*)"$', source, re.MULTILINE)
    assert len(policy) == 1
    # Opt-in pins without an expiry would be a silent no-op in production.
    assert not policy[0] or values[0]


def owner(settings: Settings) -> tuple[shell.Service, list[int]]:
    store, _ = fake_store()
    svc = shell.Service(settings, store)
    wall = [NOW_US]
    svc.store.wall, svc.store.clock = lambda: wall[0]/1e6, lambda: 100. + (wall[0]-NOW_US)/1e6
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    return svc, wall


def test_owner_tick_revokes_and_request_side_reservations_stop_at_expiry():
    expiry_us = NOW_US + 10*m.SLOT_US
    svc, wall = owner(pins(async_settle_shadow_expires_at=iso(expiry_us/1e6)))
    effects = svc.tick(wall[0])
    assert any(isinstance(e, m.StartPublisherCycle) for e in effects)
    assert svc.policy_enabled() and svc.contender()
    generation = svc.generation
    svc.journal.grant(m.day_name(NOW_US), 100)
    assert svc.port.reserve(wall[0]) is not None
    wall[0] = expiry_us
    # Request threads see the stop directly; the owner's next tick revokes.
    assert not svc.policy_enabled()
    assert svc.port.reserve(wall[0]) is None
    effects = svc.tick(wall[0])
    assert svc.generation == generation + 1
    assert not svc.enabled() and not svc.contender()
    assert not any(isinstance(e, m.StartPublisherCycle) for e in effects)
    # Monotone: no later instant re-enables it.
    wall[0] = expiry_us + 3600*1_000_000
    assert not svc.policy_enabled()


@pytest.mark.asyncio
async def test_run_closes_as_opt_out_once_expired():
    # Same path as test_policy_disabled_optout_closes; this store's wall is NOW+100 s.
    store, _ = fake_store()
    svc = shell.Service(pins(async_settle_shadow_expires_at=iso(NOW_US/1e6 + 50)), store)
    live = shell.Service(pins(async_settle_shadow_expires_at=iso(NOW_US/1e6 + 150)), fake_store()[0])
    assert live.policy_enabled() and not svc.policy_enabled()
    await svc.run()
    assert not svc.journal.days and not svc.unclosed


def test_install_skips_an_expired_canary():
    class Backend:
        _database = object()
    app = Mock()
    shell.install(app, pins(async_settle_shadow_expires_at="2026-10-23T11:00:00Z"), Backend())
    assert not app.mock_calls


@pytest.mark.parametrize("expired", [False, True])
def test_create_app_installs_nothing_for_expired_pins(monkeypatch, expired):
    from trusted_router import main, storage
    from trusted_router.services import (
        async_settle,
        async_settle_shadow,
        async_settle_shadow_cohort_runtime,
        async_settle_shadow_health,
    )
    expiry = time.time() + (-60 if expired else 600)
    config = pins(async_settle_shadow_expires_at=iso(expiry))
    backend = object()
    monkeypatch.setattr(storage, "typed_billing_store", lambda _: backend)
    monkeypatch.setattr(async_settle, "load_runtime", Mock(return_value=None))
    v1_install = Mock(side_effect=AssertionError("an expired v2 canary must not install v1"))
    health_install, cohort_install = Mock(), Mock()
    monkeypatch.setattr(async_settle_shadow, "install", v1_install)
    monkeypatch.setattr(async_settle_shadow_health, "install", health_install)
    monkeypatch.setattr(async_settle_shadow_cohort_runtime, "install", cohort_install)
    main.create_app(config, configure_store_arg=False, init_observability=False)
    v1_install.assert_not_called()
    assert health_install.called is not expired
    assert cohort_install.called is not expired


@pytest.mark.parametrize("expired", [False, True])
def test_request_cohort_gate_uses_the_effective_policy(expired):
    """The request path's shadow work (cohort.authorize) goes dormant at expiry."""
    from tests.test_async_settle_shadow_cohort import NOW, Port, additions, config, inputs, signer
    from trusted_router.async_settle_shadow_v2_schema import day_at
    from trusted_router.services.async_settle_shadow_cohort import authorize
    cfg = config(async_settle_shadow_expires_at=iso(time.time() + (-60 if expired else 600)))
    port = Port()
    before = additions()
    result = authorize(cfg, ("shadow-v2",), port, inputs(), before, signer())
    assert bool(result) is not expired
    assert before == additions()
    if expired:
        assert port.calls == [] and port.journal.days[day_at(NOW*1000000)].next_seq == 1
