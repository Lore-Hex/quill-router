"""Offline extraction of the migration scripts' fresh-install GoogleSQL schema.

This deliberately parses a narrow shell vocabulary, never executes shell or gcloud.
New migration idioms must extend the parser and regenerate spanner_ddl.py.

Scan every repository file as raw bytes decoded as UTF-8 with replacement (in a git
checkout the tracked files: untracked workspace files never ship; the named schema
sources below are read whether tracked or not), except:
(a) root tests/ and docs/ (even files executed by a deploy step are out of scope);
(b) build/dependency directories .git, .venv, node_modules, dist, build,
__pycache__, vendor, target, .next, .mypy_cache, .pytest_cache, .ruff_cache,
and .hypothesis;
(c) the existing exact data path .test_durations (generated pytest timing data);
(d) binary files with no shebang, no CODE_EXTENSIONS suffix, and a NUL byte in
the first 8 KiB. A shebang or code/script extension always defeats binary skipping.
No extension or basename pattern excludes data. Every scanned Spanner DDL
transport token must be consumed or have an occurrence-bound, reasoned exemption.
Every whole-word DB-API DDL verb on the fixed migration list must be consumed or
reviewed: schema sources, _lib.sh, workflows, infra Terraform, Cloud Build and
Dockerfiles. The verb set is checked against the installed SDK's RE_DDL.pattern.
Raw scanning includes comments and strings, without requiring an object keyword
or adjacency. Line reviews bind normalized text and occurrence count; manual
native SQL binds its digest.

Non-goals: DDL through an API/tool with no DDL-specific token (e.g. a generic
cursor.execute supplied a connection externally), transport tokens or URLs
assembled at runtime (e.g. getattr(db, "update_" + "d" + "dl")), statement text
outside the fixed migration list (e.g. ALTER TABLE in clickhouse/build_public_snapshots.py;
it cannot reach Spanner without a transport), root tests/ and docs/ (including
deploy steps executing files there), the exact data path .test_durations, the
build/dependency directories and binary files defined above, and anything
applied outside this repository (e.g. console SQL).
Lore-Hex/quill-router#1372 tracks the scheduled production INFORMATION_SCHEMA
comparison that provides the real backstop (out of this PR).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
from collections import Counter
from collections.abc import Iterator
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
                if not quote and source.startswith("<<<", pos):
                    pos += 3  # A here-string must never become a heredoc at its second '<'.
                    continue
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
    # Named inputs, tracked or not: a migration still being written must reach
    # the schema check and regeneration before it is committed. Only the broad
    # carrier sweep (repository_files) is limited to a checkout's tracked files.
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


EXCLUDED_DIRECTORIES = {
    ".git", ".venv", "node_modules", "dist", "build", "__pycache__",
    "vendor", "target", ".next",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis",
}


def _tracked_files(root: Path) -> list[tuple[Path, bytes]] | None:
    """The tracked files and their git modes when root is a git checkout, else None.

    A checkout is a directory with .git at its root. Its files come from git or
    the scan fails; falling back to a walk would read untracked workspace files.
    A submodule (git lists its files only in some configurations) or tracked
    paths that differ only by case (one file on a case-insensitive filesystem)
    are refused.
    """
    if not (root / ".git").exists():
        return None
    git = ["git", "-C", str(root)]
    # Describe the checkout at root, not one an inherited GIT_DIR (as inside a
    # git hook) points to.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    top = subprocess.run([*git, "rev-parse", "--show-toplevel"],  # noqa: S603 - fixed git query
                         capture_output=True, check=True, env=env).stdout.removesuffix(b"\n")
    if Path(os.fsdecode(top)).resolve() != root.resolve():
        raise AssertionError(f"{root}: has .git, but git places the checkout at {os.fsdecode(top)}")
    listed = subprocess.run([*git, "ls-files", "--stage", "-z"],  # noqa: S603 - fixed git query
                            capture_output=True, check=True, env=env).stdout
    paths: dict[Path, bytes] = {}
    for entry in listed.split(b"\0"):
        info, _, name = entry.partition(b"\t")
        if not name:
            continue
        path = root / os.fsdecode(name)
        if info.startswith(b"160000 "):
            raise AssertionError(f"{path}: submodules are outside the schema scan; "
                                 "extend repository_files() before adding one")
        paths[path] = info.split(b" ", 1)[0]
    folded = Counter(str(path).casefold() for path in paths)
    collisions = sorted(str(path) for path in paths if folded[str(path).casefold()] > 1)
    if collisions:
        raise AssertionError(f"tracked paths differ only by case: {collisions[:6]}; "
                             "one of them cannot be read on a case-insensitive filesystem")
    return list(paths.items())


def repository_files(root: Path) -> list[Path]:
    """The repository's files under root, minus the excluded directories.

    In a git checkout the repository is its tracked files. An untracked
    workspace file, such as the gha-creds-*.json that google-github-actions/auth
    writes into the workspace, never ships and must not be read, let alone
    echoed into a CI log. A plain directory, such as a test's copy, is walked.
    """
    tracked = _tracked_files(root)
    if tracked is None:
        paths = []
        for directory, names, files in os.walk(root):
            names[:] = sorted(name for name in names if name not in EXCLUDED_DIRECTORIES
                              and not (Path(directory) == root and name in {"tests", "docs"}))
            paths.extend(Path(directory) / name for name in files)
        return sorted(paths)
    kept = []
    for path, mode in tracked:
        parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRECTORIES for part in parts[:-1]):
            continue
        if len(parts) > 1 and parts[0] in {"tests", "docs"}:
            continue
        if path.is_file():
            kept.append(path)
        elif mode != b"120000":
            # A symlink is scanned when it resolves to a file, as the walk did;
            # a tracked file missing from disk would escape the scan.
            raise AssertionError(f"{path}: tracked but missing from the checkout (sparse or "
                                 "deleted); the schema scan needs every tracked file")
    return sorted(kept)


def migration_sources(root: Path) -> list[Path]:
    """Fixed statement review list; execution and imports do not expand it."""
    seeds = set(schema_sources(root)) | {root / "scripts/deploy/_lib.sh"}
    for path in repository_files(root):
        relative = path.relative_to(root)
        if (relative.parts[:2] == (".github", "workflows")
                or (relative.parts[0] == "infra" and path.suffix == ".tf")
                or path.name.startswith("Dockerfile")
                or (path.name.startswith("cloudbuild") and path.suffix in {".yaml", ".yml"})):
            seeds.add(path)
    return sorted(path for path in seeds if path.is_file())


# Exact reviewed paths only; new files of any name/extension default to scanning.
DATA_PATHS = {".test_durations": "generated pytest timing data at the repository root"}
# Inclusion override for the binary heuristic, never an exclusion allowlist.
CODE_EXTENSIONS = frozenset((
    ".py .pyi .sh .bash .zsh .fish .js .mjs .cjs .jsx .ts .tsx .go .java .kt .kts "
    ".rb .rs .tf .hcl .yaml .yml .json .toml .cfg .ini .sql .mk .cs .c .cc .cpp "
    ".cxx .h .hh .hpp .hxx .php .xml .properties .ps1 .psm1 .bat .cmd .pl .pm "
    ".r .lua .swift .scala .groovy .ipynb"
).split())


def transport_sources(root: Path) -> list[Path]:
    """Scan all files except the module's (a)-(d); names never imply data."""
    paths = []
    for path in repository_files(root):
        if not path.is_file() or path.relative_to(root).as_posix() in DATA_PATHS:
            continue
        with path.open("rb") as stream:
            prefix = stream.read(8192)
        if (prefix.startswith(b"#!") or path.suffix.lower() in CODE_EXTENSIONS
                or b"\0" not in prefix):
            paths.append(path)
    return paths


