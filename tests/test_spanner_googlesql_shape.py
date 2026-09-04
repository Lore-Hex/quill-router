"""Native-Spanner SQL is never parsed anywhere in CI, so parse its SHAPE here.

`tests/conformance/conftest.py` documents the gap in its own docstring: the
`spanner-pg` backend points a PostgresStore at Spanner (right dialect, wrong
store), `spanner-emulator` skips unconditionally, and the in-process fake
executes the native store but "is not the Spanner query planner". So a
GoogleSQL syntax error in the native store reaches production green.

This guard closes the class that has actually shipped: GoogleSQL rejects a
FROM-less SELECT carrying a WHERE clause ("Query without FROM clause cannot
have a WHERE clause"), while PostgreSQL accepts it. The Postgres spelling of
a guarded insert --

    INSERT INTO t (...) SELECT @a, @b WHERE NOT EXISTS (SELECT 1 FROM t ...)

-- therefore fails EVERY call on Spanner. It shipped in trust-tiers slice 1a
and broke Stripe credit application until the fix that added this test. The
Spanner spelling adds a one-row source: `FROM UNNEST([1]) AS _one`.

Two things this file guards about itself, because a shape checker that
quietly inspects nothing is worse than none: `test_guard_resolves_nearly_every_call_site`
pins how much of the real SQL it can actually read, and `test_detector_semantics`
pins the detector against hand-written cases in both directions.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "trusted_router"
# The Spanner client's own method names. psycopg spells it `.execute(`, so
# matching on these keeps the Postgres store (a dialect where the shape under
# test is perfectly legal) out of scope by construction.
SQL_METHODS = {"execute_sql", "execute_update", "execute_partitioned_dml"}
# One fragment the code builds at runtime, e.g. ", ".join(COLUMNS). No such
# fragment in this tree introduces or removes a FROM or WHERE clause.
PLACEHOLDER = " _dyn "
WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|[(),]")
STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'")
LINE_COMMENT = re.compile(r"--[^\n]*")
SET_OPERATORS = {"UNION", "INTERSECT", "EXCEPT"}
MAX_VARIANTS = 8


def _native_spanner_modules() -> list[pathlib.Path]:
    """Every module that drives Spanner directly, found by what it calls."""
    modules = []
    for path in sorted(SRC.rglob("*.py")):
        if "postgres" in path.name:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - the repo does not hold these
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in SQL_METHODS:
                modules.append(path)
                break
    return modules


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _bound_strings(tree: ast.Module) -> dict[str, list[str]]:
    """name -> the SQL it can hold, for statements assigned before use.

    Module constants and plain local assignments both land here; without this
    the guard sees only SQL written inline at the call site, which is about
    three quarters of it.
    """
    bound: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        # Every string-valued binding, not only the ones that say SELECT: a
        # name holding an UPDATE is still a statement this file must be able
        # to SEE, and coverage is the measure of whether it can.
        values = [text for text in _fold(value, {}) if PLACEHOLDER not in text]
        if values:
            bound.setdefault(target.id, []).extend(values)
    return bound


def _fold(node: ast.AST, bound: dict[str, list[str]]) -> list[str]:
    """Every string the expression can be, with dynamic parts as placeholders.

    A list rather than one string so a conditional (`a if c else b`) is judged
    as both of its arms instead of a meaningless splice of the two.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else [PLACEHOLDER]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold(node.left, bound)
        right = _fold(node.right, bound)
        return [a + b for a in left for b in right][:MAX_VARIANTS]
    if isinstance(node, ast.JoinedStr):
        parts = [_fold(value, bound) for value in node.values]
        combined = [""]
        for part in parts:
            combined = [a + b for a in combined for b in part][:MAX_VARIANTS]
        return combined
    if isinstance(node, ast.IfExp):
        return (_fold(node.body, bound) + _fold(node.orelse, bound))[:MAX_VARIANTS]
    if isinstance(node, ast.Name) and node.id in bound:
        return bound[node.id][:MAX_VARIANTS]
    return [PLACEHOLDER]


