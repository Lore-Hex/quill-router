"""Negative controls run against copies, without changing production adapters."""
from __future__ import annotations

import runpy
import shutil
from pathlib import Path

import pytest

from tests.conformance import spanner_ddl
from tests.conformance.spanner_emulator import require_emulators
from tests.conformance.spanner_schema_source import ROOT, assert_schema_matches, migration_ddl
from tests.conformance.spanner_sql_inventory import SRC, assert_complete


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
    shutil.copytree(ROOT / "scripts/deploy", scripts)
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


def test_provisioning_submits_all_ddl_and_cleans_up(monkeypatch):
    from unittest.mock import Mock

    from google.cloud import bigtable, spanner

    from tests.conformance import spanner_emulator

    monkeypatch.setattr(spanner_emulator, "require_emulators", lambda: None)
    spanner_client, bigtable_client = Mock(), Mock()
    from datetime import timedelta

    from google.cloud.spanner_v1.database_sessions_manager import DatabaseSessionsManager

    # Establish the SDK default, then assert ordering inside the constructor.
    monkeypatch.setattr(DatabaseSessionsManager, "_MAINTENANCE_THREAD_POLLING_INTERVAL", timedelta(minutes=10))

    def create_client(**kwargs):
        assert 0 < DatabaseSessionsManager._MAINTENANCE_THREAD_POLLING_INTERVAL.total_seconds() < 1
        return spanner_client

    monkeypatch.setattr(spanner, "Client", Mock(side_effect=create_client))
    monkeypatch.setattr(bigtable, "Client", Mock(return_value=bigtable_client))
    instance = spanner_client.instance.return_value
    database = instance.database.return_value
    table = bigtable_client.instance.return_value.table.return_value
    with spanner_emulator.emulator_resources() as resources:
        assert resources[0] is database
        submitted = list(instance.database.call_args.kwargs["ddl_statements"])
        for call in database.update_ddl.call_args_list:
            assert 0 < len(call.args[0]) <= 20
            submitted.extend(call.args[0])
        assert tuple(submitted) == spanner_ddl.DDL
        assert set(table.create.call_args.kwargs["column_families"]) == {"m", "activity", "benchmark", "synthetic", "rollup"}
        instance.delete.assert_not_called()
    database.close.assert_called_once()
    table.delete.assert_called_once()
    instance.delete.assert_called_once()


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
    shutil.copytree(ROOT / "scripts/deploy", scripts)
    path = scripts / "migrate_review_hole.sh"
    path.write_text(addition)
    with pytest.raises(AssertionError, match=r"migrate_review_hole.sh:\d+:"):
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

    legacy = {name for name, reason in _BACKEND_KNOWN_GAPS["spanner-fake"].items()
              if reason == _C1_LEGACY_MONEY}
    assert len(legacy) == 10
    assert _BACKEND_KNOWN_GAPS["spanner-emulator"] == _NATIVE_STORE_KNOWN_GAPS
    for name in legacy | _FAKE_ONLY_GAPS.keys():
        marks = []
        item = SimpleNamespace(callspec=SimpleNamespace(params={"store": "spanner-emulator"}),
                               originalname=name, add_marker=marks.append)
        pytest_collection_modifyitems([item])
        if name in legacy:
            assert len(marks) == 1 and marks[0].kwargs == {"strict": True, "reason": _C1_LEGACY_MONEY}
        else:
            assert not marks
