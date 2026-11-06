"""Stop row semantics on native GoogleSQL; included in CI emulator selection."""
import pytest

from scripts.async_settle.shadow_v2_stop import execute
from trusted_router.storage_gcp_shadow_observation import ObservationStore


@pytest.mark.parametrize('backend', ['spanner-emulator'])
def test_shadow_stop_one_key_idempotence_and_clear(native_emulator_resources, backend):
    database, _ = native_emulator_resources
    store = ObservationStore(database)
    first = execute(store, action='stop', actor='native-test', reason='incident', incident='test-123')
    assert store.read_runtime_stop().value() == first['row']
    assert execute(store, action='stop', actor='retry', reason='retry', incident='retry')['row'] == first['row']
    assert execute(store, action='clear', actor='reviewer', reason='resolved')['row'] is None
    assert store.read_runtime_stop() is None
    assert execute(store, action='clear', actor='reviewer', reason='retry')['row'] is None
