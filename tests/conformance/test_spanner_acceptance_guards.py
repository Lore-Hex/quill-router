"""Negative controls run against copies, without changing production adapters."""
from __future__ import annotations

import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conformance import spanner_ddl
from tests.conformance import spanner_schema_source as schema
from tests.conformance.spanner_emulator import require_emulators
from tests.conformance.spanner_schema_source import ROOT, assert_schema_matches, migration_ddl
from tests.conformance.spanner_sql_inventory import SRC, assert_complete


def copy_schema_repository(root):
    shutil.copytree(ROOT / "scripts/deploy", root / "scripts/deploy")


def test_unregistered_sql_fails_in_a_copy(tmp_path):
    source = tmp_path / "src"
    shutil.copytree(SRC, source)
    assert_complete(source)
    path = source / "storage_gcp.py"
    path.write_text(path.read_text() + '\nUNREGISTERED_SQL = "SELECT id FROM tr_entities"\n')  # noqa: S608 - deliberate mutation in a copy
    with pytest.raises(AssertionError, match="Unregistered SQL"):
        assert_complete(source)


def test_new_builder_fails_in_a_copy(tmp_path):
    source = tmp_path / "src"
    shutil.copytree(SRC, source)
    path = source / "storage_gcp.py"
    path.write_text(path.read_text() + '\ndef unregistered_statement():\n    return _API_KEY_AUTH_CONTEXT_SQL\n')
    with pytest.raises(AssertionError, match="builder inventory changed"):
        assert_complete(source)


def test_changed_column_type_fails_in_a_copy(tmp_path):
    path = tmp_path / "spanner_ddl.py"
    original = Path(spanner_ddl.__file__).read_text()
    changed = original.replace("kind STRING(64) NOT NULL", "kind INT64 NOT NULL", 1)
    assert changed != original
    path.write_text(changed)
    copied = runpy.run_path(str(path))
    with pytest.raises(AssertionError, match="DDL drift"):
        assert_schema_matches(copied["DDL"], copied["SOURCE_DIGESTS"])


def test_new_schema_source_cannot_silently_escape_parser(tmp_path):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    (scripts / "migrate_new.sh").write_text('ensure_table future_table "unknown new idiom"\n')
    with pytest.raises(AssertionError, match="Schema source changed"):
        assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, tmp_path)
    with pytest.raises(AssertionError, match="Unknown schema helper"):
        migration_ddl(tmp_path)


def test_opted_in_missing_emulator_is_failure(monkeypatch):
    monkeypatch.setenv("TR_CONFORMANCE_EMULATOR_SCHEMA", "1")
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    with pytest.raises(AssertionError, match="SPANNER_EMULATOR_HOST is required"):
        require_emulators()


def test_external_endpoint_is_rejected_before_connecting(monkeypatch):
    monkeypatch.setenv("TR_CONFORMANCE_EMULATOR_SCHEMA", "1")
    monkeypatch.setenv("SPANNER_EMULATOR_HOST", "spanner.googleapis.com:443")
    with pytest.raises(AssertionError, match="loopback emulator required"):
        require_emulators()


@pytest.mark.parametrize("failure", [False, True])
def test_acceptance_transaction_always_rolls_back(failure):
    from unittest.mock import Mock

    from tests.conformance.test_spanner_sql_acceptance import rolled_back

    database = Mock()
    session = database.session.return_value
    transaction = session.transaction.return_value
    if failure:
        with pytest.raises(RuntimeError, match="query failed"), rolled_back(database):
            raise RuntimeError("query failed")
    else:
        with rolled_back(database):
            pass
    transaction.begin.assert_called_once()
    transaction.rollback.assert_called_once()
    transaction.commit.assert_not_called()
    session.delete.assert_called_once()


