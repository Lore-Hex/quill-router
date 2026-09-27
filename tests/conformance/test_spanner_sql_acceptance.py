"""Production SQL acceptance by the native GoogleSQL server, never the Python fake.

Missing opt-in skips the server cases with an explicit reason; the completeness,
parameter-binding and builder-coverage guards always run, including on laptops.
The only SQL accommodation is the DDL-derived null-filtered-index hint.
"""
from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tests.conformance.spanner_emulator import (
    NULL_FILTERED_HINT,
    NULL_FILTERED_INDEXES,
    emulator_sql,
    names_null_filtered_index,
    sql_code,
)
from tests.conformance.spanner_sql_builders import SQLCase, builder_cases
from tests.conformance.spanner_sql_inventory import (
    assert_complete,
    builders,
    discover,
    evaluate,
    load_manifest,
    parameter_types,
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
            if scenario.get("wrapper"):
                sql = scenario["wrapper"].format(sql=sql)
            params, types = typed_parameters(scenario["types"])
            params.update(scenario.get("values", {}))
            yield SQLCase(f"{key}/{index}", [(sql, params, types)])


def all_cases():
    return [*literal_cases(), *builder_cases()]


def query_kind(sql):
    # Leading comments are production SQL, too.
    sql = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.S).lstrip(" \n\t(")
    return sql.split()[0].upper()


def test_null_filtered_hint_set_matches_registered_statements():
    statements = [statement for case in all_cases()
                  for statement in [*(case.seed or []), *case.statements]]
    expected = {sql for sql, _, _ in statements if names_null_filtered_index(sql)}
    actual = {original[0] for original in statements
              if NULL_FILTERED_HINT in sql_code(emulator_sql(original[0]))}
    assert actual == expected and expected
    for sql, _, _ in statements:
        code = sql_code(sql)
        # Independent of the rewrite regex: every index-name occurrence must be
        # the FORCE_INDEX value inside a hint, not merely somewhere in the SQL.
        covered = []
        replacements = []
        for block in re.finditer(r"@\{[^{}]*\}", code):
            has_index = False
            for force in re.finditer(r"(?:\{|,)\s*FORCE_INDEX\s*=\s*(\w+)\s*(?=,|\})", block[0], re.I):
                if force[1].lower() in NULL_FILTERED_INDEXES:
                    has_index = True
                    covered.append((block.start() + force.start(1), block.start() + force.end(1)))
                    rewritten = emulator_sql(sql[block.start():block.end()])
                    assert rewritten.startswith("@{") and rewritten.endswith("}")
                    assert NULL_FILTERED_HINT in rewritten
            if has_index:
                replacements.append((block.start(), block.end(), rewritten))
        references = [token.span() for token in re.finditer(r"\b\w+\b", code)
                      if token[0].lower() in NULL_FILTERED_INDEXES]
        assert references == covered, sql
        expected_sql = sql
        for start, end, replacement in reversed(replacements):
            expected_sql = expected_sql[:start] + replacement + expected_sql[end:]
        # Checking just the set of index references can hide changes to data.
        assert emulator_sql(sql) == expected_sql, sql


def test_timestamp_string_bindings_use_utc_z():
    from google.cloud.spanner_v1 import param_types

    for case in all_cases():
        for _, params, types in [*(case.seed or []), *case.statements]:
            for name, value in params.items():
                if types[name] == param_types.TIMESTAMP and isinstance(value, str):
                    assert value.endswith("Z"), (case.name, name, value)


def test_spanner_sql_inventory_is_complete():
    assert_complete()


def test_every_registered_case_has_exact_typed_bindings():
    sources = discover()
    for key, registration in load_manifest()["expressions"].items():
        production_types = parameter_types(sources[key])
        for scenario in registration["scenarios"]:
            for name, kind in scenario["types"].items():
                if name in production_types:
                    assert kind == production_types[name], (key, name, kind, production_types[name])
    cases = all_cases()
    assert len(cases) >= 300, "SQL acceptance inventory unexpectedly empty"
    for case in cases:
        assert case.statements, case.name
        for sql, params, types in [*(case.seed or []), *case.statements]:
            required = set(re.findall(r"@([A-Za-z_]\w*)", sql))
            assert required == params.keys() == types.keys(), (case.name, required, params.keys(), types.keys())
            assert query_kind(sql) in {"SELECT", "WITH", "UPDATE", "INSERT", "DELETE"}


def test_registered_builders_are_actually_called():
    returned = {}
    expected = builders()

    def record(frame, event, arg):
        module = frame.f_globals.get("__name__", "").removeprefix("trusted_router.")
        key = f"{module}:{frame.f_code.co_name}"
        if event == "return" and key in expected:
            returned.setdefault(key, []).append(arg)

    previous = sys.getprofile()
    try:
        # Production builders stamp the clock: freeze it so repeated equivalent
        # output can be compared without normalizing away parameter differences.
        from tests.conformance.spanner_sql_builders import NOW
        with patch("trusted_router.storage_gcp_settle_outbox._iso_now",
                   return_value=NOW.isoformat().replace("+00:00", "Z")):
            sys.setprofile(record)
            cases = builder_cases()
    finally:
        sys.setprofile(previous)
    assert expected.keys() <= returned.keys(), f"Unexercised builders: {expected.keys() - returned.keys()}"
    carried = [statement for case in cases for statement in [*(case.seed or []), *case.statements]]
    for key, outputs in returned.items():
        for output in outputs:
            statements = output if isinstance(output, list) else [output]
            for statement in statements:
                if isinstance(statement, str):
                    assert any(statement in sql for sql, _, _ in carried), key
                else:
                    assert statement in carried, f"Discarded builder output: {key}: {statement}"


