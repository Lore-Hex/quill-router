"""A structural scanner of every writer of ``tr_key_limit``.

``tr_key_limit``'s limit columns (limit_micro, day_limit_micro,
week_limit_micro, month_limit_micro) are what a strict key's admission reads:
Python's strict reserve and the fast path edge. Every write of them goes
through one guarded function per backend, which refuses in the writing
transaction a strict key's mirror going from every limit unset to any set
(storage_gcp_key_limit_mirror.write_key_limit_rows on Spanner,
PostgresStore._write_key_limit_caps_tx on Postgres). This scanner finds every
other place that could write the table, so that claim does not rest on a
list kept by hand:

- every place code names ``tr_key_limit``, in any case or quoting: in Python
  a string (DML in any case, the bare table name, a piece of an f-string, a
  concatenation, a %-format or a .format), or a constant or variable holding
  one (local, module or class, and across modules by name); in a shell, SQL,
  Terraform or YAML file, or the fast path's Go, any line that is not a
  comment;
- every mutation or DML whose table it cannot resolve: a mutation call (by
  verb, through an alias, with ``**kwargs``, or any call given ``table=``)
  whose table is not a known string, and DML whose table is filled in at run
  time (an f-string, %-format, .format or concatenation, a shell variable, a
  Go variable or format).

Reads (SELECT, ``FOR UPDATE`` locks, DDL) and messages that only mention the
table are not findings. A finding is (style, path, qualified name):

  key_limit.sql        DML naming the table
  key_limit.name       the bare table name, or a constant or variable holding it
  key_limit.text       a non-Python file naming the table
  mutation.unresolved  a mutation whose table is not a known string
  sql.dynamic          DML whose table is filled in at run time

limit_column_writers() narrows them to the scopes that may write a limit
column. Two layers decide it.

The backstop (scope_marks) does not depend on parsing. A Python function
scope (or module statement), or a text file, that holds the key-limit table
(by name or through the constant holding it), a limit column name and a
write (a DML word in a string, or a mutation or DML API name such as update,
insert_or_update, replace, execute_update, batch_update or Mutation...)
anywhere counts as writing every limit column, whatever the parser reads. A
scope that writes none is a reviewed exception, listed with a behavioral pin
test and pinned to a fingerprint of its source (ScanDetail.fingerprints), so
a later change to it fails the check until it is reviewed again. The
parser's precision decides only how many exceptions are listed, never
whether a writer is missed.

The parser fails closed too: a scope is cleared only when everything it
writes to the table is understood.

- SQL must tokenize under a lexicon PostgreSQL and GoogleSQL read alike
  (sql_tokens) and each statement mentioning the table must be one of a
  small grammar (_cleared_columns); a ``--`` comment ends at a line feed or
  a carriage return, and any other line end inside one, any block comment,
  ``#``, a backslash in a quote, a tripled quote or a prefixed literal fails
  closed.
- Python SQL must resolve to literals. ``x += piece`` is modelled as every
  piece in order and each piece alone; .format renders every named
  alternative. Any call given SQL text naming the table, and any executor
  (by name or alias, its list or tuple arguments included), runs a
  statement: it is classified where it is whole, and in a scope that
  mentions the table a part not resolved to literals (a helper's result, an
  interpolation, a % or .format argument, a str method's result) fails
  closed whatever is known around it. The one form that clears is
  f"SET LOCAL statement_timeout = {<int>}", its value provably an int.
- A Spanner mutation's columns must be written out at the call as a tuple or
  list of string literals (columns from a spread dict count as every
  column). Insert, Update and InsertOrUpdate keep the columns they omit, so
  only a listed limit column is written; Replace nulls what it omits, so it,
  and any other write call, counts as every column.
- Only a .sql file is read as SQL. In other text (a shell argument or
  heredoc, YAML, Terraform, Go), shell line continuations are joined, and a
  DML word followed by the table before a ``;`` within sixty words,
  whitespace collapsed, counts as every column, as does one where a comment
  marker could hide the ``;``.

Threat model. The inventory guards against accidental limit writers in
reviewed code: a new statement, mutation or script that writes
tr_key_limit's limit columns outside the guarded functions. Any scope that
names the table, a limit column and a write is a limit writer unless it is a
listed, behavior-pinned exception, and the parser fails closed on any SQL or
mutation it cannot resolve within the module that executes it. What remains
outside its scope is SQL whose limit column or table only arrives from
another module or from data at run time: a statement built in one module and
edited by another, where the running scope names neither, or an interpolated
value carrying SQL. Code review covers that. A cross-module helper whose
result runs in a scope that mentions the table does fail closed
(test_a_cross_module_helpers_statement_fails_closed).

api_key entity writers are out of scope by design: whatever writes an entity,
the mirror changes only through the guarded functions above.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, cast

ROOT = Path(__file__).resolve().parents[1]

TABLE = "tr_key_limit"
LIMIT_COLUMNS = frozenset({"limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro"})

_TOKEN = re.compile(r"(?<![A-Za-z0-9_])tr_key_limit(?![A-Za-z0-9_])", re.IGNORECASE)
_LOCK = re.compile(r"\bfor\s+update\b", re.IGNORECASE)
_DML_WORD = re.compile(r"\b(?:insert|update|delete|merge|upsert|replace|truncate|copy)\b", re.IGNORECASE)
_READ_WORD = re.compile(r"\bselect\b|\b(?:create|alter|drop)\s+(?:table|index|unique|null_filtered)\b",
                        re.IGNORECASE)
_DML = (r"(?:insert\s+or\s+update(?:\s+into)?|insert\s+or\s+ignore(?:\s+into)?|insert\s+into|insert|"
        r"update|merge(?:\s+into)?|upsert(?:\s+into)?|replace\s+into|delete\s+from|delete)")
_HINT = r"(?:@\{[^}]*\}\s*)?"
_PLACEHOLDER = r"""["'`]?(?:\{[^}]*\}|%s|%\(\w+\)s|\$\{?\w+\}?|\x00)["'`]?"""
# DML whose table is filled in at run time, shaped as SQL (so a message such
# as "could not update %s" is not one).
_DYNAMIC = re.compile(
    r"\b(?:update\s+" + _HINT + _PLACEHOLDER + r"\s+set\b"
    r"|insert(?:\s+or\s+\w+)?\s+into\s+" + _HINT + _PLACEHOLDER
    + r"|insert\s+or\s+update\s+" + _HINT + _PLACEHOLDER
    + r"|insert\s+" + _HINT + _PLACEHOLDER + r"\s*\("
    + r"|delete\s+from\s+" + _HINT + _PLACEHOLDER
    + r"|delete\s+" + _HINT + _PLACEHOLDER + r"\s+where\b"
    + r"|(?:merge|upsert)\s+(?:into\s+)?" + _HINT + _PLACEHOLDER
    + r"|replace\s+into\s+" + _HINT + _PLACEHOLDER + r")",
    re.IGNORECASE,
)
# A string that is only the head of such DML ("UPDATE ", "DELETE FROM "),
# its table appended at run time.
_DYNAMIC_HEAD = re.compile(r"^\s*" + _HINT + _DML + r"""\s+["'`]?$""", re.IGNORECASE)
_GO_MUTATION = re.compile(r"spanner\.(?:Insert|Update|InsertOrUpdate|Replace|Delete)\w*\(\s*([^,\s)]+)")

MUTATION_VERBS = frozenset({"insert_or_update", "insert_or_update_map", "insert_map", "update_map", "replace_map"})
AMBIGUOUS_VERBS = {"insert": 3, "update": 3, "replace": 3, "delete": 2}
MUTATION_KEYWORDS = frozenset({"table", "columns", "values", "keyset"})
MAX_RENDERINGS = 32
#: Stands for a part of a string the scanner cannot resolve to literals (an
#: f-string interpolation, a % or .format argument, a str method's result).
UNKNOWN = "\x00"

PY_EXCLUDED = ("tests", "tests-e2e", ".venv", "node_modules", ".git", "docs", "frontend")
TEXT_SUFFIXES = (".sh", ".sql", ".tf", ".yaml", ".yml", ".go")
TEXT_EXCLUDED = (*PY_EXCLUDED, "proofs")


def _is_docstring(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    parent = parents.get(id(node))
    return isinstance(parent, ast.Expr)


def names_table(text: str) -> bool:
    return _TOKEN.search(text) is not None


def classify(text: str) -> str | None:
    """What a string naming the table is: DML (``sql``), the bare table name
    (``name``), or None for a read, DDL or a message that mentions it."""
    if not names_table(text):
        return None
    unlocked = _LOCK.sub(" ", text)
    if _DML_WORD.search(unlocked):
        return "sql"
    if _READ_WORD.search(unlocked):
        return None
    if text.strip(" \t\r\n'\"`").lower() == TABLE:
        return "name"
    return None


class _Token(NamedTuple):
    """One SQL token: ``word`` (lower-cased), ``ident`` (a double-quoted or
    backtick name, its quotes off, lower-cased), ``string`` (a literal, as
    written) or ``punct`` (one character), with its span in the text."""

    kind: str
    text: str
    start: int
    end: int


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_OTHER_LINE_ENDS = "\v\f\x85\u2028\u2029"
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_PUNCT = frozenset("(),;=.<>+-*/%:@{}[]!|&~^?0123456789")


def _quoted_end(sql: str, start: int, quote: str) -> int | None:
    """The index past the quote closing the one at ``start``; a doubled
    quote stays inside. None for anything PostgreSQL and GoogleSQL read
    differently: a backslash (an escape in one, a character in the other)
    or a tripled quote (GoogleSQL's triple-quoted literal); and for a quote
    that never closes."""
    if sql.startswith(quote * 3, start):
        return None
    i = start + 1
    while i < len(sql):
        char = sql[i]
        if char == "\\":
            return None
        if char == quote:
            if sql.startswith(quote * 2, i):
                i += 2
                continue
            return i + 1
        i += 1
    return None


def sql_tokens(sql: str) -> list[_Token] | None:
    """``sql`` as tokens under a small lexicon both PostgreSQL and GoogleSQL
    read alike: words, single-quoted literals ('' inside), double-quoted and
    backtick names, PostgreSQL dollar quotes, ``--`` comments, and the
    punctuation in _PUNCT. None for anything else, which the callers count
    as writing every column: any block comment (PostgreSQL's nest,
    GoogleSQL's do not), ``#`` (a GoogleSQL comment, a PostgreSQL operator),
    a backslash, a tripled quote, a prefixed literal (E'', B'', r'', ...),
    or a quote or dollar quote that never closes."""
    tokens: list[_Token] = []
    i = 0
    while i < len(sql):
        char = sql[i]
        if char.isspace():
            i += 1
        elif sql.startswith("--", i):
            # Both dialects end a line comment at \n or \r. Any other
            # line-like character in one (\v, \f, NEL, LS, PS) is not read
            # alike, so it fails closed.
            stop = len(sql)
            for terminator in "\n\r":
                found = sql.find(terminator, i)
                if found >= 0:
                    stop = min(stop, found)
            if any(char in sql[i:stop] for char in _OTHER_LINE_ENDS):
                return None
            i = stop
        elif sql.startswith("/*", i) or sql.startswith("*/", i):
            return None
        elif char in "'\"`":
            if tokens and tokens[-1].kind == "word" and tokens[-1].end == i:
                return None  # a prefixed literal
            end = _quoted_end(sql, i, char)
            if end is None:
                return None
            body = sql[i + 1:end - 1]
            tokens.append(_Token("string" if char == "'" else "ident", body if char == "'" else body.lower(), i, end))
            i = end
        elif (dollar := _DOLLAR_QUOTE.match(sql, i)) is not None:
            close = sql.find(dollar.group(0), dollar.end())
            if close < 0:
                return None
            end = close + len(dollar.group(0))
            tokens.append(_Token("string", sql[dollar.end():close], i, end))
            i = end
        elif (word := _WORD.match(sql, i)) is not None:
            tokens.append(_Token("word", word.group(0).lower(), i, word.end()))
            i = word.end()
        elif char in _PUNCT:
            tokens.append(_Token("punct", char, i, i + 1))
            i += 1
        else:
            return None
    return tokens


def _is(tokens: list[_Token], i: int, kind: str, *texts: str) -> bool:
    return 0 <= i < len(tokens) and tokens[i].kind == kind and (not texts or tokens[i].text in texts)


#: Words that make a statement a write (or a statement this grammar does not
#: clear): each must be one the grammar accounts for.
_WRITE_WORDS = frozenset({"insert", "update", "delete", "merge", "upsert", "replace", "truncate", "copy", "call",
                          "do", "execute", "with"})
_SET_END = frozenset({"where", "returning"})


def _statements(tokens: list[_Token]) -> list[list[_Token]]:
    statements: list[list[_Token]] = [[]]
    for token in tokens:
        if token.kind == "punct" and token.text == ";":
            statements.append([])
        else:
            statements[-1].append(token)
    return [statement for statement in statements if statement]


def _hint_end(tokens: list[_Token], i: int) -> int | None:
    """Past a leading Spanner statement hint ``@{NAME=VALUE, ...}`` at ``i``
    (``i`` itself when there is none); None for any other shape."""
    if not _is(tokens, i, "punct", "@"):
        return i
    j = i + 1
    if not _is(tokens, j, "punct", "{"):
        return None
    j += 1
    while True:
        if not (_is(tokens, j, "word") and _is(tokens, j + 1, "punct", "=") and _is(tokens, j + 2, "word")):
            return None
        j += 3
        if _is(tokens, j, "punct", "}"):
            return j + 1
        if not _is(tokens, j, "punct", ","):
            return None
        j += 1


def _target_end(tokens: list[_Token], i: int) -> int | None:
    """Past the table at ``i``: ``tr_key_limit`` or ``schema.tr_key_limit``,
    plain words only. None for any other target (ONLY, brackets, quotes,
    another table)."""
    if _is(tokens, i, "word", TABLE) and not _is(tokens, i + 1, "punct", "."):
        return i + 1
    if (_is(tokens, i, "word") and not _is(tokens, i, "word", "only") and _is(tokens, i + 1, "punct", ".")
            and _is(tokens, i + 2, "word", TABLE) and not _is(tokens, i + 3, "punct", ".")):
        return i + 3
    return None


def _column_list(tokens: list[_Token], i: int) -> tuple[list[str], int] | None:
    """``( word, word, ... )`` at ``i``: its names and the index past it."""
    if not _is(tokens, i, "punct", "("):
        return None
    names = []
    j = i + 1
    while True:
        if not _is(tokens, j, "word"):
            return None
        names.append(tokens[j].text)
        j += 1
        if _is(tokens, j, "punct", ")"):
            return names, j + 1
        if not _is(tokens, j, "punct", ","):
            return None
        j += 1


def _set_list(tokens: list[_Token], i: int) -> tuple[set[str], int] | None:
    """The SET list at ``i``: ``target = expression, ...``, each target a
    plain word or a bracketed list of them, up to WHERE, RETURNING, THEN
    RETURN or the end. Its targets and the index where it ends; None for
    any other shape (FROM after it, an empty expression, unbalanced
    brackets)."""
    targets: set[str] = set()
    j = i
    while True:
        if _is(tokens, j, "word") and _is(tokens, j + 1, "punct", "="):
            targets.add(tokens[j].text)
            j += 2
        elif (listed := _column_list(tokens, j)) is not None and _is(tokens, listed[1], "punct", "="):
            targets.update(listed[0])
            j = listed[1] + 1
        else:
            return None
        depth = 0
        start = j
        while j < len(tokens):
            token = tokens[j]
            if token.kind == "punct" and token.text in "([":
                depth += 1
            elif token.kind == "punct" and token.text in ")]":
                depth -= 1
                if depth < 0:
                    return None
            elif depth == 0 and (token.kind == "punct" and token.text == ","
                                 or _is(tokens, j, "word", *_SET_END, "from")
                                 or (_is(tokens, j, "word", "then") and _is(tokens, j + 1, "word", "return"))):
                break
            j += 1
        if j == start or depth != 0:
            return None
        if _is(tokens, j, "punct", ","):
            j += 1
            continue
        if _is(tokens, j, "word", "from"):
            return None
        return targets, j


def _cleared_columns(statement: list[_Token]) -> set[str] | None:
    """The tr_key_limit columns one statement writes, when it is one this
    grammar clears; None (every column) for anything else.

    A statement with no write word is a read. Otherwise it must be exactly
    one of, after an optional leading Spanner hint:
      UPDATE <table> [[AS] alias] SET <set list> [WHERE ...]
      INSERT [OR UPDATE | OR IGNORE] [INTO] <table> (<columns>) ...
          [ON CONFLICT ... DO NOTHING | DO UPDATE SET <set list> ...]
      DELETE [FROM] <table> [WHERE ...]
    with no other write word anywhere in it (a CTE, a subquery's DML, a
    MERGE, a COPY, ...)."""
    writes = [i for i, token in enumerate(statement) if token.kind == "word" and token.text in _WRITE_WORDS]
    if not writes:
        return set()
    begin = _hint_end(statement, 0)
    if begin is None or writes[0] != begin:
        return None
    verb = statement[begin].text
    accounted = {begin}
    columns: set[str] = set()
    if verb == "update":
        j = _target_end(statement, begin + 1)
        if j is None:
            return None
        if _is(statement, j, "word", "as") and _is(statement, j + 1, "word"):
            j += 2
        elif _is(statement, j, "word") and not _is(statement, j, "word", "set"):
            j += 1
        if not _is(statement, j, "word", "set"):
            return None
        parsed = _set_list(statement, j + 1)
        if parsed is None:
            return None
        columns = parsed[0]
    elif verb == "insert":
        j = begin + 1
        if _is(statement, j, "word", "or") and _is(statement, j + 1, "word", "update", "ignore"):
            accounted.add(j + 1)
            j += 2
        if _is(statement, j, "word", "into"):
            j += 1
        j = _target_end(statement, j)
        listed = _column_list(statement, j) if j is not None else None
        if listed is None:
            return None
        columns = set(listed[0])
        for k in range(listed[1], len(statement)):
            if _is(statement, k, "word", "do") and _is(statement, k + 1, "word", "update"):
                if not (_is(statement, k + 2, "word", "set") and any(
                        _is(statement, m, "word", "on") and _is(statement, m + 1, "word", "conflict")
                        for m in range(listed[1], k))):
                    return None
                parsed = _set_list(statement, k + 3)
                if parsed is None:
                    return None
                columns |= parsed[0]
                accounted.update({k, k + 1})
            elif _is(statement, k, "word", "do") and _is(statement, k + 1, "word", "nothing"):
                accounted.add(k)
    elif verb == "delete":
        j = begin + 1
        if _is(statement, j, "word", "from"):
            j += 1
        if _target_end(statement, j) is None:
            return None
    else:
        return None
    if set(writes) - accounted:
        return None
    return columns


def written_columns(text: str) -> set[str] | None:
    """The tr_key_limit columns the statements in ``text`` may write; None
    for every column. Each statement that mentions the table must be one
    the grammar clears (_cleared_columns), whatever the other statements
    are; a text that does not tokenize under the lexicon (sql_tokens) fails
    closed whole."""
    if UNKNOWN in text:
        return None  # a part not known at scan time could be any SQL, even inside quotes
    tokens = sql_tokens(text)
    if tokens is None:
        return None
    columns: set[str] = set()
    for statement in _statements(tokens):
        if not names_table(text[statement[0].start:statement[-1].end]):
            continue
        if any(_is(statement, i, "punct", "{") and _is(statement, i + 1, "punct", "}") for i in range(len(statement))):
            return None  # a part filled in at run time could be any SQL
        cleared = _cleared_columns(statement)
        if cleared is None:
            return None
        columns |= cleared
    return columns


def assigns_limit_column(text: str) -> bool:
    """Whether DML naming the table may write a limit column: unless every
    statement that mentions the table is one the grammar clears and none of
    them writes a limit column, it may."""
    if classify(text) != "sql":
        return False
    columns = written_columns(text)
    return columns is None or bool(columns & LIMIT_COLUMNS)


def is_dynamic(text: str) -> bool:
    return _DYNAMIC.search(text) is not None


Scope = ast.AST


class _Index:
    """Assignments across every scanned module, by name: module and class
    constants, ``self.x = ...`` attributes and imported names resolve through
    it; a name assigned in several places takes every value."""

    def __init__(self, trees: Iterable[ast.Module]) -> None:
        self.globals: dict[str, list[ast.AST]] = {}
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                    self.globals.setdefault(node.target.attr, []).append(ast.Name(id="<parameter>"))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute):
                            self.globals.setdefault(target.attr, []).append(node.value)
            for node in tree.body:
                self._module_assign(node)
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        self._module_assign(item)

    def _module_assign(self, node: ast.AST) -> None:
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            self.globals.setdefault(node.target.id, []).append(ast.Name(id="<parameter>"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    self.globals.setdefault(target.id, []).append(node.value)


class _Module:
    def __init__(self, path: Path, tree: ast.Module, index: _Index) -> None:
        self.path = path
        self.tree = tree
        self.index = index
        self.parents: dict[int, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self.parents[id(child)] = node
        self.names: dict[int, str] = {}
        self.functions: dict[int, list[ast.AST]] = {}
        self._qualify(tree, "", [])
        self._locals: dict[int, dict[str, list[ast.AST]]] = {}
        self._memo: dict[int, list[str] | None] = {}

    def _qualify(self, node: ast.AST, prefix: str, stack: list[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{prefix}.{child.name}" if prefix else child.name
                inner_stack = stack + [child] if not isinstance(child, ast.ClassDef) else stack
                for sub in ast.walk(child):
                    self.names[id(sub)] = inner
                    self.functions[id(sub)] = inner_stack
                self._qualify(child, inner, inner_stack)
            elif not prefix and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                label = next((t.id for t in targets if isinstance(t, ast.Name)), "")
                for sub in ast.walk(child):
                    self.names[id(sub)] = f"<module>.{label}" if label else "<module>"
                    self.functions.setdefault(id(sub), [])
            else:
                self._qualify(child, prefix, stack)

    @property
    def relative(self) -> str:
        return self.path.relative_to(ROOT).as_posix() if self.path.is_relative_to(ROOT) else self.path.name

    def where(self, node: ast.AST) -> str:
        return self.names.get(id(node), "<module>")

    def _scope_locals(self, function: ast.AST) -> dict[str, list[ast.AST]]:
        if id(function) not in self._locals:
            found: dict[str, list[ast.AST]] = {}
            appended: dict[str, list[ast.AST]] = {}
            for node in ast.walk(function):
                if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    if isinstance(node.op, ast.Add):
                        appended.setdefault(node.target.id, []).append(node.value)
                    else:
                        found.setdefault(node.target.id, []).append(ast.Name(id="<parameter>"))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        for name in ast.walk(target):
                            if isinstance(name, ast.Name):
                                found.setdefault(name.id, []).append(
                                    node.value if isinstance(target, ast.Name) else ast.Constant(value=None))
                elif isinstance(node, (ast.For, ast.comprehension)):
                    for name in ast.walk(node.target):
                        if isinstance(name, ast.Name):
                            found.setdefault(name.id, []).append(ast.Starred(value=node.iter))
                elif isinstance(node, ast.NamedExpr):
                    found.setdefault(node.target.id, []).append(node.value)
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = function.args
                for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                            *([arguments.vararg] if arguments.vararg else []),
                            *([arguments.kwarg] if arguments.kwarg else [])]:
                    found.setdefault(arg.arg, []).append(ast.Name(id="<parameter>"))
            for appended_name, pieces in appended.items():
                tail = cast(ast.expr, pieces[0])
                for piece in pieces[1:]:
                    tail = ast.BinOp(left=tail, op=ast.Add(), right=cast(ast.expr, piece))
                tails = [tail, *(cast(ast.expr, piece) for piece in pieces)] if len(pieces) > 1 else [tail]
                bases = found.get(appended_name) or [ast.Name(id="<parameter>")]
                found[appended_name] = [
                    _augmented(ast.BinOp(left=cast(ast.expr, base), op=ast.Add(), right=one))
                    for base in bases for one in tails
                ]
            self._locals[id(function)] = found
        return self._locals[id(function)]

    def bindings(self, node: ast.AST, name: str) -> list[ast.AST] | None:
        for function in reversed(self.functions.get(id(node), [])):
            local = self._scope_locals(function)
            if name in local:
                return local[name]
        return self.index.globals.get(name)

    # -- values --------------------------------------------------------

    def values(self, node: ast.AST, seen: frozenset[int] = frozenset()) -> list[str] | None:
        """The strings an expression may be, flattened through tuples, lists,
        sets, dicts (their keys) and starred items, or None when one is not
        known."""
        if id(node) in self._memo:
            return self._memo[id(node)]
        if id(node) in seen:
            return None
        result = self._values(node, seen | {id(node)})
        if not seen:
            self._memo[id(node)] = result
        return result

    def _values(self, node: ast.AST, seen: frozenset[int]) -> list[str] | None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bytes):
                return [node.value.decode("utf-8", "replace")]
            return [node.value] if isinstance(node.value, str) else []
        if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            elements = node.keys if isinstance(node, ast.Dict) else node.elts
            out: list[str] = []
            for element in elements:
                inner = self.values(element, seen) if element is not None else None
                if inner is None:
                    return None
                out.extend(inner)
            return out
        if isinstance(node, ast.Starred):
            return self.values(node.value, seen)
        if isinstance(node, ast.IfExp):
            body, orelse = self.values(node.body, seen), self.values(node.orelse, seen)
            return None if body is None or orelse is None else body + orelse
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and (
                self._is_sequence(node.left, seen) or self._is_sequence(node.right, seen)):
            left, right = self.values(node.left, seen), self.values(node.right, seen)
            return None if left is None or right is None else left + right
        if isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(node, ast.Call) and _callee(node) in _STRING_METHODS
                and isinstance(node.func, ast.Attribute)):
            texts = self.texts(node, seen)
            return None if any(UNKNOWN in text for text in texts) else texts
        if isinstance(node, ast.Name):
            return self._bound(self.bindings(node, node.id), seen)
        if isinstance(node, ast.Attribute):
            return self._bound(self.index.globals.get(node.attr), seen)
        return None

    def _bound(self, bound: list[ast.AST] | None, seen: frozenset[int]) -> list[str] | None:
        if not bound:
            return None
        out: list[str] = []
        for value in bound:
            if isinstance(value, ast.Name) and value.id == "<parameter>":
                return None
            inner = self.values(value, seen)
            if inner is None:
                return None
            out.extend(inner)
        return out

    def _is_sequence(self, node: ast.AST, seen: frozenset[int]) -> bool:
        if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._is_sequence(node.left, seen) or self._is_sequence(node.right, seen)
        if isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in seen:
            bound = (self.bindings(node, node.id) if isinstance(node, ast.Name)
                     else self.index.globals.get(node.attr)) or []
            return any(self._is_sequence(value, seen | {id(node)}) for value in bound)
        return False

    def texts(self, node: ast.AST, seen: frozenset[int] = frozenset()) -> list[str]:
        """Renderings of a string expression, with UNKNOWN for each part that
        is not known (at most MAX_RENDERINGS)."""

        def part(sub: ast.AST) -> list[str]:
            resolved = self.values(sub, seen)
            if not resolved or len(resolved) > MAX_RENDERINGS:
                return [UNKNOWN]
            return resolved

        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            return self.values(node, seen) or [""]
        if isinstance(node, ast.JoinedStr):
            return _combine([part(v.value) if isinstance(v, ast.FormattedValue) else self.texts(v, seen)
                             for v in node.values])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _combine([self.texts(node.left, seen), self.texts(node.right, seen)])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            if isinstance(node.right, ast.Dict):
                keys = [k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else None
                        for k in node.right.keys]
                mapped = [part(v) for v in node.right.values]
                rendered = []
                for template in self.texts(node.left, seen):
                    for choice in itertools.islice(itertools.product(*mapped), MAX_RENDERINGS):
                        by_name = {k: v for k, v in zip(keys, choice, strict=True) if k is not None}
                        rendered.append(_percent_named(template, by_name))
                return rendered[:MAX_RENDERINGS] or [UNKNOWN]
            arguments = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            rendered = [
                _percent(template, list(choice))
                for template in self.texts(node.left, seen)
                for choice in itertools.islice(itertools.product(*(part(a) for a in arguments)), MAX_RENDERINGS)
            ]
            return rendered[:MAX_RENDERINGS] or [UNKNOWN]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method == "format":
                positional = [part(a) for a in node.args]
                names = [kw.arg for kw in node.keywords if kw.arg]
                named = [part(kw.value) for kw in node.keywords if kw.arg]
                spread = any(kw.arg is None for kw in node.keywords)
                rendered = []
                for template in self.texts(node.func.value, seen):
                    for choice in itertools.islice(itertools.product(*positional, *named), MAX_RENDERINGS):
                        values = list(choice)
                        by_name = dict(zip(names, values[len(positional):], strict=True))
                        text = _brace(template, values[:len(positional)], by_name)
                        rendered.append(text + UNKNOWN if spread else text)
                return rendered[:MAX_RENDERINGS] or [UNKNOWN]
            if method in ("upper", "lower", "strip", "lstrip", "rstrip"):
                return [getattr(text, method)() for text in self.texts(node.func.value, seen)]
            if method in _CONSTRUCTION_METHODS:
                # Not modelled: the receiver's text, then a part not known.
                return [text + UNKNOWN for text in self.texts(node.func.value, seen)]
        resolved = self.values(node, seen)
        if resolved:
            return resolved
        if isinstance(node, (ast.Name, ast.Attribute)) and id(node) not in seen:
            # A name not resolved to literals: what it is bound to, with the
            # parts not known in place, so the known SQL around them is kept.
            bound = (self.bindings(node, node.id) if isinstance(node, ast.Name)
                     else self.index.globals.get(node.attr)) or []
            through: list[str] = []
            for value in bound:
                if isinstance(value, ast.Name) and value.id == "<parameter>":
                    through.append(UNKNOWN)
                else:
                    through.extend(self.texts(value, seen | {id(node)}))
            return through[:MAX_RENDERINGS] or [UNKNOWN]
        return [UNKNOWN]


#: String methods whose result the scanner does not compute; a string built
#: with one is not resolved.
_CONSTRUCTION_METHODS = frozenset({"replace", "join", "format_map", "translate", "removeprefix", "removesuffix",
                                   "zfill", "center", "ljust", "rjust", "expandtabs", "casefold", "swapcase",
                                   "title", "capitalize", "partition", "rpartition", "split", "rsplit"})
_STRING_METHODS = frozenset({"format", "upper", "lower", "strip", "lstrip", "rstrip"}) | _CONSTRUCTION_METHODS


def _augmented(node: ast.BinOp) -> ast.BinOp:
    """A name's value after ``+=``: its earlier value with the pieces added."""
    node.augmented = True  # type: ignore[attr-defined]
    return node


def _brace(template: str, positional: list[str], named: dict[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        field = match.group(1)
        if field == "":
            return positional.pop(0) if positional else UNKNOWN
        if field.isdigit():
            index = int(field)
            return positional[index] if index < len(positional) else UNKNOWN
        return named.get(field, UNKNOWN)

    return re.sub(r"\{(\w*)[^}]*\}", substitute, template)


def _combine(pieces: list[list[str]]) -> list[str]:
    out = []
    for choice in itertools.islice(itertools.product(*pieces), MAX_RENDERINGS):
        out.append("".join(choice))
    return out or [""]


def _percent_named(template: str, by_name: dict[str, str]) -> str:
    """``template % {...}``: each %(name)s from the mapping, else UNKNOWN."""
    def substitute(match: re.Match[str]) -> str:
        if match.group(0) == "%%":
            return "%"
        return by_name.get(match.group(1) or "", UNKNOWN)

    return re.sub(r"%%|%\((\w+)\)[sdr]|%[sdr]", substitute, template)


def _percent(template: str, arguments: list[str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        if match.group(0) == "%%":
            return "%"
        if match.group(0).startswith("%("):
            return UNKNOWN  # a mapping key with no mapping given
        return arguments.pop(0) if arguments else UNKNOWN

    return re.sub(r"%%|%(?:\(\w+\))?[sdr]", substitute, template)


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _top_level_strings(m: _Module) -> Iterator[ast.AST]:
    """Each string expression not inside a larger one, docstrings aside."""
    for node in ast.walk(m.tree):
        if not (isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))) or (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and _callee(node) in _STRING_METHODS)):
            continue
        parent = m.parents.get(id(node))
        grandparent = m.parents.get(id(parent)) if parent is not None else None
        if isinstance(parent, (ast.JoinedStr, ast.FormattedValue)) or (
                isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod))) or (
                isinstance(parent, ast.Attribute) and isinstance(grandparent, ast.Call)
                and _callee(grandparent) in _STRING_METHODS):
            continue
        if isinstance(node, ast.BinOp) and not isinstance(node.op, (ast.Add, ast.Mod)):
            continue
        if isinstance(node, ast.Constant) and _is_docstring(node, m.parents):
            continue
        yield node


class _Finding:
    __slots__ = ("limit", "node", "style")

    def __init__(self, style: str, node: ast.AST, limit: bool) -> None:
        self.style, self.node, self.limit = style, node, limit


def _mutation_verb(m: _Module, call: ast.Call) -> str:
    name = _callee(call)
    if isinstance(call.func, ast.Name):
        for value in m.bindings(call, name) or []:
            if isinstance(value, ast.Attribute):
                name = value.attr
    return name


def _mutation_parts(m: _Module, call: ast.Call) -> tuple[ast.AST | None, ast.AST | None, bool] | None:
    """(table, columns, is_delete) of a call that is a mutation, else None.
    A missing part (None) is one that is not known."""
    name = _mutation_verb(m, call)
    keywords: dict[str, ast.AST] = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    spread = [kw.value for kw in call.keywords if kw.arg is None]
    payloads = [_dict_fields(m, value) for value in spread]
    signature = AMBIGUOUS_VERBS.get(name)
    # insert/update/replace/delete are also list, dict and str methods: one
    # is a mutation when it is called like one (mutation keywords, a spread
    # dict with a table, or a mutation's positional shape: columns a
    # sequence of names, or a key set to delete).
    is_mutation = (
        name in MUTATION_VERBS
        or (signature is not None and (
            MUTATION_KEYWORDS & keywords.keys()
            or any("table" in payload for payload in payloads)
            or (len(call.args) == signature and _positional_shape(m, name, call.args))))
        or ("table" in keywords and (MUTATION_KEYWORDS - {"table"}) & keywords.keys())
    )
    first = keywords.get("table", call.args[0] if call.args else None)
    if not is_mutation and signature is not None and first is not None:
        resolved = m.values(first) or []
        is_mutation = any(names_table(value) for value in resolved)
    if not is_mutation:
        return None
    spread_columns = False
    for payload in payloads:
        for key, item in payload.items():
            if key not in keywords:
                keywords[key] = item
                spread_columns = spread_columns or key == "columns"
        if not payload:
            return None, None, name == "delete"
    table = keywords.get("table", call.args[0] if call.args else None)
    columns = keywords.get("columns", call.args[1] if len(call.args) > 1 and name != "delete" else None)
    # Columns from a spread dict are not written out at the call (the dict may
    # be changed before it is spread): every column.
    return table, None if spread_columns else columns, name == "delete"


def _positional_shape(m: _Module, name: str, args: list[ast.expr]) -> bool:
    if name == "delete":
        keys = args[1]
        label = _callee(keys) if isinstance(keys, ast.Call) else (
            keys.id if isinstance(keys, ast.Name) else keys.attr if isinstance(keys, ast.Attribute) else "")
        return "keyset" in label.lower().replace("_", "")
    return m._is_sequence(args[1], frozenset()) and bool(m.values(args[1]))


def _dict_fields(m: _Module, node: ast.AST) -> dict[str, ast.AST]:
    """The literal fields of a dict a ``**`` spreads: a dict display or
    dict(...) bound to the name, and ``name['key'] = value`` after it."""
    fields: dict[str, ast.AST] = {}
    if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)):
        # payload["update"] of a dict display bound to payload.
        outer = _dict_fields(m, node.value)
        inner = outer.get(node.slice.value)
        return _dict_fields(m, inner) if inner is not None else {}
    sources = m.bindings(node, node.id) or [] if isinstance(node, ast.Name) else [node]
    for source in sources:
        if isinstance(source, ast.Dict):
            for key, value in zip(source.keys, source.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    fields[key.value] = value
        elif isinstance(source, ast.Call) and _callee(source) == "dict":
            fields.update({kw.arg: kw.value for kw in source.keywords if kw.arg})
    if isinstance(node, ast.Name):
        for function in m.functions.get(id(node), [])[-1:]:
            for sub in ast.walk(function):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                                and target.value.id == node.id and isinstance(target.slice, ast.Constant)
                                and isinstance(target.slice.value, str)):
                            fields[target.slice.value] = sub.value
    return fields