@pytest.mark.parametrize("failure", [None, "body", "close", "create"])
def test_provisioning_submits_all_ddl_and_cleans_up(monkeypatch, failure):
    from unittest.mock import Mock

    from google.cloud import bigtable, spanner

    from tests.conformance import spanner_emulator
    from tests.conformance.test_spanner_emulator_sdk import SDK_METHODS

    originals = [getattr(cls, method) for cls, method, _ in SDK_METHODS]

    monkeypatch.setattr(spanner_emulator, "require_emulators", lambda: None)
    spanner_client, bigtable_client = Mock(), Mock()
    from datetime import timedelta

    from google.cloud.spanner_v1.database_sessions_manager import DatabaseSessionsManager

    # Establish the SDK default, then assert ordering inside the constructor.
    monkeypatch.setattr(DatabaseSessionsManager, "_MAINTENANCE_THREAD_POLLING_INTERVAL", timedelta(minutes=10))

    def create_client(**kwargs):
        assert 0 < DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL.total_seconds() < 1
        assert all(getattr(cls, method) is not original
                   for (cls, method, _), original in zip(SDK_METHODS, originals, strict=True))
        return spanner_client

    monkeypatch.setattr(spanner, "Client", Mock(side_effect=create_client))
    monkeypatch.setattr(bigtable, "Client", Mock(return_value=bigtable_client))
    instance = spanner_client.instance.return_value
    database = instance.database.return_value
    table = bigtable_client.instance.return_value.table.return_value
    def close():
        assert 0 < DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL.total_seconds() < 1
        if failure == "close":
            raise RuntimeError("close")

    database.close.side_effect = close
    if failure == "create":
        database.create.side_effect = RuntimeError("create")

    def provision():
        with spanner_emulator.emulator_resources() as resources:
            assert resources[0] is database
            submitted = list(instance.database.call_args.kwargs["ddl_statements"])
            for call in database.update_ddl.call_args_list:
                assert 0 < len(call.args[0]) <= 20
                submitted.extend(call.args[0])
            assert tuple(submitted) == spanner_ddl.DDL
            assert set(table.create.call_args.kwargs["column_families"]) == {"m", "activity", "benchmark", "synthetic", "rollup"}
            instance.delete.assert_not_called()
            if failure == "body":
                raise RuntimeError("body")

    if failure:
        with pytest.raises(RuntimeError, match=failure):
            provision()
    else:
        provision()
    database.close.assert_called_once()
    if failure == "create":
        table.delete.assert_not_called()
    else:
        table.delete.assert_called_once()
    instance.delete.assert_called_once()
    assert DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL == timedelta(minutes=10)
    assert [getattr(cls, method) for cls, method, _ in SDK_METHODS] == originals


@pytest.mark.parametrize("phase", ["create", "begin", "body", "rollback", "delete"])
def test_transaction_cleanup_preserves_original_failure(phase):
    from unittest.mock import Mock

    from tests.conformance.test_spanner_sql_acceptance import rolled_back

    database = Mock()
    session = database.session.return_value
    transaction = session.transaction.return_value
    if phase in {"create", "delete"}:
        getattr(session, phase).side_effect = RuntimeError(phase)
    elif phase in {"begin", "rollback"}:
        getattr(transaction, phase).side_effect = RuntimeError(phase)
    if phase in {"create", "begin", "body"}:
        transaction.rollback.side_effect = ValueError("cleanup rollback")
        session.delete.side_effect = ValueError("cleanup delete")
    with pytest.raises(RuntimeError, match=phase), rolled_back(database):
        if phase == "body":
            raise RuntimeError("body")
    session.delete.assert_called_once()
    transaction.commit.assert_not_called()


@pytest.mark.parametrize("addition", [
    'ddl=$(cat <<EOF\nCREATE TABLE review_hole (id STRING(64)) PRIMARY KEY(id)\nEOF\n)\ngcloud spanner databases ddl update db --ddl="$ddl"\n',
    "gcloud spanner databases ddl update db --ddl='ALTER TABLE tr_entities ADD COLUMN review_hole STRING(64)'\n",
    'DEFINITION="STRING(64)"\nensure_column tr_entities review_hole "$DEFINITION"\n',
], ids=["heredoc", "single-quoted-alter", "variable-definition"])
def test_schema_blind_spots_fail_closed_in_copies(tmp_path, addition):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_review_hole.sh"
    path.write_text(addition)
    with pytest.raises(AssertionError, match=r"migrate_review_hole.sh:\d+:"):
        migration_ddl(tmp_path)


@pytest.mark.parametrize("addition", [
    "apply_ddl 'alter table tr_entities add column review_lost STRING(64)'\n",
    'apply_ddl "ALTER\nTABLE tr_entities ADD COLUMN review_lost STRING(64)"\n',
    'gcloud spanner databases ddl update db --ddl="CREATE TABLE review_kept (id STRING(64)) PRIMARY KEY(id)"; '
    "gcloud spanner databases ddl update db --ddl='ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)'\n",
    'apply_ddl "$UNPARSED_DDL"\n',
    'apply_ddl $UNPARSED_DDL\n',
    "APPLY_DDL 'alter table tr_entities add column review_lost STRING(64)'\n",
    "DDL 'alter\ntable tr_entities add column review_lost STRING(64)'\n",
    "gcloud spanner databases ddl update db \\\n --DDL 'alter table tr_entities add column review_lost STRING(64)'\n",
], ids=["lowercase-alter", "split-alter-table", "same-line-dispatches", "quoted-variable",
        "bare-variable", "uppercase-helper", "uppercase-ddl-helper", "uppercase-gcloud-option"])
