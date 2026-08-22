from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    summarise,
)

SCRIPT = "scripts/deploy/public_surface_edge.sh"
LIVE_MAP_BYTES = (
    json.dumps(
        {
            "name": "trusted-router-control-map",
            "defaultService": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/backendServices/trusted-router-control-backend"
            ),
        },
        separators=(",", ":"),
    )
    + "\n"
).encode()


def _is_cloud_mutation(call: list[str]) -> bool:
    joined = " ".join(call)
    return any(
        marker in joined
        for marker in (
            " security-policies create ",
            " security-policies update ",
            " security-policies rules create ",
            " security-policies rules update ",
            " network-endpoint-groups create ",
            " backend-services create ",
            " backend-services update ",
            " backend-services add-backend ",
            " url-maps import ",
        )
    )


def test_absent_public_policy_fails_before_any_cloud_mutation(
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
                r"compute security-policies describe trusted-router-public-edge",
            ),
        ),
    )
    harness = DeployScriptHarness(tmp_path / "missing-policy")

    run = harness.run(SCRIPT, args=("prepare",))

    assert run.returncode != 0
    assert "required pre-existing Cloud Armor policy" in run.stderr
    assert "gcloud compute security-policies create trusted-router-public-edge" in run.stderr
    assert "(?:trustedrouter" in run.stderr
    assert not any(_is_cloud_mutation(call) for call in run.calls)


def test_prepare_attaches_existing_policy_without_mutating_it(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "prepare-edge")

    run = harness.run(SCRIPT, args=("prepare",))

    assert run.returncode == 0, summarise(run)
    backend_updates = [
        call
        for call in run.calls
        if "backend-services" in call and "update" in call
    ]
    assert len(backend_updates) == 1
    update = backend_updates[0]
    assert "--enable-cdn" in update
    assert "--custom-request-header=X-TrustedRouter-Client-IP:{client_ip_address}" in update
    assert "--logging-sample-rate=0.1" in update
    assert "--security-policy=trusted-router-public-edge" in update
    assert not any(
        "security-policies" in call and any(item in call for item in ("create", "update"))
        for call in run.calls
    )


def test_cutover_refuses_companion_ingress_before_url_map_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SCRIPT_FIXTURES[SCRIPT]
    responses: list[tuple[str, str]] = []
    for pattern, response in original.responses:
        if "run services describe trusted-router-public" in pattern:
            service = json.loads(response)
            service["metadata"]["annotations"]["run.googleapis.com/ingress"] = "all"
            service["spec"]["template"]["spec"]["containers"][0]["env"][0][
                "value"
            ] = "untrusted"
            response = json.dumps(service, separators=(",", ":"))
        responses.append((pattern, response))
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        replace(original, responses=tuple(responses)),
    )
    harness = DeployScriptHarness(tmp_path / "companion-cutover")

    run = harness.run(
        SCRIPT,
        args=("cutover",),
        extra_env={"TR_PUBLIC_EDGE_STATE_DIR": str(tmp_path / "state")},
    )

    assert run.returncode != 0
    assert "is not in routed mode" in run.stderr
    assert not any("url-maps" in call and "import" in call for call in run.calls)


def test_cutover_then_rollback_imports_captured_map_byte_for_byte(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "edge-rollback")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)
    captured = state_dir / "trusted-router-control-map.pre-public-cutover.json"
    assert captured.read_bytes() == LIVE_MAP_BYTES
    cutover_imports = [
        call for call in cutover.calls if "url-maps" in call and "import" in call
    ]
    assert len(cutover_imports) == 1
    assert any("public-candidate.json" in item for item in cutover_imports[0])

    rollback = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    assert rollback.returncode == 0, summarise(rollback)
    rollback_imports = [
        call for call in rollback.calls if "url-maps" in call and "import" in call
    ]
    assert len(rollback_imports) == 1
    rollback_source = next(
        item.removeprefix("--source=")
        for item in rollback_imports[0]
        if item.startswith("--source=")
    )
    assert Path(rollback_source) == captured
    assert Path(rollback_source).read_bytes() == LIVE_MAP_BYTES
