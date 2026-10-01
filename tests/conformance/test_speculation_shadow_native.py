"""Real GoogleSQL transaction gate; a missing emulator is explicitly a skip."""
from types import SimpleNamespace

import pytest

from tests.test_speculation_shadow import event
from trusted_router.services.speculation_shadow import identity, project
from trusted_router.storage_gcp_speculation_shadow import SpannerSpeculationShadow

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")


def test_native_shadow_atomic_dedup_and_scoped_projection(native_emulator_resources):
    database, _ = native_emulator_resources
    store = SpannerSpeculationShadow(SimpleNamespace(_database=database), SimpleNamespace(speculation_shadow_plane="shadow-test"))
    store.ready()
    for sequence in range(1, 4):
        e = event(sequence, auth="same-authorization")
        store.transaction(lambda tx, e=e: project(tx, e, "p", "inc", e.sequence, False, 2000))
    assert len(store.read("scope", identity("key", "w", "k"))["successes"]) == 1
    assert store.read("producer", "p")["sequence"] == 3
    before = store.read("scope", identity("key", "w", "k"))
    def abort(tx):
        tx.put("scope", identity("key", "w", "k"), {"bad": True})
        raise ValueError("rollback")
    with pytest.raises(ValueError, match="rollback"):
        store.transaction(abort)
    assert store.read("scope", identity("key", "w", "k")) == before