def test_every_ddl_dispatch_is_consumed_in_copies(tmp_path, addition):
    # Review round 2 probes now require rejection even with refreshed digests.
    from tests.conformance.spanner_schema_source import source_digests

    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_money_primitives.sh"
    original = path.read_text() + '\nddl() { gcloud spanner databases ddl update db --ddl="$1"; }\n'
    path.write_text(original + addition)
    line = original.count("\n") + 1
    # Joined continuations report the physical start of the logical command.
    with pytest.raises(AssertionError, match=rf"migrate_money_primitives.sh:{line}: unconsumed DDL dispatch"):
        assert_schema_matches(spanner_ddl.DDL, source_digests(tmp_path), tmp_path)


def test_lowercase_split_create_index_is_extracted(tmp_path):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_money_primitives.sh"
    path.write_text(path.read_text() + '\napply_ddl "create\nindex review_added ON tr_entities (kind)"\n')
    ddl = migration_ddl(tmp_path)
    assert "create index review_added ON tr_entities (kind)" in ddl
    assert len(ddl) == len(spanner_ddl.DDL) + 1


@pytest.mark.parametrize("addition, carrier", [
    ('''args=(spanner databases ddl update "$DATABASE"
      "--instance=$INSTANCE"
      "--ddl=ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)")
gcloud "${args[@]}"
''', "ddl"),
    ('''$GCLOUD spanner databases ddl update "$DATABASE" --ddl-file=/dev/stdin <<'SQL'
ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)
SQL
''', "--ddl-file"),
    ('''gcp "spanner" "databases" "ddl" "update" "$DATABASE" "--ddl=ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)"
''', "ddl"),
    ('''args=("spanner" "databases" "ddl" "update" "$DATABASE"
      "--DDL=alter table tr_entities add column review_lost STRING(64)")
gcloud "${args[@]}"
''', "ddl"),
    ('"$GCLOUD" spanner databases create "$DATABASE"\n', "databases create"),
    ('args=(spanner databases ddl\n update "$DATABASE")\n', "ddl"),
], ids=["reviewer-array", "variable-gcloud-ddl-file-heredoc", "library-gcp-wrapper",
        "uppercase-flag-lowercase-sql", "unknown-create", "multiline-command-words"])
def test_literal_ddl_carriers_fail_closed_in_copies(tmp_path, addition, carrier):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    library = scripts / "_lib.sh"
    library.write_text(library.read_text() + '\ngcp() { gcloud --project "$PROJECT_ID" "$@"; }\n')
    path = scripts / "migrate_review_hole.sh"
    prefix = ""
    if addition.startswith("gcp "):
        # Exercise a script that actually sources the library defining gcp.
        path = scripts / "migrate_generation_records.sh"
        prefix = path.read_text() + "\n"
    path.write_text(prefix + addition)
    line = prefix.count("\n") + addition[:addition.index(carrier)].count("\n") + 1
    with pytest.raises(AssertionError) as error:
        migration_ddl(tmp_path)
    assert f"{path}:{line}:" in str(error.value)
    assert f"DDL carrier: {carrier}" in str(error.value)


def test_quoted_array_ddl_carrier_fails_closed(tmp_path):
    # All command words are quoted too: only the literal quoted flag exposes
    # this array to the carrier scan. Removing quoted text must kill this test.
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_review_hole.sh"
    path.write_text('''args=("spanner" "databases" "ddl" "update" "$DATABASE"
      "--instance=$INSTANCE"
      "--ddl=ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)")
gcloud "${args[@]}"
''')
    with pytest.raises(AssertionError, match=r"migrate_review_hole.sh:1: unconsumed DDL carrier: ddl"):
        migration_ddl(tmp_path)


