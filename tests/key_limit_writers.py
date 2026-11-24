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
  key_limit.indirect   code reached by a computed name, or an executor passed
                       on, in a scope that names the table (below)
  key_limit.escape     a value naming the table leaving its scope other than
                       by an executor, a mutation or a followed helper (below)
  mutation.unresolved  a mutation whose table is not a known string
  sql.dynamic          DML whose table is filled in at run time

limit_column_writers() narrows them to the scopes that may write a limit
column. Three layers decide it: the escape rule, the backstop and the
parser.

A scope is an outermost function with every function, lambda, comprehension
and class body nested in it (_Module.outer), or a module statement or class
body outside any function, or a text file. getattr(obj, "name") with a
literal name, through any alias of getattr, and obj.__getattribute__("name")
are read as obj.name before either layer runs (_Fetches).

The escape rule (_escape_routes) decides where SQL text naming the table may
go. A value whose rendering may name the table (a string constant, f-string,
concatenation or format naming it, a name or attribute bound to one, or a
list, tuple, dict or set holding one) may leave its scope only by:

- a recognised executor call (any of its arguments: the statement is judged
  by the rules below, its parameters are data) or mutation call (judged by
  the mutation rules); or
- a call to a helper of the module that derived_sinks follows, at a
  parameter shown to reach only executors (clean_sinks: every use of it,
  and of each name it is bound to or put into, is one of these routes).

