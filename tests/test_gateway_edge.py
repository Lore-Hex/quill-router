from __future__ import annotations

import base64
import json
from copy import deepcopy
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
CONTROL = json.loads((Path(__file__).parent / "fixtures/gateway-edge/control-backend.fixture.json").read_text())


def gateway_fixture():
    # Independent literal live evidence plus reviewed differences, NOT the renderer.
    value = deepcopy(CONTROL)
    for key in ("kind", "selfLink", "cdnPolicy"):
        value.pop(key)
    value.update(name=BACKEND, enableCDN=False)
    value["backends"] = [entry for entry in value["backends"] if any(
        f"/regions/{region}/" in entry["group"] for region in TARGETS)]
    value["outlierDetection"] = {
        "consecutiveErrors": 12, "consecutiveGatewayFailure": 12,
        "enforcingConsecutiveErrors": 100, "enforcingConsecutiveGatewayFailure": 100,
        "interval": {"seconds": "1"}, "baseEjectionTime": {"seconds": "30"},
        "maxEjectionPercent": 50, "enforcingSuccessRate": 100,
        "successRateMinimumHosts": 5, "successRateRequestVolume": 100,
        "successRateStdevFactor": 1900,
    }
    return value


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
        "metadata": {"annotations": {"run.googleapis.com/minScale": "2",
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
         json.dumps(gateway_fixture())),
        (r"backend-services describe trusted-router-control-backend .*--format=json",
         json.dumps(CONTROL)),
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
    parity = deepcopy(CONTROL)
    for key in ("kind", "selfLink", "name", "backends", "enableCDN", "cdnPolicy"):
        parity.pop(key)
    assert {key: value for key, value in desired.items() if key not in {
        "name", "backends", "enableCDN", "outlierDetection"}} == parity
    assert desired["name"] == BACKEND and desired["enableCDN"] is False
    assert desired["backends"] == [{"group": f"{BASE}/regions/{r}/networkEndpointGroups/"
                                    "trusted-router-control-neg"} for r in TARGETS]
    assert desired["outlierDetection"] == {
        "consecutiveErrors": 12, "enforcingConsecutiveErrors": 100,
        "consecutiveGatewayFailure": 12, "enforcingConsecutiveGatewayFailure": 100,
        "interval": {"seconds": 1, "nanos": 0},
        "baseEjectionTime": {"seconds": 30, "nanos": 0}, "maxEjectionPercent": 50,
    }
    describes = [c for c in result.calls if "backend-services" in c and "describe" in c
                 and "trusted-router-control-backend" in c]
    assert len(describes) == 2  # Prepare snapshot, then fresh verification evidence.
    assert (harness.root / "state/gateway-backend.pre-prepare.json").is_file()


def test_prepare_and_verify_literal_live_control(tmp_path, monkeypatch):
    control = json.loads((Path(__file__).parent / "fixtures/gateway-edge/control-backend.fixture.json").read_text())
    assert control["enableCDN"] is True and control["cdnPolicy"]
    replace_response(monkeypatch, r"backend-services describe trusted-router-control-backend", control)
    harness = DeployScriptHarness(tmp_path / "live-control")
    prepared = run(harness, "prepare")
    assert prepared.returncode == 0, summarise(prepared)
    imports = mutations(prepared.calls)
    assert len(imports) == 1 and "backend-services" in imports[0] and "import" in imports[0]
    source = next(a.split("=", 1)[1] for a in imports[0] if a.startswith("--source="))
    desired = json.loads(Path(source).read_text())
    assert desired["enableCDN"] is False
    assert "cdnPolicy" not in desired
    assert desired["backends"] == [{"group": f"{BASE}/regions/{r}/networkEndpointGroups/"
                                    "trusted-router-control-neg"} for r in TARGETS]
    assert desired["outlierDetection"] == {
        "consecutiveErrors": 12, "enforcingConsecutiveErrors": 100,
        "consecutiveGatewayFailure": 12, "enforcingConsecutiveGatewayFailure": 100,
        "interval": {"seconds": 1, "nanos": 0},
        "baseEjectionTime": {"seconds": 30, "nanos": 0}, "maxEjectionPercent": 50,
    }
    verified = run(harness, "verify")
    assert verified.returncode == 0, summarise(verified)
    assert not mutations(verified.calls)


@pytest.mark.parametrize("field,value", [
    ("enableCDN", True), ("backends", [{"group": "neg", "preference": "PREFERRED"}]),
])
def test_prepare_overrides_control_cdn_and_preference(tmp_path, monkeypatch, field, value):
    control = deepcopy(CONTROL)
    control[field] = value
    replace_response(monkeypatch, r"backend-services describe trusted-router-control-backend", control)
    harness = DeployScriptHarness(tmp_path / "overrides")
    result = run(harness, "prepare")
    assert result.returncode == 0, summarise(result)
    imports = mutations(result.calls)
    assert len(imports) == 1 and "backend-services" in imports[0] and "import" in imports[0]
    source = next(a.split("=", 1)[1] for a in imports[0] if a.startswith("--source="))
    desired = json.loads(Path(source).read_text())
    assert desired["enableCDN"] is False
    assert "cdnPolicy" not in desired
    assert desired["backends"] == [{"group": f"{BASE}/regions/{r}/networkEndpointGroups/"
                                    "trusted-router-control-neg"} for r in TARGETS]


@pytest.mark.parametrize("source,field,value", [
    (source, field, value)
    for source in ("control", "rendered")
    for field, value in (
        ("iap", {"enabled": True}), ("healthChecks", ["health-check"]),
    )
] + [
    ("rendered", "enableCDN", True),
    ("rendered", "backends", [{"group": "neg", "preference": "PREFERRED"}]),
    ("rendered", "timeoutSec", 31), ("rendered", "cdnPolicy", {}),
])
def test_prepare_rejects_unsafe_backend_before_mutations(tmp_path, monkeypatch, source, field, value):
    harness = DeployScriptHarness(tmp_path / "unsafe")
    if source == "control":
        control = deepcopy(CONTROL)
        control[field] = value
        replace_response(monkeypatch, r"backend-services describe trusted-router-control-backend", control)
    else:
        # Corrupt only the serialized import payload, not the verifier's
        # independent expected configuration. This tests parity as well as bans.
        path = harness.mirror / "scripts/deploy/gateway_edge_config.py"
        path.write_text(path.read_text().replace(
            '        print(json.dumps(desired, indent=2))',
            f'        desired[{field!r}] = {value!r}\n        print(json.dumps(desired, indent=2))'))
    result = run(harness, "prepare")
    assert result.returncode != 0, summarise(result)
    assert field in result.stderr
    # Exact read-only call inventory: catches ANY mutation, not just known verbs.
    expected = [["gcloud", "projects", "describe", "quill-cloud-proxy", "--format=value(projectNumber)"]]
    for region in TARGETS:
        expected.extend([
            ["gcloud", "run", "services", "describe", "trusted-router", "--region", region, "--format=json"],
            ["gcloud", "run", "revisions", "describe", f"trusted-router-{region}", "--region", region, "--format=json"],
            ["gcloud", "compute", "network-endpoint-groups", "describe", "trusted-router-control-neg",
             "--region", region, "--format=json"],
        ])
    expected.append(["gcloud", "compute", "backend-services", "describe", "trusted-router-control-backend",
                     "--global", "--format=json"])
    expected = [[call[0], "--project", "quill-cloud-proxy", *call[1:]] for call in expected]
    gc_calls = [call for call in result.calls if call[0] in {"gc", "gcloud"}]
    assert len(gc_calls) == len(expected), gc_calls
    assert gc_calls == expected
    assert mutations(result.calls) == []


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
    actual = gateway_fixture()
    if "." in field:
        parent, child = field.split(".", 1)
        actual[parent] = {**actual[parent], child: value}
    else:
        actual[field] = value
    replace_response(monkeypatch, r"backend-services describe trusted-router-gateway-backend .*--format=json", actual)
    result = run(DeployScriptHarness(tmp_path / "drift"), "verify")
    assert result.returncode != 0 and "gateway" in result.stderr
    assert not mutations(result.calls)


@pytest.mark.parametrize("failovers", ["", "us-central1", "us-east4", "europe-west4", "unknown"])
def test_unsafe_geography_refused(tmp_path, failovers):
    result = run(DeployScriptHarness(tmp_path / "geo"), "prepare", TR_GATEWAY_FAILOVER_REGIONS=failovers)
    assert result.returncode != 0
    assert not mutations(result.calls)
    assert not any("run" in call or "compute" in call for call in result.calls)


@pytest.mark.parametrize("fault", ["missing", "cold", "one-warm", "split", "release", "digest", "not-ready", "wrong-neg"])
def test_fleet_preflight_refuses_unsafe_failover(tmp_path, monkeypatch, fault):
    region = TARGETS[1]
    if fault == "missing":
        fixture = SCRIPT_FIXTURES[SCRIPT]
        monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, replace(
            fixture, failures=(rf"run services describe trusted-router --region {region}",)))
    elif fault in {"cold", "one-warm", "split"}:
        value = service(region)
        if fault in {"cold", "one-warm"}:
            value["metadata"]["annotations"]["run.googleapis.com/minScale"] = "0" if fault == "cold" else "1"
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
    assert int(minimums["southamerica-east1"]) >= 2