@pytest.mark.parametrize("addition, carrier", [
    ('''args=(spanner databases ddl update "$DATABASE"
      "--ddl=ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)")
gcloud "${args[@]}"
''', "ddl"),
    ('''gcp() { gcloud --project "$PROJECT_ID" "$@"; }
gcp "spanner" "databases" "ddl" "update" db "--ddl=ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)"
''', "ddl"),
    ('''gcp() { gcloud --project "$PROJECT_ID" "$@"; }
gcp spanner databases ddl update db --ddl-file=/dev/stdin <<'SQL'
ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)
SQL
''', "--ddl-file"),
], ids=["library-only-array", "library-only-wrapper", "library-only-ddl-file"])
def test_library_carriers_fail_with_original_digest_guard(tmp_path, addition, carrier):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "_lib.sh"
    text = path.read_text() + "\n" + addition
    path.write_text(text)
    line = text[:text.index(carrier)].count("\n") + 1
    with pytest.raises(AssertionError) as error:
        assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, tmp_path)
    assert f"{path}:{line}:" in str(error.value)
    assert f"DDL carrier: {carrier}" in str(error.value)


@pytest.mark.parametrize("carrier", ["--ddl-file", "--DDL-FILE", "ddl-file"])
def test_ddl_file_rejected_even_inside_consumed_dispatch(tmp_path, carrier):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_review_hole.sh"
    path.write_text('gcloud spanner databases ddl update db '
                    '--ddl="CREATE INDEX review_added ON tr_entities (kind)" '
                    f'"{carrier}=/dev/stdin"\n')
    with pytest.raises(AssertionError) as error:
        migration_ddl(tmp_path)
    assert f"{path}:1: unsupported DDL carrier: {carrier}" in str(error.value)


@pytest.mark.parametrize("name", ["infra.sh", "migrate_money_primitives.sh", "_lib.sh"])
def test_carrier_comments_require_exemptions_in_copies(tmp_path, name):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / name
    path.write_text(path.read_text() + '\n# --ddl --ddl-file ddl-file ddl update databases create\n'
                    + 'echo done # "--DDL=" --ddl-file databases create\n')
    with pytest.raises(AssertionError, match="DDL carrier:"):
        migration_ddl(tmp_path)


@pytest.mark.parametrize("expression", [
    '"INSERT OR IGNORE INTO tr_entities (kind, id) VALUES (@kind, @id)"',
    '"/* comment */ SELECT id FROM tr_entities"',
    '"(SELECT id FROM tr_entities)"',
    '"SELECT" + columns + " FROM tr_entities"',
    'f"{prefix} SELECT id FROM tr_entities"',
], ids=["insert-or-ignore", "comment", "parenthesized", "split-prefix", "dynamic-prefix"])
def test_sql_detection_variants_in_new_module_fail_in_copies(tmp_path, expression):
    source = tmp_path / "src"
    shutil.copytree(SRC, source)
    (source / "new_native_module.py").write_text("SQL = " + expression + "\n")
    with pytest.raises(AssertionError, match="Unregistered SQL.*new_native_module"):
        assert_complete(source)


def test_native_legacy_gaps_are_strict_at_collection():
    from types import SimpleNamespace

    from tests.conformance.conftest import (
        _BACKEND_KNOWN_GAPS,
        _C1_LEGACY_MONEY,
        _FAKE_ONLY_GAPS,
        _NATIVE_STORE_KNOWN_GAPS,
        pytest_collection_modifyitems,
    )

    legacy = {name for name, reason in _NATIVE_STORE_KNOWN_GAPS.items()
              if reason == _C1_LEGACY_MONEY}
    assert len(legacy) == 10
    assert len(_BACKEND_KNOWN_GAPS["spanner-emulator"]) == len(_NATIVE_STORE_KNOWN_GAPS)
    for fixture, name in legacy | _FAKE_ONLY_GAPS.keys():
        marks = []
        item = SimpleNamespace(callspec=SimpleNamespace(params={fixture: "spanner-emulator"}),
                               nodeid="tests/conformance/" + name + "[backend=spanner-emulator]",
                               originalname=name.split("::")[-1], add_marker=marks.append)
        pytest_collection_modifyitems([item])
        if (fixture, name) in legacy:
            assert len(marks) == 1 and marks[0].kwargs == {"strict": True, "reason": _C1_LEGACY_MONEY}
        else:
            assert not marks


