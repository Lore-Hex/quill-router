from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.deploy import gateway_edge_config as CONFIG
from scripts.deploy import service_surface_url_map as URL_MAP
from trusted_router.config import Settings
from trusted_router.main import create_app

from .deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, ScriptFixture, summarise
from .route_inventory import route_paths
from .test_internal_surface_edge import _resolve_backend

SCRIPT = "scripts/deploy/gateway_edge.sh"
BACKEND = "trusted-router-gateway-backend"
BASE = "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy"
GATEWAY = f"{BASE}/global/backendServices/{BACKEND}"
TARGETS = ["us-central1", "southamerica-east1"]


def live_map(internal="trusted-router-control-backend"):
    link = f"{BASE}/global/backendServices/"
    return URL_MAP.rewrite_url_map(
        {"name": "trusted-router-control-map", "fingerprint": "before",
         "defaultService": link + "trusted-router-control-backend",
         "tests": [{"host": "trustedrouter.com", "path": "/internal/gateway/authorize"}]},
        link + "trusted-router-public-backend", link + "trusted-router-control-backend",
        link + "trusted-router-control-backend", link + internal,
        ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
    )


def service(region):
    return {
        "metadata": {"annotations": {"run.googleapis.com/minScale": "1",
                     "run.googleapis.com/ingress": "internal-and-cloud-load-balancing"}},
        "status": {"conditions": [{"type": "Ready", "status": "True"}],
                   "traffic": [{"revisionName": f"trusted-router-{region}", "percent": 100}]},
    }


def revision(region):
    return {"metadata": {"name": f"trusted-router-{region}"},
            "spec": {"containers": [{"env": [{"name": "TR_RELEASE", "value": "abc1234"}]}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}],
                       "imageDigest": "image@sha256:" + "a" * 64}}


@pytest.fixture(autouse=True)
def cloud_fixture(monkeypatch):
    responses = [
        (r"projects describe.*projectNumber", "44325983244"),
        (r"backend-services describe trusted-router-gateway-backend .*--format=json",
         json.dumps(CONFIG.backend("quill-cloud-proxy", BACKEND, TARGETS))),
        (r"url-maps describe trusted-router-control-map .*--format=json", json.dumps(live_map())),
    ]
    for region in TARGETS:
        responses += [
            (rf"run services describe trusted-router --region {region} .*--format=json",
             json.dumps(service(region))),
            (rf"run revisions describe trusted-router-{region} .*--format=json",
             json.dumps(revision(region))),
            (rf"network-endpoint-groups describe trusted-router-control-neg --region {region}",
             json.dumps({"networkEndpointType": "SERVERLESS", "cloudRun": {"service": "trusted-router"},
                         "selfLink": f"{BASE}/regions/{region}/networkEndpointGroups/"
                         "trusted-router-control-neg"})),
        ]
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, ScriptFixture(responses=tuple(responses)))


def replace_response(monkeypatch, pattern, value):
    fixture = SCRIPT_FIXTURES[SCRIPT]
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, replace(
        fixture, responses=((pattern, json.dumps(value)), *fixture.responses)))


def run(harness, command, **env):
    return harness.run(SCRIPT, args=(command,), extra_env={
        "TR_GATEWAY_EDGE_STATE_DIR": str(harness.root / "state"),
        "TR_GATEWAY_EDGE_ROLLBACK_CONFIRM_SECONDS": "0", **env})


def mutations(calls):
    return [c for c in calls if any(x in c for x in ("import", "create", "update", "add-backend"))]