# Every parity field from the live describe, plus response headers and a future
# setting: copying a fixed allowlist must not silently lose new edge behavior.
PARITY_CHANGES = [
    ("timeoutSec", 45), ("customRequestHeaders", ["X-Test:request"]),
    ("customResponseHeaders", ["X-Test:response"]),
    ("logConfig", {"enable": True, "sampleRate": 0.25,
                   "optionalMode": "CUSTOM", "optionalFields": ["tls.protocol"]}),
    ("securityPolicy", f"{BASE}/global/securityPolicies/replacement"),
    ("compressionMode", "DISABLED"), ("sessionAffinity", "CLIENT_IP"),
    ("connectionDraining", {"drainingTimeoutSec": 10}), ("portName", "other"),
    ("port", 81), ("affinityCookieTtlSec", 60), ("description", "new description"),
    ("futureParitySetting", {"enabled": True}),
]


@pytest.mark.parametrize("field,value", PARITY_CHANGES)
def test_prepare_copies_live_parity(tmp_path, monkeypatch, field, value):
    control, actual = deepcopy(CONTROL), gateway_fixture()
    control[field] = actual[field] = value
    replace_response(monkeypatch, r"backend-services describe trusted-router-control-backend", control)
    replace_response(monkeypatch, r"backend-services describe trusted-router-gateway-backend", actual)
    harness = DeployScriptHarness(tmp_path / "parity")
    result = run(harness, "prepare")
    assert result.returncode == 0, summarise(result)
    desired = json.loads((harness.root / "state/gateway-backend.desired.json").read_text())
    assert desired[field] == value