@pytest.mark.parametrize("backend", ["spanner-fake", "spanner-emulator"])
def test_native_rollup_gap_is_strict_at_collection(backend):
    from types import SimpleNamespace

    from tests.conformance.conftest import _FAKE_ONLY_GAPS, pytest_collection_modifyitems

    name = "test_synthetic_rollups_apply_ranges_order_limit_and_histogram_option"
    marks = []
    item = SimpleNamespace(callspec=SimpleNamespace(params={"store": backend}),
                           nodeid="tests/conformance/test_store_semantics.py::" + name + "[backend=" + backend + "]",
                           originalname=name, add_marker=marks.append)
    pytest_collection_modifyitems([item])
    assert name not in _FAKE_ONLY_GAPS
    assert len(marks) == 1 and marks[0].name == "xfail" and marks[0].kwargs["strict"] is True
    assert "#1370" in marks[0].kwargs["reason"]


@pytest.mark.parametrize("fixture", ["store", "user_credit_transfer_store", "unrelated_store"])
@pytest.mark.parametrize("backend", ["memory", "postgres", "spanner-pg", "spanner-fake", "spanner-emulator"])
@pytest.mark.parametrize("module", ["test_store_semantics.py", "test_unrelated.py"])
def test_gap_registration_matches_only_its_module_fixture_and_backend(fixture, backend, module):
    from types import SimpleNamespace

    from tests.conformance.conftest import pytest_collection_modifyitems

    name = "test_finalize_unknown_authorization_is_false_not_error"
    marks = []
    item = SimpleNamespace(callspec=SimpleNamespace(params={fixture: backend}), originalname=name,
                           nodeid=f"tests/conformance/{module}::{name}[backend={backend}]",
                           add_marker=marks.append)
    pytest_collection_modifyitems([item])
    expected = (module == "test_store_semantics.py" and fixture == "store"
                and backend in {"spanner-fake", "spanner-emulator"})
    assert bool(marks) == expected
    if marks:
        assert len(marks) == 1 and marks[0].kwargs["strict"] is True


def test_all_gap_registrations_match_collected_items_for_every_backend(tmp_path):
    import subprocess
    import sys
    import textwrap

    from tests.conformance.conftest import _BACKEND_KNOWN_GAPS

    names = {test_id.split("::")[-1].split("[", 1)[0] for gaps in _BACKEND_KNOWN_GAPS.values()
             for _, test_id in gaps}
    unrelated = tmp_path / "test_unrelated.py"
    unrelated.write_text("import pytest\n" + "\n".join(
        '@pytest.mark.parametrize("store", ["spanner-fake", "spanner-emulator"])\n'
        '@pytest.mark.parametrize("user_credit_transfer_store", ["spanner-fake", "spanner-emulator"])\n'
        f"def {name}(store, user_credit_transfer_store): pass\n"
        for name in sorted(names)
    ))
    # A separate collection makes this guard work even when selected on its
    # own. No fixtures run, no server is contacted, and no pytest cache is made.
    probe = textwrap.dedent('''
        import sys
        import pytest
        from tests.conformance.conftest import _BACKEND_KNOWN_GAPS, gap_test_id, validate_gap_registrations

        class VerifyGaps:
            def pytest_collection_finish(self, session):
                validate_gap_registrations(session.items)
                for backend, registrations in _BACKEND_KNOWN_GAPS.items():
                    for (fixture, test_id), reason in registrations.items():
                        matches = [item for item in session.items
                                   if gap_test_id(item) == test_id
                                   and getattr(getattr(item, "callspec", None), "params", {}).get(fixture) == backend]
                        assert matches, f"Dead gap registration: {backend}/{fixture}/{test_id}"
                        for item in matches:
                            marks = list(item.iter_markers("xfail"))
                            assert len(marks) == 1, item.nodeid
                            assert marks[0].kwargs == {"strict": True, "reason": reason}, item.nodeid
                collisions = [item for item in session.items if item.path.name == "test_unrelated.py"]
                assert collisions
                for item in collisions:
                    assert not list(item.iter_markers("xfail")), item.nodeid

        raise SystemExit(pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider",
                                     "tests/conformance/test_store_semantics.py", sys.argv[1]], plugins=[VerifyGaps()]))
    ''')
    result = subprocess.run([sys.executable, "-c", probe, str(unrelated)], cwd=ROOT, capture_output=True, text=True, timeout=60)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("escape", ["renamed-helper", "eval", "printf-eval", "sourced-file"])
