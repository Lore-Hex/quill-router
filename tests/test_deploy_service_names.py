from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INFRA = (ROOT / "scripts/deploy/infra.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("env_update", "expected_console", "expected_legacy"),
    (
        ({}, "trusted-router-console", "trusted-router"),
        ({"SERVICE": "router-canary"}, "router-canary-console", "router-canary"),
        (
            {
                "TR_CONSOLE_SERVICE": "reviewed-console",
                "TR_LEGACY_CONSOLE_SERVICE": "reviewed-monolith",
            },
            "reviewed-console",
            "reviewed-monolith",
        ),
    ),
)
def test_console_and_legacy_monolith_names_resolve_independently(
    tmp_path: Path,
    env_update: dict[str, str],
    expected_console: str,
    expected_legacy: str,
) -> None:
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        "#!/usr/bin/env bash\nprintf '123456789\\n'\n",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PROJECT_ID": "test-project",
    }
    for variable in (
        "SERVICE",
        "TR_CONSOLE_SERVICE",
        "TR_LEGACY_CONSOLE_SERVICE",
    ):
        env.pop(variable, None)
    env.update(env_update)

    result = subprocess.run(  # noqa: S603 - fixed local shell and repository script
        [
            "/bin/bash",
            "-c",
            (
                "source scripts/deploy/_lib.sh; "
                "printf '%s\\t%s\\n' \"$CONSOLE_SERVICE\" \"$LEGACY_CONSOLE_SERVICE\""
            ),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{expected_console}\t{expected_legacy}"


def test_legacy_iam_retirement_rejects_a_console_monolith_alias() -> None:
    start = INFRA.index("verify_legacy_runtime_retirement_ready() {")
    end = INFRA.index("verify_identity_resource_manager_ancestors_empty() {", start)
    helper = INFRA[start:end]

    assert 'if [ "$CONSOLE_SERVICE" = "$LEGACY_CONSOLE_SERVICE" ]; then' in helper
    assert "split console ${CONSOLE_SERVICE} aliases the legacy monolith" in helper
    assert helper.index('"$CONSOLE_SERVICE"') < helper.index("local -a services=(")
