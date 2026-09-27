"""Repository transport and fixed migration statement guards, exercised in copies."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter

import pytest

from tests.conformance import spanner_ddl
from tests.conformance import spanner_schema_source as schema


@pytest.fixture(scope="module")
def migration_files():
    return schema.migration_sources(schema.ROOT)


@pytest.fixture
def repo(tmp_path, migration_files):
    shutil.copytree(schema.ROOT / "scripts/deploy", tmp_path / "scripts/deploy",
                    ignore=shutil.ignore_patterns("__pycache__"))
    for original in migration_files:
        target = tmp_path / original.relative_to(schema.ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    (tmp_path / "src/trusted_router").mkdir(parents=True, exist_ok=True)
    return tmp_path


def execute(repo, relative):
    library = repo / "scripts/deploy/_lib.sh"
    runner = "python" if relative.endswith(".py") else "node" if relative.endswith(".mjs") else "bash"
    library.write_text(library.read_text() + f"\n{runner} {relative}\n")


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
    if relative.startswith("scripts/"):
        execute(repo, relative)
    with pytest.raises(AssertionError, match=rf"{relative}:3: unconsumed DDL carrier: update_ddl"):
        schema.migration_ddl(repo)


@pytest.mark.parametrize("relative", [".github/workflows/review.yml", ".github/workflows/review.unfamiliar",
                                      "infra/review.tf", "Dockerfile.review",
                                      "cloudbuild-review.yaml"])
def test_lowercase_statement_in_new_file_fails(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\nalter table tr_entities add column review_lost STRING(64)\n")
    with pytest.raises(AssertionError, match=rf"{relative}:2: unconsumed DDL carrier: alter"):
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
    "DROP VIEW", "DROP SEQUENCE", "GRANT", "REVOKE", "RENAME", "ANALYZE",
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
    with pytest.raises(AssertionError, match=r"migrate_money_primitives.sh:\d+: unconsumed DDL carrier: CREATE"):
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
    if relative == "scripts/review.py" and carrier == "CREATE OR REPLACE VIEW":
        path = repo / relative
        path.write_text(payload)
        execute(repo, relative)
        assert schema.migration_ddl(repo) == spanner_ddl.DDL
        return
    path = repo / relative
    prefix = path.read_text() + "\n" if path.exists() else ""
    if path.suffix == ".sh":
        prefix += "python - <<'PYTHON'\n"
        payload += "PYTHON\n"
    path.write_text(prefix + payload)
    if relative == "scripts/review.py":
        execute(repo, relative)
    line = prefix.count("\n") + payload[:payload.index(carrier)].count("\n") + 1
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, schema.source_digests(repo), repo)
    assert f"{path}:{line}: unconsumed DDL carrier: {carrier.split()[0]}" in str(error.value)
    assert "expected occurrence count" in str(error.value)
    assert "SHA-256" in str(error.value)


def test_review_six_javascript_regex_then_exec_in_copy(repo):
    path = repo / "scripts/review.mjs"
    path.write_text(r'''const urlPattern = /https?:\/\//; execFileSync('gcloud', ['spanner', 'databases', 'ddl', 'update', 'db', '--ddl=ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)']);
''')
    execute(repo, "scripts/review.mjs")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(schema.ROOT / relative, path)
    assert schema.migration_ddl(repo) == spanner_ddl.DDL
    path.write_text(path.read_text().replace("'lightning','operator'", "'unreviewed','operator'"))
    with pytest.raises(AssertionError, match=r"spanner_provenance.sql:5: DDL carrier: DROP;.*re-review"):
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


@pytest.mark.parametrize("relative", ["tests/review.py", "docs/review.md", *[
    f"new_package/{name}/review.py" for name in sorted(schema.EXCLUDED_DIRECTORIES)
]])
def test_files_outside_both_surfaces_are_not_scanned(tmp_path, relative):
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


def test_statement_scan_is_word_bounded(tmp_path):
    path = tmp_path / "review.txt"
    path.write_text("RECREATE TABLE example\nALTERED TABLE example\nDROPPED TABLE example\n")
    schema.assert_ddl_carriers_consumed(path, [], tmp_path)


@pytest.mark.parametrize("identifier", ["middleware", "middle_tier", "paddle", "middle", "paddleocr"])
def test_middleware_is_not_a_transport_in_copy(repo, identifier):
    path = repo / "scripts/new_import.py"
    path.write_text(f"from trusted_router import {identifier}\n")
    schema.assert_ddl_carriers_consumed(path, [], repo)
    assert schema.migration_ddl(repo) == spanner_ddl.DDL


@pytest.mark.parametrize("carrier", [
    "update_database_ddl", "UpdateDatabaseDdl", "updateDdl", "updateDDL", "--ddl-file", "/ddl",
    "DDL", "--DDL", "ddls", "spanner_dbapi", "updateSchema", "spanner cli --source=x.sql",
    "spanner-cli", "spanner', 'cli", "jdbc:cloudspanner", "liquibase", "flyway", "sqlalchemy_spanner",
    "spanner+spanner:", "extra_statements", "extraStatements", "ExtraStatements", "extra-statements",
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
def test_out_of_scope_statements_need_no_registry_change_in_copy(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = schema.ROOT / relative
    prefix = original.read_text() + "\n" if original.exists() else ""
    path.write_text(prefix + "CREATE TABLE analytics (id UInt64) ENGINE = MergeTree ORDER BY id;\n")
    assert schema.migration_ddl(repo) == spanner_ddl.DDL


@pytest.mark.parametrize("relative", [
    "clickhouse/013_example.sql", "experiments/new/helper.py",
    "src/trusted_router/static/openapi-public.json", "infra_elsewhere/new.tf",
    "src/trusted_router/storage_postgres_schema.sql", "sites/new.js",
])
def test_repository_transport_fails_outside_old_surfaces(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = schema.ROOT / relative
    prefix = original.read_text() + "\n" if original.is_file() else ""
    path.write_text(prefix + 'client.update_database_ddl(statements=open("x.sql").read())\n')
    with pytest.raises(AssertionError) as error:
        schema.migration_ddl(repo)
    assert f"{path}:{prefix.count(chr(10)) + 1}: unconsumed DDL carrier: update_database_ddl" in str(error.value)


@pytest.mark.parametrize("transport", [
    'client.update_database_ddl(statements=open("x.sql").read())',
    'spanner_dbapi.connect()', 'client.updateSchema(statements)',
    'x = "jdbc:cloudspanner:/db"', 'import sqlalchemy_spanner',
])
def test_runtime_transport_fails_without_registering_runtime_statements(repo, transport):
    path = repo / "src/trusted_router/review.py"
    path.write_text('sql = "CREATE TABLE postgres_table (id int)"\n' + transport + "\n")
    with pytest.raises(AssertionError, match="review.py:2: unconsumed DDL carrier:"):
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

DBAPI = """from google.cloud import spanner_dbapi
conn = spanner_dbapi.connect(instance_id='i', database_id='d', project='p')
conn.autocommit = True
conn.cursor().execute("ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)")
"""


def test_review_seven_dbapi_from_library_fails_on_transport(repo):
    path = repo / "clickhouse/review_schema.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text(DBAPI)
    execute(repo, "clickhouse/review_schema.py")
    with pytest.raises(AssertionError, match="review_schema.py:1: unconsumed DDL carrier: spanner_dbapi"):
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)


def test_review_seven_multiline_statement_in_migration_heredoc(repo):
    path = repo / "scripts/deploy/migrate_money_primitives.sh"
    path.write_text(path.read_text() + '\npython - <<"PYTHON"\nconn.cursor().execute("""ALTER\nTABLE tr_entities ADD COLUMN review_lost STRING(64)""")\nPYTHON\n')
    with pytest.raises(AssertionError, match=r"unconsumed DDL carrier: ALTER"):
        schema.assert_schema_matches(spanner_ddl.DDL, schema.source_digests(repo), repo)


def test_spanner_cli_source_in_migration_fails(repo):
    path = repo / "scripts/deploy/migrate_money_primitives.sh"
    path.write_text(path.read_text() + "\ngcloud spanner cli --source=x.sql\n")
    with pytest.raises(AssertionError, match="unconsumed DDL carrier: spanner cli"):
        schema.migration_ddl(repo)


def test_timing_only_regeneration_needs_no_registry_change(repo):
    path = repo / ".test_durations"
    data = json.loads((schema.ROOT / ".test_durations").read_text())
    path.write_text(json.dumps({key: value + 0.1 for key, value in data.items()}, indent=2))
    assert schema.migration_ddl(repo) == spanner_ddl.DDL


@pytest.mark.parametrize("command,bridge", [
    ("timeout 60s uv run --frozen python review_helpers/check.py", ""),
    ("env -u PYTHONPATH python review_helpers/check.py", ""),
    ("printf '%s\\n' review_helpers/check.py | xargs python", ""),
    ("result=`python review_helpers/check.py`", ""),
    ('''python -c 'import subprocess; subprocess.run(["python", "review_helpers/check.py"], check=True)' ''', ""),
    ('''python - <<'PYTHON'
import subprocess
subprocess.run(["python", "review_helpers/check.py"], check=True)
PYTHON''', ""),
    ("python review_bridge.py", '''import subprocess
args = ["python", "review_helpers/check.py"]
subprocess.run(args, check=True)
'''),
    ("python review_bridge.py", '''import subprocess
subprocess.run(args=["python", "review_helpers/check.py"], check=True)
'''),
    ("python review_bridge.py", '''from subprocess import run
run(["python", "review_helpers/check.py"], check=True)
'''),
    ("python review_bridge.py", '''import os, subprocess
subprocess.run(os.environ["COMMAND"], shell=True, check=True)
'''),
    ("(cd review_helpers && python check.py)", ""),
    ("uv run --directory review_helpers python check.py", ""),
    ('timeout 60s python "$SCRIPT"', ""),
    ("", ""),
], ids=["timeout", "env", "xargs", "backticks", "python-c", "python-heredoc",
        "subprocess-argument-list", "subprocess-keyword", "subprocess-import",
        "subprocess-dynamic", "cd", "uv-directory", "dynamic-target", "unlaunched"])
def test_round_eight_helper_transport_fails_regardless_of_launcher(repo, command, bridge):
    path = repo / "review_helpers/check.py"
    path.parent.mkdir()
    path.write_text(DBAPI)
    # A benign root namesake must not conceal the helper in another directory.
    (repo / "check.py").write_text("pass\n")
    (repo / "review_bridge.py").write_text(bridge)
    library = repo / "scripts/deploy/_lib.sh"
    library.write_text(library.read_text() + "\n" + command + "\n")
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
    assert f"{path}:1: unconsumed DDL carrier: spanner_dbapi" in str(error.value)


@pytest.mark.parametrize("relative", ["clickhouse/build_public_snapshots.py",
                                      "src/trusted_router/regional_quota_reconcile_gate.py"])
def test_deploy_program_transport_fails_but_statement_only_is_documented_non_goal(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(schema.ROOT / relative, path)
    prefix = path.read_text() + "\n"
    path.write_text(prefix + 'statement = "ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)"\n')
    assert path not in schema.migration_sources(repo)
    assert "statement text\noutside the fixed migration list" in schema.__doc__
    doc = (schema.ROOT / "docs/storage-portability/spanner-emulator-conformance.md").read_text()
    assert "statement text outside the fixed migration list" in doc
    assert schema.migration_ddl(repo) == spanner_ddl.DDL
    path.write_text(path.read_text() + "database.update_ddl([statement])\n")
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
    assert f"{path}:{prefix.count(chr(10)) + 2}: unconsumed DDL carrier: update_ddl" in str(error.value)


@pytest.mark.parametrize("name", [
    *["new" + suffix for suffix in (
        ".py .pyi .sh .bash .zsh .js .mjs .cjs .ts .tsx .go .java .kt .rb .rs .tf .hcl "
        ".yaml .yml .json .toml .cfg .ini .sql .mk .cs .cc .cpp .h .php .xml .properties .ps1 .jsx .kts .ipynb .unknown"
    ).split()],
    "Dockerfile", "Dockerfile.new", "Makefile", "Makefile.new", "Procfile",
    "executable", "shebang", "extensionless",
])
def test_every_code_configuration_file_kind_is_transport_scanned(repo, name):
    path = repo / "new_area" / name
    path.parent.mkdir()
    prefix = "#!/usr/bin/env python\n" if name == "shebang" else ""
    path.write_text(prefix + "database.update_ddl(statements)\n")
    if name == "executable":
        path.chmod(0o755)
    assert path in schema.transport_sources(repo)
    with pytest.raises(AssertionError) as error:
        schema.migration_ddl(repo)
    assert f"{path}:{prefix.count(chr(10)) + 1}: unconsumed DDL carrier: update_ddl" in str(error.value)


@pytest.mark.parametrize("relative", list(schema.DATA_PATHS))
def test_data_transport_words_need_no_registry_change(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("database.update_ddl(statements)\n")
    assert path not in schema.transport_sources(repo)
    assert schema.migration_ddl(repo) == spanner_ddl.DDL


@pytest.mark.parametrize("relative", ["scripts/deploy/_lib.sh", ".github/workflows/review.yml",
                                      "infra/nested/review.tf", "cloudbuild-review.yaml", "Dockerfile.review"])
def test_fixed_migration_list_matches_statements_across_newlines(repo, relative):
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = path.read_text() + "\n" if path.exists() else ""
    path.write_text(prefix + 'sql = """ALTER\nTABLE tr_entities ADD COLUMN review_lost STRING(64)"""\n')
    with pytest.raises(AssertionError) as error:
        schema.migration_ddl(repo)
    assert f"{path}:{prefix.count(chr(10)) + 1}: unconsumed DDL carrier: ALTER" in str(error.value)


@pytest.mark.parametrize("relative,command,payload,carrier", [
    ("review_helpers/schema.php", "php review_helpers/schema.php",
     '<?php $database->updateDdl(["ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)"]);',
     "updateDdl"),
    ("review_helpers/Program.cs", "dotnet run --project review_helpers",
     'connection.CreateDdlCommand("ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)");',
     "CreateDdlCommand"),
    ("review_helpers/schema.ipynb", "",
     json.dumps({"cells": [{"cell_type": "code", "source": ["database.update_ddl(statements)"]}]}),
     "update_ddl"),
    ("review_helpers/schema.xml", "",
     '<action method="updateDdl">ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)</action>',
     "updateDdl"),
], ids=["php", "csharp", "notebook", "xml"])
def test_round_nine_transports_in_previously_omitted_files(repo, relative, command, payload, carrier):
    path = repo / relative
    path.parent.mkdir()
    path.write_text(payload + "\n")
    library = repo / "scripts/deploy/_lib.sh"
    library.write_text(library.read_text() + "\n" + command + "\n")
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
    assert f"{path}:1: unconsumed DDL carrier: {carrier}" in str(error.value)


@pytest.mark.parametrize("statement", [
    "ALTER/*c*/TABLE tr_entities ADD COLUMN review_lost STRING(64)",
    "ALTER -- c\nTABLE tr_entities ADD COLUMN review_lost STRING(64)",
    "RENAME TABLE review_old TO review_new",
    "GRANT SELECT ON TABLE tr_entities TO ROLE review_role",
    "REVOKE SELECT ON TABLE tr_entities FROM ROLE review_role",
    "ANALYZE",
], ids=["block-comment", "line-comment", "rename", "grant", "revoke", "analyze"])
def test_round_nine_dbapi_ddl_verbs_fail_in_library_assignments(repo, statement):
    path = repo / "scripts/deploy/_lib.sh"
    prefix = path.read_text() + "\n"
    path.write_text(prefix + f'REVIEW_SQL="{statement}"\n')
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
    verb = re.match(r"\w+", statement)[0]
    assert f"{path}:{prefix.count(chr(10)) + 1}: unconsumed DDL carrier: {verb}" in str(error.value)


def test_ddl_verbs_match_installed_sdk():
    schema.assert_sdk_ddl_verbs_match()


def test_sdk_new_ddl_verb_requires_review(monkeypatch):
    from google.cloud.spanner_dbapi import parse_utils

    monkeypatch.setattr(parse_utils, "RE_DDL", re.compile(parse_utils.RE_DDL.pattern[:-1] + "|TRUNCATE)"))
    with pytest.raises(AssertionError, match="Review SDK DDL verb drift"):
        schema.assert_sdk_ddl_verbs_match()


@pytest.mark.parametrize("verb", sorted(schema.DDL_VERBS))
def test_each_whole_word_verb_requires_review_without_object(tmp_path, verb):
    path = tmp_path / "review.sh"
    path.write_text(f'LOG="{verb.lower()}"\n')
    with pytest.raises(AssertionError, match=f"DDL carrier: {verb.lower()}"):
        schema.assert_ddl_carriers_consumed(path, [], tmp_path)


@pytest.mark.parametrize("nul_offset", [0, 8191, 8192, None])
def test_binary_detection_uses_first_eight_kib(tmp_path, nul_offset):
    path = tmp_path / "unknown.format"
    raw = b"x" * 8200 + b" database.update_ddl(statements)"
    if nul_offset is not None:
        raw = raw[:nul_offset] + b"\0" + raw[nul_offset:]
    path.write_bytes(raw)
    assert (path in schema.transport_sources(tmp_path)) == (nul_offset is None or nul_offset >= 8192)


def test_data_exclusions_have_review_reasons():
    for relative, reason in schema.DATA_PATHS.items():
        assert (schema.ROOT / relative).is_file()
        assert reason.strip()


@pytest.mark.parametrize("name", [
    "schema.txt", "schema.lock", "schema.md", "NOTICE_schema.sh",
    "LICENSE", "LICENSE-MIT", "NOTICE.third-party", "README.md", ".test_durations",
    *["schema" + suffix for suffix in (
        ".rst .csv .tsv .svg .png .jpg .jpeg .gif .ico .webp .pdf .woff .woff2 .ttf"
    ).split()],
])
def test_round_fifteen_data_names_do_not_hide_executed_helpers(repo, name):
    relative = f"review_helpers/{name}"
    path = repo / relative
    path.parent.mkdir()
    path.write_text('gcloud spanner databases ddl update db --ddl="ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)"\n')
    execute(repo, relative)
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
    assert f"{path}:1: unconsumed DDL carrier: ddl" in str(error.value)


@pytest.mark.parametrize("name,shebang", [
    ("schema.sh", b"#!/bin/bash\n"),
    ("schema.sh", b""),
    ("schema.SH", b""),
    ("schema", b"#!/bin/bash\n"),
    ("schema.txt", b"#!/bin/bash\n"),
], ids=["shebang-sh", "extension-only", "uppercase-extension", "shebang-only", "shebang-txt"])
def test_round_fifteen_nul_comment_does_not_hide_executed_helpers(repo, name, shebang):
    relative = f"review_helpers/{name}"
    path = repo / relative
    path.parent.mkdir()
    path.write_bytes(shebang + b'# literal NUL: \0\n'
                     b'gcloud spanner databases ddl update db --ddl="ALTER TABLE tr_entities ADD COLUMN review_lost STRING(64)"\n')
    execute(repo, relative)
    with pytest.raises(AssertionError) as error:
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
    line = 3 if shebang else 2
    assert f"{path}:{line}: unconsumed DDL carrier: ddl" in str(error.value)


def test_round_fifteen_binary_without_shebang_or_extension_is_skipped(repo):
    path = repo / "binary_asset"
    path.write_bytes(b"\x89\xff\0\x01database.update_ddl(statements)\xfe")
    assert path not in schema.transport_sources(repo)
    schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)


@pytest.mark.parametrize("change", ["duplicate", "remove", "transport"])
def test_review_note_line_exemption_is_occurrence_bound(repo, change):
    relative = ".codex-review-1.md"
    path = repo / relative
    shutil.copyfile(schema.ROOT / relative, path)
    schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
    line = next(iter(schema.DDL_EXEMPTIONS["lines"][relative]))
    if change == "duplicate":
        path.write_text(path.read_text() + line + "\n")
    elif change == "remove":
        path.write_text(path.read_text().replace(line, ""))
    else:
        path.write_text(path.read_text() + "database.update_ddl(statements)\n")
    diagnostic = "unconsumed DDL carrier" if change == "transport" else "line exemption occurrence count changed"
    with pytest.raises(AssertionError, match=diagnostic):
        schema.assert_schema_matches(spanner_ddl.DDL, spanner_ddl.SOURCE_DIGESTS, repo)
