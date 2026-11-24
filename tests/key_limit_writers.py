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
column: a mutation or payload whose columns include one or are not known, or
DML assigning one or a column filled in at run time.

api_key entity writers are out of scope by design: whatever writes an entity,
the mirror changes only through the guarded functions above.
"""

from __future__ import annotations

import ast
import itertools
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TABLE = "tr_key_limit"
LIMIT_COLUMNS = frozenset({"limit_micro", "day_limit_micro", "week_limit_micro", "month_limit_micro"})

_TOKEN = re.compile(r"(?<![A-Za-z0-9_])tr_key_limit(?![A-Za-z0-9_])", re.IGNORECASE)
_LOCK = re.compile(r"\bfor\s+update\b", re.IGNORECASE)
_DML_WORD = re.compile(r"\b(?:insert|update|delete|merge|upsert|replace)\b", re.IGNORECASE)
_READ_WORD = re.compile(r"\bselect\b|\b(?:create|alter|drop)\s+(?:table|index|unique|null_filtered)\b",
                        re.IGNORECASE)
_DML = (r"(?:insert\s+or\s+update(?:\s+into)?|insert\s+or\s+ignore(?:\s+into)?|insert\s+into|insert|"
        r"update|merge(?:\s+into)?|upsert(?:\s+into)?|replace\s+into|delete\s+from|delete)")
_HINT = r"(?:@\{[^}]*\}\s*)?"
_PLACEHOLDER = r"""["'`]?(?:\{[^}]*\}|%s|%\(\w+\)s|\$\{?\w+\}?)["'`]?"""
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
# The table as a statement names it: quoted or not, schema-qualified or not.
_TABLE_REF = r"""(?:[`"']?\w+[`"']?\s*\.\s*)*[`"']?tr_key_limit[`"']?(?![A-Za-z0-9_])"""
_STATEMENT_ON_TABLE = re.compile(r"\b(?P<verb>" + _DML + r")\s+" + _HINT + _TABLE_REF, re.IGNORECASE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CLAUSE_END = re.compile(r"(?:where|returning|then\s+return|from|on\s+conflict)\b", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_GO_MUTATION = re.compile(r"spanner\.(?:Insert|Update|InsertOrUpdate|Replace|Delete)\w*\(\s*([^,\s)]+)")

MUTATION_VERBS = frozenset({"insert_or_update", "insert_or_update_map", "insert_map", "update_map", "replace_map"})
AMBIGUOUS_VERBS = {"insert": 3, "update": 3, "replace": 3, "delete": 2}
MUTATION_KEYWORDS = frozenset({"table", "columns", "values", "keyset"})
MAX_RENDERINGS = 32

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


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    """``text`` split at ``separator`` outside brackets and quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    for char in text:
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"`":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _until_close(text: str) -> str:
    """``text`` up to the bracket that closes the one before it."""
    depth = 0
    quote = ""
    for i, char in enumerate(text):
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return text[:i]
            depth -= 1
    return text


def _clause(text: str) -> str:
    """A SET list: ``text`` up to WHERE, RETURNING, THEN RETURN, FROM, ON
    CONFLICT or ``;`` outside brackets and quotes (a subquery's FROM is
    inside its brackets)."""
    depth = 0
    quote = ""
    for i, char in enumerate(text):
        if quote:
            quote = "" if char == quote else quote
            continue
        if char in "'\"`":
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif depth == 0 and (char == ";" or (
                (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")) and _CLAUSE_END.match(text, i))):
            return text[:i]
    return text


def _name(text: str) -> str | None:
    """A column as written, qualified or quoted, as its bare lower-case
    name; None when it is not one (filled in at run time, an expression)."""
    name = text.strip().split(".")[-1].strip().strip("'\"`").lower()
    return name if _IDENTIFIER.fullmatch(name) else None


def _assignment_targets(clause: str) -> set[str] | None:
    """The columns a SET list assigns, ``(a, b) = (...)`` included; None
    when one is not a column name."""
    targets: set[str] = set()
    for assignment in _split_top_level(clause):
        if not assignment.strip():
            continue
        sides = _split_top_level(assignment, "=")
        if len(sides) < 2:
            return None
        left = sides[0].strip()
        names = _split_top_level(left[1:-1]) if left.startswith("(") and left.endswith(")") else [left]
        for written in names:
            name = _name(written)
            if name is None:
                return None
            targets.add(name)
    return targets or None


def _statement_columns(verb: str, rest: str) -> set[str] | None:
    """The columns one statement on the table writes (``rest`` follows the
    table name); None when they are not known, which counts as every one."""
    verb = verb.lower()
    if verb.startswith("delete"):
        return set()
    if verb.startswith("merge"):
        return None
    if verb == "update":
        head = re.match(r"\s*(?:(?:as\s+)?(?!set\b)\w+\s+)?set\b", rest, re.IGNORECASE)
        return _assignment_targets(_clause(rest[head.end():])) if head else None
    head = re.match(r"\s*(?:(?:as\s+)?(?!values\b|select\b|default\b|with\b)\w+\s*)?\(", rest, re.IGNORECASE)
    if head is None:
        return None  # no column list: every column
    listed = _until_close(rest[head.end():])
    names = [_name(column) for column in _split_top_level(listed)]
    if any(name is None for name in names):
        return None
    targets = {name for name in names if name is not None}
    statement = rest[head.end() + len(listed):].split(";", 1)[0]
    for update in re.finditer(r"\bdo\s+update\s+set\b", statement, re.IGNORECASE):
        more = _assignment_targets(_clause(statement[update.end():]))
        if more is None:
            return None
        targets |= more
    return targets


def statements_on_table(text: str) -> list[tuple[int, int, set[str] | None]]:
    """Each statement on the table in ``text``: (start, end, written columns)."""
    found = []
    for match in _STATEMENT_ON_TABLE.finditer(text):
        end = text.find(";", match.end())
        end = len(text) if end < 0 else end
        found.append((match.start(), end, _statement_columns(match.group("verb"), text[match.end():end])))
    return found


def assigns_limit_column(text: str) -> bool:
    """Whether DML naming the table may write a limit column: one it
    assigns or lists, or columns it does not name (an INSERT without a
    column list, a target filled in at run time, a MERGE). DML and the
    table in one string with no statement on the table this can read
    counts too."""
    if classify(text) != "sql":
        return False
    statements = statements_on_table(_BLOCK_COMMENT.sub(" ", text))
    if not statements:
        return True
    return any(columns is None or columns & LIMIT_COLUMNS for _start, _end, columns in statements)


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
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
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
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
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
            for node in ast.walk(function):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value is not None:
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
            return None if any("{}" in text for text in texts) else texts
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
        """Renderings of a string expression, with ``{}`` for each part that
        is not known (at most MAX_RENDERINGS)."""

        def part(sub: ast.AST) -> list[str]:
            resolved = self.values(sub, seen)
            if not resolved or len(resolved) > MAX_RENDERINGS:
                return ["{}"]
            return resolved

        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            return self.values(node, seen) or [""]
        if isinstance(node, ast.JoinedStr):
            return _combine([part(v.value) if isinstance(v, ast.FormattedValue) else self.texts(v, seen)
                             for v in node.values])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _combine([self.texts(node.left, seen), self.texts(node.right, seen)])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            arguments = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
            rendered = [
                _percent(template, list(choice))
                for template in self.texts(node.left, seen)
                for choice in itertools.islice(itertools.product(*(part(a) for a in arguments)), MAX_RENDERINGS)
            ]
            return rendered[:MAX_RENDERINGS] or ["{}"]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method == "format":
                positional = [part(a) for a in node.args]
                named = {kw.arg: part(kw.value) for kw in node.keywords if kw.arg}
                rendered = []
                for template in self.texts(node.func.value, seen):
                    for choice in itertools.islice(itertools.product(*positional), MAX_RENDERINGS):
                        rendered.append(_brace(template, list(choice), {k: v[0] for k, v in named.items()}))
                return rendered[:MAX_RENDERINGS] or ["{}"]
            if method in ("upper", "lower", "strip", "lstrip", "rstrip"):
                return [getattr(text, method)() for text in self.texts(node.func.value, seen)]
        resolved = self.values(node, seen)
        return resolved if resolved else ["{}"]


_STRING_METHODS = frozenset({"format", "upper", "lower", "strip", "lstrip", "rstrip"})


def _brace(template: str, positional: list[str], named: dict[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        field = match.group(1)
        if field == "":
            return positional.pop(0) if positional else "{}"
        if field.isdigit():
            index = int(field)
            return positional[index] if index < len(positional) else "{}"
        return named.get(field, "{}")

    return re.sub(r"\{(\w*)[^}]*\}", substitute, template)


def _combine(pieces: list[list[str]]) -> list[str]:
    out = []
    for choice in itertools.islice(itertools.product(*pieces), MAX_RENDERINGS):
        out.append("".join(choice))
    return out or [""]


def _percent(template: str, arguments: list[str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        if match.group(0) == "%%":
            return "%"
        return arguments.pop(0) if arguments else "{}"

    return re.sub(r"%%|%(?:\(\w+\))?[sdr]", substitute, template)


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _top_level_strings(m: _Module) -> Iterator[ast.AST]:
    """Each string expression not inside a larger one, docstrings aside."""
    for node in ast.walk(m.tree):
        if not (isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
                isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))) or (
                isinstance(node, ast.Call) and _callee(node) == "format")):
            continue
        parent = m.parents.get(id(node))
        grandparent = m.parents.get(id(parent)) if parent is not None else None
        if isinstance(parent, (ast.JoinedStr, ast.FormattedValue)) or (
                isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod))) or (
                isinstance(parent, ast.Attribute) and isinstance(grandparent, ast.Call)
                and _callee(grandparent) == "format"):
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


def _mutation_parts(m: _Module, call: ast.Call) -> tuple[ast.AST | None, ast.AST | None, bool] | None:
    """(table, columns, is_delete) of a call that is a mutation, else None.
    A missing part (None) is one that is not known."""
    name = _callee(call)
    if isinstance(call.func, ast.Name):
        for value in m.bindings(call, name) or []:
            if isinstance(value, ast.Attribute):
                name = value.attr
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
    if not is_mutation:
        return None
    for payload in payloads:
        for key, item in payload.items():
            keywords.setdefault(key, item)
        if not payload:
            return None, None, name == "delete"
    table = keywords.get("table", call.args[0] if call.args else None)
    columns = keywords.get("columns", call.args[1] if len(call.args) > 1 and name != "delete" else None)
    return table, columns, name == "delete"


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


def _writes_limit_columns(m: _Module, columns: ast.AST | None) -> bool:
    if columns is None:
        return True
    resolved = m.values(columns)
    return resolved is None or any(c.lower() in LIMIT_COLUMNS for c in resolved)


def _python_findings(m: _Module) -> Iterator[_Finding]:
    strings = list(_top_level_strings(m))
    for node in strings:
        for text in m.texts(node):
            kind = classify(text)
            if kind is not None:
                yield _Finding("key_limit." + kind, node, kind == "sql" and assigns_limit_column(text))
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
                limit = not delete and _writes_limit_columns(m, columns)
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
                if resolved is None:
                    yield _Finding("mutation.unresolved", node, _writes_limit_columns(m, fields.get("columns")))
                elif any(names_table(value) for value in resolved):
                    yield _Finding("key_limit.name", node, _writes_limit_columns(m, fields.get("columns")))


def _strip_comments(path: Path, text: str) -> str:
    markers = {".go": ("//",), ".sql": ("--",), ".tf": ("#", "//")}.get(path.suffix, ("#",))
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(markers))


def _text_findings(path: Path) -> Iterator[tuple[str, bool]]:
    text = _BLOCK_COMMENT.sub(" ", _strip_comments(path, path.read_text(encoding="utf-8", errors="replace")))
    if names_table(text):
        statements = statements_on_table(text)
        limit = any(columns is None or columns & LIMIT_COLUMNS for _start, _end, columns in statements)
        # A line with DML and the table that no statement read here covers:
        # its columns are not known.
        offset = 0
        for line in text.splitlines(keepends=True):
            if classify(line) == "sql" and not any(
                    start < offset + len(line) and offset <= end for start, end, _columns in statements):
                limit = True
            offset += len(line)
        if path.suffix == ".go" and _GO_MUTATION.search(text):
            limit = limit or any(re.search(rf'"{column}"', text) for column in LIMIT_COLUMNS)
        yield "key_limit.text", limit
    if _DYNAMIC.search(text):
        yield "sql.dynamic", True
    if path.suffix == ".go":
        for match in _GO_MUTATION.finditer(text):
            if not match.group(1).startswith('"'):
                yield "mutation.unresolved", True


Finding = tuple[str, str, str]


def scan_paths_detail(python: Iterable[Path], text: Iterable[Path]) -> dict[Finding, bool]:
    """Every finding, with whether its scope may write a limit column."""
    python = list(python)
    trees = [ast.parse(p.read_text(encoding="utf-8")) for p in python]
    index = _Index(trees)
    found: dict[Finding, bool] = {}
    for path, tree in zip(python, trees, strict=True):
        m = _Module(path, tree, index)
        for finding in _python_findings(m):
            key = (finding.style, m.relative, m.where(finding.node))
            found[key] = found.get(key, False) or finding.limit
    for path in text:
        relative = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name
        for style, limit in _text_findings(path):
            key = (style, relative, "<file>")
            found[key] = found.get(key, False) or limit
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


def scan_repository_detail() -> dict[Finding, bool]:
    return scan_paths_detail(*repository_paths())


def scan_repository() -> set[Finding]:
    return set(scan_repository_detail())


if __name__ == "__main__":
    for finding, limit in sorted(scan_repository_detail().items()):
        print(finding, "LIMIT" if limit else "")
