"""Run PR3 fault injections against disposable copies, never the worktree.

Usage: uv run python -m tests.speculation_shadow_mutations
Each baseline and mutant executes behavior tests. Import/compile errors and
timeouts are build-broken, not successful detection.
"""
from __future__ import annotations

import ast
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
     '            speculation_shadow.resolved(api_key, body.invocation_nonce)',
     '            STORE.get_gateway_boot("mutation-boot")\n            speculation_shadow.resolved(api_key, body.invocation_nonce)',
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


MUTATIONS.extend([
    ("cached current deadlines ignored", SERVICE,
     'if (cached["trust_fresh_until"] <= facts["trust_fresh_until"]',
     'if (True', UNIT + "test_cached_grant_rechecks_current_deadlines"),
    ("exhausted start window untyped", SERVICE,
     'if deadline - 2 <= now:', 'if False:', UNIT + "test_cached_grant_rechecks_current_deadlines"),
    ("native backend deselected", "tests/conformance/test_speculation_shadow_native.py",
     '@pytest.mark.parametrize("backend", ["spanner-emulator"])',
     '@pytest.mark.parametrize("backend", ["native"])', UNIT + "test_native_shadow_is_selected_by_ci"),
    ("native explicit CI list omitted", ".github/workflows/ci.yml",
     '          tests/conformance/test_speculation_shadow_native.py\n', '', UNIT + "test_native_shadow_is_selected_by_ci"),
    ("Stage D observer changes estimate", SERVICE,
     '    observation = _CURRENT.get()\n    if observation is not None and not observation.retired:\n        observation.authorization_id = authorization.id',
     '    if authorization.pricing_snapshot is not None:\n        authorization.estimated_microdollars += 1\n    observation = _CURRENT.get()\n    if observation is not None and not observation.retired:\n        observation.authorization_id = authorization.id',
     "tests/test_gateway_authorize_spanner_operations.py::test_shadow_response_money_and_sql_differential"),
    ("expired replay recreates dedup", SERVICE,
     'if event is not None and not now - MAX_DELIVERY_SECONDS <= event.occurred_at <= now:',
     'if False:', UNIT + "test_expired_delivery_after_ttl_cannot_requalify_or_extend_retention"),
    ("old success recreates dedup", SERVICE,
     'and not event.replay and now - HISTORY_SECONDS <= event.occurred_at <= now:',
     'and not event.replay:', UNIT + "test_success_older_than_history_does_not_recreate_dedup"),
    ("retention policy omitted", "scripts/deploy/migrate_speculation_shadow.sh",
     'ensure_policy tr_speculation_shadow_success\n', '', UNIT + "test_shadow_ttl_only_applies_to_event_and_success"),
])

# One actual source-site removal per boundary, including each paused fallback.
# Keep indentation executable; compile/import failure is never a mutation kill.
for relative in (GATEWAY, "src/trusted_router/gateway_timing.py", SERVICE):
    source = (ROOT / relative).read_text()
    lines = source.splitlines(keepends=True)
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With):
            continue
        expression = node.items[0].context_expr
        if not isinstance(expression, ast.Call):
            continue
        func = expression.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name != "isolate":
            continue
        site = expression.args[0].value
        end = node.lineno
        start = end - 1
        old = ''.join(lines[start:end])
        while source.count(old) != 1:
            start -= 1
            old = ''.join(lines[start:end])
        boundary = lines[end - 1]
        replacement = boundary[:len(boundary) - len(boundary.lstrip())] + "if True:\n"
        new = old[:-len(boundary)] + replacement
        test = (UNIT + "test_every_gateway_callback_boundary_including_arguments" if relative == GATEWAY
                else UNIT + "test_content_free_observation_and_observer_failure" if relative == SERVICE
                else UNIT + "test_observer_fault_preserves_response_and_exception_identity")
        MUTATIONS.append((f"remove {relative.split('/')[-1]} {site} boundary L{end}", relative, old, new, test))


