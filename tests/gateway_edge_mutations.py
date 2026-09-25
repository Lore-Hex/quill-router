"""Run each new gateway-edge test against a behavioral regression in a COPY.

Usage: <test-venv>/bin/python tests/gateway_edge_mutations.py
No git operations, cloud CLIs, or network; deploy scripts use the recording harness.
"""
from __future__ import annotations

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
     '"timeoutSec": 30', '"timeoutSec": 31',
     "prepare_imports_complete_backend_without_routing"),
    ("backend drift ignored", "scripts/deploy/gateway_edge_config.py",
     '    for key, value in expected.items():',
     '    return\n    for key, value in expected.items():',
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
    ("failover cold again", "scripts/deploy/_lib.sh",
     'southamerica-east1=1}', 'southamerica-east1=0}',
     "failover_minimum_is_in_rollout_config"),
    ("updated warm inventory rejects cold failover", "scripts/deploy/_lib.sh",
     'southamerica-east1=1}', 'southamerica-east1=0}',
     "tests/test_deploy_secret_wiring.py::test_all_attested_control_plane_regions_remain_warm"),
]


def main() -> None:
    killed_cases = 0
    with tempfile.TemporaryDirectory(prefix="gateway-edge-mutations-") as temporary:
        copy_root = Path(temporary) / "repo"
        copy_root.mkdir()
        for name in ("src", "scripts", "tests", "clickhouse"):
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