It may be built on (concatenated, formatted, put in a container literal,
indexed, given to a str method or to str, repr, list, tuple, set or sorted),
bound to a name (whose uses are followed, a closure's included), put into a
local container with append, extend, insert, add, update, setdefault or +=
(one made in the function, whose uses, and those of every name it was bound
to, are followed; a container from a parameter, a module, a class or a loop
is shared with other scopes, an escape), looped
over into a name, or used in a way that keeps none of its text (compared,
tested, measured with len, given to isinstance, used as a lookup key, a str
predicate's receiver, or a discarded expression). Every other route is
key_limit.escape, every column: given to any other callee (a function,
method, lambda, class, callable object, exception or unknown name,
positionally, by keyword or spread: a raised message reaches a handler in
another scope), returned, yielded, stored in an attribute, a subscript or a
global, unpacked, a default argument, an assert's message, or anything not
listed. The routes are an allow list, so a
construct not thought of lands in the escape by default.

The backstop (scope_marks) does not depend on parsing. A scope that holds
the key-limit table (by name or through the constant holding it), a limit
column name and a write (a DML word in a string, or a mutation or DML API
name such as update, insert_or_update, replace, execute_update, batch_update
or Mutation...) anywhere counts as writing every limit column, whatever the
parser reads. A scope that writes none is a reviewed exception, listed with
a behavioral pin test and pinned to a fingerprint of its source
(ScanDetail.fingerprints: every definition of the scope's qualified name in
the module, a conditional or repeated def or a class's duplicate method
included, and every other statement binding that name where it is defined),
so a later change to it fails the check until it is reviewed again. The
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
  alternative. Any call given SQL text naming the table, any executor (by
  name, or through a name or attribute bound to one: run = conn.execute,
  self.run = conn.execute, setattr(obj, "run", conn.execute)), and any of
  the module's own helpers that runs a statement it is given (derived_sinks:
  its parameter reaches an executor's statement, through its local names, a
  nested function or another such helper) runs a statement: it is
  classified where it is whole, and in a scope that mentions the table a
  part not resolved to literals (a helper's result, an interpolation, a % or
  .format argument, a str method's result, a nested function's parameter)
  fails closed whatever is known around it. A statement given to a helper
  that may change it (one that builds on the parameter, is decorated, holds
  exec, eval or compile, or calls by a computed name) fails closed in such a
  scope or wherever it names the table. The one form that clears is
  f"SET LOCAL statement_timeout = {<int>}", its value provably an int.
- In a scope that mentions the table, these fail closed (key_limit.indirect,
  every column): getattr, attrgetter, methodcaller or __getattribute__ given
  a name that is not a literal, setattr given one, attrgetter or
  methodcaller naming an executor, a mutation API or such a helper; exec,
  eval and compile; and an executor, a mutation API, such a helper, or
  getattr, setattr, exec or eval itself, used other than by calling it or
  binding it to a name or attribute (passed on, returned, stored in a
  container, wrapped by functools.partial or a decorator).
- A Spanner mutation's columns must be written out at the call as a tuple or
  list of string literals (columns from a spread dict count as every
  column). Insert, Update and InsertOrUpdate keep the columns they omit, so
  only a listed limit column is written; Replace nulls what it omits, so it,
  and any other write call, counts as every column.
- Only a .sql file is read as SQL. In other text (a shell argument or
  heredoc, YAML, Terraform, Go), a DML word followed by the table before a
  ``;`` within sixty words, whitespace collapsed, counts as every column, as
  does one where a comment marker could hide the ``;``. Every check reads
  the text as written and with shell line continuations joined (as is, and
  with the next line's indentation dropped, as a YAML block scalar drops
  it): a shell joins them on a command line and in a heredoc whose
  delimiter is not quoted, and passes them on in one whose is; a finding of
  any reading counts. A line in a heredoc's body or inside a string left
  open, after a continuation, or holding another line break is never taken
  for a comment, and a table from a command substitution or a psql variable
  is one filled in at run time.

Threat model. The inventory guards against accidental limit writers in
reviewed code: a new statement, mutation or script that writes
tr_key_limit's limit columns outside the guarded functions. SQL text naming
the table may reach an executor only directly or through followed helpers of
the same module; every other route fails closed (the escape rule). Any scope
that names the table, a limit column and a write is a limit writer unless it
is a listed, behavior-pinned exception, and the parser fails closed, as
listed above, on SQL or a mutation it cannot resolve within the module that
executes it. What remains outside its scope is SQL whose table only arrives
from another module or from data at run time: a statement built in one
module from text that does not name the table and completed in another, or
an interpolated value carrying SQL. A module constant naming the table (such
as KEY_LIMIT_TABLE) is followed into every scanned module that reads it.
Code review covers the rest. A cross-module helper whose result runs in a
scope that mentions the table does fail closed
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


def _dynamic_dml(placeholder: str) -> re.Pattern[str]:
    """DML whose table is filled in at run time, shaped as SQL (so a message
    such as "could not update %s" is not one)."""
    return re.compile(
        r"\b(?:update\s+" + _HINT + placeholder + r"\s+set\b"
        r"|insert(?:\s+or\s+\w+)?\s+into\s+" + _HINT + placeholder
        + r"|insert\s+or\s+update\s+" + _HINT + placeholder
        + r"|insert\s+" + _HINT + placeholder + r"\s*\("
        + r"|delete\s+from\s+" + _HINT + placeholder
        + r"|delete\s+" + _HINT + placeholder + r"\s+where\b"
        + r"|(?:merge|upsert)\s+(?:into\s+)?" + _HINT + placeholder
        + r"|replace\s+into\s+" + _HINT + placeholder + r")",
        re.IGNORECASE,
    )


_DYNAMIC = _dynamic_dml(_PLACEHOLDER)
#: In a shell, SQL, Terraform, YAML or Go file the table may also come from a
#: command substitution ($(...), or `...` holding a space or $) or a psql
#: variable (:name, :'name', :"name").
_TEXT_DYNAMIC = _dynamic_dml(r"""(?:""" + _PLACEHOLDER + r"""|\$\([^)]*\)|`[^`]*[\s$][^`]*`|:['"]?\w+['"]?)""")
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
        #: getattr and the names bound to it (_getattr_aliases).
        self.getattrs: set[str] = {"getattr"}
        self.attribute_aliases: dict[str, set[str]] = {}
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                    self.globals.setdefault(node.target.attr, []).append(ast.Name(id="<parameter>"))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Attribute):
                            self.globals.setdefault(target.attr, []).append(node.value)
                elif (isinstance(node, ast.Call) and _callee(node) == "setattr" and len(node.args) == 3
                      and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                    # setattr(obj, "name", value) binds obj.name like an assignment.
                    self.globals.setdefault(node.args[1].value, []).append(node.args[2])
                elif isinstance(node, ast.alias) and node.asname and node.asname != node.name.rsplit(".", 1)[-1]:
                    # import x as y, from m import x as y: y is another name for x.
                    self.globals.setdefault(node.asname, []).append(ast.Name(id=node.name.rsplit(".", 1)[-1]))
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
        #: The outermost function around a node (a nested def, lambda,
        #: comprehension or class body belongs to it), by qualified name.
        self.outers: dict[int, str] = {}
        self.outer_of: dict[str, str] = {}
        self.outer_nodes: dict[str, ast.AST] = {}
        self._qualify(tree, "", [], "")
        self._locals: dict[int, dict[str, list[ast.AST]]] = {}
        self._memo: dict[int, list[str] | None] = {}
        self.callees: dict[int, set[str]] = {}
        #: The module's helpers that run a statement they are given, and
        #: those shown to only run it (clean_sinks).
        self.sinks: dict[str, list[_Sink]] = {}
        self.clean: set[tuple[str, _Sink]] = set()
        self.loads: dict[str, list[ast.AST]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self.loads.setdefault(node.id, []).append(node)

    def _qualify(self, node: ast.AST, prefix: str, stack: list[ast.AST], outer: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{prefix}.{child.name}" if prefix else child.name
                function = not isinstance(child, ast.ClassDef)
                inner_stack = stack + [child] if function else stack
                inner_outer = outer or (inner if function else "")
                self.outer_of.setdefault(inner, inner_outer or inner)
                if inner_outer == inner:
                    self.outer_nodes.setdefault(inner, child)
                for sub in ast.walk(child):
                    self.names[id(sub)] = inner
                    self.functions[id(sub)] = inner_stack
                    self.outers[id(sub)] = inner_outer
                self._qualify(child, inner, inner_stack, inner_outer)
            elif isinstance(child, ast.Lambda):
                # A lambda's parameters are its own; its body is its scope's.
                inner_stack = stack + [child]
                for sub in ast.walk(child):
                    self.functions[id(sub)] = inner_stack
                self._qualify(child, prefix, inner_stack, outer)
            elif not prefix and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                label = next((t.id for t in targets if isinstance(t, ast.Name)), "")
                name = f"<module>.{label}" if label else "<module>"
                self.outer_of.setdefault(name, name)
                for sub in ast.walk(child):
                    self.names[id(sub)] = name
                    self.functions.setdefault(id(sub), [])
                self._qualify(child, prefix, stack, outer)
            else:
                self._qualify(child, prefix, stack, outer)

    @property
    def relative(self) -> str:
        return self.path.relative_to(ROOT).as_posix() if self.path.is_relative_to(ROOT) else self.path.name

    def where(self, node: ast.AST) -> str:
        return self.names.get(id(node), "<module>")

    def outer(self, node: ast.AST) -> str:
        """The scope the backstop and the fail-closed rules judge a node by:
        its outermost enclosing function, or its own module statement or
        class body when it is in none."""
        return self.outers.get(id(node)) or self.where(node)

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
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
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


def _callee_names(m: _Module, func: ast.AST, seen: frozenset[int] = frozenset()) -> set[str]:
    """Every name a callee may be: its own, and through a local name bound
    to another callee (run = conn.execute, ga = getattr), that one's, or
    through an attribute bound anywhere (self.run = conn.execute,
    setattr(obj, "run", conn.execute)), the name it is bound to. A getattr
    with a literal name and a default (getattr(obj, "name", d)) may be
    either."""
    if not seen and id(func) in m.callees:
        return m.callees[id(func)]
    if id(func) in seen or len(seen) > 6:
        return set()
    inner = seen | {id(func)}
    names: set[str] = set()
    if isinstance(func, ast.Call):
        default = _getattr_default(m, func)
        if default is not None:
            names = {str(cast(ast.Constant, func.args[1]).value)} | _callee_names(m, default, inner)
    elif isinstance(func, ast.Attribute):
        names = {func.attr} | _attribute_aliases(m, func.attr)
    elif isinstance(func, ast.Name):
        names = {func.id}
        for value in m.bindings(func, func.id) or []:
            if isinstance(value, (ast.Name, ast.Attribute, ast.Call)):
                names |= _callee_names(m, value, inner)
    if not seen:
        m.callees[id(func)] = names
    return names


def _getattr_default(m: _Module, call: ast.Call) -> ast.expr | None:
    """The default of getattr(obj, "name", default), else None."""
    if (_callee(call) in m.index.getattrs and len(call.args) == 3 and _literal_name(call.args[1])
            and not call.keywords):
        return call.args[2]
    return None


def _attribute_aliases(m: _Module, attr: str) -> set[str]:
    """The names an attribute name is bound to anywhere (self.run =
    conn.execute, setattr(obj, "run", conn.execute), self.run = run_sql),
    one step, memoized by name."""
    cache = m.index.attribute_aliases
    if attr not in cache:
        found: set[str] = set()
        for value in m.index.globals.get(attr) or []:
            if isinstance(value, ast.Call):
                default = _getattr_default(m, value)
                if default is None:
                    continue
                found.add(str(cast(ast.Constant, value.args[1]).value))
                value = default
            if isinstance(value, ast.Attribute):
                found.add(value.attr)
            elif isinstance(value, ast.Name) and value.id != "<parameter>":
                found.add(value.id)
        cache[attr] = found - {attr}
    return cache[attr]


#: The modules whose attributes are the builtins below (builtins.exec,
#: operator.methodcaller).
_REFLECTION_MODULES = frozenset({"builtins", "operator"})


def _builtin(m: _Module, func: ast.AST) -> str | None:
    """Which builtin that reaches code by a name or text computed at run
    time a callee is (getattr, setattr, exec, eval, compile, attrgetter,
    methodcaller), by name or through a name bound to it, or as an
    attribute of builtins or operator; or any object's __getattribute__."""
    if isinstance(func, ast.Attribute):
        if func.attr == "__getattribute__":
            return func.attr
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id in _REFLECTION_MODULES and func.attr in _REFLECTION:
            return func.attr
        return None
    if not isinstance(func, ast.Name):
        return None
    names = {func.id}
    for value in m.bindings(func, func.id) or []:
        if isinstance(value, ast.Name):
            names.add(value.id)
        elif isinstance(value, ast.Attribute):
            names.add(_builtin(m, value) or "")
    hits = sorted(names & _REFLECTION)
    return hits[0] if hits else None


def _literal_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier()


def _verbs(m: _Module, call: ast.Call) -> set[str]:
    return _callee_names(m, call.func) or {_callee(call)}


#: The mutation verb a call is taken as when it may be several, the one
#: writing the most first (REPLACE nulls what it omits).
_VERB_ORDER = ("replace", "replace_map", "insert_or_update_map", "insert_map", "update_map", "insert_or_update",
               "insert", "update", "delete")


def _mutation_verb(m: _Module, call: ast.Call) -> str:
    names = _verbs(m, call)
    return next((verb for verb in _VERB_ORDER if verb in names), _callee(call))


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
#: Builtins that reach an attribute, or run code, by a name or text computed
#: at run time.
_FETCHERS = frozenset({"getattr", "attrgetter", "methodcaller", "__getattribute__"})
_EVALUATORS = frozenset({"exec", "eval", "compile"})
_REFLECTION = _FETCHERS | _EVALUATORS | {"setattr"}


def _python_findings(m: _Module) -> Iterator[_Finding]:
    found = list(_string_findings(m))
    marks = scope_marks(m)
    mentions = {outer for outer, (seen, _anchor) in marks.items() if "table" in seen}
    found += list(_statement_calls(m, mentions))
    found += list(_indirect_uses(m, mentions))
    found += list(_escape_findings(m))
    yield from found
    # The backstop, which does not depend on parsing: a scope holding the
    # table, a limit column and a write anywhere (in the functions, lambdas,
    # comprehensions and class bodies nested in it too) writes every limit
    # column.
    by_scope: dict[str, list[_Finding]] = {}
    for finding in found:
        if finding.style.startswith("key_limit."):
            by_scope.setdefault(m.outer(finding.node), []).append(finding)
    for outer, (seen, anchor) in marks.items():
        if seen >= _BACKSTOP:
            for existing in by_scope.get(outer, []):
                yield _Finding(existing.style, existing.node, True)
            if outer not in by_scope:
                yield _Finding("key_limit.scope", anchor, True)


_LIMIT_WORD = re.compile(r"\b(?:" + "|".join(sorted(LIMIT_COLUMNS)) + r")\b", re.IGNORECASE)
#: Names of mutation and DML APIs, as identifiers or attributes.
_WRITE_APIS = frozenset({"insert", "update", "insert_or_update", "replace", "upsert", "execute_update",
                         "batch_update", "execute_partitioned_dml", "execute_batch_dml", "insert_map", "update_map",
                         "insert_or_update_map", "replace_map"})
_BACKSTOP = frozenset({"table", "limit", "write"})


def scope_marks(m: _Module) -> dict[str, tuple[set[str], ast.AST]]:
    """Per scope: which of the table, a limit column and a write it holds
    anywhere, and a node in it. A scope is an outermost function with every
    function, lambda, comprehension and class body nested in it, or a module
    statement or class body outside any function (_Module.outer).

    The table: a string piece naming it, or a name or attribute holding it (a
    module constant such as KEY_LIMIT_TABLE). A limit column: a string piece
    naming one, an identifier or attribute spelled as one, or a name holding
    one. A write: a DML word in a string piece, an identifier or attribute
    spelled as a mutation or DML API (or Mutation...), getattr(obj, "name")
    read as obj.name (_Fetches). Docstrings aside."""
    marks: dict[str, tuple[set[str], ast.AST]] = {}

    def at(node: ast.AST) -> set[str]:
        key = m.outer(node)
        return marks.setdefault(key, (set(), m.outer_nodes.get(key, node)))[0]

    for node in ast.walk(m.tree):
        seen = at(node)
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
        seen = at(node)
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
    """Each call that runs a statement (an executor by name or alias, a
    helper that runs a statement it is given, or any call given SQL text
    naming the table): its statement is classified where it is whole; in a
    scope that mentions the table a part not resolved to literals fails
    closed, whatever is known around it; and a statement given to a helper
    that may change it before it runs fails closed in such a scope, or
    wherever it names the table."""
    for node in ast.walk(m.tree):
        if not isinstance(node, ast.Call):
            continue
        mentioned = m.outer(node) in mentions
        for statement, changed in _statements_run(m, node):
            texts = m.texts(statement)
            for text in texts:
                if classify(text) == "sql":
                    yield _Finding("key_limit.sql", node, changed or assigns_limit_column(text))
            if changed and (mentioned or any(names_table(text) for text in texts)):
                yield _Finding("key_limit.sql", node, True)
            elif mentioned and any(UNKNOWN in text for text in texts) and not _timeout_setting(statement):
                yield _Finding("key_limit.sql", node, True)


def _elements(node: ast.AST) -> list[ast.AST]:
    """A statement argument's statements: each of a list or tuple literal
    (the first of a nested tuple, a batch's (sql, params, types)), or itself."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [element.elts[0] if isinstance(element, (ast.List, ast.Tuple)) and element.elts else element
                for element in node.elts]
    return [node]


def _statements_run(m: _Module, call: ast.Call) -> list[tuple[ast.AST, bool]]:
    """The statements a call runs, each with whether it may be changed
    before it runs: an executor's (by name or alias), a statement-running
    helper's (_Index.sinks), and any argument that is SQL text naming the
    table."""
    statements: list[tuple[ast.AST, bool]] = []
    verbs = _verbs(m, call)
    if verbs & _EXECUTORS:
        first = _executed_statement(m, call)
        statements += [(statement, False)
                       for statement in (_elements(first) if first is not None else [ast.Name(id="<parameter>")])]
    for name in sorted(verbs - _EXECUTORS):
        for sink in m.sinks.get(name, ()):
            statements += [(statement, sink.changes) for argument in _sink_arguments(call, sink)
                           for statement in _elements(argument) if _may_be_text(m, statement)]
    run = {id(statement) for statement, _changed in statements}
    for argument in [*call.args, *(kw.value for kw in call.keywords)]:
        for element in _elements(argument):
            if id(element) in run:
                continue
            texts = m.texts(element)
            if any(names_table(text) and classify(text) != "name" and (
                    classify(text) == "sql" or _READ_WORD.search(text) or _SQL_CLAUSE.search(text))
                   for text in texts):
                statements.append((element, False))
    return statements


def _indirect_uses(m: _Module, mentions: set[str]) -> Iterator[_Finding]:
    """In a scope that mentions the table, each use of code by a name or
    text computed at run time, and each use of what may run a statement or
    write a mutation other than calling it, fails closed (every column):

    - getattr, attrgetter, methodcaller or __getattribute__ given a name that
      is not a literal (getattr(obj, "name") is read as obj.name), setattr
      given one, and attrgetter or methodcaller naming an executor, a
      mutation API or a statement-running helper;
    - exec, eval and compile;
    - an executor, a mutation API, a statement-running helper, or getattr,
      setattr, exec or eval itself, passed on, stored, returned or wrapped
      (functools.partial, a decorator applied to it) instead of called or
      bound to a name or attribute (whose calls are followed)."""
    targets = _INDIRECT_TARGETS | m.sinks.keys()
    for node in ast.walk(m.tree):
        if m.outer(node) not in mentions:
            continue
        if isinstance(node, ast.Call):
            builtin = _builtin(m, node.func)
            if builtin in _EVALUATORS:
                yield _Finding("key_limit.indirect", node, True)
            elif builtin is not None and _computed_name(builtin, node, targets):
                yield _Finding("key_limit.indirect", node, True)
        elif (isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
              and _refers_to(m, node, targets) and not _called_or_bound(m, node)):
            yield _Finding("key_limit.indirect", node, True)


#: What may run a statement or write a mutation when called.
_INDIRECT_TARGETS = _EXECUTORS | _WRITE_APIS | MUTATION_VERBS


def _computed_name(builtin: str, call: ast.Call, targets: Iterable[str]) -> bool:
    """Whether a getattr-like call reaches a name not known (or, for
    attrgetter and methodcaller, one that runs a statement or writes)."""
    if builtin in ("getattr", "setattr"):
        names = call.args[1:2]
    elif builtin == "__getattribute__":
        names = call.args[-1:]
    elif builtin == "methodcaller":
        names = call.args[:1]
    elif builtin == "attrgetter":
        names = call.args
    else:
        return False
    if not names or not all(isinstance(n, ast.Constant) and isinstance(n.value, str) for n in names):
        return True
    return builtin in ("attrgetter", "methodcaller") and any(
        str(cast(ast.Constant, n).value).rsplit(".", 1)[-1] in targets for n in names)


def _refers_to(m: _Module, node: ast.Name | ast.Attribute, targets: Iterable[str]) -> bool:
    """Whether a name or attribute is an executor, a mutation API, a
    statement-running helper, or getattr, setattr, exec or eval: by its own
    name (a name only when no variable of that name is bound), or through
    one it is bound to."""
    reach = set(targets) | _REFLECTION
    if isinstance(node, ast.Attribute):
        return bool(_callee_names(m, node) & reach)
    aliased = _callee_names(m, node) - {node.id}
    return bool(aliased & reach) or (node.id in reach and not m.bindings(node, node.id))


def _called_or_bound(m: _Module, node: ast.AST) -> bool:
    parent = m.parents.get(id(node))
    if isinstance(parent, ast.Call) and parent.func is node:
        return True
    if isinstance(parent, ast.Assign) and parent.value is node:
        return all(isinstance(target, (ast.Name, ast.Attribute)) for target in parent.targets)
    return (isinstance(parent, ast.AnnAssign) and parent.value is node
            and isinstance(parent.target, (ast.Name, ast.Attribute)))


# -- helpers that run a statement they are given ---------------------------


class _Sink(NamedTuple):
    """A helper that runs a statement it is given: where a caller passes it
    (a position among the arguments a caller gives, self or cls aside, or
    -1, and the parameter's name; rest for *args from that position or
    **kwargs), and whether it may change the statement before it runs."""

    position: int
    name: str
    changes: bool
    rest: bool = False


#: Decorators that do not wrap the function they are applied to.
_PLAIN_DECORATORS = frozenset({"staticmethod", "classmethod", "property", "cached_property", "abstractmethod",
                               "cache", "lru_cache", "overload", "override", "setter", "getter", "deleter"})


def _decorated(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name not in _PLAIN_DECORATORS:
            return True
    return False


def _parameters(m: _Module, function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, _Sink]:
    """Each parameter, as the place a caller passes it."""
    arguments = function.args
    positional = [a.arg for a in [*arguments.posonlyargs, *arguments.args]]
    if positional and isinstance(m.parents.get(id(function)), ast.ClassDef) and not any(
            getattr(d, "id", "") == "staticmethod" for d in function.decorator_list):
        positional = positional[1:]
    found = {name: _Sink(index, name, False) for index, name in enumerate(positional)}
    for argument in arguments.kwonlyargs:
        found[argument.arg] = _Sink(-1, argument.arg, False)
    if arguments.vararg:
        found[arguments.vararg.arg] = _Sink(len(positional), arguments.vararg.arg, False, rest=True)
    if arguments.kwarg:
        found[arguments.kwarg.arg] = _Sink(-1, arguments.kwarg.arg, False, rest=True)
    return found


def _sink_arguments(call: ast.Call, sink: _Sink) -> list[ast.AST]:
    """The arguments a call gives a helper where it takes its statement."""
    if sink.rest:
        if sink.position >= 0:
            return list(call.args[sink.position:])
        return [kw.value for kw in call.keywords]
    for keyword in call.keywords:
        if keyword.arg == sink.name:
            return [keyword.value]
    if 0 <= sink.position < len(call.args) and not any(
            isinstance(a, ast.Starred) for a in call.args[:sink.position + 1]):
        return [call.args[sink.position]]
    if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
        return [ast.Name(id="<parameter>")]  # given through a spread: not known
    return []


class _Calls(NamedTuple):
    function: ast.FunctionDef | ast.AsyncFunctionDef
    module: _Module
    calls: list[tuple[ast.Call, set[str], bool]]


def _computed_call(m: _Module, call: ast.Call) -> list[ast.AST] | None:
    """The arguments of a call whose callee is reached by a name not known
    (getattr(obj, name)(...), methodcaller(name, ...)(obj)), else None."""
    func = call.func
    if isinstance(func, ast.Call):
        builtin = _builtin(m, func.func)
        if builtin == "methodcaller":
            return [*func.args[1:], *(kw.value for kw in func.keywords)]
        if builtin in _FETCHERS:
            return [*call.args, *(kw.value for kw in call.keywords)]
    return None


def derived_sinks(m: _Module) -> dict[str, list[_Sink]]:
    """The module's helpers that run a statement they are given, by name (a
    function or method of the module, a dunder aside), for its own calls:
    one whose parameter reaches, through the names it uses, the statement
    an executor or another such helper runs, or an argument of a call by a
    computed name, directly or in a function nested in it. It may change
    the statement when the statement is not the parameter itself, it is
    decorated, the helper it gives it to may, or the call is by a computed
    name; one that holds exec, eval or compile may run any parameter,
    changed. (A helper in another module is outside the threat model.)"""
    functions: list[_Calls] = []
    for node in ast.walk(m.tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("__")
                and _parameters(m, node)):
            calls = [(call, _verbs(m, call),
                      _builtin(m, call.func) in _EVALUATORS or _computed_call(m, call) is not None)
                     for call in ast.walk(node) if isinstance(call, ast.Call)]
            if calls:
                functions.append(_Calls(node, m, calls))
    sinks: dict[str, list[_Sink]] = {}
    for _round in range(8):
        reach = sinks.keys() | _EXECUTORS
        changed = False
        for entry in functions:
            if not any(special or verbs & reach for _call, verbs, special in entry.calls):
                continue
            found = _sink_entries(entry, sinks)
            known = set(sinks.get(entry.function.name, ()))
            if not found <= known:
                sinks[entry.function.name] = sorted(known | found)
                changed = True
        if not changed:
            break
    return sinks


def _sink_entries(entry: _Calls, sinks: dict[str, list[_Sink]]) -> set[_Sink]:
    m, function = entry.module, entry.function
    parameters = _parameters(m, function)
    decorated = _decorated(function)
    found: set[_Sink] = set()
    for call, verbs, special in entry.calls:
        if special and _builtin(m, call.func) in _EVALUATORS:
            found |= {sink._replace(changes=True) for sink in parameters.values()}
            continue
        run: list[tuple[ast.AST, bool]] = []
        if verbs & _EXECUTORS:
            first = _executed_statement(m, call)
            run += [(statement, False) for statement in (_elements(first) if first is not None else [])]
        for name in sorted(verbs - _EXECUTORS):
            for sink in sinks.get(name, ()):
                run += [(statement, sink.changes) for argument in _sink_arguments(call, sink)
                        for statement in _elements(argument)]
        if special:
            run += [(argument, True) for argument in _computed_call(m, call) or []]
        for statement, changes in run:
            for name in _parameters_reached(m, statement, parameters):
                direct = isinstance(statement, ast.Name) and statement.id == name and all(
                    _is_parameter(value) for value in m.bindings(statement, name) or [])
                found.add(parameters[name]._replace(changes=changes or decorated or not direct))
    return found


def _is_parameter(value: ast.AST) -> bool:
    return isinstance(value, ast.Name) and value.id == "<parameter>"


def _parameters_reached(m: _Module, expression: ast.AST, parameters: dict[str, _Sink]) -> set[str]:
    """The parameters a statement's text is built from: the statement
    itself, a piece of a concatenation, f-string, % or .format, a string
    method's receiver or argument, a branch of a conditional, an argument
    of a call that builds it, through the names it uses and what they are
    bound to (not a condition, a comparison or an attribute's object)."""
    reached: set[str] = set()
    visited: set[int] = set()
    queue = [expression]
    while queue and len(visited) < 256:
        node = queue.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        if isinstance(node, ast.Name):
            bound = m.bindings(node, node.id) or []
            if node.id in parameters and any(_is_parameter(value) for value in bound):
                reached.add(node.id)
            queue += [value for value in bound if not _is_parameter(value)]
        elif isinstance(node, ast.JoinedStr):
            queue += [part.value for part in node.values if isinstance(part, ast.FormattedValue)]
        elif isinstance(node, ast.BinOp):
            queue += [node.left, node.right]
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            queue += node.elts
        elif isinstance(node, ast.Dict):
            queue += node.values
        elif isinstance(node, ast.IfExp):
            queue += [node.body, node.orelse]
        elif isinstance(node, ast.BoolOp):
            queue += node.values
        elif isinstance(node, (ast.Starred, ast.FormattedValue)):
            queue.append(node.value)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in _STRING_METHODS:
                queue.append(node.func.value)
            queue += [*node.args, *(keyword.value for keyword in node.keywords)]
    return reached


def _may_be_text(m: _Module, node: ast.AST, depth: int = 0) -> bool:
    """Whether an argument may carry statement text: anything but a
    non-string literal, a comparison or a negation (or a name bound only to
    those)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, bytes))
    if isinstance(node, (ast.Compare, ast.UnaryOp)):
        return False
    if isinstance(node, ast.Name) and depth < 4:
        bound = m.bindings(node, node.id)
        return not bound or any(_is_parameter(value) or _may_be_text(m, value, depth + 1) for value in bound)
    return True


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


# -- the escape rule ---------------------------------------------------------
#
# A value whose rendering may name the table (a string constant, f-string,
# concatenation or format naming it, a name bound to one, or a container
# literal holding one) may leave its scope only by a recognised executor or
# mutation call (judged by the rules above), or by a followed helper of the
# module, through a parameter shown to reach only executors. Every other
# route fails closed (key_limit.escape, every column). The routes are an
# allow list: a consumer not listed, of any shape, is an escape.

#: Builtins whose result holds no text of their argument.
_ABSORBING_BUILTINS = frozenset({"len", "bool", "isinstance", "issubclass", "hash", "id", "type", "callable", "print"})
#: Builtins whose result is their argument's text, judged in turn.
_TEXT_BUILTINS = frozenset({"str", "repr", "format", "list", "tuple", "set", "frozenset", "sorted", "reversed"})
#: str methods whose result holds no text of the string.
_STR_PREDICATES = frozenset({"startswith", "endswith", "count", "find", "rfind", "index", "rindex", "isidentifier",
                             "isalnum", "isalpha", "isascii", "isdecimal", "isdigit", "islower", "isnumeric",
                             "isprintable", "isspace", "istitle", "isupper"})
#: str methods whose result is the string's text, judged in turn.
_STR_TEXT_METHODS = frozenset(name for name in dir(str) if not name.startswith("_")) - _STR_PREDICATES
#: Methods that put a value into the local container they are called on.
_CONTAINER_MUTATORS = frozenset({"append", "extend", "insert", "add", "update", "setdefault"})

TaintKey = tuple[int, str]
_STOP: tuple[str, ast.AST | None] = ("stop", None)
_ESCAPE: tuple[str, ast.AST | None] = ("escape", None)


def _carries_table(texts: Iterable[str]) -> bool:
    return any(names_table(text.replace(UNKNOWN, " ")) for text in texts)


def _binding_scope(m: _Module, node: ast.AST, name: str) -> int:
    """The function whose local a name is where it is used, or 0 for a
    module or class name."""
    for function in reversed(m.functions.get(id(node), [])):
        if name in m._scope_locals(function):
            return id(function)
    return 0


def _declared_global(m: _Module, node: ast.AST, name: str) -> bool:
    functions = m.functions.get(id(node), [])
    return bool(functions) and any(isinstance(sub, (ast.Global, ast.Nonlocal)) and name in sub.names
                                   for sub in ast.walk(functions[-1]))


def _route(m: _Module, node: ast.AST, clean: set[tuple[str, _Sink]], binds: list[TaintKey]) -> ast.AST | None:
    """Follow a value naming the table up through what builds on it (a
    concatenation, an f-string, a format, a str method, a container, a
    subscript, a conditional) to where it goes: a name it is bound to (added
    to binds, its uses judged in turn), an allowed consumer, or an escape
    (the node returned)."""
    while True:
        parent = m.parents.get(id(node))
        if parent is None:
            return None
        verdict, after = _step(m, node, parent, clean, binds)
        if verdict == "up":
            node = after or parent
            continue
        return node if verdict == "escape" else None


def _bind(m: _Module, target: ast.expr, binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    if not isinstance(target, ast.Name) or _declared_global(m, target, target.id):
        return _ESCAPE  # unpacked, stored in an attribute or subscript, or a global
    binds.append((_binding_scope(m, target, target.id), target.id))
    return _STOP


def _bind_container(m: _Module, target: ast.expr, binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """A value put into the container a name holds. The container must be
    made in this function (a list, dict or set display, comprehension, or
    list(), dict() or set()), directly or through names bound to one: then
    that name and every name it was bound to are followed (b = a;
    b.append(value) changes a too). A container from anywhere else (a
    parameter, a module or class name, a loop, an attribute, a call) is
    shared with other scopes: an escape."""
    if not isinstance(target, ast.Name):
        return _ESCAPE
    keys: list[TaintKey] = []
    queue: list[ast.Name] = [target]
    while queue and len(keys) < 64:
        alias = queue.pop()
        key = (_binding_scope(m, alias, alias.id), alias.id)
        if key in keys:
            continue
        if key[0] == 0 or _declared_global(m, alias, alias.id):
            return _ESCAPE
        keys.append(key)
        for value in m.bindings(alias, alias.id) or [ast.Name(id="<parameter>")]:
            if isinstance(value, ast.BinOp) and getattr(value, "augmented", False):
                value = value.left  # x += ...: x's value before
            if isinstance(value, ast.Name) and not _is_parameter(value):
                queue.append(value)
            elif not (isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)) or (
                    isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                    and value.func.id in ("list", "dict", "set") and not m.bindings(value.func, value.func.id))):
                return _ESCAPE
    binds.extend(keys)
    return _STOP


def _bind_augmented(m: _Module, target: ast.expr, binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """x += value: a string is rebound (x is followed, as for x = ...); any
    other value is changed in place, as a container is."""
    if isinstance(target, ast.Name):
        bases = [value.left if isinstance(value, ast.BinOp) and getattr(value, "augmented", False) else value
                 for value in m.bindings(target, target.id) or []]
        if bases and all(isinstance(value, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes))) for value in bases):
            return _bind(m, target, binds)
    return _bind_container(m, target, binds)


def _step(m: _Module, node: ast.AST, parent: ast.AST, clean: set[tuple[str, _Sink]],
          binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """One step of a route: ("up", next) when the next node holds the
    value's text, ("stop", None) for an allowed consumer, a binding or a use
    that keeps none of the text, else ("escape", None)."""
    if isinstance(parent, (ast.BinOp, ast.JoinedStr, ast.FormattedValue, ast.BoolOp, ast.Starred, ast.ListComp,
                           ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        return ("up", parent)
    if isinstance(parent, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        return ("up", parent) if isinstance(getattr(parent, "ctx", ast.Load()), ast.Load) else _ESCAPE
    if isinstance(parent, ast.IfExp):
        return _STOP if node is parent.test else ("up", parent)
    if isinstance(parent, ast.comprehension):
        return _bind(m, parent.target, binds) if node is parent.iter else _STOP
    if isinstance(parent, ast.Subscript):
        if node is parent.value:
            return ("up", parent) if isinstance(parent.ctx, ast.Load) else _STOP
        return _STOP if isinstance(parent.ctx, ast.Load) else _ESCAPE
    if isinstance(parent, (ast.Compare, ast.Expr)) or (
            isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not)):
        return _STOP
    if isinstance(parent, (ast.If, ast.While, ast.Assert)) and node is parent.test:
        return _STOP  # (an assert's message is raised with it: an escape)
    if isinstance(parent, ast.NamedExpr):
        verdict = _bind(m, parent.target, binds)
        return ("up", parent) if verdict == _STOP else verdict
    if isinstance(parent, ast.AugAssign) and node is parent.value:
        return _bind_augmented(m, parent.target, binds)
    if isinstance(parent, (ast.Assign, ast.AnnAssign)) and node is parent.value:
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        verdicts = [_bind(m, target, binds) for target in targets]
        return _ESCAPE if _ESCAPE in verdicts else _STOP
    if isinstance(parent, (ast.For, ast.AsyncFor)) and node is parent.iter:
        return _bind(m, parent.target, binds)
    if isinstance(parent, ast.Attribute) and node is parent.value:
        call = m.parents.get(id(parent))
        if isinstance(call, ast.Call) and call.func is parent:
            if parent.attr in _STR_PREDICATES or parent.attr in _CONTAINER_MUTATORS:
                return _STOP  # a bool, or a value put into this container (judged on its own)
            if parent.attr in _STR_TEXT_METHODS:
                return ("up", call)  # the method's result is the text
        return _ESCAPE
    if isinstance(parent, ast.keyword):
        call = m.parents.get(id(parent))
        return _argument_step(m, node, call, clean, binds) if isinstance(call, ast.Call) else _ESCAPE
    if isinstance(parent, ast.Call):
        # A value called is not text (a str is not callable); an argument is.
        return _STOP if node is parent.func else _argument_step(m, node, parent, clean, binds)
    return _ESCAPE


def _argument_step(m: _Module, node: ast.AST, call: ast.Call, clean: set[tuple[str, _Sink]],
                   binds: list[TaintKey]) -> tuple[str, ast.AST | None]:
    """A value given to a call, positionally, by keyword or spread."""
    verbs = _verbs(m, call)
    if verbs & _EXECUTORS or _mutation_parts(m, call) is not None:
        return _STOP  # judged by the executor and mutation rules
    for name in sorted(verbs & m.sinks.keys()):
        for sink in m.sinks[name]:
            if (name, sink) in clean and any(given is node for given in _sink_arguments(call, sink)):
                return _STOP  # a followed helper of the module that only runs it
    func = call.func
    if isinstance(func, ast.Name) and not m.bindings(func, func.id):
        if func.id in _ABSORBING_BUILTINS:
            return _STOP
        if func.id in _TEXT_BUILTINS:
            return ("up", call)
    if isinstance(func, ast.Attribute) and func.attr in _STR_TEXT_METHODS and _is_text(m, func.value):
        return ("up", call)  # sep.join(...), template.format(...), text.replace(..., value)
    if isinstance(func, ast.Attribute) and func.attr in _CONTAINER_MUTATORS and isinstance(func.value, ast.Name):
        return _bind_container(m, func.value, binds)  # into a local container, whose uses are judged in turn
    return _ESCAPE


def _is_text(m: _Module, node: ast.AST) -> bool:
    """Whether a method's receiver is a string (a literal, or a name bound
    only to strings), so its .join, .format or .replace builds text."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        bound = m.bindings(node, node.id) or []
        return bool(bound) and all(isinstance(value, (ast.Constant, ast.JoinedStr)) and (
            not isinstance(value, ast.Constant) or isinstance(value.value, str)) for value in bound)
    return False


def _escape_routes(m: _Module, seeds: Iterable[ast.AST], tainted: Iterable[TaintKey],
                   clean: set[tuple[str, _Sink]]) -> list[ast.AST]:
    """Every escape of the given values and of the names given, following
    each name a value is bound to (or put into) to its uses."""
    escapes: list[ast.AST] = []
    done: set[int] = set()
    names: set[TaintKey] = set()
    queue = list(seeds)
    for key in tainted:
        names.add(key)
        queue += _uses(m, key)
    while queue:
        node = queue.pop()
        if id(node) in done:
            continue
        done.add(id(node))
        binds: list[TaintKey] = []
        escape = _route(m, node, clean, binds)
        if escape is not None:
            escapes.append(escape)
        for key in binds:
            if key not in names:
                names.add(key)
                queue += _uses(m, key)
    return escapes


def _uses(m: _Module, key: TaintKey) -> list[ast.AST]:
    scope, name = key
    return [use for use in m.loads.get(name, []) if _binding_scope(m, use, name) == scope]


def _table_values(m: _Module) -> list[ast.AST]:
    """The values whose rendering may name the table: string expressions
    (docstrings aside), and names and attributes resolved to such text or
    bound to a list, tuple, dict or set holding it (in any module)."""
    seeds: list[ast.AST] = [node for node in _top_level_strings(m) if _carries_table(m.texts(node))]
    for node in ast.walk(m.tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            resolved = m.values(node)
            bound = (m.bindings(node, node.id) if isinstance(node, ast.Name) else m.index.globals.get(node.attr)) or []
            if (resolved and _carries_table(resolved)) or any(_holds_table(m, value) for value in bound):
                seeds.append(node)
    return seeds


def _holds_table(m: _Module, value: ast.AST) -> bool:
    return isinstance(value, (ast.Tuple, ast.List, ast.Set, ast.Dict)) and any(
        _carries_table(m.texts(sub)) for sub in ast.walk(value)
        if isinstance(sub, ast.JoinedStr) or (isinstance(sub, ast.Constant) and isinstance(sub.value, str)))


def clean_sinks(m: _Module) -> set[tuple[str, _Sink]]:
    """The module's helper parameters shown to reach only executors: every
    use of the parameter, and of each name it is bound to or put into, goes
    to an executor or mutation call, or to another such parameter, or keeps
    none of its text."""
    entries = [(name, sink, function) for name, sinks in m.sinks.items() for sink in sinks
               for function in ast.walk(m.tree)
               if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and function.name == name]
    clean = {(name, sink) for name, sink, _function in entries}
    changed = True
    while changed:
        changed = False
        for name, sink, function in entries:
            if (name, sink) in clean and _escape_routes(m, [], [(id(function), sink.name)], clean):
                clean.discard((name, sink))
                changed = True
    return clean


def _escape_findings(m: _Module) -> Iterator[_Finding]:
    m.clean = clean_sinks(m)
    for node in _escape_routes(m, _table_values(m), [], m.clean):
        yield _Finding("key_limit.escape", node, True)


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


#: Line breaks other than a line feed: a reader that ends a line at one sees
#: what follows it as a line of its own.
_LINE_BREAKS = "\r\v\f\x1c\x1d\x1e\x85  "
_HEREDOC = re.compile(r"""(?<!<)<<(?!<)-?\s*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([A-Za-z_]\w*))""")


def _strip_comments(path: Path, text: str) -> str:
    """Drop comment lines, but none that may not be one: in a shell, YAML or
    Terraform file a line in a heredoc's body (data, whatever it starts
    with, the delimiter quoted or not: psql reads "# 0; UPDATE ..." as an
    operator and a statement) or inside a string a line before left open,
    a line after one ending in a backslash (the shell continues that line),
    and a line holding another line break (a reader that ends the line there
    sees text after it)."""
    markers = {".go": ("//",), ".sql": ("--",), ".tf": ("#", "//")}.get(path.suffix, ("#",))
    shell = path.suffix not in (".sql", ".go")
    kept = []
    delimiters: list[str] = []
    quote: str | None = None
    continued = False
    for line in text.split("\n"):
        body = line.removesuffix("\r")
        if delimiters:
            if body.strip() == delimiters[0]:
                delimiters.pop(0)
            kept.append(line)
        elif quote or continued or not body.lstrip().startswith(markers) or any(c in body for c in _LINE_BREAKS):
            kept.append(line)
            if shell:
                if quote is None:
                    delimiters += [next(group for group in match.groups() if group)
                                   for match in _HEREDOC.finditer(body)]
                quote = _open_quote(body, quote)
        continued = body.endswith("\\")
    return "\n".join(kept)


def _open_quote(line: str, quote: str | None) -> str | None:
    """The quote a shell line leaves open, from the one it starts in (a
    comment ends the line; a backslash escapes outside single quotes)."""
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            quote = None if char == "'" else quote
        elif quote == '"':
            if char == "\\":
                index += 1
            elif char == '"':
                quote = None
        elif char == "\\":
            index += 1
        elif char == "#" and (index == 0 or line[index - 1] in " \t;|&("):
            break
        elif char in "'\"":
            quote = char
        index += 1
    return quote


def _dml_near_table(text: str) -> bool:
    """Whether a DML word is followed by the table before a ``;`` and within
    sixty words, whitespace collapsed (so blank lines, padding and
    indentation in a heredoc or a multiline string do not separate them); a
    lock's FOR UPDATE aside."""
    collapsed = " ".join(_LOCK.sub(" ", text).split())
    for match in _DML_WORD.finditer(collapsed):
        region = " ".join(collapsed[match.end():].split(" ")[:61])
        if names_table(region.split(";", 1)[0]):
            return True
        # A comment may hold the ; that seemed to end the statement.
        if names_table(region) and any(mark in region for mark in ("/*", "*/", "--", "#")):
            return True
    return False


def _readings(path: Path) -> list[str]:
    """A text file's comments stripped, read as written and with every
    shell line continuation joined (as is, and with the next line's
    indentation dropped, as a YAML block scalar drops it before the shell
    sees the script): a shell joins them on a command line and in a heredoc
    whose delimiter is not quoted, and passes them on in one whose delimiter
    is. A finding of any reading is one."""
    text = _strip_comments(path, path.read_text(encoding="utf-8", errors="replace"))
    return list(dict.fromkeys([text, re.sub(r"\\\r?\n", "", text), re.sub(r"\\\r?\n[ \t]*", "", text)]))


def _text_findings(path: Path) -> Iterator[tuple[str, bool]]:
    for text in _readings(path):
        yield from _reading_findings(path, text)


def _reading_findings(path: Path, text: str) -> Iterator[tuple[str, bool]]:
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
        if _LIMIT_WORD.search(text) and _DML_WORD.search(_LOCK.sub(" ", text)):
            limit = True
        yield "key_limit.text", limit
    if _TEXT_DYNAMIC.search(text):
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


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)


def _scope_fingerprint(m: _Module, where: str) -> str:
    """The source of the scope a finding is judged by (its outermost
    function, or its module statement or class body), positions aside (the
    SQL manifest's fingerprint, stable across Python versions): every
    definition of that qualified name in the module (a conditional or
    repeated def, or a class's duplicate method, included) and every other
    statement binding its name where it is defined (an assignment, import,
    loop target or del, or a function declaring it global), in source order.
    A module statement outside any definition: the whole module."""
    from tests.conformance.spanner_sql_inventory import fingerprint

    outer = m.outer_of.get(where, where)
    if outer == "<module>":
        return fingerprint(m.tree)
    statements: dict[int, ast.AST] = {
        id(node): node for node in ast.walk(m.tree)
        if isinstance(node, _DEFINITIONS) and m.names.get(id(node)) == outer
        and m.names.get(id(m.parents.get(id(node)))) != outer}
    if not statements:
        return fingerprint(m.tree)
    for node in _rebindings(m, outer):
        statements.setdefault(id(node), node)
    ordered = sorted(statements.values(), key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)))
    return _fingerprint("\n".join(fingerprint(node) for node in ordered))