@pytest.mark.parametrize("field,value", PARITY_CHANGES)
def test_verify_detects_later_control_drift(tmp_path, monkeypatch, field, value):
    control = deepcopy(CONTROL)
    control[field] = value
    replace_response(monkeypatch, r"backend-services describe trusted-router-control-backend", control)
    result = run(DeployScriptHarness(tmp_path / "later-drift"), "verify")
    assert result.returncode != 0 and "parity drifted" in result.stderr
    assert field in result.stderr
    assert not mutations(result.calls)


@pytest.mark.parametrize("field", ["logConfig", "outlierDetection", "backends", "top-level"])
def test_verify_accepts_only_known_api_defaults(field):
    expected = CONFIG.backend("quill-cloud-proxy", BACKEND, TARGETS, CONTROL)
    actual = deepcopy(expected)
    if field == "logConfig":
        expected[field].pop("optionalMode")
        actual[field]["optionalFields"] = []
    elif field == "outlierDetection":
        actual[field] = gateway_fixture()[field]
    elif field == "backends":
        actual[field] = gateway_fixture()[field][::-1]
        for entry in actual[field]:
            entry["preference"] = "DEFAULT"
    else:
        for key in ("port", "portName", "sessionAffinity", "connectionDraining", "affinityCookieTtlSec"):
            expected.pop(key)
        actual.update(customResponseHeaders=[], healthChecks=[], iap={"enabled": False},
                      fingerprint="server-only", id="123", creationTimestamp="now", usedBy=[])
    CONFIG.verify_backend(actual, expected)


