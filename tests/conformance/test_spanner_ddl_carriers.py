"""Repo-wide literal DDL guard: copies exercise the surface, not transports."""
from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter

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
                                              (REST, "ddl")], ids=["python", "rest"])
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
                                      ".github/workflows/review.yml", ".github/workflows/review.unfamiliar",
                                      "infra/review.tf", "Dockerfile.review",
                                      "cloudbuild-review.yaml", "new_package/review.go"])
def test_lowercase_statement_in_new_file_fails(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\nalter table tr_entities add column review_lost STRING(64)\n")
    with pytest.raises(AssertionError, match=rf"{relative}:2: unconsumed DDL carrier: alter table"):
        schema.migration_ddl(repo)


@pytest.mark.parametrize("carrier", [
    "--ddl", "ddl-file", "ddl update", "databases create", "update_ddl", "UpdateDatabaseDdl", "updateDdl",
    "extra_statements", "extraStatements", "ddl_statements", "databases/db/ddl",
    "update_database_ddl", "prefixDdlSuffix", "ddl =", "CREATE OR REPLACE VIEW",
    "CREATE IF NOT EXISTS TABLE", "CREATE VECTOR INDEX", "ALTER SCHEMA", "DROP MODEL",
    "CREATE PROPERTY GRAPH", "DROP ROLE", "ALTER PROTO BUNDLE", "CREATE LOCALITY GROUP", "DROP PLACEMENT",
    "CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX", "CREATE NULL_FILTERED INDEX",
    "CREATE UNIQUE NULL_FILTERED INDEX", "CREATE SEARCH INDEX", "CREATE CHANGE STREAM", "CREATE VIEW",
    "CREATE SEQUENCE", "ALTER TABLE", "ALTER INDEX", "ALTER DATABASE", "DROP TABLE", "DROP INDEX",
    "DROP VIEW", "DROP SEQUENCE", "ROW DELETION POLICY",
])
def test_all_literal_carriers_include_quotes_and_ignore_case(tmp_path, carrier):
    path = tmp_path / "scripts/review.txt"
    path.parent.mkdir()
    path.write_text(f'"{carrier}"\n')
    with pytest.raises(AssertionError, match=r"review.txt:1: .*DDL carrier:"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


@pytest.mark.parametrize("relative,text", [
    ("scripts/review.py", '# update_ddl\nx = "hello"  # ALTER TABLE\n'),
    ("scripts/review.sh", '# update_ddl\necho hello # ALTER TABLE\n'),
    ("scripts/review.sql", '-- update_ddl\nSELECT 1; /* ALTER TABLE */\n'),
    ("scripts/review.mjs", '// update_ddl\nconst x = 1; /* ALTER TABLE */\n'),
    (".github/workflows/review.yml", '# update_ddl\nname: hello # ALTER TABLE\n'),
])
def test_comments_and_quoted_comment_markers_are_scanned(tmp_path, relative, text):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(AssertionError, match="DDL carrier: update_ddl"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)
    path.write_text(text + '"# update_ddl"\n')
    with pytest.raises(AssertionError, match="DDL carrier: update_ddl"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


def test_heredoc_explanation_is_scanned_without_parsing_prose_as_shell(tmp_path):
    path = tmp_path / "scripts/review.sh"
    path.parent.mkdir()
    path.write_text("cat <<'HELP'\nThe database's ddl update operation.\nHELP\n")
    with pytest.raises(AssertionError, match=r"review.sh:2: unconsumed DDL carrier: ddl"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


def test_exemption_is_exact_normalized_line_and_path(tmp_path, monkeypatch):
    path = tmp_path / "scripts/review.sh"
    path.parent.mkdir()
    line = 'log "starting ddl update"'
    monkeypatch.setattr(schema, "DDL_EXEMPTIONS", {
        "files": {}, "lines": {"scripts/review.sh": {line: {"count": 1, "reason": "Explanatory log only."}}},
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
    for relative, entry in registry["files"].items():
        assert hashlib.sha256((schema.ROOT / relative).read_bytes()).hexdigest() == entry["sha256"]
        assert entry["reason"].strip() and "\n" not in entry["reason"]
    for relative, entries in registry["lines"].items():
        path = schema.ROOT / relative
        lines = Counter(schema.normalized_statement(line)
                        for line in path.read_bytes().decode("utf-8", errors="replace").split("\n"))
        for line, entry in entries.items():
            assert line == schema.normalized_statement(line)
            assert lines[line] == entry["count"] > 0, (relative, line)
            assert entry["reason"].strip() and "\n" not in entry["reason"]
    for directory, entry in registry["directories"].items():
        assert (schema.ROOT / directory).is_dir()
        assert entry["reason"].strip()
    for pattern, entry in registry["statement_files"].items():
        assert list(schema.ROOT.glob(pattern))
        assert entry["reason"].strip()


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


ADMIN_CALL = '''from google.cloud import spanner_admin_database_v1
client = spanner_admin_database_v1.DatabaseAdminClient()
client.update_database_ddl(request={
    "database": "projects/p/instances/i/databases/d",
    "statements": [
        "CREATE OR REPLACE VIEW review_view SQL SECURITY INVOKER "
        "AS SELECT kind, id FROM tr_entities"
    ],
}).result(timeout=120)
'''


@pytest.mark.parametrize("relative", ["scripts/deploy/migrate_money_primitives.sh",
                                      "scripts/deploy/_lib.sh", "scripts/review.py"])
@pytest.mark.parametrize("payload,carrier", [
    (ADMIN_CALL, "update_database_ddl"),
    ('client.update_database_ddl(request=request)\n', "update_database_ddl"),
    ('statement = "CREATE OR REPLACE VIEW review_view AS SELECT kind FROM tr_entities"\n',
     "CREATE OR REPLACE VIEW"),
], ids=["reviewer-admin-call", "transport-only", "statement-only"])
def test_review_six_admin_carriers_in_copies(repo, relative, payload, carrier):
    path = repo / relative
    prefix = path.read_text() + "\n" if path.exists() else ""
    if path.suffix == ".sh":
        prefix += "python - <<'PYTHON'\n"
        payload += "PYTHON\n"
    path.write_text(prefix + payload)
    line = prefix.count("\n") + payload[:payload.index(carrier)].count("\n") + 1
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, schema.source_digests(repo), repo)
    assert f"{path}:{line}: unconsumed DDL carrier: {carrier}" in str(error.value)
    assert "expected occurrence count" in str(error.value)
    assert "SHA-256" in str(error.value)


def test_review_six_javascript_regex_then_exec_in_copy(repo):
    path = repo / "scripts/review.mjs"
    path.write_text(r'''const urlPattern = /https?:\/\//; execFileSync('gcloud', ['spanner', 'databases', 'ddl', 'update', 'db', '--ddl=ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)']);
''')
    with pytest.raises(AssertionError, match=r"scripts/review.mjs:1: unconsumed DDL carrier: ddl"):
        schema.migration_ddl(repo)


def test_here_string_is_never_a_heredoc_in_copy(repo):
    path = repo / "scripts/review.sh"
    path.write_text("cat <<<hello\n")
    source, lines = schema.shell_source(path)
    assert source == "cat <<<hello\n"
    assert schema.ddl_dispatch_arguments(source, path, lines, repo) == ([], [])
    assert schema.migration_ddl(repo) == spanner_ddl.DDL
    migration = repo / "scripts/deploy/migrate_money_primitives.sh"
    migration.write_text(migration.read_text() + "\ncat <<<hello\n")
    assert schema.migration_ddl(repo) == spanner_ddl.DDL


def test_duplicate_exempt_line_fails_in_copy(repo):
    path = repo / "scripts/deploy/migrate_entity_ttl.sh"
    path.write_text(path.read_text() + '\nlog "  ALTER TABLE tr_entities DROP COLUMN ephemeral_expires_at;"\n')
    with pytest.raises(AssertionError, match=r"occurrence count changed: expected 1, found 2"):
        schema.migration_ddl(repo)


def test_removed_exempt_line_fails_in_copy(repo):
    path = repo / "scripts/deploy/migrate_entity_ttl.sh"
    path.write_text("\n".join(line for line in path.read_text().splitlines()
                              if 'log "  ALTER TABLE tr_entities DROP COLUMN' not in line))
    with pytest.raises(AssertionError, match=r"occurrence count changed: expected 1, found 0"):
        schema.migration_ddl(repo)


def test_manual_sql_check_body_is_digest_bound_in_copy(repo):
    relative = "scripts/lightning/spanner_provenance.sql"
    path = repo / relative
    path.parent.mkdir(parents=True)
    shutil.copyfile(schema.ROOT / relative, path)
    assert schema.migration_ddl(repo) == spanner_ddl.DDL
    path.write_text(path.read_text().replace("'lightning','operator'", "'unreviewed','operator'"))
    with pytest.raises(AssertionError, match=r"spanner_provenance.sql:6: DDL carrier: ALTER TABLE;.*re-review"):
        schema.migration_ddl(repo)


@pytest.mark.parametrize("change", ["\n-- harmless edit\n", "SELECT 1;\n"])
def test_file_digest_checks_even_without_remaining_carriers(tmp_path, monkeypatch, change):
    path = tmp_path / "manual.sql"
    raw = b"ALTER TABLE example ADD COLUMN name STRING(MAX);\n"
    path.write_bytes(raw)
    monkeypatch.setattr(schema, "DDL_EXEMPTIONS", {
        "files": {"manual.sql": {"sha256": hashlib.sha256(raw).hexdigest(), "reason": "Manual upgrade."}},
        "lines": {},
    })
    schema.assert_ddl_carriers_consumed(path, [], tmp_path)
    path.write_text(change)
    with pytest.raises(AssertionError, match="file exemption SHA-256 changed; re-review"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


def test_directory_exemption_only_covers_its_own_statements(tmp_path, monkeypatch):
    directory = tmp_path / "clickhouse/schema"
    directory.mkdir(parents=True)
    path = directory / "review.sql"
    monkeypatch.setattr(schema, "DDL_EXEMPTIONS", {
        "files": {}, "lines": {}, "directories": {
            "clickhouse/schema": {"database": "ClickHouse", "reason": "ClickHouse's own SQL schema directory."},
        },
    })
    path.write_text("CREATE TABLE analytics (id UInt64) ENGINE = MergeTree ORDER BY id;\n")
    schema.assert_ddl_carriers_consumed(path, [], tmp_path)
    other = tmp_path / "clickhouse/review.sql"
    other.write_bytes(path.read_bytes())
    with pytest.raises(AssertionError, match="DDL carrier: CREATE TABLE"):
        schema.assert_ddl_carriers_consumed(other, [], tmp_path)
    path.write_text(path.read_text() + "# update_database_ddl\n")
    with pytest.raises(AssertionError, match="DDL carrier: update_database_ddl"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)
    schema.DDL_EXEMPTIONS["lines"]["clickhouse/schema/review.sql"] = {
        "# update_database_ddl": {"count": 1, "reason": "Schema comment, not a Spanner transport."},
    }
    schema.assert_ddl_carriers_consumed(path, [], tmp_path)


@pytest.mark.parametrize("relative", ["tests/review.py", "docs/review.md", *[
    f"new_package/{name}/review.py" for name in sorted(schema.EXCLUDED_DIRECTORIES)
]])
def test_only_tests_docs_dependencies_and_build_directories_are_pruned(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("update_database_ddl\n")
    assert schema.carrier_sources(tmp_path) == []


def test_raw_scan_never_invokes_shell_parser_and_decodes_leniently(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Carrier scan must never lex source")

    monkeypatch.setattr(schema, "shell_tokens", forbidden)
    path = tmp_path / "review.sh"
    path.write_bytes(b"\xff\" unterminated quote\n# update_database_ddl\n")
    with pytest.raises(AssertionError, match=r"review.sh:2: unconsumed DDL carrier: update_database_ddl"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


def test_statement_scan_is_same_line_and_word_bounded(tmp_path):
    path = tmp_path / "review.txt"
    path.write_text("RECREATE TABLE example\nCREATE TABLET example\nCREATE\nTABLE example\n")
    schema.assert_ddl_carriers_consumed(path, [], tmp_path)


@pytest.mark.parametrize("identifier", ["middleware", "middle_tier", "paddle", "middle", "paddleocr"])
def test_middleware_is_not_a_transport_in_copy(repo, identifier):
    path = repo / "scripts/new_import.py"
    path.write_text(f"from trusted_router import {identifier}\n")
    schema.assert_ddl_carriers_consumed(path, [], repo)
    assert schema.migration_ddl(repo) == spanner_ddl.DDL


@pytest.mark.parametrize("carrier", [
    "update_database_ddl", "UpdateDatabaseDdl", "updateDdl", "updateDDL", "--ddl-file", "/ddl",
    "DDL", "--DDL", "extra_statements", "extraStatements", "ExtraStatements", "extra-statements",
    "databases create", "databases', 'create", "DATABASES CREATE",
])
def test_transport_identifier_parts(tmp_path, carrier):
    path = tmp_path / "carrier.txt"
    path.write_text(carrier)
    with pytest.raises(AssertionError, match="DDL carrier:"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


@pytest.mark.parametrize("relative", [
    "clickhouse/013_example.sql", "clickhouse/new_query.py", "experiments/new/schema.sql",
    "src/trusted_router/storage_postgres.py", "src/trusted_router/storage_postgres_schema.sql",
    "scripts/lightning/postgres_provenance.sql", ".codex-review-new.md", ".test_durations",
    "src/trusted_router/static/openapi-public.json",
])
def test_statement_exemptions_allow_new_or_changed_content_in_copy(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = schema.ROOT / relative
    prefix = original.read_text() + "\n" if original.exists() else ""
    path.write_text(prefix + "CREATE TABLE analytics (id UInt64) ENGINE = MergeTree ORDER BY id;\n")
    assert schema.migration_ddl(repo) == spanner_ddl.DDL


@pytest.mark.parametrize("relative", [
    "clickhouse/013_example.sql", "experiments/new/helper.py", ".codex-review-new.md",
    "src/trusted_router/static/openapi-public.json",
    "src/trusted_router/storage_postgres_schema.sql", "scripts/lightning/spanner_provenance.sql",
])
@pytest.mark.parametrize("transport", [
    'client.update_database_ddl(statements=open("x.sql").read())',
    '$GCLOUD --ddl-file=x.sql',
    'gcloud spanner databases ddl update db --ddl="$(cat x.sql)"',
    'execFileSync("gcloud", ["--ddl=CREATE OR REPLACE VIEW v AS SELECT 1"])',
])
def test_transport_in_exempt_file_or_directory_fails(repo, monkeypatch, relative, transport):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = schema.ROOT / relative
    prefix = original.read_text() + "\n" if original.exists() else ""
    path.write_text(prefix + transport + "\n")
    if relative in schema.DDL_EXEMPTIONS["files"]:
        # Even a reviewed native SQL digest cannot exempt a transport.
        monkeypatch.setitem(schema.DDL_EXEMPTIONS["files"], relative, {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "reason": "Manual native SQL.",
        })
    with pytest.raises(AssertionError, match="(?:unconsumed|unsupported) DDL carrier:"):
        schema.migration_ddl(repo)


def test_duplicate_exempt_transport_line_fails_in_copy(repo):
    relative = "scripts/deploy/migrate_money_primitives.sh"
    path = repo / relative
    line = next(line for line in schema.DDL_EXEMPTIONS["lines"][relative]
                if list(schema.ddl_transport_matches(line)))
    path.write_text(path.read_text() + "\n" + line + "\n")
    with pytest.raises(AssertionError, match="line exemption occurrence count changed"):
        schema.migration_ddl(repo)


def test_multiline_transport_needs_every_line_exempted(tmp_path, monkeypatch):
    path = tmp_path / "review.txt"
    path.write_text("databases\ncreate\n")
    monkeypatch.setattr(schema, "DDL_EXEMPTIONS", {
        "files": {}, "lines": {"review.txt": {
            "databases": {"count": 1, "reason": "Reviewed first line only."},
        }},
    })
    with pytest.raises(AssertionError, match="unconsumed DDL carrier: databases"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)