#: Mutations that keep the columns they omit (Spanner's Update, Insert of a
#: new row, InsertOrUpdate): only a listed column is written. Replace nulls
#: every column it omits, and a _map or any other write call is not read.
_COLUMN_PRESERVING_VERBS = frozenset({"insert", "update", "insert_or_update"})


def _writes_limit_columns(m: _Module, columns: ast.AST | None) -> bool:
    """Whether a mutation's columns may include a limit column: unless they
    are written out at the call as a tuple or list of string literals (a
    name, a constant, a list later extended or anything computed counts as
    every column), they may."""
    if not isinstance(columns, (ast.Tuple, ast.List)) or not all(
            isinstance(c, ast.Constant) and isinstance(c.value, str) for c in columns.elts):
        return True
    return any(str(c.value).lower() in LIMIT_COLUMNS for c in columns.elts if isinstance(c, ast.Constant))


#: Calls that run SQL: their statement must be resolved to literals in a scope
#: that writes the table.
_EXECUTORS = frozenset({"execute", "executemany", "executescript", "execute_sql", "execute_update",
                        "execute_partitioned_dml", "batch_update", "execute_batch_dml"})


def _python_findings(m: _Module) -> Iterator[_Finding]:
    found = list(_string_findings(m))
    marks = scope_marks(m)
    mentions = {where for where, (seen, _anchor) in marks.items() if "table" in seen}
    found += list(_statement_calls(m, mentions))
    yield from found
    # The backstop, which does not depend on parsing: a scope holding the
    # table, a limit column and a write anywhere writes every limit column.
    by_scope: dict[str, list[_Finding]] = {}
    for finding in found:
        if finding.style.startswith("key_limit."):
            by_scope.setdefault(m.where(finding.node), []).append(finding)
    for where, (seen, anchor) in marks.items():
        if seen >= _BACKSTOP:
            for existing in by_scope.get(where, []):
                yield _Finding(existing.style, existing.node, True)
            if where not in by_scope:
                yield _Finding("key_limit.scope", anchor, True)


