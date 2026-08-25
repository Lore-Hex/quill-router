from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    summarise,
)

SCRIPT = "scripts/deploy/internal_surface_edge.sh"


def _resolve_backend(url_map: dict[str, object], host: str, path: str) -> str:
    host_rule = next(
        rule
        for rule in url_map["hostRules"]  # type: ignore[index]
        if host in rule["hosts"]  # type: ignore[index]
    )
    matcher = next(
        item
        for item in url_map["pathMatchers"]  # type: ignore[index]
        if item["name"] == host_rule["pathMatcher"]  # type: ignore[index]
    )
    matches: list[tuple[int, int, str]] = []
    for rule in matcher["pathRules"]:  # type: ignore[index]
        for pattern in rule["paths"]:
            if pattern.endswith("/*"):
                prefix = pattern[:-1]
                if path.startswith(prefix):
                    matches.append((len(prefix), 0, rule["service"]))
            elif path == pattern:
                matches.append((len(pattern), 1, rule["service"]))
    if not matches:
        return str(matcher["defaultService"])  # type: ignore[index]
    return max(matches) [2]


def test_emitted_internal_cutover_map_uses_google_path_precedence(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "internal-map")
    state_dir = tmp_path / "state"

    run = harness.run(
        SCRIPT,
        args=("cutover",),
        extra_env={"TR_INTERNAL_EDGE_STATE_DIR": str(state_dir)},
    )

    assert run.returncode == 0, summarise(run)
    emitted = json.loads((harness.root / "url-map-state.json").read_text())
    expected = {
        "/internal/gateway/authorize": "trusted-router-internal-backend",
        "/internal/gateway/settle": "trusted-router-internal-backend",
        "/v1/internal/gateway/authorize": "trusted-router-internal-backend",
        "/internal": "trusted-router-internal-backend",
        "/v1/internal": "trusted-router-internal-backend",
        "/console": "trusted-router-control-backend",
        "/auth/session": "trusted-router-control-backend",
        "/bedrock-group-buy": "trusted-router-control-backend",
        "/": "trusted-router-public-backend",
        "/status.json": "trusted-router-public-backend",
        "/static/x.css": "trusted-router-public-backend",
    }
    for path, backend in expected.items():
        resolved = _resolve_backend(emitted, "trustedrouter.com", path)
        assert resolved.rsplit("/", 1)[-1] == backend


def _is_mutation(call: list[str]) -> bool:
    joined = " ".join(call)
    return any(
        marker in joined
        for marker in (
            " security-policies create ",
            " security-policies update ",
            " network-endpoint-groups create ",
            " backend-services create ",
            " backend-services update ",
            " backend-services add-backend ",
            " url-maps import ",
        )
    )


def test_missing_internal_policy_refuses_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SCRIPT_FIXTURES[SCRIPT]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        replace(
            original,
            failures=(
                *original.failures,
                r"security-policies describe trusted-router-internal-edge",
            ),
        ),
    )
    run = DeployScriptHarness(tmp_path / "missing-policy").run(
        SCRIPT, args=("prepare",)
    )
    assert run.returncode != 0
    assert "required pre-existing Cloud Armor policy" in run.stderr
    assert "gcloud compute security-policies create trusted-router-internal-edge" in (
        run.stderr
    )
    assert not any(_is_mutation(call) for call in run.calls)


def test_prepare_pins_cdn_off_header_logging_and_precreated_policy(
    tmp_path: Path,
) -> None:
    run = DeployScriptHarness(tmp_path / "prepare").run(SCRIPT, args=("prepare",))
    assert run.returncode == 0, summarise(run)
    updates = [
        call
        for call in run.calls
        if "backend-services" in call and "update" in call
    ]
    assert len(updates) == 1
    update = updates[0]
    assert "--no-enable-cdn" in update
    assert "--enable-cdn" not in update
    assert "--custom-request-header=X-TrustedRouter-Client-IP:{client_ip_address}" in update
    assert "--enable-logging" in update
    assert "--logging-sample-rate=1.0" in update
    assert "--security-policy=trusted-router-internal-edge" in update
    assert not any("security-policies" in call and "create" in call for call in run.calls)


def test_cutover_keeps_public_and_control_backend_assignments_unchanged(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "backend-isolation")
    run = harness.run(
        SCRIPT,
        args=("cutover",),
        extra_env={"TR_INTERNAL_EDGE_STATE_DIR": str(tmp_path / "state")},
    )
    assert run.returncode == 0, summarise(run)
    emitted = json.loads((harness.root / "url-map-state.json").read_text())
    matcher = next(
        item
        for item in emitted["pathMatchers"]
        if item["name"] == "trusted-router-service-surfaces"
    )
    services = {
        path: rule["service"].rsplit("/", 1)[-1]
        for rule in matcher["pathRules"]
        for path in rule["paths"]
    }
    assert services["/console"] == "trusted-router-control-backend"
    assert services["/auth/*"] == "trusted-router-control-backend"
    assert services["/v1/models"] == "trusted-router-public-backend"
    assert matcher["defaultService"].endswith("/trusted-router-public-backend")
    capture = json.loads(
        (
            tmp_path
            / "state"
            / "trusted-router-control-map.pre-internal-cutover.capture.json"
        ).read_text()
    )
    before = json.loads(base64.b64decode(capture["source_json_base64"]))
    for path in (
        "/console",
        "/auth/session",
        "/bedrock-group-buy",
        "/",
        "/status.json",
        "/static/x.css",
    ):
        assert _resolve_backend(before, "trustedrouter.com", path) == _resolve_backend(
            emitted, "trustedrouter.com", path
        )


def test_failed_unknown_cutover_automatically_restores_capture(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "auto-restore")
    state_dir = tmp_path / "state"
    run = harness.run(
        SCRIPT,
        args=("cutover",),
        extra_env={
            "TR_INTERNAL_EDGE_STATE_DIR": str(state_dir),
            "HARNESS_URL_MAP_IMPORT_FAIL_AFTER_APPLY": "1",
            "TR_INTERNAL_EDGE_ROLLBACK_CONFIRM_SECONDS": "0",
        },
    )
    assert run.returncode != 0
    assert "restoring captured map" in run.stderr
    imports = [call for call in run.calls if "url-maps" in call and "import" in call]
    assert len(imports) == 2
    restored = json.loads((harness.root / "url-map-state.json").read_text())
    assert restored["defaultService"].endswith("/trusted-router-public-backend")
    assert _resolve_backend(
        restored, "trustedrouter.com", "/internal/gateway/authorize"
    ).endswith("/trusted-router-control-backend")
