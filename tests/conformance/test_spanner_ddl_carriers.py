"""Repo-wide literal DDL guard: copies exercise the surface, not transports."""
from __future__ import annotations

import json
import shutil

import pytest

from tests.conformance import spanner_ddl
from tests.conformance import spanner_schema_source as schema


@pytest.fixture
def repo(tmp_path):
    shutil.copytree(schema.ROOT / "scripts/deploy", tmp_path / "scripts/deploy",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return tmp_path


PYTHON_HEREDOC = '''uv run --frozen python - <<'PYTHON'
import os
from google.cloud import spanner
client = spanner.Client(project=os.environ['GCP_PROJECT_ID'])
db = client.instance(os.environ['SPANNER_INSTANCE_ID']).database(
    os.environ['SPANNER_DATABASE_ID'])
db.update_ddl([
    'ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)'
]).result(timeout=120)
PYTHON
'''
REST = '''curl --request PATCH "https://spanner.googleapis.com/v1/projects/$PROJECT/instances/$INSTANCE/databases/$DATABASE/ddl" \\
  --data '{"statements": ["alter table tr_entities add column review_lost STRING(64)"]}'
'''


@pytest.mark.parametrize("name", ["migrate_money_primitives.sh", "_lib.sh"])
@pytest.mark.parametrize("addition,carrier", [(PYTHON_HEREDOC, "update_ddl"),
                                              (REST, "databases/$DATABASE/ddl")], ids=["python", "rest"])
def test_review_round_five_transports_fail_in_copies(repo, name, addition, carrier):
    path = repo / "scripts/deploy" / name
    prefix = path.read_text() + "\n"
    path.write_text(prefix + addition)
    line = prefix.count("\n") + addition[:addition.index(carrier)].count("\n") + 1
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, schema.source_digests(repo), repo)
    message = str(error.value)
    assert f"{path}:{line}: unconsumed DDL carrier: {carrier}" in message
    assert "make the schema extractor consume it (a real schema change)" in message
    assert f"add a reviewed exemption entry in {schema.EXEMPTION_REGISTRY}" in message


