"""Offline extraction of the migration scripts' fresh-install GoogleSQL schema.

This deliberately parses a narrow shell vocabulary, never executes shell or gcloud.
New migration idioms must extend the parser and regenerate spanner_ddl.py.

Repo-wide guard surface: every file in scripts/ and .github/workflows/, plus
src/trusted_router/**/*.py. Tests and docs are outside the surface.
Threat model: a developer applies DDL in any literal form, including Python or
REST. Every literal carrier (case-insensitive, quotes included, comments excluded)
must belong to an extracted dispatch or an exact-line reviewed registry exemption.
Deliberate obfuscation of the carrier words themselves (e.g. "--d""dl" or
building the flag from variables) is out of scope. Literal carriers fail closed.
"""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import re
import shlex
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXEMPTION_REGISTRY = "tests/conformance/spanner_ddl_exemptions.json"
DDL_EXEMPTIONS = json.loads((ROOT / EXEMPTION_REGISTRY).read_text())


# Reviewed dispatch ARGUMENTS, normalized for whitespace ONLY. Shell variable names are case-sensitive.
# Helper bodies are expanded by their recognized callers below. No wildcard variables.
REVIEWED_DDL_STATEMENTS = {
    "infra.sh": {
        r"ALTER DATABASE \`${SPANNER_DATABASE_ID}\` SET OPTIONS (version_retention_period = '7d')": "database retention option",
    },
    "migrate_gateway_request_index.sh": {
        "DROP INDEX $OLD": "retire historical unique index",
    },
    "migrate_request_retention.sh": {
        "ALTER TABLE $1 ADD COLUMN $2 TIMESTAMP": "expanded ensure_column helper",
        "ALTER TABLE $table ADD ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))": "expanded ensure_policy helper",
    },
    "migrate_trust_reconciliation.sh": {
        "DROP TABLE tr_trust_backfill": "recreate empty legacy marker; retain current CREATE",
    },
}
for _file in ("migrate_spend_lease.sh", "migrate_typed_counters.sh"):
    REVIEWED_DDL_STATEMENTS.setdefault(_file, {})[
        "ALTER TABLE ${table} ADD COLUMN ${col} ${ddl}"
    ] = "expanded ensure_column helper"
REVIEWED_DDL_STATEMENTS["migrate_typed_counters.sh"][
    "ALTER TABLE ${table} ALTER COLUMN ${col} SET OPTIONS (allow_commit_timestamp=true)"
] = "expanded ensure_commit_ts_col helper"


def shell_tokens(source: str, comments: list[tuple[int, int]] | None = None) -> list[re.Match[str]]:
    """Offset-preserving shell words, including executable substitutions.

    Nested command substitutions have their own quote scope. Returning their
    tokens too prevents a quoted assignment from hiding a sink or an eval.
    This lexer does not expand or execute any shell text.
    """
    spans: set[tuple[int, int]] = set()
    heredocs: list[str] = []

    def scan(pos: int, terminator: str = "") -> int:
        while pos < len(source):
            if terminator and source[pos] == terminator:
                spans.add((pos, pos + 1))
                return pos + 1
            if source[pos] in " \t\r":
                pos += 1
                continue
            if source[pos] == "#":
                end = source.find("\n", pos)
                if comments is not None:
                    comments.append((pos, len(source) if end < 0 else end))
                pos = len(source) if end < 0 else end
                continue
            if source[pos] == "\n" and heredocs and comments is not None:
                # Heredoc bodies are literal input, not shell comments/quotes.
                # Keep their raw carriers visible to the independent scan.
                pos += 1
                for delimiter in heredocs:
                    closing = re.search(rf"(?m)^\t*{re.escape(delimiter)}$", source[pos:])
                    assert closing, f"unterminated heredoc at offset {pos}"
                    pos += closing.end()
                heredocs.clear()
                continue
            if source[pos] in "\n;|&()":
                start = pos
                pos += 1
                spans.add((start, pos))
                if source[start] == "(" and terminator == ")":
                    pos = scan(pos, ")")
                continue
            start = pos
            quote = ""
            while pos < len(source):
                char = source[pos]
                if not quote and comments is not None and source.startswith("<<", pos):
                    heredoc = re.match(r"<<-?[ \t]*(['\"]?)([A-Za-z_]\w*)\1", source[pos:])
                    if heredoc:
                        heredocs.append(heredoc[2])
                        pos += heredoc.end()
                        continue
                if char == "\\" and quote != "'":
                    pos += 2
                elif char == "'" and quote != '"':
                    quote = "" if quote else "'"
                    pos += 1
                elif char == '"' and quote != "'":
                    quote = "" if quote else '"'
                    pos += 1
                elif quote != "'" and source.startswith("$(", pos):
                    pos = scan(pos + 2, ")")
                elif not quote and char == terminator:
                    break
                elif quote != "'" and char == "`":
                    pos = scan(pos + 1, "`")
                elif not quote and (char in " \t\r\n;|&()" or char == terminator):
                    break
                else:
                    pos += 1
            assert not quote, f"unterminated shell quote at offset {start}"
            assert pos <= len(source), f"unfinished shell escape at offset {start}"
            spans.add((start, pos))
        assert not terminator, f"unterminated shell substitution at offset {pos}"
        return pos

    scan(0)
    pattern = re.compile(r".+", re.S)
    return [match for start, end in sorted(spans)
            if (match := pattern.match(source, start, end)) is not None]


