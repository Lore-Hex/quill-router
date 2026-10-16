"""Real Uvicorn application resolution and ordinary startup without sockets."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from starlette.applications import Starlette

from tests.test_async_settle_shadow_startup_harness import fake_harness
from trusted_router import serve
from trusted_router.services import async_settle_shadow_startup_harness as harness


def serving_server(app, *, factory=False):
    config = serve.build_config({})
    config.app = app
    config.factory = factory
    config.load()
    server = serve.DrainingServer(config, drain_seconds=0)
    server.lifespan = config.lifespan_class(config)
    return server


async def start_serving(app):
    server = serving_server(app)
    try:
        # Uvicorn runs its real lifespan and startup, with no sockets to bind.
        await server.startup(sockets=[])
        assert server.started and not server.should_exit
    finally:
        await server.lifespan.shutdown()
    return server


@pytest.mark.parametrize('application_type', [FastAPI, Starlette])
async def test_startup_without_harness_state(application_type):
    app = application_type()
    server = await start_serving(app)
    assert harness.serving_application(server) is (app if application_type is FastAPI else None)
    assert not hasattr(app.state, 'async_settle_shadow_startup')


async def test_starlette_state_does_not_start_harness():
    app = Starlette()
    h, _, _ = fake_harness()
    async def begin():
        pytest.fail('Starlette must keep ordinary serving')
    app.state.async_settle_shadow_startup = h
    app.state.async_settle_shadow_startup_begin = begin
    await start_serving(app)
    assert h.task is None


@pytest.mark.parametrize('failure', ['resolver_raises', 'resolver_starlette', 'hook_raises'])
async def test_startup_continues_when_hook_unavailable(monkeypatch, failure):
    app = FastAPI()
    h, _, _ = fake_harness()
    async def begin():
        pytest.fail('unavailable startup records must not begin')
    app.state.async_settle_shadow_startup = h
    app.state.async_settle_shadow_startup_begin = begin
    def unavailable(*args):
        raise RuntimeError('startup records unavailable')
    if failure == 'resolver_starlette':
        other = Starlette()
        other.state.async_settle_shadow_startup = h
        other.state.async_settle_shadow_startup_begin = begin
        monkeypatch.setattr(harness, 'serving_application', lambda server: other)
    else:
        name = 'serving_application' if failure == 'resolver_raises' else 'listener_started'
        monkeypatch.setattr(harness, name, unavailable)
    await start_serving(app)
    assert h.task is None


async def test_application_startup_exit_does_not_start_harness(monkeypatch):
    @asynccontextmanager
    async def lifespan(app):
        raise RuntimeError('application startup stopped')
        yield
    app = FastAPI(lifespan=lifespan)
    called = []
    monkeypatch.setattr(harness, 'listener_started', lambda app: called.append(app))
    server = serving_server(app)
    await server.startup(sockets=[])
    assert server.should_exit and not server.started
    assert called == []


def test_application_chain_is_bounded():
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    server = serving_server(FastAPI())
    wrapper = ProxyHeadersMiddleware(server.config.loaded_app)
    wrapper.app = wrapper
    server.config.loaded_app = wrapper
    assert harness.serving_application(server) is None