@pytest.fixture(scope="session")
def sql_database(native_emulator_resources):
    return native_emulator_resources[0]


@contextmanager
def rolled_back(database):
    """Explicit SDK transaction, no run_in_transaction/implicit commit anywhere."""
    session = database.session()
    transaction = None
    original_error = None
    try:
        session.create()
        transaction = session.transaction()
        transaction.begin()
        yield transaction
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        cleanup_errors = []
        for cleanup in ([transaction.rollback] if transaction is not None else []) + [session.delete]:
            try:
                cleanup()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors and original_error is None:
            raise cleanup_errors[0]


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
        if len(case.statements) == 1 and query_kind(case.statements[0][0]) in {"SELECT", "WITH"}:
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
                                 if query_kind(case.statements[0][0]) in {"UPDATE", "INSERT", "DELETE"}
                                 and "THEN RETURN" not in case.statements[0][0]], ids=lambda case: case.name)
def test_production_batch_dml_acceptance(sql_database, case):
    # DML the adapter currently executes sequentially is checked in batch form
    # too: promotion to a batch must not introduce an untested SQL dialect path.
    with rolled_back(sql_database) as transaction:
        execute_dml(transaction, case.statements, batch=True)


def function_limit_sql(count):
    return "SELECT [" + ",".join("IF(id='x',body,id)" for _ in range(count)) + "] FROM tr_entities"  # noqa: S608 - server-limit controls


# Same table, column expression and function in each pair; only the prohibited
# argument (or repetition count) changes. Error matching is restriction-specific.
CANARIES = [
    ("row-dependent-create-if-missing",
     "SELECT JSON_SET(PARSE_JSON(body), '$.x', 1, create_if_missing => (id='x')) FROM tr_entities",
     "SELECT JSON_SET(PARSE_JSON(body), '$.x', 1, create_if_missing => true) FROM tr_entities",
     r"(?is)(?=.*create_if_missing)(?=.*(?:literal|query parameter))"),
    ("row-dependent-json-remove-path",
     "SELECT JSON_REMOVE(PARSE_JSON(body), CONCAT('$.',id)) FROM tr_entities",
     "SELECT JSON_REMOVE(PARSE_JSON(body), '$.x') FROM tr_entities",
     r"(?i)\bArgument 2 to JSON_REMOVE must be (?:a constant expression|a literal or query parameter)\b"),
    ("over-1000-functions", function_limit_sql(520), function_limit_sql(450),
     r"Number of functions exceeds the maximum allowed limit of 1000"),
]


@pytest.mark.parametrize(("name", "sql", "control", "pattern"), CANARIES, ids=[case[0] for case in CANARIES])
def test_emulator_rejects_known_production_failures(sql_database, name, sql, control, pattern):
    from google.api_core.exceptions import InvalidArgument

    with pytest.raises(InvalidArgument, match=pattern):
        with sql_database.snapshot() as snapshot:
            list(snapshot.execute_sql(sql))


@pytest.mark.parametrize(("name", "sql", "control", "pattern"), CANARIES, ids=[case[0] for case in CANARIES])
def test_emulator_accepts_controls_for_production_regressions(sql_database, name, sql, control, pattern):
    with sql_database.snapshot() as snapshot:
        list(snapshot.execute_sql(control))


@pytest.mark.parametrize("case", CANARIES, ids=[case[0] for case in CANARIES])
def test_canary_rejection_is_specific(case):
    from google.api_core.exceptions import InvalidArgument

    unrelated = InvalidArgument("Unsupported function PARSE_JSON on a column expression; invalid argument path")
    assert re.search(case[3], str(unrelated)) is None


def test_frozen_fragments_are_inside_fingerprinted_scopes():
    import ast

    sources = discover()
    fragments = {"where", "arms", "sibling", "phase_sql", "suffix_sql", "tail", "suffix"}
    seen = set()
    for key, registration in load_manifest()["expressions"].items():
        source = sources[key]
        assigned = {node.id for node in ast.walk(source.scope)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        for scenario in registration["scenarios"]:
            for name in fragments & scenario["bindings"].keys():
                assert name in assigned, f"Frozen fragment outside fingerprinted scope: {key}: {name}"
                assert source.fingerprint == registration["fingerprint"], key
                seen.add(name)
    assert fragments <= seen


@pytest.mark.parametrize("message", [
    "Argument 2 to JSON_REMOVE must be a constant expression",
    "Argument 2 to JSON_REMOVE must be a literal or query parameter [at 1:38]",
], ids=["production", "emulator-run-2"])
def test_json_remove_reported_restriction_texts(message):
    # Supplied diagnostic fixtures verify the matcher, not live server wording.
    pattern = next(case[3] for case in CANARIES if case[0] == "row-dependent-json-remove-path")
    assert re.search(pattern, message)
    for unrelated in ("Argument 2 to JSON_REMOVE must be constant",
                      "Argument 1 to JSON_REMOVE must be a constant expression",
                      "Argument 2 to JSON_SET must be a literal or query parameter"):
        assert re.search(pattern, unrelated) is None


def test_register_claim_scenario_has_production_value_shapes():
    from trusted_router.spend_leases import spend_lease_scope_salt

    case = next(case for case in literal_cases() if case.name == "storage_gcp_spend_lease:register_claim:1/0")
    _, params, _ = case.statements[0]
    # DDL's CLAIM branch requires a non-null provisional_id; the global default
    # is None for BOUND rows. Keep this override local to the inserting scenario.
    assert isinstance(params["provisional_id"], str) and 0 < len(params["provisional_id"]) <= 64
    assert 0 < len(params["scope"]) <= 256
    assert params["scope_salt"] == spend_lease_scope_salt(params["scope"])