def test_sink_discovery_rejects_review_round_three_escapes(tmp_path, monkeypatch, escape):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_money_primitives.sh"
    # Review the removed declaration exemption so the escape still reaches extraction.
    monkeypatch.delitem(schema.DDL_EXEMPTIONS["lines"]["scripts/deploy/migrate_money_primitives.sh"], "apply_ddl() {")
    source = path.read_text().replace("apply_ddl", "run_schema")
    additions = {
        "renamed-helper": "run_schema 'alter table tr_entities add column review_lost STRING(64)'\n",
        "eval": '''eval "run_schema 'alter table tr_entities add column review_lost STRING(64)'"\n''',
        "printf-eval": '''SQL=$(printf '%s %s' 'alter table tr_entities' 'add column review_lost STRING(64)')
eval "run_schema '$SQL'"
''',
        "sourced-file": 'source "../../external-schema.sh"\n',
    }
    (tmp_path / "external-schema.sh").write_text(additions["renamed-helper"])
    path.write_text(source + "\n" + additions[escape])
    with pytest.raises(AssertionError, match=r"migrate_money_primitives.sh:\d+:"):
        migration_ddl(tmp_path)


def test_renamed_dispatcher_supported_call_is_extracted(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_money_primitives.sh"
    # The renamed declaration no longer needs its count-bound carrier exemption.
    monkeypatch.delitem(schema.DDL_EXEMPTIONS["lines"]["scripts/deploy/migrate_money_primitives.sh"], "apply_ddl() {")
    path.write_text(path.read_text().replace("apply_ddl", "run_schema")
                    + '\nrun_schema "ALTER TABLE tr_entities ADD COLUMN review_added STRING(64)"\n')
    assert "ALTER TABLE tr_entities ADD COLUMN review_added STRING(64)" in migration_ddl(tmp_path)


@pytest.mark.parametrize("addition", [
    '''bash -c 'gcloud spanner databases ddl update db --ddl="ALTER TABLE tr_entities ADD COLUMN lost INT64"'\n''',
    '''sh -c 'gcloud spanner databases ddl update db --ddl="ALTER TABLE tr_entities ADD COLUMN lost INT64"'\n''',
    '. "../../external-schema.sh"\n',
    'gcloud spanner databases ddl update db --ddl="$SQL" <<< "$SQL"\n',
    '<<< "unused" gcloud spanner databases ddl update db --ddl="CREATE INDEX review_added ON tr_entities (kind)"\n',
    'run_schema() { gcloud spanner databases ddl update db --ddl="${1,,}"; }\n',
    'run_schema() { local ddl="$2"; gcloud spanner databases ddl update db --ddl="$ddl"; }\n',
    'run_schema() { local ddl="$1"; ddl="$2"; gcloud spanner databases ddl update db --ddl="$ddl"; }\n',
], ids=["bash-c", "sh-c", "dot-source", "here-string", "leading-here-string", "transformed-parameter", "wrong-parameter", "reassigned-parameter"])
def test_unsupported_shell_dispatch_forms_fail_closed(tmp_path, addition):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    (scripts / "migrate_review_hole.sh").write_text(addition)
    with pytest.raises(AssertionError, match=r"migrate_review_hole.sh:\d+:"):
        migration_ddl(tmp_path)


def test_allowlist_preserves_shell_variable_case(tmp_path):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "migrate_gateway_request_index.sh"
    path.write_text(path.read_text().replace('ddl "DROP INDEX $OLD"',
                                            'old=tr_receipt_key_versions; ddl "DROP INDEX $old"'))
    with pytest.raises(AssertionError, match=r"migrate_gateway_request_index.sh:\d+: unconsumed DDL dispatch"):
        migration_ddl(tmp_path)


def test_unrelated_library_edit_needs_no_schema_regeneration(tmp_path):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "_lib.sh"
    path.write_text(path.read_text() + '\n# Unrelated deploy helper.\nreview_status() { gc run services list; }\n')
    assert migration_ddl(tmp_path) == spanner_ddl.DDL
    assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, tmp_path)


@pytest.mark.parametrize("addition", [
    'gc spanner databases ddl update --ddl="ALTER TABLE tr_entities ADD COLUMN x STRING(1)"',
    'gcloud spanner databases ddl update --ddl="ALTER TABLE tr_entities ADD COLUMN x STRING(1)"',
    'gc spanner databases create db',
    'gcloud --project "$PROJECT_ID" spanner databases create db',
    'run_schema() { gc spanner databases ddl update db --ddl="$1"; }',
    'function run_schema { gcloud spanner databases ddl update db --ddl="$1"; }',
    'run_schema() { gc spanner databases create db; }',
    '''eval 'gc spanner databases ddl update db --ddl="ALTER TABLE tr_entities ADD COLUMN x STRING(1)"' ''',
    '''bash -c 'gc spanner databases create db' ''',
    '''sh -c 'gcloud spanner databases create db' ''',
    'source "${SCRIPT_DIR}/nested.sh"',
    '. "${SCRIPT_DIR}/nested.sh"',
    'result="$(gc spanner databases create db)"',
    'gc spanner databases \\\n  ddl update db --ddl="$SQL"',
], ids=["gc-update", "gcloud-update", "gc-create", "gcloud-create", "gc-dispatcher",
        "gcloud-dispatcher", "create-dispatcher", "eval", "bash-c", "sh-c", "source",
        "dot-source", "substitution", "continuation"])