@pytest.mark.parametrize("field,value", [
    ("logConfig.optionalMode", "INCLUDE_ALL_OPTIONAL"),
    ("logConfig.optionalFields", ["tls.protocol"]),
    ("outlierDetection.successRateRequestVolume", 1),
    ("outlierDetection.futureSetting", 1),
    ("backends.capacityScaler", 0.5), ("backends.preference", "PREFERRED"),
    ("backends.failover", True), ("cdnPolicy", {}),
    ("customResponseHeaders", ["X-Unexpected:yes"]),
])
def test_verify_rejects_unexpected_readback(field, value):
    expected = CONFIG.backend("quill-cloud-proxy", BACKEND, TARGETS, CONTROL)
    actual = gateway_fixture()
    if "." in field:
        parent, child = field.split(".")
        target = actual[parent][0] if parent == "backends" else actual[parent]
        target[child] = value
    else:
        actual[field] = value
    with pytest.raises(ValueError, match="parity drifted"):
        CONFIG.verify_backend(actual, expected)


@pytest.mark.parametrize("field", ["consecutiveErrors", "consecutiveGatewayFailure"])
def test_outlier_threshold_clears_app_retry_runs(field):
    threshold = CONFIG.OUTLIER[field]
    # One three-attempt cycle, two concurrent callers, observed four-burst.
    for app_run in (3, 6, 4):
        assert threshold >= 2 * app_run
    assert threshold <= 12  # Bound the chosen outage-detection tradeoff too.
    total_rate = (9500 + 9500) / 3600
    assert 45 < threshold / (total_rate / 20) < 46


@pytest.mark.parametrize("command", ["prepare", "verify"])
def test_readonly_routing_failure_guidance(tmp_path, monkeypatch, command):
    fixture = SCRIPT_FIXTURES[SCRIPT]
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, replace(
        fixture, failures=(r"backend-services describe trusted-router-control-backend",)))
    result = run(DeployScriptHarness(tmp_path / command), command)
    assert result.returncode != 0
    assert "Restore the captured URL map" not in result.stderr
    assert "gateway_edge.sh rollback" not in result.stderr
    assert ("URL map unchanged" if command == "prepare" else "read-only checks") in result.stderr
    assert not mutations(result.calls)


def test_neg_sharing_failure_is_actionable_and_does_not_cutover(tmp_path, monkeypatch):
    fixture = SCRIPT_FIXTURES[SCRIPT]
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, replace(
        fixture, failures=(r"backend-services import trusted-router-gateway-backend",)))
    result = run(DeployScriptHarness(tmp_path / "sharing"), "prepare")
    assert result.returncode != 0
    assert "serverless NEG cannot be shared by two backend services" in result.stderr
    assert "dedicated trusted-router-gateway-neg NEGs pointing at the same service" in result.stderr
    assert "docs/runbooks/gateway-billing-edge.md" in result.stderr
    assert "URL map unchanged" in result.stderr
    assert not any("url-maps" in call for call in result.calls)


def test_operator_runbook_is_durable():
    root = Path(__file__).resolve().parents[1]
    script = (root / SCRIPT).read_text()
    assert "CODEX-REPORT-A1.md" not in script
    assert "docs/runbooks/gateway-billing-edge.md" in script.splitlines()[1]
    doc = (root / "docs/runbooks/gateway-billing-edge.md").read_text()
    for command in ("prepare", "verify", "cutover", "rollback"):
        assert f"bash scripts/deploy/gateway_edge.sh {command}" in doc
    for evidence in ("Prerequisites", "Failure drill", "Latency", "Cost", "25 s",
                     "9,500", "2.27P", "trusted-router-gateway-neg", "60 minutes"):
        assert evidence in doc
