"""Run PR3 fault injections against disposable copies, never the worktree.

Usage: uv run python -m tests.speculation_shadow_mutations
Each baseline and mutant executes behavior tests. Import/compile errors and
timeouts are build-broken, not successful detection.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "src/trusted_router/services/speculation_shadow.py"
GATEWAY = "src/trusted_router/routes/internal/gateway.py"
ADAPTER = "src/trusted_router/storage_gcp_speculation_shadow.py"
UNIT = "tests/test_speculation_shadow.py::"
MUTATIONS = [
    ("duplicate callback counts", SERVICE,
     'if tx.get("success", unique) is None and tx.get("success", invocation) is None:',
     'if True:', UNIT + "test_distinct_history_replay_and_invocation_dedup"),
    ("current request qualifies", SERVICE,
     'now - 600 <= s[1] < now', 'now - 600 <= s[1] <= now',
     UNIT + "test_current_request_cannot_qualify_its_predecision"),
    ("submit waits on worker IO", SERVICE,
     '\n        if not self.lock.acquire(blocking=False):',
     '\n        self.store.ready()\n        if not self.lock.acquire(blocking=False):',
     UNIT + "test_submit_does_not_wait_for_worker_io"),
    ("extra synchronous boot read", GATEWAY,
     '        speculation_shadow.resolved(api_key, body.invocation_nonce)',
     '        STORE.get_gateway_boot("mutation-boot")\n        speculation_shadow.resolved(api_key, body.invocation_nonce)',
     "tests/test_gateway_authorize_spanner_operations.py::test_warm_lookup_authorize_exact_sequence_and_contents"),
    ("success clears sticky loss", SERVICE,
     '    state.update(submitted=submitted, expires_at=now + 5, membership=membership)',
     '    if event is not None and event.status == 200 and not loss:\n        state.pop("lost", None)\n    state.update(submitted=submitted, expires_at=now + 5, membership=membership)',
     UNIT + "test_loss_restart_unacknowledged_tail_and_late_membership"),
    ("lifetime topup treated as paid", SERVICE,
     '    headroom = paid_lower_bound(max(0, facts["credits"] - facts["usage"] - facts["reserved"]), paid, now)',
     '    headroom = min(max(0, facts["credits"] - facts["usage"] - facts["reserved"]), facts.get("lifetime_topup", facts["credits"]))',
     UNIT + "test_promotional_mixed_funds_conservation"),
    ("real grant type", SERVICE,
     '"typ": SHADOW_TYP', '"typ": "speculation-eligibility+jws"',
     UNIT + "test_exact_v1_signer_round_trip_frozen_verifier_and_authority"),
    ("real store namespace", ADAPTER,
     'return "tr_speculation_shadow_" + table', 'return "tr_credit_balance"',
     UNIT + "test_authority_namespace_at_native_mutation_boundary"),
    ("first batch identity reused", SERVICE,
     'facts = self.store.resolve(item["lookup_digest"], now)\n                        if any(facts.get(k) != item[k]',
     'facts = self.store.resolve(items[0]["lookup_digest"], now)\n                        if any(facts.get(k) != items[0][k]',
     UNIT + "test_batch_binding_every_member_and_exact_body"),
]


def main(names: set[str] | None = None) -> None:
    results = []
    with tempfile.TemporaryDirectory(prefix="astra-r3-mutations-", dir="/private/tmp") as directory:
        copied = Path(directory)
        for name in ("src", "tests", "scripts", "docs"):
            shutil.copytree(ROOT / name, copied / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "pyproject.toml", copied / "pyproject.toml")
        env = {**os.environ, "PYTHONPATH": str(copied / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        def run(test: str) -> tuple[str, str]:
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test,
                       "--tb=short", "--disable-warnings"]
            completed = subprocess.run(command, cwd=copied, env=env,  # noqa: S603 - fixed repository test command
                                       capture_output=True, text=True, timeout=120, check=False)
            output = completed.stdout + completed.stderr
            broken = completed.returncode not in (0, 1) or any(s in output for s in ("ERROR collecting", "SyntaxError", "ImportError"))
            return ("build-broken" if broken else "survived" if completed.returncode == 0 else "red"), output
        for name, relative, old, new, test in MUTATIONS:
            if names is not None and name not in names:
                continue
            path = copied / relative
            original = path.read_text()
            assert original.count(old) == 1, name
            baseline, output = run(test)
            if baseline != "survived":
                raise AssertionError(f"baseline {name}: {output}")
            try:
                path.write_text(original.replace(old, new))
                compile(path.read_text(), str(path), "exec")
                result, output = run(test)
            except (SyntaxError, subprocess.TimeoutExpired) as exc:
                result, output = "build-broken", str(exc)
            finally:
                path.write_text(original)
            restored, restored_output = run(test)
            assert restored == "survived", restored_output
            summary = [line for line in output.splitlines() if "failed" in line or "passed" in line or line.startswith("FAILED")]
            results.append({"mutation": name, "baseline": "pass", "result": result, "restored": "pass", "gate": test, "summary": summary})
            print(json.dumps(results[-1]), flush=True)
    destination = ROOT / "docs/speculation-shadow-mutations.json"
    if names is not None and destination.exists():
        previous = {row["mutation"]: row for row in json.loads(destination.read_text())}
        previous.update({row["mutation"]: row for row in results})
        results = list(previous.values())
    destination.write_text(json.dumps(results, indent=2) + "\n")
    if any(row["result"] != "red" for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