def carrier_sources(root: Path) -> list[Path]:
    return sorted(set(migration_sources(root)) | set(transport_sources(root)))


# Prefilter only; whole-part checks below decide whether a candidate is a carrier.
IDENTIFIER_TOKEN = re.compile(r"(?<![\w-])[\w-]*(?:ddls?|extra|statements|databases|create|spanner|cli)[\w-]*", re.I)
IDENTIFIER_PART = re.compile(r"[^\W_]+", re.UNICODE)
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def ddl_transport_matches(source: str) -> Iterator[re.Match[str]]:
    """Keep raw token spans while matching whole identifier parts, not substrings."""
    previous = ""
    previous_token: re.Match[str] | None = None
    spans: set[tuple[int, int]] = set()
    for token in IDENTIFIER_TOKEN.finditer(source):
        if previous_token is not None and IDENTIFIER_PART.search(source[previous_token.end():token.start()]):
            previous = ""
        for word in IDENTIFIER_PART.finditer(token[0]):
            for part in CAMEL_BOUNDARY.split(word[0]):
                part = part.lower()
                if part in {"ddl", "ddls"}:
                    spans.add(token.span())
                if (previous, part) in {("extra", "statements"), ("databases", "create"), ("spanner", "cli")}:
                    assert previous_token is not None
                    spans.add((previous_token.start(), token.end()))
                previous, previous_token = part, token
    for match in re.finditer(
        r"\b(?:spanner_dbapi|updateSchema|sqlalchemy_spanner|liquibase|flyway)\b|"
        r"\bspanner(?:\s+|-)cli\b|jdbc:cloudspanner\b|spanner\+spanner:", source, re.I,
    ):
        spans.add(match.span())
    for start, end in sorted(spans):
        match = re.compile(r"[\s\S]+").match(source, start, end)
        assert match is not None
        yield match


DDL_VERBS = frozenset({"CREATE", "ALTER", "DROP", "GRANT", "REVOKE", "RENAME", "ANALYZE"})
DDL_STATEMENT = re.compile(r"\b(?:" + "|".join(sorted(DDL_VERBS)) + r")\b", re.I)