@pytest.mark.parametrize("relative", ["scripts/migrate_review.py", "src/trusted_router/migrate_review.py"])
def test_update_ddl_helper_fails_in_copies(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# A newly introduced admin helper.\ndef migrate(database, statements):\n    database.update_ddl(statements)\n")
    with pytest.raises(AssertionError, match=rf"{relative}:3: unconsumed DDL carrier: update_ddl"):
        schema.migration_ddl(repo)


@pytest.mark.parametrize("relative", ["scripts/review", "scripts/review.sql", "scripts/review.unfamiliar",
                                      "scripts/__pycache__/review.py", "scripts/__pycache__/review.pyc",
                                      ".github/workflows/review.yml", ".github/workflows/review.unfamiliar"])
def test_lowercase_statement_in_new_file_fails(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\nalter table tr_entities add column review_lost STRING(64)\n")
    with pytest.raises(AssertionError, match=rf"{relative}:2: unconsumed DDL carrier: alter table"):
        schema.migration_ddl(repo)


@pytest.mark.parametrize("carrier", [
    "--ddl", "ddl-file", "ddl update", "databases create", "update_ddl", "UpdateDatabaseDdl", "updateDdl",
    "extra_statements", "extraStatements", "ddl_statements", "databases/db/ddl",
    "CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX", "CREATE NULL_FILTERED INDEX",
    "CREATE UNIQUE NULL_FILTERED INDEX", "CREATE SEARCH INDEX", "CREATE CHANGE STREAM", "CREATE VIEW",
    "CREATE SEQUENCE", "ALTER TABLE", "ALTER INDEX", "ALTER DATABASE", "DROP TABLE", "DROP INDEX",
    "DROP VIEW", "DROP SEQUENCE", "ROW DELETION POLICY",
])
def test_all_literal_carriers_include_quotes_and_ignore_case(tmp_path, carrier):
    path = tmp_path / "scripts/review.txt"
    path.parent.mkdir()
    path.write_text(f'"{carrier.swapcase()}"\n')
    with pytest.raises(AssertionError, match=r"review.txt:1: .*DDL carrier:"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


@pytest.mark.parametrize("relative,text", [
    ("scripts/review.py", '# update_ddl\nx = "hello"  # ALTER TABLE\n'),
    ("scripts/review.sh", '# update_ddl\necho hello # ALTER TABLE\n'),
    ("scripts/review.sql", '-- update_ddl\nSELECT 1; /* ALTER TABLE */\n'),
    ("scripts/review.mjs", '// update_ddl\nconst x = 1; /* ALTER TABLE */\n'),
    (".github/workflows/review.yml", '# update_ddl\nname: hello # ALTER TABLE\n'),
])
def test_comments_are_excluded_but_quoted_comment_markers_are_scanned(tmp_path, relative, text):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(text)
    schema.assert_ddl_carriers_consumed(path, [], tmp_path)
    path.write_text(text + '"# update_ddl"\n')
    with pytest.raises(AssertionError, match="DDL carrier: update_ddl"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


def test_heredoc_explanation_is_scanned_without_parsing_prose_as_shell(tmp_path):
    path = tmp_path / "scripts/review.sh"
    path.parent.mkdir()
    path.write_text("cat <<'HELP'\nThe database's ddl update operation.\nHELP\n")
    with pytest.raises(AssertionError, match=r"review.sh:2: unconsumed DDL carrier: ddl update"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


def test_exemption_is_exact_normalized_line_and_path(tmp_path, monkeypatch):
    path = tmp_path / "scripts/review.sh"
    path.parent.mkdir()
    line = 'log "starting ddl update"'
    monkeypatch.setattr(schema, "DDL_EXEMPTIONS", {
        "files": {}, "lines": {"scripts/review.sh": {line: "Explanatory log only."}},
    })
    path.write_text('  log   "starting ddl update"\n')
    schema.assert_ddl_carriers_consumed(path, [], tmp_path)
    for change in [line + '; database.update_ddl(statements)', line.upper()]:
        path.write_text(change + "\n")
        with pytest.raises(AssertionError, match="reviewed exemption entry"):
            schema.assert_ddl_carriers_consumed(path, [], tmp_path)
    other = path.with_name("other.sh")
    other.write_text(line)
    with pytest.raises(AssertionError, match="other.sh:1:"):
        schema.assert_ddl_carriers_consumed(other, [], tmp_path)


def test_registry_entries_are_present_normalized_and_reasoned():
    registry = json.loads((schema.ROOT / schema.EXEMPTION_REGISTRY).read_text())
    for relative, reason in registry["files"].items():
        assert (schema.ROOT / relative).is_file()
        assert reason.strip() and "\n" not in reason
    for relative, entries in registry["lines"].items():
        path = schema.ROOT / relative
        lines = {schema.normalized_statement(line) for line in path.read_text().splitlines()}
        for line, reason in entries.items():
            assert line == schema.normalized_statement(line)
            assert line in lines, (relative, line)
            assert reason.strip() and "\n" not in reason


def test_retirement_script_is_extracted_and_changes_fresh_schema(repo):
    path = repo / "scripts/deploy/retire_settle_outbox_hot_index.sh"
    assert path in schema.schema_sources(repo)
    path.write_text(path.read_text().replace('--ddl="DROP INDEX tr_settle_outbox_due"',
                                            '--ddl="DROP INDEX tr_settle_outbox_due_v2"'))
    ddl = schema.migration_ddl(repo)
    assert ddl == tuple(statement for statement in spanner_ddl.DDL
                        if not statement.startswith("CREATE NULL_FILTERED INDEX tr_settle_outbox_due_v2 "))


def test_unconsumed_literal_assignment_is_not_an_extracted_dispatch(repo):
    path = repo / "scripts/deploy/migrate_money_primitives.sh"
    path.write_text(path.read_text() + '\nLOST="CREATE TABLE uncalled (id INT64) PRIMARY KEY(id)"\n')
    with pytest.raises(AssertionError, match=r"migrate_money_primitives.sh:\d+: unconsumed DDL carrier: CREATE TABLE"):
        schema.migration_ddl(repo)
