"""Run each new gateway-edge test against a behavioral regression in a COPY.

Usage: <test-venv>/bin/python tests/gateway_edge_mutations.py
No git operations, cloud CLIs, or network; deploy scripts use the recording harness.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "tests/test_gateway_edge.py::test_"
MUTATIONS = [
    ("generator override removed", "scripts/deploy/service_surface_url_map.py",
     "    if gateway_backend:\n", "    if False:\n",
     "gateway_generator_contract_and_all_registered_routes"),
    ("public rewrite drops gateway", "scripts/deploy/public_surface_edge.sh",
     "    --preserve-gateway-backend \\\n", "",
     "other_edge_cutovers_preserve_gateway[public]"),
    ("internal rewrite drops gateway", "scripts/deploy/internal_surface_edge.sh",
     "    --preserve-gateway-backend \\\n", "",
     "other_edge_cutovers_preserve_gateway[internal]"),
    ("wrong backend timeout", "scripts/deploy/gateway_edge_config.py",
     '    result["name"] = name', '    result["timeoutSec"] = 31\n    result["name"] = name',
     "prepare_imports_complete_backend_without_routing"),
    ("backend drift ignored", "scripts/deploy/gateway_edge_config.py",
     '    observed, desired = normalized_backend(actual), normalized_backend(expected)',
     '    return\n    observed, desired = normalized_backend(actual), normalized_backend(expected)',
     "verify_rejects_backend_drift"),
    ("geography guard bypassed", "scripts/deploy/gateway_edge_config.py",
     '    targets = [primary, *failovers.split(",")]',
     '    return [primary, *failovers.split(",")]\n    targets = [primary, *failovers.split(",")]',
     "unsafe_geography_refused"),
    ("fleet preflight skipped", "scripts/deploy/gateway_edge.sh",
     '  preflight\n', '  : # preflight omitted\n',
     "fleet_preflight_refuses_unsafe_failover"),
    ("rollback does nothing", "scripts/deploy/gateway_edge.sh",
     'rollback() {\n', 'rollback() {\n  return 0\n',
     "cutover_capture_and_rollback_preserve_other_routes"),
    ("failed import not restored", "scripts/deploy/gateway_edge.sh",
     '    rollback ||', '    true ||',
     "unknown_import_failure_restores_and_prints_exact_rollback"),
    ("stale capture accepted", "scripts/deploy/url_map_capture.py",
     '    raise ValueError(\n        "current URL-map content matches neither the captured source nor candidate"\n    )',
     '    print("candidate")',
     "rollback_refuses_stale_map"),
    ("malformed gateway rule discarded", "scripts/deploy/service_surface_url_map.py",
     '    if not rules:\n', '    if rules or not rules:\n',
     "generator_refuses_partial_gateway_rule"),
    ("failover minimum reduced to one", "scripts/deploy/_lib.sh",
     'southamerica-east1=2}', 'southamerica-east1=1}',
     "failover_minimum_is_in_rollout_config"),
    ("updated warm inventory rejects one-instance failover", "scripts/deploy/_lib.sh",
     'southamerica-east1=2}', 'southamerica-east1=1}',
     "tests/test_deploy_secret_wiring.py::test_all_attested_control_plane_regions_remain_warm"),
    ("log optional defaults compared strictly", "scripts/deploy/gateway_edge_config.py",
     '    if "outlierDetection" in result:',
     '    result["logConfig"] = value.get("logConfig", {})\n    if "outlierDetection" in result:',
     "verify_accepts_only_known_api_defaults[logConfig]"),
    ("outlier defaults compared strictly", "scripts/deploy/gateway_edge_config.py",
     '        result["outlierDetection"] = outlier',
     '        result["outlierDetection"] = value["outlierDetection"]',
     "verify_accepts_only_known_api_defaults[outlierDetection]"),
    ("NEG defaults compared strictly", "scripts/deploy/gateway_edge_config.py",
     '    return result\n\n\ndef verify_backend',
     '    result["backends"] = value["backends"]\n    return result\n\n\ndef verify_backend',
     "verify_accepts_only_known_api_defaults[backends]"),
    ("top-level defaults compared strictly", "scripts/deploy/gateway_edge_config.py",
     '    result = deepcopy(BACKEND_DEFAULTS)', '    result = {}',
     "verify_accepts_only_known_api_defaults[top-level]"),
    ("live parity source discarded", "scripts/deploy/gateway_edge_config.py",
     'for key, value in control.items() if key not in OUTPUT_ONLY',
     'for key, value in {}.items() if key not in OUTPUT_ONLY',
     "prepare_copies_live_parity"),
    ("later parity drift ignored", "scripts/deploy/gateway_edge_config.py",
     '    observed, desired = normalized_backend(actual), normalized_backend(expected)',
     '    return\n    observed, desired = normalized_backend(actual), normalized_backend(expected)',
     "verify_detects_later_control_drift"),
    ("unknown read-back fields ignored", "scripts/deploy/gateway_edge_config.py",
     '    observed, desired = normalized_backend(actual), normalized_backend(expected)',
     '    return\n    observed, desired = normalized_backend(actual), normalized_backend(expected)',
     "verify_rejects_unexpected_readback"),
    ("three gateway failures eject again", "scripts/deploy/gateway_edge_config.py",
     '"consecutiveGatewayFailure": 12', '"consecutiveGatewayFailure": 3',
     "outlier_threshold_clears_app_retry_runs[consecutiveGatewayFailure]"),
    ("five general errors eject again", "scripts/deploy/gateway_edge_config.py",
     '"consecutiveErrors": 12', '"consecutiveErrors": 5',
     "outlier_threshold_clears_app_retry_runs[consecutiveErrors]"),
    ("one warm standby passes preflight", "scripts/deploy/gateway_edge_config.py",
     '"run.googleapis.com/minScale", 0)) < 2:', '"run.googleapis.com/minScale", 0)) < 1:',
     "fleet_preflight_refuses_unsafe_failover[one-warm]"),
    ("prepare tells operator to restore map", "scripts/deploy/gateway_edge.sh",
     'URL map unchanged. Inspect backend', 'Restore the captured URL map. Inspect backend',
     "readonly_routing_failure_guidance[prepare]"),
    ("verify tells operator to restore map", "scripts/deploy/gateway_edge.sh",
     'read-only checks changed no routing.', 'Restore the captured URL map.',
     "readonly_routing_failure_guidance[verify]"),
    ("NEG sharing fallback guidance removed", "scripts/deploy/gateway_edge.sh",
     'serverless NEG cannot be shared by two backend services', 'backend failed',
     "neg_sharing_failure_is_actionable_and_does_not_cutover"),
    ("operator header points to uncommitted report", "scripts/deploy/gateway_edge.sh",
     '# Leader-local gateway billing with a warm regional failover. See docs/runbooks/gateway-billing-edge.md.',
     '# Leader-local gateway billing with a warm regional failover. See CODEX-REPORT-A1.md.',
     "operator_runbook_is_durable"),

]


def main() -> None:
    killed_cases = 0
    with tempfile.TemporaryDirectory(prefix="gateway-edge-mutations-") as temporary:
        copy_root = Path(temporary) / "repo"
        copy_root.mkdir()
        for name in ("src", "scripts", "tests", "clickhouse", "docs"):
            shutil.copytree(ROOT / name, copy_root / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"), symlinks=True)
        shutil.copy(ROOT / "pyproject.toml", copy_root / "pyproject.toml")
        for index, (label, file, old, new, selection) in enumerate(MUTATIONS):
            path = copy_root / file
            original = path.read_text()
            if old not in original:
                raise RuntimeError(f"mutation target changed: {label}")
            path.write_text(original.replace(old, new))
            xml_path = Path(temporary) / f"{index}.xml"
            try:
                result = subprocess.run(  # noqa: S603
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     (selection if "::" in selection else PREFIX + selection), "--tb=short", f"--junitxml={xml_path}"],
                    cwd=copy_root, capture_output=True, text=True, check=False,
                    env={**os.environ, "PYTHONPATH": str(copy_root / "src") + os.pathsep
                         + os.environ.get("PYTHONPATH", "")},
                )
                cases = ET.parse(xml_path).findall(".//testcase")  # noqa: S314 - own pytest output
                # A collection error, timeout, or skipped test is not a kill.
                if (result.returncode != 1 or not cases
                        or any(case.find("failure") is None for case in cases)):
                    raise RuntimeError(f"survived/invalid mutation {label}:\n"
                                       f"{result.stdout}\n{result.stderr}")
                killed_cases += len(cases)
                print(f"KILLED: {label}: {len(cases)} test cases", flush=True)
            finally:
                path.write_text(original)
                # Same-length edits can otherwise reuse a same-second pyc.
                for cache in copy_root.rglob("__pycache__"):
                    shutil.rmtree(cache)
    print(f"{len(MUTATIONS)} behavioral mutations killed; {killed_cases} new/updated test cases failed as required")


if __name__ == "__main__":
    main()