def test_gateway_generator_contract_and_all_registered_routes():
    old = live_map()
    generated = CONFIG.candidate(old, GATEWAY, ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"])
    app = create_app(Settings(environment="test", service_surface="combined"),
                     configure_store_arg=False, init_observability=False)
    paths = route_paths(app)
    gateway_paths = {p for p in paths if p.startswith(("/internal/gateway/", "/v1/internal/gateway/"))}
    assert len(gateway_paths) >= 30
    internal = create_app(Settings(environment="test", service_surface="internal"),
                          configure_store_arg=False, init_observability=False)
    assert gateway_paths <= route_paths(internal)
    for path in paths | {"/internal/gateway", "/v1/internal/gateway", "/internal/gatewayish/x"}:
        wanted = GATEWAY if path in gateway_paths or path in {
            "/internal/gateway", "/v1/internal/gateway"} else _resolve_backend(old, "trustedrouter.com", path)
        assert _resolve_backend(generated, "trustedrouter.com", path) == wanted, path
    assert generated["tests"][0]["service"] == GATEWAY
    # No argument means the exact old four-surface output, including map tests.
    assert URL_MAP.rewrite_url_map(generated,
        *[f"{BASE}/global/backendServices/{b}" for b in (
            "trusted-router-public-backend", "trusted-router-control-backend",
            "trusted-router-control-backend", "trusted-router-control-backend")],
        ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"]) == old


@pytest.mark.parametrize("edge", ["public", "internal"])
def test_other_edge_cutovers_preserve_gateway(tmp_path, edge):
    harness = DeployScriptHarness(tmp_path / edge)
    before = CONFIG.candidate(live_map(), GATEWAY,
                             ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"])
    (harness.root / "url-map-state.json").write_text(json.dumps(before))
    result = harness.run(f"scripts/deploy/{edge}_surface_edge.sh", args=("cutover",),
                         extra_env={f"TR_{edge.upper()}_EDGE_STATE_DIR": str(tmp_path / "state")})
    assert result.returncode == 0, summarise(result)
    after = json.loads((harness.root / "url-map-state.json").read_text())
    assert URL_MAP.existing_gateway_backend(after) == GATEWAY
    assert _resolve_backend(after, "trustedrouter.com", "/internal/other") .endswith(
        "/trusted-router-internal-backend" if edge == "internal" else "/trusted-router-control-backend")


def test_prepare_imports_complete_backend_without_routing(tmp_path):
    harness = DeployScriptHarness(tmp_path / "prepare")
    result = run(harness, "prepare")
    assert result.returncode == 0, summarise(result)
    imports = mutations(result.calls)
    assert len(imports) == 1 and "backend-services" in imports[0] and "import" in imports[0]
    source = next(a.split("=", 1)[1] for a in imports[0] if a.startswith("--source="))
    desired = json.loads(Path(source).read_text())
    assert desired == {
        "name": BACKEND, "loadBalancingScheme": "EXTERNAL_MANAGED", "protocol": "HTTP",
        "enableCDN": False, "timeoutSec": 30,
        "customRequestHeaders": ["X-TrustedRouter-Client-IP:{client_ip_address}"],
        "logConfig": {"enable": True, "sampleRate": 0.1},
        "securityPolicy": f"{BASE}/global/securityPolicies/trusted-router-legacy-edge",
        "backends": [{"group": f"{BASE}/regions/{r}/networkEndpointGroups/"
                      "trusted-router-control-neg"} for r in TARGETS],
        "outlierDetection": {"consecutiveErrors": 5, "enforcingConsecutiveErrors": 100,
                             "consecutiveGatewayFailure": 3, "enforcingConsecutiveGatewayFailure": 100,
                             "interval": {"seconds": 1, "nanos": 0},
                             "baseEjectionTime": {"seconds": 30, "nanos": 0},
                             "maxEjectionPercent": 50},
    }
    assert (harness.root / "state/gateway-backend.pre-prepare.json").is_file()


@pytest.mark.parametrize("field,value", [
    ("enableCDN", True), ("timeoutSec", 31), ("loadBalancingScheme", "EXTERNAL"),
    ("protocol", "HTTPS"), ("securityPolicy", "wrong"), ("customRequestHeaders", []),
    ("logConfig", {"enable": False}), ("backends", []), ("outlierDetection", {}),
    *[("outlierDetection." + field, 0) for field in (
        "consecutiveErrors", "enforcingConsecutiveErrors", "consecutiveGatewayFailure",
        "enforcingConsecutiveGatewayFailure", "maxEjectionPercent")],
    ("outlierDetection.interval", {"seconds": 2}),
    ("outlierDetection.baseEjectionTime", {"seconds": 1}),
])
def test_verify_rejects_backend_drift(tmp_path, monkeypatch, field, value):
    actual = CONFIG.backend("quill-cloud-proxy", BACKEND, TARGETS)
    if "." in field:
        parent, child = field.split(".", 1)
        actual[parent] = {**actual[parent], child: value}
    else:
        actual[field] = value
    replace_response(monkeypatch, r"backend-services describe .*--format=json", actual)
    result = run(DeployScriptHarness(tmp_path / "drift"), "verify")
    assert result.returncode != 0 and "gateway" in result.stderr
    assert not mutations(result.calls)


@pytest.mark.parametrize("failovers", ["", "us-central1", "us-east4", "europe-west4", "unknown"])
def test_unsafe_geography_refused(tmp_path, failovers):
    result = run(DeployScriptHarness(tmp_path / "geo"), "prepare", TR_GATEWAY_FAILOVER_REGIONS=failovers)
    assert result.returncode != 0
    assert not mutations(result.calls)
    assert not any("run" in call or "compute" in call for call in result.calls)


@pytest.mark.parametrize("fault", ["missing", "cold", "split", "release", "digest", "not-ready", "wrong-neg"])
def test_fleet_preflight_refuses_unsafe_failover(tmp_path, monkeypatch, fault):
    region = TARGETS[1]
    if fault == "missing":
        fixture = SCRIPT_FIXTURES[SCRIPT]
        monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, replace(
            fixture, failures=(rf"run services describe trusted-router --region {region}",)))
    elif fault in {"cold", "split"}:
        value = service(region)
        if fault == "cold":
            value["metadata"]["annotations"]["run.googleapis.com/minScale"] = "0"
        else:
            value["status"]["traffic"][0]["percent"] = 50
        replace_response(monkeypatch, rf"run services describe trusted-router --region {region}", value)
    elif fault == "wrong-neg":
        replace_response(monkeypatch, rf"network-endpoint-groups describe .*--region {region}", {})
    else:
        value = revision(region)
        if fault == "release":
            value["spec"]["containers"][0]["env"][0]["value"] = "def5678"
        elif fault == "digest":
            value["status"]["imageDigest"] = "image@sha256:" + "b" * 64
        else:
            value["status"]["conditions"][0]["status"] = "False"
        replace_response(monkeypatch, rf"run revisions describe trusted-router-{region}", value)
    result = run(DeployScriptHarness(tmp_path / fault), "cutover")
    assert result.returncode != 0
    assert not mutations(result.calls)


@pytest.mark.parametrize("internal", ["trusted-router-control-backend", "trusted-router-internal-backend"])
def test_cutover_capture_and_rollback_preserve_other_routes(tmp_path, monkeypatch, internal):
    before = live_map(internal)
    replace_response(monkeypatch, r"url-maps describe .*--format=json", before)
    harness = DeployScriptHarness(tmp_path / "cutover")
    result = run(harness, "cutover")
    assert result.returncode == 0, summarise(result)
    after = json.loads((harness.root / "url-map-state.json").read_text())
    assert _resolve_backend(after, "trustedrouter.com", "/internal/gateway/settle") == GATEWAY
    assert _resolve_backend(after, "trustedrouter.com", "/internal/other").endswith("/" + internal)
    capture = json.loads((harness.root / "state/trusted-router-control-map.pre-gateway-cutover.capture.json").read_text())
    assert json.loads(base64.b64decode(capture["source_json_base64"])) == before
    result = run(harness, "rollback")
    assert result.returncode == 0, summarise(result)
    restored = json.loads((harness.root / "url-map-state.json").read_text())
    restored["fingerprint"] = before["fingerprint"]
    assert URL_MAP._strip_output_only(restored) == before


def test_unknown_import_failure_restores_and_prints_exact_rollback(tmp_path):
    harness = DeployScriptHarness(tmp_path / "failure")
    result = run(harness, "cutover", HARNESS_URL_MAP_IMPORT_FAIL_AFTER_APPLY="1")
    assert result.returncode != 0
    assert len([c for c in result.calls if "url-maps" in c and "import" in c]) == 2
    restored = json.loads((harness.root / "url-map-state.json").read_text())
    assert URL_MAP.existing_gateway_backend(restored) is None
    assert f"TR_GATEWAY_EDGE_STATE_DIR={harness.root}/state" in result.stderr
    assert "PROJECT_ID=quill-cloud-proxy" in result.stderr and "gateway_edge.sh rollback" in result.stderr


def test_rollback_refuses_stale_map(tmp_path):
    harness = DeployScriptHarness(tmp_path / "stale")
    assert run(harness, "cutover").returncode == 0
    state = harness.root / "url-map-state.json"
    live = json.loads(state.read_text())
    live["description"] = "unrelated operator change"
    state.write_text(json.dumps(live))
    result = run(harness, "rollback")
    assert result.returncode != 0 and "stale or corrupt" in result.stderr
    assert not mutations(result.calls)


def test_generator_refuses_partial_gateway_rule():
    live = live_map()
    live["pathMatchers"][-1]["pathRules"].append(
        {"paths": ["/internal/gateway/authorize"], "service": GATEWAY})
    with pytest.raises(ValueError, match="ambiguous gateway"):
        URL_MAP.existing_gateway_backend(live)


def test_failover_minimum_is_in_rollout_config(tmp_path):
    harness = DeployScriptHarness(tmp_path / "minimum")
    harness.write_script("scripts/deploy/read_gateway_min.sh", '''#!/usr/bin/env bash
set -eu
source "$(dirname "$0")/_lib.sh"
printf '%s\\n' "$TR_CLOUD_RUN_MIN_INSTANCES_BY_REGION"
''')
    result = harness.run("scripts/deploy/read_gateway_min.sh")
    assert result.returncode == 0, summarise(result)
    minimums = dict(item.split("=") for item in result.stdout.strip().split(","))
    assert int(minimums["southamerica-east1"]) >= 1
