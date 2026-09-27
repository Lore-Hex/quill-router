"""Offline extraction of the migration scripts' fresh-install GoogleSQL schema.

This deliberately parses a narrow shell vocabulary, never executes shell or gcloud.
New migration idioms must extend the parser and regenerate spanner_ddl.py.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
        source = re.sub(r"\\\n", " ", path.read_text())
        # Comments can contain suggested rollback DDL, never schema to apply.
        source = re.sub(r"(?m)^\s*#.*$", "", source)
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
            ddl = " ".join(match[2].split())
            ddl = re.sub(r"\$([A-Z_]+)", lambda variable, variables=variables: variables[variable[1]], ddl)
            assert "$" not in ddl, (path, ddl)
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
            additions[table, column] = " ".join(definition.split())
        for match in re.finditer(r'"ALTER TABLE (\w+) ADD COLUMN (\w+) ([^"]+)"', source):
            table, column, definition = match.groups()
            additions[table, column] = definition.replace(r"\$", "$")
        policy_calls = list(re.finditer(r'(?m)^\s*ensure_policy (\w+)\s*$', source))
        assert len(policy_calls) == calls.count("ensure_policy"), f"unparsed ensure_policy call in {path}"
        for match in policy_calls:
            helper = re.search(r'ALTER TABLE \$table ADD ROW DELETION POLICY \(([^"\n]+)\)', source)
            assert helper, f"unknown ensure_policy helper in {path}"
            policies[match[1]] = helper[1]
        for match in re.finditer(r'"ALTER TABLE (\w+) ADD ROW DELETION POLICY \(([^"\n]+)\)"', source):
            policies[match[1]] = match[2]
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
    return tuple(result)


if __name__ == "__main__":
    import pprint

    target = ROOT / "tests/conformance/spanner_ddl.py"
    target.write_text(
        '"""Fresh-install production GoogleSQL, generated from deployment scripts.\n\n'
        'Regenerate: python -m tests.conformance.spanner_schema_source\n'
        'Do not remove emulator-incompatible DDL; provisioning must report it.\n"""\n\n'
        + "SOURCE_DIGESTS = " + pprint.pformat(source_digests(), width=96) + "\n\n"
        + "DDL = " + pprint.pformat(migration_ddl(), width=96) + "\n"
    )
