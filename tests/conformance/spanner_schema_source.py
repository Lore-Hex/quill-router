"""Offline extraction of the migration scripts' fresh-install GoogleSQL schema.

This deliberately parses a narrow shell vocabulary, never executes shell or gcloud.
New migration idioms must extend the parser and regenerate spanner_ddl.py.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# Exact reviewed non-schema lines; no wildcard for new DDL dispatches.
# Helper bodies are expanded by their recognized calls below. Drops upgrade an
# existing installation; fresh installs use the current CREATE definitions.
REVIEWED_DDL_LINES = {
    "infra.sh": {
        '--ddl="ALTER DATABASE \\`${SPANNER_DATABASE_ID}\\` SET OPTIONS (version_retention_period = \'7d\')"': "database retention option, not table schema",
    },
    "migrate_entity_ttl.sh": {
        '--instance="$INSTANCE" ${PROJECT_ARG[@]+"${PROJECT_ARG[@]}"} --ddl="$ddl"': "apply_ddl helper dispatches parsed literal callers",
        'log "  ALTER TABLE tr_entities DROP ROW DELETION POLICY;"': "printed rollback advice only",
        'log "  ALTER TABLE tr_entities DROP COLUMN ephemeral_expires_at;"': "printed rollback advice only",
    },
    "migrate_gateway_request_index.sh": {
        '--project="$PROJECT" --ddl="$1"': "ddl helper dispatches parsed literal callers",
        '1) ddl "DROP INDEX $OLD" ;;': "--retire-unique removes historical unique index",
    },
    "migrate_generation_records.sh": {
        '--ddl="$DDL"': "dispatches parsed DDL CREATE literal",
        '--ddl="$INDEX_DDL"': "dispatches parsed INDEX_DDL CREATE literal",
    },
    "migrate_request_retention.sh": {
        '--instance="$INSTANCE" --ddl="$1"': "ddl helper dispatches parsed literal callers",
        'ddl "ALTER TABLE $1 ADD COLUMN $2 TIMESTAMP"': "expanded ensure_column helper",
        'ddl "ALTER TABLE $table ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))"': "expanded ensure_policy helper",
    },
    "migrate_trust_reconciliation.sh": {
        'apply_ddl "DROP TABLE tr_trust_backfill"': "destructive empty legacy marker recreation; current CREATE retained",
    },
}
for _file in (
    "migrate_receipt_key_versions.sh", "migrate_money_primitives.sh",
    "migrate_spend_lease.sh", "migrate_trust_reconciliation.sh", "migrate_typed_counters.sh",
):
    REVIEWED_DDL_LINES.setdefault(_file, {})[
        '--instance="$INSTANCE" "${PROJECT_ARG[@]}" --ddl="$1"'
    ] = "apply_ddl helper dispatches parsed literal callers"
for _file in ("migrate_spend_lease.sh", "migrate_typed_counters.sh"):
    REVIEWED_DDL_LINES[_file][
        'apply_ddl "ALTER TABLE ${table} ADD COLUMN ${col} ${ddl}"'
    ] = "expanded ensure_column helper"
REVIEWED_DDL_LINES["migrate_typed_counters.sh"][
    'apply_ddl "ALTER TABLE ${table} ALTER COLUMN ${col} SET OPTIONS (allow_commit_timestamp=true)"'
] = "expanded ensure_commit_ts_col helper"

DDL_LINE = re.compile(r"CREATE TABLE|CREATE .*INDEX|ALTER TABLE|DROP (?:TABLE|INDEX)|ROW DELETION POLICY|--ddl")


def assert_no_shell_variables(statement: str, location: str) -> None:
    # This production generated column contains a SQL JSON path, not a shell
    # variable. No other dollar syntax (including $DEFINITION) is accepted.
    assert "$" not in statement.replace("'$.expires_at'", "''"), f"{location}: unresolved shell variable: {statement}"


def schema_sources(root: Path = ROOT) -> list[Path]:
    scripts = root / "scripts/deploy"
    return [scripts / "infra.sh", *sorted(scripts.glob("migrate_*.sh"))]


def source_digests(root: Path = ROOT) -> dict[str, str]:
    # Also guard shell idioms the intentionally narrow extractor cannot expand.
    # A new migration or changed helper must be reviewed before regeneration.
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in schema_sources(root)}


def assert_schema_matches(ddl: tuple[str, ...], digests: dict[str, str], root: Path = ROOT) -> None:
    assert digests == source_digests(root), "Schema source changed; review extraction before regenerating spanner_ddl.py"
    assert ddl == migration_ddl(root), "GoogleSQL DDL drift from deployment migrations"


def migration_ddl(root: Path = ROOT) -> tuple[str, ...]:
    sources = schema_sources(root)
    creates: dict[str, str] = {}
    indexes: dict[str, str] = {}
    additions: dict[tuple[str, str], str] = {}
    policies: dict[str, str] = {}
    commit_columns: set[tuple[str, str]] = set()
    for path in sources:
        logical_lines = []
        physical_lines = []
        pending = ""
        start = 1
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not pending:
                start = number
            if line.endswith("\\"):
                pending += line[:-1] + " "
            else:
                logical_lines.append(pending + line)
                physical_lines.append(start)
                pending = ""
        assert not pending, f"{path}:{start}: unfinished shell continuation"
        source = re.sub(r"(?m)^[ \t]*#.*$", "", "\n".join(logical_lines))

        def location(match, path=path, physical_lines=physical_lines, source=source):
            return f"{path}:{physical_lines[source.count(chr(10), 0, match.start())]}"

        consumed: set[int] = set()

        def consume(match, consumed=consumed, source=source):
            consumed.update(range(source.count("\n", 0, match.start()) + 1,
                                  source.count("\n", 0, match.end()) + 2))

        # Comments can contain suggested rollback DDL, never schema to apply.
        calls = re.findall(r"(?m)^\s*(ensure_\w+)\s+", source)
        # infra.sh also bootstraps IAM; ensure_project_role emits no SQL.
        unknown = set(calls) - {"ensure_column", "ensure_policy", "ensure_commit_ts_col", "ensure_project_role"}
        assert not unknown, f"Unknown schema helper in {path}: {sorted(unknown)}; extend the parser"
        commit_calls = re.findall(r"(?m)^\s*ensure_commit_ts_col\s+(\w+)\s+(\w+)\s*$", source)
        assert len(commit_calls) == calls.count("ensure_commit_ts_col"), f"unparsed commit timestamp call in {path}"
        if commit_calls:
            options = re.search(r'ALTER COLUMN \$\{col\} SET OPTIONS \(([^)]+)\)', source)
            assert options and re.sub(r"\s+", "", options[1]).lower() == "allow_commit_timestamp=true", "unknown commit timestamp helper"
            commit_columns.update(commit_calls)
        variables = dict(re.findall(r"(?m)^([A-Z_]+)=([a-zA-Z_][a-zA-Z_0-9]*)$", source))
        for match in re.finditer(r'''(["'])(CREATE (?:TABLE|(?:UNIQUE )?(?:NULL_FILTERED )?INDEX)\b.*?)\1''', source, re.S):
            consume(match)
            ddl = " ".join(match[2].split())
            ddl = re.sub(r"\$([A-Z_]+)", lambda variable, variables=variables: variables.get(variable[1], variable[0]), ddl)
            assert_no_shell_variables(ddl, location(match))
            if ddl.startswith("CREATE TABLE"):
                name = ddl.split()[2]
                if name in creates:
                    assert creates[name] == ddl, f"conflicting fresh schemas for {name}"
                creates[name] = ddl
            else:
                name = re.search(r"INDEX (\w+)", ddl)[1]
                if name in indexes:
                    assert indexes[name] == ddl, f"conflicting index {name}"
                indexes[name] = ddl
        column_calls = list(re.finditer(r'(?m)^\s*ensure_column\s+(\w+)\s+(\w+)(?:[ \t]+"([^"]+)")?[ \t]*$', source))
        assert len(column_calls) == calls.count("ensure_column"), f"unparsed ensure_column call in {path}"
        for match in column_calls:
            table, column, definition = match.groups()
            if definition is None:
                helper = re.search(r'ALTER TABLE \$1 ADD COLUMN \$2 ([^"\n]+)', source)
                assert helper, f"unknown ensure_column helper in {path}"
                definition = helper[1]
            assert_no_shell_variables(definition, location(match))
            additions[table, column] = " ".join(definition.split())
        for match in re.finditer(r'"ALTER TABLE (\w+) ADD COLUMN (\w+) ([^"]+)"', source):
            table, column, definition = match.groups()
            consume(match)
            additions[table, column] = definition.replace(r"\$", "$")
        policy_calls = list(re.finditer(r'(?m)^\s*ensure_policy (\w+)\s*$', source))
        assert len(policy_calls) == calls.count("ensure_policy"), f"unparsed ensure_policy call in {path}"
        for match in policy_calls:
            helper = re.search(r'ALTER TABLE \$table ADD ROW DELETION POLICY \(([^"\n]+)\)', source)
            assert helper, f"unknown ensure_policy helper in {path}"
            policies[match[1]] = helper[1]
        for match in re.finditer(r'"ALTER TABLE (\w+) ADD ROW DELETION POLICY \(([^"\n]+)\)"', source):
            consume(match)
            policies[match[1]] = match[2]
        reviewed = dict(REVIEWED_DDL_LINES.get(path.name, {}))
        for line, text in enumerate(source.splitlines(), 1):
            if not DDL_LINE.search(text) or line in consumed:
                continue
            first = physical_lines[line - 1]
            end = physical_lines[line] if line < len(physical_lines) else len(path.read_text().splitlines()) + 1
            for number in range(first, end):
                physical = path.read_text().splitlines()[number - 1].strip()
                if not DDL_LINE.search(physical):
                    continue
                assert physical in reviewed, f"{path}:{number}: unconsumed DDL: {physical}"
                reviewed.pop(physical)  # duplicate new dispatch also needs review
    result = list(creates.values())
    for (table, column), definition in additions.items():
        assert table in creates, f"missing base table {table}"
        # Fresh CREATE wins over additive nullable rolling-upgrade definitions.
        if not re.search(rf"(?:\(|,)\s*{column}\s", creates[table]):
            result.append(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    for table, column in sorted(commit_columns):
        assert table in creates, f"missing commit timestamp table {table}"
        already_set = re.search(rf"\b{column} TIMESTAMP OPTIONS \(allow_commit_timestamp\s*=\s*true\)", creates[table])
        if not already_set:
            result.append(f"ALTER TABLE {table} ALTER COLUMN {column} SET OPTIONS (allow_commit_timestamp=true)")
    result.extend(indexes.values())
    result.extend(f"ALTER TABLE {table} ADD ROW DELETION POLICY ({policy})" for table, policy in policies.items())
    assert len(creates) >= 15 and len(indexes) >= 10, "schema extraction unexpectedly empty"
    for statement in result:
        assert_no_shell_variables(statement, "extracted DDL")
    return tuple(result)


if __name__ == "__main__":
    import pprint

    target = ROOT / "tests/conformance/spanner_ddl.py"
    ddl = migration_ddl()
    from tests.conformance.spanner_ddl import DDL as previous

    print("".join(difflib.unified_diff(
        [statement + "\n" for statement in previous],
        [statement + "\n" for statement in ddl],
        fromfile="checked-in DDL", tofile="extracted DDL",
    )), end="")
    target.write_text(
        '"""Fresh-install production GoogleSQL, generated from deployment scripts.\n\n'
        'Regenerate: python -m tests.conformance.spanner_schema_source\n'
        'Do not remove emulator-incompatible DDL; provisioning must report it.\n"""\n\n'
        + "SOURCE_DIGESTS = " + pprint.pformat(source_digests(), width=96) + "\n\n"
        + "DDL = " + pprint.pformat(ddl, width=96) + "\n"
    )
