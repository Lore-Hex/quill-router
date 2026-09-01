"""Pin which gateway functions carry the billing-path Spanner RPC budget.

Written because a decorator can be silently detached without breaking anything
a normal test would notice. Python permits blank lines between a decorator and
its `def`, and the decorator then binds to the FIRST function that follows. So
inserting a new helper between `@spanner_rpc_budget(...)` and
`_settle_gateway_authorization` moves the budget onto the helper and leaves the
function that writes billing state unguarded -- while still compiling, still
importing, and still passing every behavioural test in the suite.

That is exactly what happened on the settle-failover-Sentry branch: the budget
landed on an observability helper. A behavioural test cannot catch it, because
the budget only changes what happens under RPC pressure. So this asserts the
attachment itself, from the AST.
"""

from __future__ import annotations

import ast
from pathlib import Path

from trusted_router.routes.internal.gateway import (
    _BILLING_PATH_SPANNER_BUDGET_SECONDS,
    _SPEND_LEASE_SHADOW_SPANNER_BUDGET_SECONDS,
)

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "src" / "trusted_router" / "routes" / "internal" / "gateway.py"

#: The billing path: everything that authorizes or settles gateway spend. These
#: hold the budget because an RPC stall here stalls inference authorization.
BILLING_PATH_FUNCTIONS = frozenset(
    {
        "_authorize_gateway_sync",
        "_settle_gateway_with_admission_sync",
        "_settle_gateway_authorization",
    }
)

_BUDGET = "spanner_rpc_budget(_BILLING_PATH_SPANNER_BUDGET_SECONDS)"
_SHADOW_BUDGET = "spanner_rpc_budget(_SPEND_LEASE_SHADOW_SPANNER_BUDGET_SECONDS)"


def _functions_carrying_the_budget() -> frozenset[str]:
    tree = ast.parse(GATEWAY.read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if ast.unparse(decorator) == _BUDGET
    )


def test_billing_path_functions_carry_the_rpc_budget() -> None:
    """Equality, not containment: catches a detached budget AND a stray one.

    Containment would pass if the decorator drifted onto an extra function while
    still covering these two, which is half of the failure this file exists for.
    """
    assert _functions_carrying_the_budget() == BILLING_PATH_FUNCTIONS


def test_no_blank_line_between_the_budget_and_its_function() -> None:
    """The specific edit that detaches it, refused at the source level.

    The AST test above already catches the consequence; this names the cause, so
    a reviewer seeing it go red knows immediately what to look for.
    """
    lines = GATEWAY.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines):
        if line.strip().startswith(f"@{_BUDGET}"):
            following = lines[number + 1].strip()
            assert following.startswith(("def ", "async def ", "@")), (
                f"line {number + 1} decorates nothing: the budget on line "
                f"{number + 1} is followed by {following!r}, so it binds to "
                f"whatever function appears next instead of the one below it"
            )


def test_billing_budget_finishes_before_enclave_header_timeout() -> None:
    """The enclave's direct control-plane client has a 25-second header cap."""

    assert _BILLING_PATH_SPANNER_BUDGET_SECONDS == 20.0


def test_non_authoritative_spend_shadow_has_its_own_background_budget() -> None:
    """Background evidence tolerates cross-region retries without client latency."""

    tree = ast.parse(GATEWAY.read_text(encoding="utf-8"))
    shadow_functions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if ast.unparse(decorator) == _SHADOW_BUDGET
    }

    assert shadow_functions == {"_persist_spend_lease_shadow"}
    assert _SPEND_LEASE_SHADOW_SPANNER_BUDGET_SECONDS == 5.0


def test_spend_shadow_recording_is_not_decorated_as_a_spanner_call() -> None:
    """The request-thread helper may enqueue only; it cannot call Spanner."""

    tree = ast.parse(GATEWAY.read_text(encoding="utf-8"))
    record = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_record_spend_lease_shadow"
    )
    assert not record.decorator_list
    calls = {ast.unparse(node.func) for node in ast.walk(record) if isinstance(node, ast.Call)}
    assert "STORE.record_spend_lease_shadow" not in calls
    assert "_SPEND_LEASE_SHADOW_DISPATCHER.submit" in calls