def assert_sdk_ddl_verbs_match() -> None:
    from google.cloud.spanner_dbapi.parse_utils import RE_DDL

    # Fail closed if the SDK changes either its verbs or its pattern structure.
    verbs = re.fullmatch(r"\^\\s\*\(([A-Z]+(?:\|[A-Z]+)*)\)", RE_DDL.pattern)
    assert verbs is not None, f"Review changed SDK RE_DDL pattern: {RE_DDL.pattern!r}"
    assert set(verbs[1].split("|")) == DDL_VERBS, (
        f"Review SDK DDL verb drift: SDK={verbs[1]}, guard={sorted(DDL_VERBS)}"
    )


def exemption_remedy() -> str:
    return (
        "make the schema extractor consume it (a real schema change), or add a reviewed "
        f"exemption entry in {EXEMPTION_REGISTRY} with a reason "
        "(line: path + whitespace-normalized text + expected occurrence count; "
        "native SQL file: path + SHA-256)"
    )


def assert_ddl_carriers_consumed(path: Path, dispatch_spans: list[tuple[int, int]] | None,
                                 root: Path = ROOT, *, statements: bool = True) -> None:
    """Require consumption or review of the selected surface's literal carriers."""
    relative = path.relative_to(root).as_posix()
    raw = path.read_bytes()
    source = raw.decode("utf-8", errors="replace")
    raw_lines = source.split("\n")
    matches = [(match, True) for match in ddl_transport_matches(source)]
    if statements:
        matches.extend((match, False) for match in DDL_STATEMENT.finditer(source))
    matches.sort(key=lambda item: (item[0][0].lower() not in {"--ddl-file", "ddl-file"}, item[0].start()))
    file_exemption = DDL_EXEMPTIONS["files"].get(relative)
    if file_exemption is not None:
        assert file_exemption["reason"].strip()
        if hashlib.sha256(raw).hexdigest() != file_exemption["sha256"]:
            match = matches[0][0] if matches else None
            line = source.count("\n", 0, match.start()) + 1 if match else 1
            carrier = match[0] if match else "previously exempt file"
            raise AssertionError(f"{path}:{line}: DDL carrier: {carrier}; file exemption SHA-256 changed; "
                                 f"re-review the entire file; {exemption_remedy()}")
    exemptions = DDL_EXEMPTIONS["lines"].get(relative, {})
    normalized_lines = [normalized_statement(line) for line in raw_lines]
    counts = Counter(normalized_lines)
    for text, entry in exemptions.items():
        assert entry["reason"].strip() and type(entry["count"]) is int and entry["count"] > 0
        if counts[text] != entry["count"]:
            line = normalized_lines.index(text) + 1 if text in normalized_lines else 1
            raise AssertionError(f"{path}:{line}: DDL carrier: {text}; line exemption occurrence count "
                                 f"changed: expected {entry['count']}, found {counts[text]}; "
                                 f"re-review the line; {exemption_remedy()}")
    statement_exempt = file_exemption is not None
    for match, transport in matches:
        line = source.count("\n", 0, match.start()) + 1
        last_line = source.count("\n", 0, match.end() - 1) + 1
        if all(normalized_lines[i - 1] in exemptions for i in range(line, last_line + 1)):
            continue
        if not transport and statement_exempt:
            continue
        unsupported = match[0].lower() in {"--ddl-file", "ddl-file"}
        if dispatch_spans is None and not unsupported:
            continue  # Structural extraction must first account for dispatches.
        if not unsupported and any(start <= match.start() and match.end() <= end
                                   for start, end in dispatch_spans or []):
            continue
        kind = "unsupported" if unsupported else "unconsumed"
        raise AssertionError(f"{path}:{line}: {kind} DDL carrier: {match[0]}; {exemption_remedy()}")


def shell_source(path: Path) -> tuple[str, list[int]]:
    raw = path.read_bytes().decode("utf-8", errors="replace")
    comments: list[tuple[int, int]] = []
    try:
        shell_tokens(raw, comments)
    except AssertionError as exc:
        raise AssertionError(f"{path}:1: {exc}") from exc
    for start, end in reversed(comments):
        raw = raw[:start] + re.sub(r"[^\n]", " ", raw[start:end]) + raw[end:]
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
    assert_sdk_ddl_verbs_match()
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
    migrations = migration_sources(root)
    for path in migrations:
        if path not in accounted:
            assert_ddl_carriers_consumed(path, [], root)
    # Retain the explicit manual-native-SQL statement review in addition to transports.
    for relative in DDL_EXEMPTIONS["files"]:
        path = root / relative
        if path.is_file():
            assert_ddl_carriers_consumed(path, [], root)
    for path in sorted(set(transport_sources(root)) - set(migrations)):
        assert_ddl_carriers_consumed(path, [], root, statements=False)
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