def _rebindings(m: _Module, outer: str) -> list[ast.AST]:
    """Each statement binding a definition's name in the module or class body
    it is defined in."""
    if outer.startswith("<module>."):
        namespace, bare = "", outer.split(".", 1)[1]
    else:
        namespace, _, bare = outer.rpartition(".")
    found = []
    for node in ast.walk(m.tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            binds = node.id == bare
        elif isinstance(node, ast.alias):
            binds = (node.asname or node.name.split(".", 1)[0]) == bare
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binds = node.name == bare
        else:
            binds = False
        if binds and _namespace(m, node, bare) == namespace:
            statement: ast.AST = node
            while not isinstance(statement, ast.stmt) and id(statement) in m.parents:
                statement = m.parents[id(statement)]
            found.append(statement)
    return found


def _namespace(m: _Module, node: ast.AST, bare: str) -> str | None:
    """The module ("") or class body (its qualified name) a binding is in,
    or None for a function's local."""
    parent = m.parents.get(id(node))
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            declared = any(isinstance(sub, ast.Global) and bare in sub.names for sub in ast.walk(parent))
            return "" if declared else None
        if isinstance(parent, ast.ClassDef):
            return m.names.get(id(parent), "")
        parent = m.parents.get(id(parent))
    return ""


def _getattr_aliases(trees: list[ast.Module]) -> set[str]:
    """getattr and every name bound to it, in any module (ga = getattr,
    from builtins import getattr as ga)."""
    edges: list[tuple[str, str]] = []
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, (ast.Name, ast.Attribute)):
                source = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                edges += [(target.id, source) for target in targets if isinstance(target, ast.Name)]
            elif isinstance(node, ast.alias) and node.asname:
                edges.append((node.asname, node.name.rsplit(".", 1)[-1]))
    aliases = {"getattr"}
    grown = True
    while grown:
        grown = False
        for alias, source in edges:
            if source in aliases and alias not in aliases:
                aliases.add(alias)
                grown = True
    return aliases


