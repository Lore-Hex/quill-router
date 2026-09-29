"""The scheduled audit's reader must accept every fixture object on Spanner."""
import os
import re

import pytest

from scripts.audit_spanner_schema import expected_schema
from tests.conformance.spanner_ddl import DDL


def test_expected_schema_reads_every_fixture_table_and_index():
    if os.getenv("TR_CONFORMANCE_EMULATOR_SCHEMA") != "1":
        pytest.skip("set TR_CONFORMANCE_EMULATOR_SCHEMA=1 to require the loopback emulator")
    endpoint = os.environ["SPANNER_EMULATOR_HOST"]
    schema = expected_schema(endpoint)
    tables = {match[1] for sql in DDL if (match := re.match(r"CREATE TABLE (\w+)", sql))}
    indexes = {(match[2], match[1]) for sql in DDL if (match := re.match(r"CREATE (?:UNIQUE )?(?:NULL_FILTERED )?INDEX (\w+) ON (\w+)", sql))}
    assert tables and indexes
    assert {key.removeprefix("table/") for key in schema if key.startswith("table/")} == tables
    assert {key.removeprefix("index/") for key in schema if key.startswith("index/") and not key.endswith("/PRIMARY_KEY")} == {f"{table}/{index}" for table, index in indexes}
    assert all(schema[f"index/{table}/PRIMARY_KEY"]["columns"] for table in tables)
    assert schema["column/tr_entities/ephemeral_expires_at"]["IS_GENERATED"] == "ALWAYS"
    assert schema["constraint/tr_trust_event/tr_trust_event_provider"]["CHECK_CLAUSE"]