def test_reviewed_library_rejects_ddl_and_indirection(tmp_path, addition):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    (scripts / "nested.sh").write_text('gc spanner databases create db\n')
    path = scripts / "_lib.sh"
    original = path.read_text()
    path.write_text(original + "\n" + addition + "\n")
    line = len(original.splitlines()) + 2
    # Raw carriers report their physical line, including after a continuation.
    if addition.startswith("gc spanner databases \\\n"):
        line += 1
    with pytest.raises(AssertionError, match=rf"_lib\.sh:{line}:"):
        migration_ddl(tmp_path)


@pytest.mark.parametrize("replacement", [
    'gc() { gcloud --project "$PROJECT_ID"; }',
    'gc() { gcloud --project "$PROJECT_ID" "$@"; }\ngc() { gcloud "$@"; }',
    'gc() { gcloud --project "$PROJECT_ID" "$@"; }\nfunction gc { gcloud "$@"; }',
    '# gc() { gcloud --project "$PROJECT_ID" "$@"; }',
], ids=["missing-forwarding", "duplicate", "duplicate-keyword", "missing-definition"])
def test_reviewed_library_requires_unique_gc_wrapper(tmp_path, replacement):
    from tests.conformance.spanner_schema_source import REVIEWED_GC_WRAPPER

    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "_lib.sh"
    path.write_text(path.read_text().replace(REVIEWED_GC_WRAPPER, replacement))
    with pytest.raises(AssertionError, match=r"_lib\.sh:\d+: .*gc"):
        migration_ddl(tmp_path)


def test_reviewed_library_wrapper_allows_normalized_whitespace(tmp_path):
    from tests.conformance.spanner_schema_source import REVIEWED_GC_WRAPPER

    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    path = scripts / "_lib.sh"
    path.write_text(path.read_text().replace(REVIEWED_GC_WRAPPER,
                                            'gc() {\n\tgcloud  --project "$PROJECT_ID" "$@";\n}\n'))
    assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, tmp_path)


@pytest.mark.parametrize("backend", ["spanner-fake", "spanner-emulator"])
def test_new_gap_parameter_variant_fails_collection(backend):
    from types import SimpleNamespace

    from tests.conformance.conftest import pytest_collection_modifyitems

    name = "test_finalize_unknown_authorization_is_false_not_error"
    marks = []
    item = SimpleNamespace(callspec=SimpleNamespace(params={"store": backend, "mode": "new-supported-mode"}),
                           nodeid=f"tests/conformance/test_store_semantics.py::{name}[backend={backend}-mode=new-supported-mode]",
                           originalname=name, add_marker=marks.append)
    with pytest.raises(AssertionError, match="Unregistered gap variant"):
        pytest_collection_modifyitems([item])
    assert not marks


def test_dead_gap_registration_fails_collection_check():
    from tests.conformance.conftest import validate_gap_registrations

    with pytest.raises(AssertionError, match="Dead gap registration"):
        validate_gap_registrations([])


@pytest.mark.parametrize("body", [
    "function run_schema { gcloud spanner databases ddl update db --ddl=\"$1\"; }\n"
    "run_schema 'alter table tr_entities add column lost INT64'\n",
    "function run_schema() { gcloud spanner databases ddl update db --ddl=\"$1\"; }\n"
    "run_schema 'alter table tr_entities add column lost INT64'\n",
    "run_schema() ( gcloud spanner databases ddl update db --ddl=\"$1\"; )\n",
    '''result="$(eval "run_schema '$SQL'")"\n''',
    '''e""val "run_schema '$SQL'"\n''',
    '''result="$(gcloud spanner databases ddl update db --ddl='alter table tr_entities add column lost INT64')"\n''',
], ids=["function-keyword", "function-parentheses", "unsupported-function-body", "nested-eval", "quoted-eval", "nested-sink"])
def test_sink_discovery_handles_shell_structure(tmp_path, body):
    scripts = tmp_path / "scripts/deploy"
    copy_schema_repository(tmp_path)
    (scripts / "migrate_review_hole.sh").write_text(body)
    with pytest.raises(AssertionError, match=r"migrate_review_hole.sh:\d+:"):
        migration_ddl(tmp_path)


