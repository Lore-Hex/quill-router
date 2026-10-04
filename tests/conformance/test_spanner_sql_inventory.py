"""Always-run SQL inventory guard, independent of emulator availability."""
from tests.conformance.spanner_sql_inventory import assert_complete


def test_spanner_sql_inventory_complete():
    assert_complete()