def _statements(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    bound = _bound_strings(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in SQL_METHODS:
            continue
        argument = node.args[0] if node.args else _keyword(node, "sql")
        if argument is None:
            continue
        for text in _fold(argument, bound):
            if "SELECT" in text.upper():
                found.append((node.lineno, " ".join(text.split())))
    return found


def _keyword(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _call_sites(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text())
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _call_name(node) in SQL_METHODS
    ]


def _from_less_select_with_where(sql: str) -> bool:
    """True when some SELECT has a WHERE at its own paren depth and no FROM.

    Each SELECT is judged in the scope it opens, so a subquery with its own
    FROM never excuses the outer query and vice versa. A set operator ends the
    arm under examination WITHOUT ending the walk: every arm of a UNION is its
    own SELECT and each one needs its own row source.
    """
    sql = LINE_COMMENT.sub(" ", STRING_LITERAL.sub(" _str ", sql))
    words: list[tuple[str, int]] = []
    depth = 0
    for match in WORD.finditer(sql):
        word = match.group(0)
        if word == "(":
            depth += 1
        elif word == ")":
            depth -= 1
        else:
            words.append((word.upper(), depth))
    for index, (word, depth) in enumerate(words):
        if word != "SELECT":
            continue
        for later, later_depth in words[index + 1 :]:
            if later_depth < depth:
                break  # this SELECT's scope closed
            if later_depth != depth:
                continue  # inside a subquery: not this SELECT's own clause
            if later == "FROM":
                break  # has a row source, fine
            if later == "WHERE":
                return True
            if later in SET_OPERATORS:
                break  # next arm is judged on its own terms
    return False


@pytest.mark.parametrize("path", _native_spanner_modules(), ids=lambda p: p.name)
def test_no_from_less_select_with_where(path: pathlib.Path) -> None:
    offenders = [
        f"{path.name}:{line} -> {sql[:120]}"
        for line, sql in _statements(path)
        if _from_less_select_with_where(sql)
    ]
    assert not offenders, (
        "GoogleSQL rejects a FROM-less SELECT that has a WHERE clause. "
        "Give it a one-row source (FROM UNNEST([1]) AS _one):\n" + "\n".join(offenders)
    )


def test_guard_reads_the_modules_it_claims_to() -> None:
    modules = {path.name for path in _native_spanner_modules()}
    assert "storage_gcp_trust.py" in modules, modules
    assert "storage_gcp.py" in modules, modules
    assert not [name for name in modules if "postgres" in name], modules
    trust = SRC / "storage_gcp_trust.py"
    guarded = [sql for _line, sql in _statements(trust) if "NOT EXISTS" in sql]
    assert guarded, "the guarded inserts in storage_gcp_trust.py were not extracted"
    assert all("UNNEST([1])" in sql for sql in guarded), guarded


def test_guard_resolves_nearly_every_call_site() -> None:
    """A shape checker is only worth its coverage, so pin the coverage.

    SQL assigned to a name before use is the majority of these call sites. If
    the resolver regresses to inline literals only, the guard would still pass
    every other test in this file while reading a quarter of the statements.
    """
    resolved = unresolved = 0
    misses: list[str] = []
    for path in _native_spanner_modules():
        tree = ast.parse(path.read_text())
        bound = _bound_strings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in SQL_METHODS:
                continue
            argument = node.args[0] if node.args else _keyword(node, "sql")
            texts = _fold(argument, bound) if argument is not None else []
            if any(PLACEHOLDER not in text for text in texts):
                resolved += 1
            else:
                unresolved += 1
                misses.append(f"{path.name}:{node.lineno}")
    total = resolved + unresolved
    assert total > 150, f"only {total} call sites found; the scanner regressed"
    coverage = resolved / total
    assert coverage >= 0.85, (
        f"resolved {resolved}/{total} ({coverage:.0%}) native Spanner statements; "
        f"unread: {', '.join(misses[:12])}"
    )


@pytest.mark.parametrize(
    ("sql", "flagged"),
    [
        ("INSERT INTO t (a) SELECT @a WHERE NOT EXISTS (SELECT 1 FROM t WHERE a=@a)", True),
        ("SELECT @a, @b WHERE TRUE", True),
        # A set operator must not disarm the walk for the arms after it.
        ("SELECT @a UNION ALL SELECT @b WHERE TRUE", True),
        ("SELECT @a FROM t UNION ALL SELECT @b WHERE TRUE", True),
        ("SELECT * EXCEPT (secret) FROM t UNION ALL SELECT @b WHERE TRUE", True),
        (
            "INSERT INTO t (a) SELECT @a FROM UNNEST([1]) AS _one "
            "WHERE NOT EXISTS (SELECT 1 FROM t WHERE a=@a)",
            False,
        ),
        ("SELECT @a FROM UNNEST([1]) UNION ALL SELECT @b FROM UNNEST([1]) WHERE TRUE", False),
        ("SELECT a FROM t WHERE a=@a", False),
        ("SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a=t.a)", False),
        ("SELECT (SELECT 1 FROM u LIMIT 1) AS x FROM t WHERE t.a=@a", False),
        ("SELECT @a", False),
        # A literal or a comment that merely contains the words is not a clause.
        ("SELECT 'where the FROM is' AS x", False),
        ("SELECT @a -- WHERE this is a comment\n", False),
    ],
)
def test_detector_semantics(sql: str, flagged: bool) -> None:
    assert _from_less_select_with_where(sql) is flagged