service_source = (ROOT / SERVICE).read_text()
RESTORE_RECOVERY = service_source.split("def restore_context(", 1)[1].split("    except BaseException:\n", 1)[1].split("\n\n\n@contextmanager", 1)[0]
# The outer reset handler includes independently guarded fallback restoration.
assert RESTORE_RECOVERY.startswith("        try:\n            variable.set(previous)")
timing_source = (ROOT / "src/trusted_router/gateway_timing.py").read_text()
FINALIZATION = timing_source.split("def _authorize_outcome(", 1)[1].split("        finally:\n", 1)[1].split("            cleanup(finish)", 1)[0]
completion_body = FINALIZATION.split("            finally:\n", 1)[0].removeprefix("            try:\n")
FLAT_FINALIZATION = "".join(line[4:] for line in completion_body.splitlines(keepends=True)) + "            cleanup(stack.close)\n            cleanup(cleanup_timing)\n"


MUTATIONS.extend([
    ("unguard loss recorder", SERVICE,
     "    except Exception:\n        _COVERAGE_UNKNOWN = True",
     "    except Exception:\n        raise", UNIT + "test_every_gateway_callback_boundary_including_arguments"),
    ("drop ContextVar restoration fallback", SERVICE,
     RESTORE_RECOVERY, "        raise",
     UNIT + "test_failed_reset_restores_context_for_next_sync_authorize"),
    ("classify only Exception exits", "src/trusted_router/gateway_timing.py",
     "        except BaseException as exc:\n            error = exc",
     "        except Exception as exc:\n            error = exc", UNIT + "test_abnormal_authorize_is_never_a_success"),
])


MUTATIONS.extend([
    ("flatten unconditional cleanup nesting", "src/trusted_router/gateway_timing.py",
     FINALIZATION, FLAT_FINALIZATION,
     UNIT + "test_interrupted_finalization_restores_scopes_and_records_loss"),
    ("swallow recorder process-control exceptions", SERVICE,
     "    except Exception:\n        _COVERAGE_UNKNOWN = True",
     "    except BaseException:\n        _COVERAGE_UNKNOWN = True",
     UNIT + "test_loss_recorder_interrupt_propagates_after_completion_failure"),
    ("retire observation after restoration", SERVICE,
     "        try:\n            observation.retired = True\n        finally:\n            restore_context(_CURRENT, token, previous)",
     "        restore_context(_CURRENT, token, previous)\n        observation.retired = True",
     UNIT + "test_double_restoration_failure_retires_before_next_sync_authorize"),
])


MUTATIONS.extend([
    ("retirement outside restoration finally", SERVICE,
     "        try:\n            observation.retired = True\n        finally:\n            restore_context(_CURRENT, token, previous)",
     "        observation.retired = True\n        restore_context(_CURRENT, token, previous)",
     UNIT + "test_assignment_fault_restores_scopes_and_preserves_response"),
    ("ignore observation request identity", SERVICE,
     " and current.request_identity is request_identity", "",
     UNIT + "test_reentrant_authorize_has_independent_request_observation"),
    ("mark finished outside isolation", "src/trusted_router/gateway_timing.py",
     "            cleanup(finish)", "            finish()",
     UNIT + "test_assignment_fault_restores_scopes_and_preserves_response"),
])


def main(names: set[str] | None = None) -> None:
    results = []
    with tempfile.TemporaryDirectory(prefix="astra-r3-mutations-", dir="/private/tmp") as directory:
        copied = Path(directory)
        for name in ("src", "tests", "scripts", "docs", ".github"):
            shutil.copytree(ROOT / name, copied / name, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "pyproject.toml", copied / "pyproject.toml")
        env = {**os.environ, "PYTHONPATH": str(copied / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        def run(test: str) -> tuple[str, str]:
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test,
                       "--tb=short", "--disable-warnings"]
            completed = subprocess.run(command, cwd=copied, env=env,  # noqa: S603 - fixed repository test command
                                       capture_output=True, text=True, timeout=240, check=False)
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
                if path.suffix == ".py":
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
