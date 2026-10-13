"""Clean-interpreter policy-empty request probe; baseline is main 6a398b5f."""
import json
import os
import sys

NAMES = {"async_settle_shadow_lifecycle", "async_settle_shadow_journal", "async_settle_shadow_health_state",
         "async_settle_shadow_attachments", "async_settle_shadow_health", "storage_gcp_shadow_observation",
         "storage_gcp_shadow_io", "storage_gcp_shadow_attachments"}


def assert_dormant():
    assert not any("shadow_v2_" in n or n.rsplit(".", 1)[-1] in NAMES for n in sys.modules), "empty-policy v2 import"


def probe():
    import trusted_router.main  # noqa: F401
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
    return dict(modules=sorted(n for n in sys.modules if n.startswith("trusted_router")), outputs=outputs)


if __name__ == "__main__":
    print(json.dumps(probe(), sort_keys=True))