_LIMIT_WORD = re.compile(r"\b(?:" + "|".join(sorted(LIMIT_COLUMNS)) + r")\b", re.IGNORECASE)
#: Names of mutation and DML APIs, as identifiers or attributes.
_WRITE_APIS = frozenset({"insert", "update", "insert_or_update", "replace", "upsert", "execute_update",
                         "batch_update", "execute_partitioned_dml", "execute_batch_dml", "insert_map", "update_map",
                         "insert_or_update_map", "replace_map"})
_BACKSTOP = frozenset({"table", "limit", "write"})


def scope_marks(m: _Module) -> dict[str, tuple[set[str], ast.AST]]:
    """Per scope (innermost function, or module statement): which of the
    table, a limit column and a write it holds anywhere, and a node in it.

    The table: a string piece naming it, or a name or attribute holding it (a
    module constant such as KEY_LIMIT_TABLE). A limit column: a string piece
    naming one, an identifier or attribute spelled as one, or a name holding
    one. A write: a DML word in a string piece, or an identifier or attribute
    spelled as a mutation or DML API (or Mutation...). Docstrings aside."""
    marks: dict[str, tuple[set[str], ast.AST]] = {}
    for node in ast.walk(m.tree):
        seen, _anchor = marks.setdefault(m.where(node), (set(), node))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _is_docstring(node, m.parents):
                continue
            if names_table(node.value):
                seen.add("table")
            if _LIMIT_WORD.search(node.value):
                seen.add("limit")
            if _DML_WORD.search(_LOCK.sub(" ", node.value)):
                seen.add("write")
        elif isinstance(node, ast.keyword) and node.arg and node.arg.lower() in LIMIT_COLUMNS:
            seen.add("limit")
        elif isinstance(node, (ast.Name, ast.Attribute)):
            ident = node.id if isinstance(node, ast.Name) else node.attr
            if ident.lower() in LIMIT_COLUMNS:
                seen.add("limit")
            if ident in _WRITE_APIS or ident.startswith("Mutation"):
                seen.add("write")
            if isinstance(node.ctx, ast.Load):
                values = m.values(node) or []
                if any(classify(value) == "name" for value in values):
                    seen.add("table")
                if any(_LIMIT_WORD.search(value) for value in values):
                    seen.add("limit")
    # Rendered string expressions too, so a name split across literals
    # ('day_limit_' + 'micro') is seen whole.
    for node in _top_level_strings(m):
        if isinstance(node, ast.Constant):
            continue
        seen, _anchor = marks.setdefault(m.where(node), (set(), node))
        for text in m.texts(node):
            known = text.replace(UNKNOWN, " ")
            if names_table(known):
                seen.add("table")
            if _LIMIT_WORD.search(known):
                seen.add("limit")
            if _DML_WORD.search(_LOCK.sub(" ", known)):
                seen.add("write")
    return marks


