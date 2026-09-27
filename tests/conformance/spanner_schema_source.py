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


# Reviewed dispatch ARGUMENTS, normalized for whitespace and case. Helper
# bodies are expanded by their recognized callers below. No wildcard variables.
REVIEWED_DDL_STATEMENTS = {
    "infra.sh": {
        r"ALTER DATABASE \`${SPANNER_DATABASE_ID}\` SET OPTIONS (version_retention_period = '7d')": "database retention option",
    },
    "migrate_entity_ttl.sh": {"$ddl": "apply_ddl forwards parsed callers"},
    "migrate_gateway_request_index.sh": {
        "$1": "ddl forwards parsed callers",
        "DROP INDEX $OLD": "retire historical unique index",
    },
    "migrate_request_retention.sh": {
        "$1": "ddl forwards parsed callers",
        "ALTER TABLE $1 ADD COLUMN $2 TIMESTAMP": "expanded ensure_column helper",
        "ALTER TABLE $table ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))": "expanded ensure_policy helper",
    },
    "migrate_trust_reconciliation.sh": {
        "DROP TABLE tr_trust_backfill": "recreate empty legacy marker; retain current CREATE",
    },
}
for _file in (
    "migrate_receipt_key_versions.sh", "migrate_money_primitives.sh",
    "migrate_spend_lease.sh", "migrate_trust_reconciliation.sh", "migrate_typed_counters.sh",
):
    REVIEWED_DDL_STATEMENTS.setdefault(_file, {})["$1"] = "apply_ddl forwards parsed callers"
for _file in ("migrate_spend_lease.sh", "migrate_typed_counters.sh"):
    REVIEWED_DDL_STATEMENTS[_file][
        "ALTER TABLE ${table} ADD COLUMN ${col} ${ddl}"
    ] = "expanded ensure_column helper"
REVIEWED_DDL_STATEMENTS["migrate_typed_counters.sh"][
    "ALTER TABLE ${table} ALTER COLUMN ${col} SET OPTIONS (allow_commit_timestamp=true)"
] = "expanded ensure_commit_ts_col helper"

# Shell words retain their offsets and quoted segments, including embedded
# newlines. Comments/quoted strings cannot masquerade as helper invocations.
_SHELL_TOKEN = re.compile(
    r"\#[^\n]*|(?:\\.|\"(?:\\.|[^\"\\])*\"|'[^']*'|[^\s;|&()\"'\\#])+|[^\s]",
    re.S,
)
_DDL_DISPATCH = re.compile(r"apply_ddl|ddl|--ddl(?:=.*)?", re.I | re.S)


def normalized_statement(text: str) -> str:
    return " ".join(text.split()).casefold()


def ddl_dispatch_arguments(source: str) -> list[tuple[int, int]]:
    """Every helper call / --ddl argument, not merely lines with SQL keywords.

    Checking --ddl everywhere also covers gc (infra.sh's gcloud alias) and
    database-create commands. Unknown/dynamic arguments must fail closed.
    """
    tokens = [token for token in _SHELL_TOKEN.finditer(source) if not token[0].startswith("#")]
    spans = []
    for i, token in enumerate(tokens):
        if not _DDL_DISPATCH.fullmatch(token[0]):
            continue
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if token[0].lower() == "ddl":
            # The gcloud subcommand, not the shell helper.
            if i and tokens[i - 1][0].lower() == "databases":
                continue
        if following is not None and following[0] == "(":
            continue  # helper definition
        if "=" in token[0]:
            spans.append((token.start() + token[0].index("=") + 1, token.end()))
        elif following is not None and following[0] not in {";", "|", "&", ")", "}"}:
            spans.append(following.span())
        else:
            spans.append((token.end(), token.end()))  # missing argument fails
    return spans


def shell_argument(text: str) -> str:
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


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

        consumed: set[tuple[int, int]] = set()

        def consume(match, consumed=consumed):
            consumed.add(match.span())

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
        for match in re.finditer(r'''(["'])(CREATE\s+(?:TABLE|(?:UNIQUE\s+)?(?:NULL_FILTERED\s+)?INDEX)\b.*?)\1''', source, re.I | re.S):
            consume(match)
            ddl = " ".join(match[2].split())
            ddl = re.sub(r"\$([A-Z_]+)", lambda variable, variables=variables: variables.get(variable[1], variable[0]), ddl)
            assert_no_shell_variables(ddl, location(match))
            if ddl.upper().startswith("CREATE TABLE"):
                name = ddl.split()[2]
                if name in creates:
                    assert creates[name] == ddl, f"conflicting fresh schemas for {name}"
                creates[name] = ddl
            else:
                name = re.search(r"INDEX (\w+)", ddl, re.I)[1]
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
        reviewed = {normalized_statement(text): reason
                    for text, reason in REVIEWED_DDL_STATEMENTS.get(path.name, {}).items()}
        # A variable dispatch is accepted only if its sole literal assignment
        # was itself parsed, e.g. MARKER_DDL. Unknown shell expansion is rejected.
        assignments: dict[str, list[tuple[int, int]]] = {}
        for assignment in re.finditer(r"(?m)^[ \t]*([A-Z_]+)=", source):
            literal = next((span for span in consumed if span[0] == assignment.end()), None)
            assignments.setdefault(assignment[1], []).append(literal or (-1, -1))
        for start, end in ddl_dispatch_arguments(source):
            argument = shell_argument(source[start:end])
            if (start, end) in consumed:
                continue
            variable = re.fullmatch(r"\$([A-Z_]+)|\$\{([A-Z_]+)\}", argument)
            if variable:
                definitions = assignments.get(variable[1] or variable[2], [])
                if len(definitions) == 1 and definitions[0] in consumed:
                    continue
            normalized = normalized_statement(argument)
            number = physical_lines[source.count("\n", 0, start)]
            assert normalized in reviewed, f"{path}:{number}: unconsumed DDL dispatch: {argument}"
            reviewed.pop(normalized)  # a second unparsed dispatch needs review
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
