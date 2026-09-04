"""Native-Spanner SQL is never parsed anywhere in CI, so parse its SHAPE here.

`tests/conformance/conftest.py` documents the gap in its own docstring: the
`spanner-pg` backend points a PostgresStore at Spanner (right dialect, wrong
store), `spanner-emulator` skips unconditionally, and the in-process fake
executes the native store but "is not the Spanner query planner". The result
is that a GoogleSQL syntax error in `storage_gcp*.py` reaches production
green.

This guard closes the one class that has actually shipped: GoogleSQL rejects
a FROM-less SELECT carrying a WHERE clause ("Query without FROM clause cannot
have a WHERE clause"), while PostgreSQL accepts it. The Postgres spelling of a
guarded insert --

    INSERT INTO t (...) SELECT @a, @b WHERE NOT EXISTS (SELECT 1 FROM t ...)

-- therefore fails EVERY call on Spanner. It shipped in trust-tiers slice 1a
and broke Stripe credit application until the fix that added this test. The
Spanner spelling adds a one-row source: `FROM UNNEST([1]) AS _one`.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "trusted_router"
SQL_METHODS = {"execute_sql", "execute_update", "execute_partitioned_dml"}
# A non-constant fragment (e.g. ", ".join(COLUMNS)) stands in as one token: no
# fragment the code builds that way introduces or removes a FROM/WHERE clause.
PLACEHOLDER = " _dyn "
WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|[(),]")


def _native_spanner_modules() -> list[pathlib.Path]:
    return sorted(SRC.glob("storage_gcp*.py"))


def _fold(node: ast.AST) -> str | None:
    """The statement text, with dynamic fragments replaced by a placeholder."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else PLACEHOLDER
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_fold(value) for value in node.values]
        return None if any(part is None for part in parts) else "".join(parts)  # type: ignore[arg-type]
    return PLACEHOLDER


def _statements(path: pathlib.Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in SQL_METHODS:
            continue
        text = _fold(node.args[0])
        if text and "SELECT" in text.upper():
            found.append((node.lineno, " ".join(text.split())))
    return found


def _from_less_select_with_where(sql: str) -> bool:
    """True when some SELECT has a WHERE at its own paren depth and no FROM.

    Each SELECT is judged at the depth it opens on, so a subquery that has its
    own FROM never excuses the outer query, and vice versa.
    """
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
                continue  # inside a subquery: not this SELECT's clause
            if later == "FROM":
                break  # has a row source, fine
            if later in {"WHERE", "UNION", "INTERSECT", "EXCEPT"}:
                return later == "WHERE"
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
        "Add a one-row source (FROM UNNEST([1]) AS _one):\n" + "\n".join(offenders)
    )


def test_the_guard_actually_reads_statements() -> None:
    """An extractor that silently finds nothing would make the guard vacuous."""
    modules = _native_spanner_modules()
    assert modules, "no storage_gcp*.py modules found"
    total = sum(len(_statements(path)) for path in modules)
    assert total > 100, f"only {total} statements extracted; the extractor regressed"
    trust = SRC / "storage_gcp_trust.py"
    guarded = [sql for _line, sql in _statements(trust) if "NOT EXISTS" in sql]
    assert guarded, "the guarded inserts in storage_gcp_trust.py were not extracted"
    assert all("UNNEST([1])" in sql for sql in guarded), guarded


@pytest.mark.parametrize(
    ("sql", "flagged"),
    [
        ("INSERT INTO t (a) SELECT @a WHERE NOT EXISTS (SELECT 1 FROM t WHERE a=@a)", True),
        ("SELECT @a, @b WHERE TRUE", True),
        (
            "INSERT INTO t (a) SELECT @a FROM UNNEST([1]) AS _one "
            "WHERE NOT EXISTS (SELECT 1 FROM t WHERE a=@a)",
            False,
        ),
        ("SELECT a FROM t WHERE a=@a", False),
        ("SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u WHERE u.a=t.a)", False),
        ("SELECT (SELECT 1 FROM u LIMIT 1) AS x FROM t WHERE t.a=@a", False),
        ("SELECT @a", False),
    ],
)
def test_detector_semantics(sql: str, flagged: bool) -> None:
    assert _from_less_select_with_where(sql) is flagged
