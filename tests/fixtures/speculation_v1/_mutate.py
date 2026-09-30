"""Run the requested mutations on disposable COPIES; never edits the worktree.

Invoke with the repository Python environment. Report assertions verbatim.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODULE = Path("src/trusted_router/speculation_protocol.py")
TEST = Path("tests/test_speculation_protocol.py")
MUTATIONS = [
    ("dry-run accepted as real", "dry_run_cannot_dispatch",
     '_require(not grant.shadow, "dry_run_cannot_dispatch")', 'pass'),
    ("ignore key epoch", "binding_key_epoch", 'for field in BINDINGS:',
     'for field in BINDINGS:\n        if field == "key_epoch":\n            continue'),
    ("ignore boot binding", "binding_boot_id", 'for field in BINDINGS:',
     'for field in BINDINGS:\n        if field == "boot_id":\n            continue'),
    ("ignore route binding", "route_endpoint_id",
     '_require(route == context.get("route"), "route")', 'pass'),
    ("classify every 402 as workspace", "verdict:lifetime_limit",
     '    breaker = "key_boot"', '    if status == 402:\n        scope = "workspace"\n    breaker = "key_boot"'),
    ("classify every 429 as key", "verdict:rate_unknown",
     '    breaker = "key_boot"', '    if status == 429:\n        scope = "key"\n    breaker = "key_boot"'),
    ("round B down", "money_fractional",
     'product // 1_000_000 + int(product % 1_000_000 != 0)', 'product // 1_000_000'),
    ("accept shadow-purpose key for real grant", "real_type_shadow_key",
     'key.purpose == purpose', '(key.purpose == purpose or key.purpose == "shadow-grant")'),
    ("skip canonical-payload check", "payload_whitespace",
     '_require(payload == _canonical(claims), "canonical_payload")', 'pass'),
    ("accept padded base64", "signature_padded", 'def _b64decode(value: str) -> bytes:',
     'def _b64decode(value: str) -> bytes:\n    value = value.rstrip("=")'),
    ("allow bool as int", "bool_integer", 'type(value) is int', 'isinstance(value, int)'),
    ("change one fixture byte", "fixture_pins", None, None),
]


def main() -> None:
    original = (ROOT / MODULE).read_text()
    rows = []
    with tempfile.TemporaryDirectory(prefix="sol-spec-mutations-", dir="/private/tmp") as tmp:
        target = Path(tmp)
        (target / MODULE).parent.mkdir(parents=True)
        (target / "src/trusted_router/__init__.py").write_text("")
        shutil.copytree(ROOT / "tests/fixtures/speculation_v1", target / "tests/fixtures/speculation_v1")
        shutil.copyfile(ROOT / TEST, target / TEST)
        (target / "pytest.ini").write_text("[pytest]\npythonpath = src\n")
        for label, case, before, after in MUTATIONS:
            code = original
            if before is not None:
                assert code.count(before) == 1, (label, code.count(before))
                code = code.replace(before, after)
            (target / MODULE).write_text(code)
            shutil.rmtree(target / "src/trusted_router/__pycache__", ignore_errors=True)
            wire = target / "tests/fixtures/speculation_v1/provider-wire.json"
            original_wire = (ROOT / "tests/fixtures/speculation_v1/provider-wire.json").read_bytes()
            wire.write_bytes(original_wire.replace(b"fixture", b"fixturf", 1) if before is None else original_wire)
            test = "test_literal[" + case + "]"
            if case.startswith("verdict:"):
                test = "test_verdict[" + case.split(":")[1] + "]"
            if case == "fixture_pins":
                test = "test_fixture_pins"
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
            result = subprocess.run(  # noqa: S603 - fixed executable and locally constructed args
                [sys.executable, "-m", "pytest", "--noconftest", "-q", "-p", "no:cacheprovider",
                 "-c", str(target / "pytest.ini"), str(TEST) + "::" + test],
                cwd=target, env=env, capture_output=True, text=True, timeout=300,
            )
            assertion = next((line.strip() for line in result.stdout.splitlines()
                              if "AssertionError:" in line), "")
            status = "red" if result.returncode == 1 and assertion else "survived" if result.returncode == 0 else "build-broken"
            rows.append({"mutant": label, "test": test, "result": status, "assertion": assertion})
            print(json.dumps(rows[-1]), flush=True)
            if status != "red":
                print(result.stdout, result.stderr, flush=True)
    assert all(row["result"] == "red" for row in rows), rows


if __name__ == "__main__":
    main()
