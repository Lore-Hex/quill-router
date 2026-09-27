"""Keep emulator provisioning tied to the deploy scripts, without a server."""
from tests.conformance.spanner_ddl import DDL, SOURCE_DIGESTS
from tests.conformance.spanner_schema_source import assert_schema_matches


def test_spanner_ddl_matches_migrations():
    assert_schema_matches(DDL, SOURCE_DIGESTS)