def test_explicitly_registered_extra_gap_variant_is_marked(monkeypatch):
    from types import SimpleNamespace

    from tests.conformance import conftest

    backend = "spanner-emulator"
    name = "test_finalize_unknown_authorization_is_false_not_error"
    test_id = f"test_store_semantics.py::{name}[backend={backend}-mode=reviewed-mode]"
    registrations = {key: dict(value) for key, value in conftest._BACKEND_KNOWN_GAPS.items()}
    registrations[backend][("store", test_id)] = "explicitly reviewed variant"
    monkeypatch.setattr(conftest, "_BACKEND_KNOWN_GAPS", registrations)
    marks = []
    item = SimpleNamespace(callspec=SimpleNamespace(params={"store": backend, "mode": "reviewed-mode"}),
                           nodeid="tests/conformance/" + test_id, originalname=name, add_marker=marks.append)
    conftest.pytest_collection_modifyitems([item])
    assert len(marks) == 1 and marks[0].kwargs == {"strict": True, "reason": "explicitly reviewed variant"}


@pytest.mark.parametrize("name", ["checkout", "checkout ending in a space "])
def test_a_git_checkout_scans_tracked_files_and_a_plain_directory_scans_all(tmp_path_factory, name):
    # google-github-actions/auth writes gha-creds-*.json into the workspace; the
    # hourly price refresh's validation run failed on it as a "DDL carrier".
    carrier = '{"note": "update_ddl"}\n'
    checkout = tmp_path_factory.mktemp("repo") / name
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # noqa: S603, S607 - fixed git setup
    tracked = ["migrate.sh", "gha-creds-tracked.json", "build/generated.sh", "tests/fixture.sh", "nested/tests/tool.sh"]
    for relative in [*tracked, "gha-creds-untracked.json", "anything.txt"]:
        (checkout / relative).parent.mkdir(parents=True, exist_ok=True)
        (checkout / relative).write_text(carrier)
    subprocess.run(["git", "-C", str(checkout), "add", *tracked], check=True)  # noqa: S603, S607 - fixed git setup

    # Tracked files are scanned whatever their name; excluded directories
    # (at any depth) and the root tests/ stay out; untracked files never count.
    assert schema.repository_files(checkout) == sorted(
        checkout / relative for relative in ("gha-creds-tracked.json", "migrate.sh", "nested/tests/tool.sh")
    )

    plain = tmp_path_factory.mktemp("plain")
    (plain / "migrate.sh").write_text(carrier)
    (plain / "gha-creds-untracked.json").write_text(carrier)

    assert schema.repository_files(plain) == [plain / "gha-creds-untracked.json", plain / "migrate.sh"]


@pytest.mark.parametrize("active", [True, False], ids=["active", "inactive"])
def test_a_git_checkout_with_a_submodule_is_refused(tmp_path_factory, active):
    git = ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "protocol.file.allow=always"]
    sub = tmp_path_factory.mktemp("sub")
    subprocess.run([*git, "init", "-q", str(sub)], check=True)  # noqa: S603 - fixed git setup
    (sub / "migrate.sh").write_text('{"note": "update_ddl"}\n')
    subprocess.run([*git, "-C", str(sub), "add", "migrate.sh"], check=True)  # noqa: S603 - fixed git setup
    subprocess.run([*git, "-C", str(sub), "commit", "-q", "-m", "sub"], check=True)  # noqa: S603 - fixed git setup
    checkout = tmp_path_factory.mktemp("super")
    subprocess.run([*git, "init", "-q", str(checkout)], check=True)  # noqa: S603 - fixed git setup
    subprocess.run(  # noqa: S603 - fixed git setup
        [*git, "-C", str(checkout), "submodule", "add", "-q", str(sub), "vendored"], check=True
    )

    if not active:
        subprocess.run(  # noqa: S603 - fixed git setup
            [*git, "-C", str(checkout), "config", "submodule.vendored.active", "false"], check=True
        )

    # Git lists a submodule's files only when it is active; refuse either way.
    with pytest.raises(AssertionError, match="submodules are outside the schema scan"):
        schema.repository_files(checkout)
