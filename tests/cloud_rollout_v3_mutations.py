"""Remove each schema-v3 lease rule in turn; its test must go red.

Usage: uv run python -m tests.cloud_rollout_v3_mutations [name ...]
Runs on a disposable copy, never the worktree. Each mutant runs only the test
named for its rule; the baseline and the restored file must pass that test,
and a compile error or timeout counts as build-broken, not as detection.
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
COORDINATOR = "scripts/deploy/cloud_rollout.py"
T = "tests/test_cloud_rollout_v3.py::"
MUTATIONS = [
    ("same component may share a cloud", COORDINATOR,
     "if version == VERSION or cloud not in CONCURRENT_CLOUDS or component == other:",
     "if version == VERSION or cloud not in CONCURRENT_CLOUDS:",
     T + "test_v3_same_component_on_one_cloud_stays_exclusive"),
    ("every cloud concurrent", COORDINATOR,
     'CONCURRENT_CLOUDS = frozenset({"gcp"})', 'CONCURRENT_CLOUDS = frozenset({"gcp", "aws", "azure"})',
     T + "test_v3_clouds_without_attributable_gates_stay_one_component_at_a_time"),
    ("public surface not exclusive", COORDINATOR,
     "    return component in EXCLUSIVE_COMPONENTS or other in EXCLUSIVE_COMPONENTS",
     "    return False",
     T + "test_v3_public_surface_is_exclusive_with_both_components"),
    ("blocked lease ignored", COORDINATOR,
     '        if lease["state"] != "active":\n            # A failed deploy',
     '        if False:\n            # A failed deploy',
     T + "test_v3_a_blocked_lease_stops_every_other_component_on_its_cloud"),
    ("cap counts leases", COORDINATOR,
     "    if new_cloud and len(clouds) >= 2:",
     '    if len(state["leases"]) >= 2:',
     T + "test_v3_cap_counts_distinct_clouds_not_leases"),
    ("holdback may be a leased cloud", COORDINATOR,
     'protected = self.holdback(leased_clouds(state) | {cloud}, state["holdback"])',
     'protected = self.holdback(set(leases) | {cloud}, state["holdback"])',
     T + "test_v3_a_leased_cloud_never_becomes_the_holdback"),
    ("release probes the whole cloud", COORDINATOR,
     '        if state["schema_version"] == V3:\n            self.component_probe(lease["cloud"], lease["component"])',
     '        if False:\n            self.component_probe(lease["cloud"], lease["component"])',
     T + "test_v3_release_probes_only_the_releasing_component"),
    ("gateway release probes the control plane too", COORDINATOR,
     '        if component == "gateway":\n            if fetch(f"{gateway}/health")',
     '        if component == "gateway":\n            fetch(f"{plane}/health")\n            if fetch(f"{gateway}/health")',
     T + "test_probe_component_reads_only_that_components_service"),
    ("control-plane release probes the gateway too", COORDINATOR,
     "        # The public service: the release evidence and the cloud's /health.\n",
     "        # The public service: the release evidence and the cloud's /health.\n        fetch(f\"{gateway}/health\")\n",
     T + "test_probe_component_reads_only_that_components_service"),
    ("sibling grant launders a changed holdback", COORDINATOR,
     '        if previous and previous["cloud"] not in excluded:\n            release = self.probe(previous["cloud"])',
     '        if False:\n            release = self.probe(previous["cloud"])',
     T + "test_v3_a_sibling_grant_never_launders_a_changed_holdback"),
    ("a changed holdback is leased over", COORDINATOR,
     "            if changed:\n                raise Refused(\"protected cloud changed outside the rollout guard; refusing",
     "            if False:\n                raise Refused(\"protected cloud changed outside the rollout guard; refusing",
     T + "test_leasing_a_changed_holdback_is_refused"),
    ("a down holdback can never be repaired", COORDINATOR,
     "            if changed:\n                raise Refused(",
     "            if changed or not self.releases(previous[\"cloud\"]):\n                raise Refused(",
     T + "test_a_down_holdback_may_still_be_leased_to_repair_it"),
    ("leasing over a holdback trusts its health probe", COORDINATOR,
     "        self.releases = releases or (observed_releases if probe is None else self._probe_releases)",
     "        self.releases = releases or self._probe_releases",
     T + "test_default_coordinator_reads_the_evidence_past_failing_services"),
    ("release evidence never read", COORDINATOR,
     "        if isinstance(release, str) and SHA.fullmatch(release):\n            seen.append(release)",
     "        if False:\n            seen.append(release)",
     T + "test_observed_releases_reads_the_evidence_twice_without_health"),
    ("release evidence read once", COORDINATOR,
     "    seen: list[str] = []\n    for _ in range(2):",
     "    seen: list[str] = []\n    for _ in range(1):",
     T + "test_observed_releases_reads_the_evidence_twice_without_health"),
    ("injected evidence ignored", COORDINATOR,
     "            changed = [r for r in self.releases(previous[\"cloud\"]) if r != previous[\"release\"]]",
     "            changed = [r for r in self._probe_releases(previous[\"cloud\"]) if r != previous[\"release\"]]",
     T + "test_a_changed_holdback_with_failing_services_is_still_refused"),
    ("held lease with epoch 0 accepted", COORDINATOR,
     "            or any(epochs.get(key, 0) < 1 for key in leases)):",
     "            or False):",
     T + "test_v3_journal_validation_refuses_states_no_client_writes"),
    ("unknown epoch key accepted", COORDINATOR,
     "            or not all(_valid_key(k) and type(v) is int and v >= 0 for k, v in epochs.items())",
     "            or not all(type(v) is int and v >= 0 for k, v in epochs.items())",
     T + "test_v3_journal_validation_refuses_states_no_client_writes"),
    ("journal id not required", COORDINATOR,
     '        raise Refused("invalid release journal id")', "        pass",
     T + "test_v3_journal_validation_refuses_states_no_client_writes"),
    ("upgrade drops carried epochs", COORDINATOR,
     "            carried = state.get(\"carried_epochs\")", "            carried = None",
     T + "test_epochs_survive_a_downgrade_and_every_upgrade_gets_a_new_journal_id"),
    ("upgrade reuses a journal id", COORDINATOR,
     '"epochs": epochs, "journal_id": uuid.uuid4().hex})', '"epochs": epochs, "journal_id": "f" * 32})',
     T + "test_epochs_survive_a_downgrade_and_every_upgrade_gets_a_new_journal_id"),
    ("downgrade drops epochs", COORDINATOR,
     "            if epochs:\n                state[\"carried_epochs\"] = epochs",
     "            if False:\n                state[\"carried_epochs\"] = epochs",
     T + "test_epochs_survive_a_downgrade_and_every_upgrade_gets_a_new_journal_id"),
    ("lanes reads once", COORDINATOR,
     "        for _ in range(8):\n            try:\n                raw, _ = self.store.read()\n                break",
     "        for _ in range(1):\n            try:\n                raw, _ = self.store.read()\n                break",
     T + "test_lanes_retries_a_conflicting_read"),
    ("lanes hides the journal id", COORDINATOR,
     '"journal_id": state.get("journal_id") if version == V3 else None}', '"journal_id": None}',
     T + "test_epochs_survive_a_downgrade_and_every_upgrade_gets_a_new_journal_id"),
    ("control-plane release skips the API service", COORDINATOR,
     '        if component == "control-plane":\n            api_service_answers(plane)',
     '        if False:\n            api_service_answers(plane)',
     T + "test_probe_component_reads_only_that_components_service"),
    ("API probe accepts any 401", COORDINATOR,
     '                    and error.get("type") == "unauthorized" and error.get("source") == "router"):',
     '                    or True):',
     T + "test_api_service_answers_only_with_its_own_401"),
    ("API probe accepts a 2xx", COORDINATOR,
     '                raise Refused("control-plane API answered an unauthenticated internal call")',
     "                return",
     T + "test_api_service_answers_only_with_its_own_401"),
    ("v2 release probes one component", COORDINATOR,
     '        if state["schema_version"] == V3:\n            self.component_probe(lease["cloud"], lease["component"])',
     '        if True:\n            self.component_probe(lease["cloud"], lease["component"])',
     T + "test_v2_release_still_probes_the_whole_cloud"),
    ("holdback cleared with any release", COORDINATOR,
     '        del state["leases"][key]\n        if not state["leases"]:',
     '        del state["leases"][key]\n        if True:',
     T + "test_v3_holdback_cleared_only_with_the_last_lease"),
    ("check guards every lease", COORDINATOR,
     '        if state["schema_version"] == V3 and key is not None:',
     '        if False:',
     T + "test_v3_check_survives_the_other_component_coming_and_going"),
    ("check ignores its own lease", COORDINATOR,
     '            return V3, state["leases"].get(key), state["holdback"]',
     '            return V3, None, state["holdback"]',
     T + "test_v3_check_refuses_when_its_own_lease_changes_meanwhile"),
    ("grant does not move the epoch", COORDINATOR,
     "            epochs[key] = epochs.get(key, 0) + 1", "            epochs[key] = epochs.get(key, 0)",
     T + "test_v3_every_grant_moves_its_lane_epoch_and_lanes_reports_it"),
    ("lanes hides epochs", COORDINATOR,
     '"epoch": epochs.get(f"{cloud}/{component}", 0) if version == V3 else None}',
     '"epoch": 0 if version == V3 else None}',
     T + "test_v3_every_grant_moves_its_lane_epoch_and_lanes_reports_it"),
    ("unknown v2 component holds one lane", COORDINATOR,
     "else list(COMPONENTS)\n", "else []\n",
     T + "test_v2_lease_without_a_known_component_holds_every_lane"),
    ("FIFO blocks other lanes", COORDINATOR,
     '               if e["cloud"] == cloud and conflicts(version, cloud, component, e["component"])]',
     '               if e["cloud"] == cloud]',
     T + "test_v3_an_earlier_waiter_for_the_other_lane_does_not_block"),
    ("FIFO ignores conflicting waiters", COORDINATOR,
     '               if e["cloud"] == cloud and conflicts(version, cloud, component, e["component"])]',
     '               if False]',
     T + "test_v3_an_earlier_conflicting_waiter_keeps_its_place"),
    ("cap FIFO counts same-cloud waiters", COORDINATOR,
     'if e["cloud"] not in clouds and e["cloud"] != cloud}',
     'if e["cloud"] not in clouds}',
     T + "test_v3_cap_fifo_a_same_cloud_waiter_shares_the_slot"),
    ("cap FIFO dropped", COORDINATOR,
     "    if len(others) >= 2 - len(clouds):", "    if False:",
     T + "test_v3_cap_fifo_earlier_waiters_for_other_new_clouds_take_slots_first"),
    ("v3 keyed by cloud", COORDINATOR,
     'return cloud if version == VERSION else f"{cloud}/{component}"', "return cloud",
     T + "test_v3_gcp_control_plane_and_gateway_hold_leases_at_once"),
    ("legacy v2 lease crashes admission", COORDINATOR,
     'conflicts(version, cloud, component, lease.get("component", ""))',
     'conflicts(version, cloud, component, lease["component"])',
     T + "test_v2_lease_without_a_component_still_blocks_its_cloud"),
    ("v2 becomes concurrent", COORDINATOR,
     "if version == VERSION or cloud not in CONCURRENT_CLOUDS or component == other:",
     "if cloud not in CONCURRENT_CLOUDS or component == other:",
     T + "test_v2_journal_keeps_one_lease_per_cloud_under_the_new_code"),
    ("upgrade with a lease", COORDINATOR,
     '                raise Busy("upgrade needs an empty journal: no lease and no waiter")',
     "                pass",
     T + "test_upgrade_needs_an_empty_journal"),
    ("downgrade with a lease", COORDINATOR,
     '                raise Busy("downgrade needs an empty journal: no lease and no waiter")',
     "                pass",
     T + "test_downgrade_needs_an_empty_journal"),
    ("fresh journal starts v3", COORDINATOR,
     '        return {"schema_version": VERSION, "leases": {}, "holdback": None}\n    version',
     '        return {"schema_version": V3, "leases": {}, "holdback": None, "epochs": {}}\n    version',
     T + "test_a_fresh_journal_starts_as_v2"),
    ("conflicting journal accepted", COORDINATOR,
     '                raise Refused("invalid release journal leases: conflicting components")',
     "                pass",
     T + "test_v3_journal_validation_refuses_states_no_client_writes"),
    ("lease key not checked", COORDINATOR,
     "                or not _valid_lease(lease, cloud) or lease.get(\"component\") != component):",
     "                or not isinstance(lease, dict)):",
     T + "test_v3_lease_key_must_match_its_cloud_and_component"),
    ("float version accepted", COORDINATOR,
     "if type(version) is not int or version not in (VERSION, V3):",
     "if version not in (VERSION, V3):",
     T + "test_unknown_journal_versions_stay_busy"),
]


def main(names: set[str] | None = None) -> None:
    results = []
    with tempfile.TemporaryDirectory(prefix="v3-lease-mutations-") as directory:
        copied = Path(directory)
        for name in ("src", "tests", "scripts"):
            shutil.copytree(ROOT / name, copied / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("pyproject.toml", ".python-version"):
            shutil.copy2(ROOT / name, copied / name)
        env = {**os.environ, "PYTHONPATH": f"{copied}:{copied / 'src'}", "PYTHONDONTWRITEBYTECODE": "1"}

        def run(test: str) -> tuple[str, str]:
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
                       test, "--tb=line", "--disable-warnings"]
            completed = subprocess.run(command, cwd=copied, env=env,  # noqa: S603 - fixed test command
                                       capture_output=True, text=True, timeout=240, check=False)
            output = completed.stdout + completed.stderr
            broken = completed.returncode not in (0, 1) or any(
                s in output for s in ("ERROR collecting", "SyntaxError", "ImportError"))
            return ("build-broken" if broken else "survived" if completed.returncode == 0 else "red"), output

        for name, relative, old, new, test in MUTATIONS:
            if names and name not in names:
                continue
            path = copied / relative
            original = path.read_text()
            assert original.count(old) == 1, f"{name}: anchor found {original.count(old)} times"
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
            results.append({"mutation": name, "result": result, "gate": test.split("::")[-1]})
            print(json.dumps(results[-1]), flush=True)
    killed = sum(row["result"] == "red" for row in results)
    print(f"{killed}/{len(results)} mutants killed")
    if killed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main(set(sys.argv[1:]) or None)
