"""The guard against scheduled cutovers turning main red at midnight.

At 2026-08-17 00:00 UTC main went red for every open pull request with no code
change (CI run 31980690855): `provider_lifecycle` retired three Wafer routes at
that instant, four tests fixtured the retired ids, and one routing-contract
param named the retired endpoint. Nothing had regressed -- the tests had simply
been written on the near side of a cutover the module itself schedules.

Two halves of the guard live here: the clock override the post-cutover CI job
uses, and the standing check that no test names a route that has ALREADY
retired. The third half is the `test-post-cutover` job in ci.yml, which runs
the whole suite with the clock pinned past the latest scheduled cutover.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trusted_router import provider_lifecycle

_REPO_ROOT = Path(__file__).parents[1]
_TESTS_DIR = Path(__file__).parent

# "moonshotai/kimi-k3-fast@wafer/prepaid" -> ("moonshotai/kimi-k3-fast", "wafer")
_ENDPOINT_ID = re.compile(r"([a-z0-9][\w.-]*/[\w.-]+)@([a-z0-9-]+)/[a-z]+")


def _code_string_literals(path: Path) -> Iterator[tuple[int, str]]:
    """Every string literal a test actually evaluates.

    Comments and docstrings are deliberately excluded: naming a retired
    endpoint in prose is how a retirement gets DOCUMENTED (PR #628 left exactly
    such a comment where the param used to be), and prose cannot break a test.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


def _retirement_date(provider_slug: str, model_id: str) -> str:
    for retirement in provider_lifecycle._RETIREMENTS:
        if retirement.provider == provider_slug and model_id in retirement.model_ids:
            return retirement.effective_at.isoformat()
    return "unknown"


def test_override_moves_the_clock_only_under_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = datetime(2027, 1, 1, tzinfo=UTC)
    monkeypatch.setenv(
        provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV, "2027-01-01T00:00:00Z"
    )

    assert provider_lifecycle._utc_now() == pinned
    # Naive and offset strings both land on UTC, like every other `at=` here.
    monkeypatch.setenv(provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV, "2027-01-01T02:00:00+02:00")
    assert provider_lifecycle._utc_now() == pinned

    # A typo must not silently degrade to the wall clock: a run that quietly
    # used the real clock would report the coverage it did not provide.
    monkeypatch.setenv(provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV, "not-a-timestamp")
    with pytest.raises(ValueError):
        provider_lifecycle._utc_now()

    monkeypatch.delenv(provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV)
    assert provider_lifecycle._utc_now() - datetime.now(UTC) < timedelta(minutes=1)


def test_override_is_ignored_outside_test_environments() -> None:
    """Production must reach the real clock even with the variable exported.

    Driven in a SUBPROCESS on purpose: in-process, `"pytest" in sys.modules` is
    unconditionally true, so the permission check cannot be exercised from
    inside the suite that trips it.
    """
    env = {
        **os.environ,
        provider_lifecycle.LIFECYCLE_CLOCK_OVERRIDE_ENV: "2027-01-01T00:00:00Z",
        "TR_ENVIRONMENT": "production",
        "PYTHONPATH": str(_REPO_ROOT / "src"),
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from datetime import UTC, datetime\n"
            "from trusted_router import provider_lifecycle\n"
            "drift = abs(provider_lifecycle._utc_now() - datetime.now(UTC)).total_seconds()\n"
            "print(drift < 60)\n",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "True"

    # Negative control: the same subprocess DOES honour the override under
    # TR_ENVIRONMENT=test, so the assertion above is about the guard and not
    # about the variable failing to reach the child at all.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from trusted_router import provider_lifecycle\n"
            "print(provider_lifecycle._utc_now().year)\n",
        ],
        capture_output=True,
        text=True,
        env={**env, "TR_ENVIRONMENT": "test"},
        cwd=_REPO_ROOT,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2027"


def test_latest_scheduled_cutover_covers_every_effective_date() -> None:
    latest = provider_lifecycle.latest_scheduled_cutover()

    for retirement in provider_lifecycle._RETIREMENTS:
        assert retirement.effective_at <= latest, retirement.provider
    assert provider_lifecycle.DEEPSEEK_V4_PRICING_EFFECTIVE_AT <= latest
    assert provider_lifecycle.FIREWORKS_DSV4_FLASH_0731_PRICING_EFFECTIVE_AT <= latest
    assert provider_lifecycle.PHALA_JULY_2026_EFFECTIVE_AT <= latest
    # Past the latest cutover, no retirement is still pending, which is what
    # makes the post-cutover CI job's clock the strictest one available.
    day_after = latest + timedelta(days=1)
    for retirement in provider_lifecycle._RETIREMENTS:
        for model_id in retirement.model_ids:
            assert provider_lifecycle.provider_model_retired(
                retirement.provider, model_id, at=day_after
            )


def test_no_test_names_an_already_retired_endpoint() -> None:
    """No `provider/model@provider/tier` id in tests/ has already retired.

    This is the specific defect PR #628 patched by hand: a
    `test_catalog_routing_contracts` param named
    `moonshotai/kimi-k3-fast@wafer/prepaid`, and the endpoint stopped existing
    at the announced minute. Endpoint ids appear in parametrize lists and
    `MODEL_ENDPOINTS[...]` lookups, so scanning the source catches both.

    The `*_lifecycle.py` files are exempt: proving a route retires is their
    subject, and they pass explicit `at=` instants rather than relying on the
    catalog the run happened to build.
    """
    offenders: list[str] = []
    scanned = 0
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        if path.name.endswith("_lifecycle.py"):
            continue
        for line_number, literal in _code_string_literals(path):
            for model_id, provider_slug in _ENDPOINT_ID.findall(literal):
                scanned += 1
                if provider_lifecycle.provider_model_retired(provider_slug, model_id):
                    offenders.append(
                        f"{path.relative_to(_REPO_ROOT)}:{line_number}: "
                        f"{model_id}@{provider_slug} retired "
                        f"{_retirement_date(provider_slug, model_id)}"
                    )

    assert not offenders, "\n".join(offenders)
    # A scan that matched nothing would pass for the wrong reason.
    assert scanned > 100, scanned
