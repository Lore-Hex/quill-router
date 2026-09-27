"""Production SQL acceptance by the native GoogleSQL server, never the Python fake.

Missing opt-in skips the server cases with an explicit reason; the completeness,
parameter-binding and builder-coverage guards always run, including on laptops.
No unsupported-feature allowlist: every rejection names a statement and fails CI.
"""
from __future__ import annotations

import re
import sys
from contextlib import contextmanager

import pytest

from tests.conformance.spanner_emulator import emulator_resources
from tests.conformance.spanner_sql_builders import SQLCase, builder_cases
from tests.conformance.spanner_sql_inventory import (
    assert_complete,
    builders,
    discover,
    evaluate,
    load_manifest,
    typed_parameters,
)

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")


def literal_cases():
    sources = discover()
    for key, registration in load_manifest()["expressions"].items():
        if key not in sources:
            continue  # completeness guard reports stale registrations separately
        for index, scenario in enumerate(registration["scenarios"]):
            sql = evaluate(sources[key], scenario["bindings"])
            params, types = typed_parameters(scenario["types"])
            params.update(scenario.get("values", {}))
            yield SQLCase(f"{key}/{index}", [(sql, params, types)])


def all_cases():
    return [*literal_cases(), *builder_cases()]


def test_spanner_sql_inventory_is_complete():
    assert_complete()


def test_every_registered_case_has_exact_typed_bindings():
    cases = all_cases()
    assert len(cases) >= 300, "SQL acceptance inventory unexpectedly empty"
    for case in cases:
        assert case.statements, case.name
        for sql, params, types in [*(case.seed or []), *case.statements]:
            required = set(re.findall(r"@([A-Za-z_]\w*)", sql))
            assert required == params.keys() == types.keys(), (case.name, required, params.keys(), types.keys())
            assert sql.strip().split()[0].upper() in {"SELECT", "WITH", "UPDATE", "INSERT", "DELETE"}


def test_registered_builders_are_actually_called():
    called = set()
    expected = builders()

    def record(frame, event, arg):
        if event == "call":
            module = frame.f_globals.get("__name__", "").removeprefix("trusted_router.")
            called.add(f"{module}:{frame.f_code.co_name}")

    previous = sys.getprofile()
    try:
        sys.setprofile(record)
        assert builder_cases()
    finally:
        sys.setprofile(previous)
    assert expected.keys() <= called, f"Unexercised builders: {expected.keys() - called}"


@pytest.fixture(scope="module")
def sql_database():
    with emulator_resources() as (database, _table, _instance):
        yield database


@contextmanager
def rolled_back(database):
    """Explicit SDK transaction, no run_in_transaction/implicit commit anywhere."""
    session = database.session()
    session.create()
    transaction = session.transaction()
    try:
        transaction.begin()
        yield transaction
    finally:
        try:
            transaction.rollback()
        finally:
            session.delete()


def execute_dml(transaction, statements, *, batch):
    if batch:
        status, counts = transaction.batch_update(statements)
        assert status.code == 0, f"Batch DML rejected: {status}"
        assert len(counts) == len(statements), "Batch DML did not execute every statement"
    else:
        counts = []
        for sql, params, types in statements:
            if "THEN RETURN" in sql:
                counts.append(len(list(transaction.execute_sql(sql, params=params, param_types=types))))
            else:
                counts.append(transaction.execute_update(sql, params=params, param_types=types))

    return counts


@pytest.mark.parametrize("case", all_cases(), ids=lambda case: case.name)
def test_production_sql_acceptance(sql_database, case):
    try:
        if len(case.statements) == 1 and case.statements[0][0].lstrip().upper().startswith(("SELECT", "WITH")):
            sql, params, types = case.statements[0]
            with sql_database.snapshot() as snapshot:
                list(snapshot.execute_sql(sql, params=params, param_types=types))
        else:
            with rolled_back(sql_database) as transaction:
                if case.seed:
                    execute_dml(transaction, case.seed, batch=False)
                counts = execute_dml(transaction, case.statements, batch=case.batch)
                if case.expected_counts is not None:
                    assert counts == case.expected_counts, "seeded statement did not exercise its target row"
    except Exception as exc:
        pytest.fail(f"GoogleSQL acceptance failed for {case.name}: {type(exc).__name__}: {exc}", pytrace=True)


@pytest.mark.parametrize("case", [case for case in literal_cases()
                                 if case.statements[0][0].lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
                                 and "THEN RETURN" not in case.statements[0][0]], ids=lambda case: case.name)
def test_production_batch_dml_acceptance(sql_database, case):
    # DML the adapter currently executes sequentially is checked in batch form
    # too: promotion to a batch must not introduce an untested SQL dialect path.
    with rolled_back(sql_database) as transaction:
        execute_dml(transaction, case.statements, batch=True)


@pytest.mark.parametrize(("name", "sql"), [
    ("row-dependent-create-if-missing", "SELECT JSON_SET(PARSE_JSON(body), '$.x', 1, create_if_missing => (id='x')) FROM tr_entities"),
    ("row-dependent-json-remove-path", "SELECT JSON_REMOVE(PARSE_JSON(body), CONCAT('$.',id)) FROM tr_entities"),
    ("over-1000-functions", "SELECT [" + ",".join("IF(id='x',body,id)" for _ in range(1001)) + "] FROM tr_entities"),  # noqa: S608 - deliberate server-limit canary
])
def test_emulator_rejects_known_production_failures(sql_database, name, sql):
    from google.api_core.exceptions import InvalidArgument

    # A success is a coverage hole, not a reason to xfail this canary. If the
    # emulator diverges, CI must report that it cannot guard this production rule.
    with pytest.raises(InvalidArgument, match="(?i)(literal|constant|function|limit|argument|expression|path)"):
        with sql_database.snapshot() as snapshot:
            list(snapshot.execute_sql(sql))


@pytest.mark.parametrize("sql", [
    "SELECT JSON_SET(PARSE_JSON('{\"x\":1}'), '$.x', 2, create_if_missing => true)",
    "SELECT JSON_REMOVE(PARSE_JSON('{\"x\":1}'), '$.x')",
    "SELECT [" + ",".join("IF(id='x',id,'y')" for _ in range(900)) + "] FROM UNNEST(['x']) AS id",  # noqa: S608 - fixed positive control
], ids=["literal-create-if-missing", "literal-json-remove-path", "below-function-limit"])
def test_emulator_accepts_controls_for_production_regressions(sql_database, sql):
    # Paired positive controls prevent a missing function/dialect from being
    # mistaken for enforcement of the three specific production restrictions.
    with sql_database.snapshot() as snapshot:
        assert len(list(snapshot.execute_sql(sql))) == 1
