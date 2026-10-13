"""Clean-interpreter policy-empty request probe; self-relative, with a closed import blocker."""
import asyncio
import importlib.abc
import json
import os
import sys
from functools import lru_cache

import pytest

NAMES = {'async_settle_shadow_v2_a2_evidence', 'async_settle_shadow_v2_a2_roots', 'async_settle_shadow_v2_a2_contract', 'async_settle_shadow_v2_deltas', 'async_settle_shadow_lifecycle', 'async_settle_shadow_health_state', 'async_settle_shadow_v2_a1_numbers', 'storage_gcp_shadow_io', 'async_settle_shadow_v2_a1_health', 'async_settle_shadow_journal', 'async_settle_shadow_v2_retained', 'async_settle_shadow_health', 'async_settle_shadow_v2_contract', 'async_settle_shadow_v2_native', 'async_settle_shadow_v2_evidence', 'async_settle_shadow_v2_projection', 'async_settle_shadow_v2_certificate', 'async_settle_shadow_attachments', 'async_settle_shadow_v2_a1_evidence', 'storage_gcp_shadow_attachments', 'async_settle_shadow_v2_a1_deltas', 'async_settle_shadow_v2_a1_reconciliation', 'async_settle_shadow_v2_schema', 'storage_gcp_shadow_observation'}

class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.rsplit(".", 1)[-1] in NAMES:
            raise AssertionError("empty-policy v2 import blocker")


def assert_dormant():
    assert not any("shadow_v2_" in n or n.rsplit(".", 1)[-1] in NAMES for n in sys.modules), "empty-policy v2 import"


def probe():
    if os.environ.get("P3_BLOCK_IMPORTS"):
        sys.meta_path.insert(0, Block())
    from trusted_router import storage_gcp_io as shared_adapter
    shared_before = {k: id(v) for k, v in vars(shared_adapter).items()}
    import trusted_router.main as main
    assert {k: id(v) for k, v in vars(shared_adapter).items()} == shared_before
    async def lifecycle():
        await main.app.router._startup()
        assert_dormant()
        assert not any("shadow_health" in repr(t.get_coro()) for t in asyncio.all_tasks())
        assert {k: id(v) for k, v in vars(shared_adapter).items()} == shared_before
        await main.app.router._shutdown()
    asyncio.run(lifecycle())
    if os.environ.get("P3_COMPUTED_LOADER_CONTROL"):
        # Deliberately computed name: the runtime check must catch what the
        # scoped lexical validator does not promise to resolve.
        import importlib
        importlib.import_module("trusted_router." + "async_settle_" + "shadow_lifecycle")
    assert_dormant()
    from trusted_router.services.async_settle import Admission
    from trusted_router.services.async_settle_shadow_admission import Observer
    obs = Observer(frozenset({"ws"}), lambda _: Admission(0, 2), lambda: None,
                   clock=lambda: 10., wall=lambda: 100.)
    outputs = []
    for tier in (0, 1, 2, 3):
        for micro in (0, 100000, 100000000000):
            obs.install_workspace("ws", 10., Admission(micro, tier))
            for health in (None, dict(v=1, authority="local", observed_at=100., worker_heartbeat=100.,
                p50_age_seconds=0., p95_age_seconds=0., oldest_unresolved_age_seconds=0., complete=True,
                sample_count=0, backlog_count=0, frozen_micro=0, dead_count=0)):
                obs.health = health
                before = dict(obs.counts)
                outputs.extend((obs.peek("ws"), obs.peek("nonmember")))
                assert dict(obs.counts) == before
    assert_dormant()
    def label(f):
        return f.__module__+"."+f.__qualname__
    app = main.app
    return dict(outputs=outputs,
        routes=[(getattr(r, "path", None), sorted(getattr(r, "methods", None) or [])) for r in app.routes],
        startup=[label(f) for f in app.router.on_startup], shutdown=[label(f) for f in app.router.on_shutdown],
        lifespan=label(type(app.router.lifespan_context)))


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))


# A lazy, process-local shared probe: each requested configuration runs once.
@lru_cache
def run_probe(workspaces, mode):
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(root/"src"), TR_ASYNC_SETTLE_SHADOW_POLICY="",
        TR_ASYNC_SETTLE_SHADOW_WORKSPACES=workspaces, TR_ASYNC_SETTLE_ENABLED="false",
        TR_STORAGE_BACKEND="memory", TR_ENVIRONMENT="test")
    env.pop("P3_COMPUTED_LOADER_CONTROL", None)
    env.pop("P3_BLOCK_IMPORTS", None)
    if mode == "mutant":
        env["P3_COMPUTED_LOADER_CONTROL"] = "1"
    elif mode == "blocked":
        env["P3_BLOCK_IMPORTS"] = "1"
    return subprocess.run([sys.executable, str(root/"tests/shadow_observation_dormancy.py")],  # noqa: S603
        env=env, capture_output=True, timeout=300)


@pytest.fixture(scope="module")
def dormancy_runs():
    class Runs:
        def __getitem__(self, key):
            return run_probe(*key)
    return Runs()
