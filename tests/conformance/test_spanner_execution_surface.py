"""Execution graph tests use tiny repository copies; no command is executed."""
from pathlib import Path

import pytest

from tests.conformance import spanner_schema_source as schema
from tests.conformance.spanner_execution_surface import execution_targets


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize("relative, text", [
    ("scripts/deploy/_lib.sh", "python clickhouse/helper.py\n"),
    (".github/workflows/new.yml", "steps:\n  - run: python clickhouse/helper.py\n"),
    ("cloudbuild-new.yml", "steps:\n  - entrypoint: bash\n    args: ['-c', 'python clickhouse/helper.py']\n"),
    ("infra/new.tf", 'provisioner "local-exec" {\n command = "python clickhouse/helper.py"\n}\n'),
    ("infra/new.tf", 'provisioner "local-exec" {\n command = <<EOF\npython clickhouse/helper.py\nEOF\n}\n'),
    ("Dockerfile.new", 'CMD ["python", "clickhouse/helper.py"]\n'),
    ("Dockerfile.new", 'COPY clickhouse/helper.py /app/helper.py\nCMD ["python", "/app/helper.py"]\n'),
    ("scripts/deploy/_lib.sh", "(./clickhouse/helper.py)\n"),
    ("scripts/deploy/_lib.sh", 'result="$(python clickhouse/helper.py)"\n'),
    ("scripts/deploy/_lib.sh", 'PY=(uv run --frozen python)\n"${PY[@]}" clickhouse/helper.py\n'),
])
def test_all_migration_roots_follow_execution_positions(tmp_path, relative, text):
    helper = write(tmp_path, "clickhouse/helper.py", "pass\n")
    entry = write(tmp_path, relative, text)
    assert execution_targets(entry, tmp_path) == {helper}
    assert helper in schema.migration_sources(tmp_path)


@pytest.mark.parametrize("command", [
    'python "$SCRIPT"', 'uv run --frozen python "$SCRIPT"',
    'bash "$SCRIPT"', 'sh "$SCRIPT"', 'node "$SCRIPT"',
    'source "$SCRIPT"', '. "$SCRIPT"', 'python -m "$MODULE"',
    '"./$SCRIPT"', 'python missing.py', 'python -m missing_module',
    'python \\\n  "$SCRIPT"',
])
def test_unknown_execution_path_is_not_silently_dropped(tmp_path, command):
    path = write(tmp_path, "scripts/deploy/_lib.sh", "# first line\n" + command + "\n")
    with pytest.raises(AssertionError, match=rf"{path}:2: unresolved execution target:"):
        schema.migration_sources(tmp_path)


def test_dynamic_reassignment_does_not_reuse_old_static_path(tmp_path):
    write(tmp_path, "safe.py", "pass\n")
    path = write(tmp_path, "scripts/deploy/_lib.sh",
                 'SCRIPT=safe.py\nSCRIPT="$INPUT"\npython "$SCRIPT"\n')
    with pytest.raises(AssertionError, match=rf"{path}:3: unresolved execution target:"):
        schema.migration_sources(tmp_path)


def test_reference_as_data_does_not_join_execution_surface(tmp_path):
    helper = write(tmp_path, "clickhouse/helper.py", "pass\n")
    write(tmp_path, "scripts/deploy/_lib.sh", 'echo "python clickhouse/helper.py"\n')
    assert helper not in schema.migration_sources(tmp_path)


def test_workflow_working_directory_resolves_file(tmp_path):
    helper = write(tmp_path, "other/helper.py", "pass\n")
    entry = write(tmp_path, ".github/workflows/new.yml",
                  "steps:\n  - working-directory: other\n    run: python helper.py\n")
    assert execution_targets(entry, tmp_path) == {helper}


def test_other_importable_package_gets_transport_only_scan(tmp_path):
    entry = write(tmp_path, "scripts/deploy/_lib.sh", "python -m app.migrate\n")
    migrate = write(tmp_path, "src/app/migrate.py", "from library import helper\n")
    sibling = write(tmp_path, "src/app/runtime.py", 'statement = "CREATE TABLE postgres_example (id int)"\n')
    helper = write(tmp_path, "library/helper.py", "from another import action\n")
    action = write(tmp_path, "another/action.py", "pass\n")
    migrations = schema.migration_sources(tmp_path)
    runtime = schema.runtime_sources(tmp_path, migrations)
    assert set(migrations) == {entry, migrate}
    assert {migrate, sibling, helper, action} <= set(runtime)
    schema.assert_ddl_carriers_consumed(sibling, [], tmp_path, statements=False)
    sibling.write_text('client.updateSchema(statements)\n')
    with pytest.raises(AssertionError, match="runtime.py:1: unconsumed DDL carrier: updateSchema"):
        schema.assert_ddl_carriers_consumed(sibling, [], tmp_path, statements=False)


def test_deploy_closure_includes_real_executed_modules():
    relative = {str(p.relative_to(schema.ROOT)) for p in schema.migration_sources(schema.ROOT)}
    assert {
        "src/trusted_router/receipt_key_backfill_cli.py",
        "src/trusted_router/cloud_rollout_completeness.py",
        "scripts/deploy/configure_bigtable_retention.py",
        "src/trusted_router/enclave_regions.py",
        "scripts/entrypoint.sh",
    } <= relative