# Only these exact source arguments have been reviewed. The imported library's
# gc wrapper and absence of DDL sinks are checked even during regeneration.
REVIEWED_SOURCES = {
    name: {"${SCRIPT_DIR}/_lib.sh": "scripts/deploy/_lib.sh"}
    for name in ("infra.sh", "migrate_generation_records.sh", "migrate_request_retention.sh")
}
REVIEWED_GC_WRAPPER = 'gc() { gcloud --project "$PROJECT_ID" "$@"; }'
# The unchanged infra script also supplies DDL when bootstrapping the database.
# This exact command is a reviewed exception to the ddl-update-only sink rule.
REVIEWED_BOOTSTRAP_COMMAND = ("gc", "spanner", "databases", "create")


def normalized_statement(text: str) -> str:
    return " ".join(text.split())


def ddl_dispatch_arguments(source: str, path: Path, physical_lines: list[int], root: Path,
                           *, reviewed_library: bool = False) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Discover dispatchers from sinks, then account for every call by span.

    This is a conservative shell vocabulary, not a shell interpreter. Reject
    indirection rather than evaluating it. Quotes retain their statement spans;
    newline/control tokens delimit commands, and braces delimit function bodies.
    """
    try:
        tokens = shell_tokens(source)
    except AssertionError as exc:
        raise AssertionError(f"{path}:1: {exc}") from exc
    if reviewed_library:
        assert_ddl_carriers_consumed(path, [], root)
    words = ["".join(shlex.split(token[0])) if token[0] != "\n" else "\n" for token in tokens]

    def fail(index: int, reason: str) -> None:
        line = physical_lines[source.count("\n", 0, tokens[index].start())]
        raise AssertionError(f"{path}:{line}: {reason}")

    sources = dict(REVIEWED_SOURCES.get(path.name, {}))
    has_gc = reviewed_library
    for i, word in enumerate(words):
        if word == "eval":
            fail(i, "unsupported shell execution: eval")
        if word in {"source", "."}:
            argument = words[i + 1] if i + 1 < len(words) else ""
            imported = sources.pop(argument, None)
            if imported is None:
                fail(i, "unreviewed sourced file")
            imported_path = root / imported
            imported_source, imported_lines = shell_source(imported_path)
            ddl_dispatch_arguments(imported_source, imported_path, imported_lines, root, reviewed_library=True)
            has_gc = True
        if word in {"bash", "sh"}:
            tail = words[i + 1:]
            for option in tail:
                if option in {"\n", ";", "|", "&", ")"}:
                    break
                if option.startswith("-") and "c" in option[1:]:
                    fail(i, "unsupported shell execution: shell -c")

    # Discover function extents without assuming their names or line layout.
    functions: list[tuple[str, int, int]] = []
    definitions: set[int] = set()
    for i in range(len(words) - 3):
        parentheses = words[i + 1:i + 3] == ["(", ")"]
        keyword = i > 0 and words[i - 1] == "function"
        if re.fullmatch(r"[A-Za-z_]\w*", words[i]) and (parentheses or keyword):
            opening = i + 3 if parentheses else i + 1
            while opening < len(words) and words[opening] == "\n":
                opening += 1
            if opening >= len(words) or words[opening] != "{":
                fail(i, "unsupported function body")
            depth = 1
            closing = opening + 1
            while closing < len(words) and depth:
                depth += (words[closing] == "{") - (words[closing] == "}")
                closing += 1
            if depth:
                fail(i, "unterminated function body")
            functions.append((words[i], opening, closing - 1))
            definitions.add(i)

    if reviewed_library:
        gc_definitions = sorted(i for i in definitions if words[i] == "gc")
        if not gc_definitions:
            raise AssertionError(f"{path}:1: missing reviewed gc wrapper")
        if len(gc_definitions) != 1:
            fail(gc_definitions[1], "duplicate gc definition")
        i = gc_definitions[0]
        _, _, closing = next(function for function in functions if function[0] == "gc")
        beginning = i - 1 if i and words[i - 1] == "function" else i
        wrapper = source[tokens[beginning].start():tokens[closing].end()]
        if normalized_statement(wrapper) != REVIEWED_GC_WRAPPER:
            fail(i, "reviewed gc forwarding changed")

    spans = []
    dispatch_spans = []
    dispatchers: set[str] = set()
    bootstrap_seen = False
    for i, word in enumerate(words):
        if word.lower() not in ({"gcloud", "gc"} if has_gc else {"gcloud"}):
            continue
        end = i + 1
        while end < len(words) and words[end] not in {"\n", ";", "|", "&", ")", "}"}:
            end += 1
        command = [value.lower() for value in words[i:end]]
        sink = any(command[j:j + 4] == ["spanner", "databases", "ddl", "update"]
                   for j in range(1, len(command) - 3))
        create = any(command[j:j + 3] == ["spanner", "databases", "create"]
                     for j in range(1, len(command) - 2))
        if reviewed_library and (sink or create):
            fail(i, "DDL sink or dispatcher in reviewed library")
        bootstrap = (path.name == "infra.sh" and tuple(words[i:i + 4]) == REVIEWED_BOOTSTRAP_COMMAND)
        if not sink and not bootstrap:
            continue
        if bootstrap:
            if bootstrap_seen:
                fail(i, "duplicate reviewed bootstrap command")
            bootstrap_seen = True
        beginning = i
        while beginning and words[beginning - 1] not in {"\n", ";", "|", "&", "(", "{"}:
            beginning -= 1
        if any("<<<" in token[0] for token in tokens[beginning:end]):
            fail(i, "here-string feeding DDL sink")
        options = [j for j in range(i, end) if re.fullmatch(r"--ddl(?:=.*)?", tokens[j][0], re.I | re.S)]
        if len(options) != 1:
            fail(i, "DDL sink requires exactly one understood --ddl argument")
        j = options[0]
        token = tokens[j]
        if "=" in token[0]:
            span = (token.start() + token[0].index("=") + 1, token.end())
        elif j + 1 < end:
            span = tokens[j + 1].span()
        else:
            fail(i, "DDL sink missing argument")
        dispatch_spans.append((tokens[i].start(), tokens[end - 1].end()))
        enclosing = [(name, opening, closing) for name, opening, closing in functions if opening < i < closing]
        if not enclosing:
            spans.append(span)
            continue
        if len(enclosing) != 1:
            fail(i, "nested dispatcher function")
        name, opening, closing = enclosing[0]
        argument = source[span[0]:span[1]]
        if argument not in {'"$1"', '"${1}"'}:
            variable = re.fullmatch(r'"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})"', argument)
            if variable is None:
                fail(i, "unsupported dispatcher DDL parameter")
            parameter = variable[1] or variable[2]
            assignments = [k for k in range(opening + 1, closing)
                           if re.match(rf"{re.escape(parameter)}=", tokens[k][0])]
            if (len(assignments) != 1 or assignments[0] >= i
                    or tokens[assignments[0]][0] not in {f'{parameter}="$1"', f'{parameter}="${{1}}"'}):
                fail(i, "unsupported dispatcher DDL parameter assignment")
        dispatchers.add(name.lower())

    for i, token in enumerate(tokens):
        if words[i].lower() not in dispatchers or i in definitions:
            continue
        # 'ddl' in the gcloud command words is not a call to a shell function.
        if i and words[i - 1].lower() == "databases":
            continue
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if following is None or following[0] in {"\n", ";", "|", "&", ")", "}"}:
            spans.append((token.end(), token.end()))
        else:
            spans.append(following.span())
        dispatch_spans.append((token.start(), spans[-1][1]))
    return spans, dispatch_spans


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
    return [scripts / "infra.sh", *sorted(scripts.glob("migrate_*.sh")),
            scripts / "retire_settle_outbox_hot_index.sh"]


def source_digests(root: Path = ROOT) -> dict[str, str]:
    # Also guard shell idioms the intentionally narrow extractor cannot expand.
    # A new migration or changed helper must be reviewed before regeneration.
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in schema_sources(root)}


def assert_schema_matches(ddl: tuple[str, ...], digests: dict[str, str], root: Path = ROOT) -> None:
    assert digests == source_digests(root), "Schema source changed; review extraction before regenerating spanner_ddl.py"
    assert ddl == migration_ddl(root), "GoogleSQL DDL drift from deployment migrations"


def is_generated_bytecode(path: Path) -> bool:
    # Only binary interpreter artifacts are excluded, never source files merely
    # placed in a cache directory or given an unfamiliar extension.
    if path.suffix != ".pyc" or "__pycache__" not in path.parts:
        return False
    with path.open("rb") as stream:
        return stream.read(4)[2:] == b"\r\n"


def carrier_sources(root: Path) -> list[Path]:
    """Discover the surface, never an execution graph or an extension allowlist."""
    return sorted({path for directory in (root / "scripts", root / ".github/workflows")
                   for path in directory.rglob("*") if path.is_file() and not is_generated_bytecode(path)}
                  | set((root / "src/trusted_router").rglob("*.py")))


def uncommented_source(path: Path) -> str:
    """Blank comments without changing offsets, newlines, or quoted text."""
    source = path.read_text()
    comments: list[tuple[int, int]] = []
    if path.suffix == ".py":
        offsets = [0]
        for line in source.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments.append((offsets[token.start[0] - 1] + token.start[1],
                                 offsets[token.end[0] - 1] + token.end[1]))
    elif path.suffix == ".sh" or source.startswith(("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash")):
        try:
            shell_tokens(source, comments)
        except AssertionError as exc:
            raise AssertionError(f"{path}:1: {exc}") from exc
    else:
        # Skip quoted strings before matching comments. Unknown file types have
        # no assumed comment syntax: their entire raw text remains in scope.
        comment_pattern = {
            ".sql": r"--[^\n]*|/\*[\s\S]*?\*/",
            ".mjs": r"//[^\n]*|/\*[\s\S]*?\*/",
            ".js": r"//[^\n]*|/\*[\s\S]*?\*/",
            ".yaml": r"(?<!\S)\#[^\n]*",
            ".yml": r"(?<!\S)\#[^\n]*",
            ".toml": r"\#[^\n]*",
        }.get(path.suffix, r"\#[^\n]*" if path.name in {"Dockerfile", "Caddyfile"} else None)
        if comment_pattern:
            strings = r"(?:'[^']*(?:''[^']*)*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`)"
            for match in re.finditer(f"{strings}|(?P<comment>{comment_pattern})", source):
                if match.group("comment") is not None:
                    comments.append(match.span())
    for start, end in reversed(comments):
        source = source[:start] + re.sub(r"[^\n]", " ", source[start:end]) + source[end:]
    return source


DDL_CARRIER = re.compile(
    r"--ddl(?:-file)?|ddl-file|ddl\s+update|databases\s+create|update_ddl|"
    r"UpdateDatabaseDdl|updateDdl|extra_statements|extraStatements|ddl_statements|"
    r"databases/[^\s\"'/?]+/ddl\b|"
    r"CREATE\s+(?:TABLE|(?:UNIQUE\s+)?(?:NULL_FILTERED\s+)?INDEX|SEARCH\s+INDEX|"
    r"CHANGE\s+STREAM|VIEW|SEQUENCE)\b|ALTER\s+(?:TABLE|INDEX|DATABASE)\b|"
    r"DROP\s+(?:TABLE|INDEX|VIEW|SEQUENCE)\b|ROW\s+DELETION\s+POLICY\b", re.I,
)


def assert_ddl_carriers_consumed(path: Path, dispatch_spans: list[tuple[int, int]] | None,
                                 root: Path = ROOT) -> None:
    """Fail closed on every literal carrier, regardless of transport or syntax."""
    relative = path.relative_to(root).as_posix()
    if relative in DDL_EXEMPTIONS["files"]:
        return
    source = uncommented_source(path)
    raw_lines = path.read_text().splitlines()
    exemptions = DDL_EXEMPTIONS["lines"].get(relative, {})
    matches = list(DDL_CARRIER.finditer(source))
    # Keep the early unsupported ddl-file check: an understood --ddl argument
    # cannot account for a second, unsupported file argument in the same call.
    matches.sort(key=lambda match: match[0].lower() not in {"--ddl-file", "ddl-file"})
    for match in matches:
        line = source.count("\n", 0, match.start()) + 1
        last_line = source.count("\n", 0, match.end() - 1) + 1
        if all(normalized_statement(raw_lines[i - 1]) in exemptions for i in range(line, last_line + 1)):
            continue
        unsupported = match[0].lower() in {"--ddl-file", "ddl-file"}
        if dispatch_spans is None and not unsupported:
            continue  # Structural extraction must first account for dispatches.
        if not unsupported and any(start <= match.start() and match.end() <= end
                                   for start, end in dispatch_spans or []):
            continue
        kind = "unsupported" if unsupported else "unconsumed"
        raise AssertionError(
            f"{path}:{line}: {kind} DDL carrier: {match[0]}; "
            "make the schema extractor consume it (a real schema change), or add a reviewed "
            f"exemption entry in {EXEMPTION_REGISTRY}"
        )


def shell_source(path: Path) -> tuple[str, list[int]]:
    raw = uncommented_source(path)
    assert not raw.endswith("\\\n"), f"{path}:{raw.count(chr(10))}: unfinished shell continuation"
    # Keep offsets identical to the raw file for the independent carrier scan.
    source = raw.replace("\\\n", "  ")
    physical_lines = [1]
    number = 1
    for offset, char in enumerate(raw):
        if char == "\n":
            number += 1
            if source[offset] == "\n":
                physical_lines.append(number)
    return source, physical_lines


def migration_ddl(root: Path = ROOT) -> tuple[str, ...]:
    sources = schema_sources(root)
    # The library must stay carrier-free even if no script sources it anymore.
    assert_ddl_carriers_consumed(root / "scripts/deploy/_lib.sh", [], root)
    accounted: dict[Path, list[tuple[int, int]]] = {}
    creates: dict[str, str] = {}
    indexes: dict[str, str] = {}
    additions: dict[tuple[str, str], str] = {}
    policies: dict[str, str] = {}
    commit_columns: set[tuple[str, str]] = set()
    retired_indexes: set[str] = set()
    for path in sources:
        assert_ddl_carriers_consumed(path, None, root)
        source, physical_lines = shell_source(path)

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
        used_literals: set[tuple[int, int]] = set()
        arguments, dispatch_spans = ddl_dispatch_arguments(source, path, physical_lines, root)
        for start, end in arguments:
            argument = shell_argument(source[start:end])
            if (start, end) in consumed:
                used_literals.add((start, end))
                continue
            variable = re.fullmatch(r"\$([A-Z_]+)|\$\{([A-Z_]+)\}", argument)
            if variable:
                definitions = assignments.get(variable[1] or variable[2], [])
                if len(definitions) == 1 and definitions[0] in consumed:
                    used_literals.add(definitions[0])
                    continue
            if path.name == "retire_settle_outbox_hot_index.sh" and re.fullmatch(r"DROP INDEX \w+", argument):
                retired_indexes.add(argument.split()[2])
                continue
            normalized = normalized_statement(argument)
            number = physical_lines[source.count("\n", 0, start)]
            assert normalized in reviewed, f"{path}:{number}: unconsumed DDL dispatch: {argument}"
            reviewed.pop(normalized)  # a second unparsed dispatch needs review
        # Recognizing a sink is insufficient: all its dispatch arguments above
        # must be consumed (or individually reviewed) before these spans count.
        accounted[path] = [*dispatch_spans, *used_literals]
        assert_ddl_carriers_consumed(path, accounted[path], root)
    for path in carrier_sources(root):
        if path not in accounted:
            assert_ddl_carriers_consumed(path, [], root)
    for name in retired_indexes:
        indexes.pop(name, None)  # Historical indexes may already be absent on fresh installs.
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