def _statement_calls(m: _Module, mentions: set[str]) -> Iterator[_Finding]:
    """Each call that runs a statement (an executor by name or alias, or any
    call given SQL text naming the table): its statement is classified
    where it is whole, and in a scope that mentions the table a part not
    resolved to literals fails closed, whatever is known around it."""
    for node in ast.walk(m.tree):
        if not isinstance(node, ast.Call):
            continue
        for statement in _statements_run(m, node):
            texts = m.texts(statement)
            for text in texts:
                if classify(text) == "sql":
                    yield _Finding("key_limit.sql", node, assigns_limit_column(text))
            if m.where(node) in mentions and any(UNKNOWN in text for text in texts) and not _timeout_setting(
                    statement):
                yield _Finding("key_limit.sql", node, True)


def _elements(node: ast.AST) -> list[ast.AST]:
    """A statement argument's statements: each of a list or tuple literal
    (the first of a nested tuple, a batch's (sql, params, types)), or itself."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [element.elts[0] if isinstance(element, (ast.List, ast.Tuple)) and element.elts else element
                for element in node.elts]
    return [node]


def _statements_run(m: _Module, call: ast.Call) -> list[ast.AST]:
    statements: list[ast.AST] = []
    if _mutation_verb(m, call) in _EXECUTORS:
        first = _executed_statement(m, call)
        statements += _elements(first) if first is not None else [ast.Name(id="<parameter>")]
    for argument in [*call.args, *(kw.value for kw in call.keywords)]:
        for element in _elements(argument):
            if element in statements:
                continue
            texts = m.texts(element)
            if any(names_table(text) and classify(text) != "name" and (
                    classify(text) == "sql" or _READ_WORD.search(text) or _SQL_CLAUSE.search(text))
                   for text in texts):
                statements.append(element)
    return statements


_TIMEOUT_SETTING = re.compile(r"\s*set\s+local\s+statement_timeout\s*=\s*", re.IGNORECASE)


def _timeout_setting(statement: ast.AST) -> bool:
    """The one statement with a part not known that clears:
    f"SET LOCAL statement_timeout = {<int>}", its value provably an int
    (int(...), or min/max of ints)."""
    if not isinstance(statement, ast.JoinedStr) or len(statement.values) != 2:
        return False
    head, value = statement.values
    return (isinstance(head, ast.Constant) and isinstance(head.value, str)
            and _TIMEOUT_SETTING.fullmatch(head.value) is not None
            and isinstance(value, ast.FormattedValue) and value.conversion == -1 and value.format_spec is None
            and _provably_int(value.value))


def _provably_int(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, int) and not isinstance(node.value, bool)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
        if node.func.id == "int" and len(node.args) == 1:
            return True
        if node.func.id in ("min", "max") and node.args:
            return all(_provably_int(argument) for argument in node.args)
    return False


def _executed_statement(m: _Module, call: ast.Call) -> ast.AST | None:
    """The statement an executor runs: its first argument, a sql=/query=
    keyword, or the "sql" of a request dict (the REST client's)."""
    for keyword in call.keywords:
        if keyword.arg in ("sql", "query", "statement"):
            return keyword.value
        if keyword.arg == "request" and isinstance(keyword.value, ast.Dict):
            for key, value in zip(keyword.value.keys, keyword.value.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "sql":
                    return value
    return call.args[0] if call.args else None


_SQL_CLAUSE = re.compile(r"\b(?:set|into|from|values|where|using|join)\b", re.IGNORECASE)


def _unknown_could_write(text: str) -> bool:
    """Whether a string naming the table, shaped as SQL (a clause word in
    its known text), has a part not known that could supply a write: unless
    its known text is a SELECT, an unknown part counts as writing every
    column. (One with a write word is held to the grammar, which fails it
    closed on the unknown part.)"""
    if UNKNOWN not in text or not names_table(text):
        return False
    known = text.replace(UNKNOWN, " ")
    return bool(_SQL_CLAUSE.search(known)) and not known.lstrip().lower().startswith("select")


def _string_findings(m: _Module) -> Iterator[_Finding]:
    strings = list(_top_level_strings(m))
    for node in strings:
        for text in m.texts(node):
            kind = classify(text)
            if kind is not None:
                yield _Finding("key_limit." + kind, node, kind == "sql" and assigns_limit_column(text))
            elif _unknown_could_write(text):
                yield _Finding("key_limit.sql", node, True)
            if _DYNAMIC.search(text) or _DYNAMIC_HEAD.match(text):
                yield _Finding("sql.dynamic", node, True)

    inside_strings = {id(sub) for node in strings for sub in ast.walk(node) if sub is not node}
    for node in ast.walk(m.tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load) and (
                id(node) not in inside_strings):
            resolved = m.values(node)
            if resolved and any(classify(value) == "name" for value in resolved):
                yield _Finding("key_limit.name", node, False)
        if isinstance(node, ast.Call):
            parts = _mutation_parts(m, node)
            if parts is not None:
                table, columns, delete = parts
                resolved = m.values(table) if table is not None else None
                limit = not delete and (_mutation_verb(m, node) not in _COLUMN_PRESERVING_VERBS
                                        or _writes_limit_columns(m, columns))
                if resolved is None:
                    yield _Finding("mutation.unresolved", node, limit)
                elif any(names_table(value) for value in resolved):
                    yield _Finding("key_limit.name", node, limit)
        if isinstance(node, ast.Dict):
            fields: dict[str | None, ast.AST] = {}
            for key, value in zip(node.keys, node.values, strict=True):
                names = m.values(key) if key is not None else None
                fields[names[0] if names else None] = value
            table = fields.get("table")
            if table is not None and "values" in fields:
                resolved = m.values(table)
                # The payload's verb is its key in the dict around it.
                outer = m.parents.get(id(node))
                verbs = [m.values(k) for k, v in zip(outer.keys, outer.values, strict=True)
                         if v is node and k is not None] if isinstance(outer, ast.Dict) else []
                verb = verbs[0][0].lower() if verbs and verbs[0] else ""
                limit = verb not in _COLUMN_PRESERVING_VERBS or _writes_limit_columns(m, fields.get("columns"))
                if resolved is None:
                    yield _Finding("mutation.unresolved", node, limit)
                elif any(names_table(value) for value in resolved):
                    yield _Finding("key_limit.name", node, limit)


def _strip_comments(path: Path, text: str) -> str:
    markers = {".go": ("//",), ".sql": ("--",), ".tf": ("#", "//")}.get(path.suffix, ("#",))
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(markers))


def _dml_near_table(text: str) -> bool:
    """Whether a DML word is followed by the table before a ``;`` and within
    sixty words, whitespace collapsed (so blank lines, padding and
    indentation in a heredoc or a multiline string do not separate them); a
    lock's FOR UPDATE aside."""
    collapsed = " ".join(_LOCK.sub(" ", text.replace("\\\n", "")).split())
    for match in _DML_WORD.finditer(collapsed):
        region = " ".join(collapsed[match.end():].split(" ")[:61])
        if names_table(region.split(";", 1)[0]):
            return True
        # A comment may hold the ; that seemed to end the statement.
        if names_table(region) and any(mark in region for mark in ("/*", "*/", "--", "#")):
            return True
    return False


def _text_findings(path: Path) -> Iterator[tuple[str, bool]]:
    text = _strip_comments(path, path.read_text(encoding="utf-8", errors="replace"))
    if names_table(text):
        if path.suffix == ".sql":
            columns = written_columns(text)
            limit = columns is None or bool(columns & LIMIT_COLUMNS)
        else:
            # SQL inside another language's quoting (a shell argument or
            # heredoc, YAML, Terraform, Go) is not read as SQL: DML near
            # the table counts as writing every column.
            limit = _dml_near_table(text)
        if path.suffix == ".go" and _GO_MUTATION.search(text):
            limit = limit or any(re.search(rf'"{column}"', text) for column in LIMIT_COLUMNS)
        # The backstop, per file: the table, a limit column and a write.
        if _LIMIT_WORD.search(text) and _DML_WORD.search(_LOCK.sub(" ", text.replace("\\\n", ""))):
            limit = True
        yield "key_limit.text", limit
    if _DYNAMIC.search(text):
        yield "sql.dynamic", True
    if path.suffix == ".go":
        for match in _GO_MUTATION.finditer(text):
            if not match.group(1).startswith('"'):
                yield "mutation.unresolved", True


Finding = tuple[str, str, str]


class ScanDetail(dict[Finding, bool]):
    """Every finding, with whether its scope may write a limit column, and a
    fingerprint of the source of each finding's scope (a reviewed exception
    is pinned to the source it was reviewed at)."""

    fingerprints: dict[tuple[str, str], str]


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scope_fingerprint(m: _Module, where: str) -> str:
    """The scope's code, positions aside (the SQL manifest's fingerprint,
    stable across Python versions): its function or module statement, or
    the whole module."""
    from tests.conformance.spanner_sql_inventory import fingerprint

    for node in ast.walk(m.tree):
        if m.names.get(id(node)) == where and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
            parent = m.parents.get(id(node))
            if parent is None or m.names.get(id(parent)) != where:
                return fingerprint(node)
    return fingerprint(m.tree)


def scan_paths_detail(python: Iterable[Path], text: Iterable[Path]) -> ScanDetail:
    """Every finding, with whether its scope may write a limit column."""
    python = list(python)
    sources = [p.read_text(encoding="utf-8") for p in python]
    trees = [ast.parse(source) for source in sources]
    index = _Index(trees)
    found = ScanDetail()
    found.fingerprints = {}
    for path, tree in zip(python, trees, strict=True):
        m = _Module(path, tree, index)
        for finding in _python_findings(m):
            where = m.where(finding.node)
            key = (finding.style, m.relative, where)
            found[key] = found.get(key, False) or finding.limit
            if (m.relative, where) not in found.fingerprints:
                found.fingerprints[(m.relative, where)] = _scope_fingerprint(m, where)
    for path in text:
        relative = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
        raw = path.read_text(encoding="utf-8", errors="replace")
        for style, limit in _text_findings(path):
            key = (style, relative, "<file>")
            found[key] = found.get(key, False) or limit
            found.fingerprints[(relative, "<file>")] = _fingerprint(raw)
    return found


def scan_paths(python: Iterable[Path], text: Iterable[Path]) -> set[Finding]:
    return set(scan_paths_detail(python, text))


def limit_column_writers(detail: dict[Finding, bool]) -> set[tuple[str, str]]:
    """The (path, scope)s that name tr_key_limit and may write a limit
    column. An unresolved or dynamic finding is listed with the table it
    does write."""
    return {(path, where) for (style, path, where), limit in detail.items()
            if limit and style.startswith("key_limit.")}


def _excluded(path: Path, excluded: tuple[str, ...]) -> bool:
    """Tests (any tests directory), virtualenvs, dependencies and docs."""
    parts = path.relative_to(ROOT).parts
    return bool(parts) and parts[0] in excluded or any(
        p in (".venv", "node_modules", ".git", "tests") for p in parts)


def repository_paths() -> tuple[list[Path], list[Path]]:
    python = [p for p in sorted(ROOT.rglob("*.py")) if not _excluded(p, PY_EXCLUDED)]
    text = [p for p in sorted(ROOT.rglob("*")) if p.suffix in TEXT_SUFFIXES and p.is_file()
            and not _excluded(p, TEXT_EXCLUDED) and not p.name.endswith("_test.go")]
    return python, text


def scan_repository_detail() -> ScanDetail:
    return scan_paths_detail(*repository_paths())


def scan_repository() -> set[Finding]:
    return set(scan_repository_detail())


if __name__ == "__main__":
    detail = scan_repository_detail()
    for finding, limit in sorted(detail.items()):
        print(finding, "LIMIT" if limit else "")
    for scope in sorted(limit_column_writers(detail)):
        print("fingerprint", scope, detail.fingerprints[scope])