class _Fetches(ast.NodeTransformer):
    """Reads getattr(obj, "name") (through any alias of getattr) and
    obj.__getattribute__("name") as obj.name before any rule runs, so every
    rule treats them exactly as the attribute."""

    def __init__(self, aliases: set[str]) -> None:
        self.aliases = aliases

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        target: ast.expr | None = None
        attribute: ast.expr | None = None
        if name in self.aliases and len(node.args) == 2 and not node.keywords:
            target, attribute = node.args
        elif name == "__getattribute__" and isinstance(func, ast.Attribute) and not node.keywords:
            if len(node.args) == 1:
                target, attribute = func.value, node.args[0]
            elif len(node.args) == 2:
                target, attribute = node.args
        if target is not None and attribute is not None and _literal_name(attribute):
            fetched = ast.Attribute(value=target, attr=str(cast(ast.Constant, attribute).value), ctx=ast.Load())
            return ast.copy_location(fetched, node)
        return node


def scan_paths_detail(python: Iterable[Path], text: Iterable[Path]) -> ScanDetail:
    """Every finding, with whether its scope may write a limit column."""
    python = list(python)
    sources = [p.read_text(encoding="utf-8") for p in python]
    trees = [ast.parse(source) for source in sources]
    getattrs = _getattr_aliases(trees)
    fetches = _Fetches(getattrs)
    trees = [cast(ast.Module, fetches.visit(tree)) for tree in trees]
    index = _Index(trees)
    index.getattrs = getattrs
    modules = [_Module(path, tree, index) for path, tree in zip(python, trees, strict=True)]
    for m in modules:
        m.sinks = derived_sinks(m)
    found = ScanDetail()
    found.fingerprints = {}
    for m, source in zip(modules, sources, strict=True):
        written: _Module | None = None  # the module as written, for fingerprints
        for finding in _python_findings(m):
            where = m.where(finding.node)
            key = (finding.style, m.relative, where)
            found[key] = found.get(key, False) or finding.limit
            if (m.relative, where) not in found.fingerprints:
                written = written or _Module(m.path, ast.parse(source), index)
                found.fingerprints[(m.relative, where)] = _scope_fingerprint(written, where)
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
