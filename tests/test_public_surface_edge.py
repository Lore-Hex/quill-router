from __future__ import annotations

import base64
import hashlib
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
IMPORTABLE_LIVE_MAP_BYTES = (
    json.dumps(
        {
            "name": "trusted-router-control-map",
            "fingerprint": "source-fingerprint",
            "defaultService": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/backendServices/trusted-router-control-backend"
            ),
        },
        separators=(",", ":"),
    )
    + "\n"
).encode()


def _capture_path(state_dir: Path) -> Path:
    return state_dir / "trusted-router-control-map.pre-public-cutover.capture.json"


def _captured_source(capture_path: Path) -> bytes:
    capture = json.loads(capture_path.read_text())
    return base64.b64decode(capture["source_json_base64"], validate=True)


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
    assert "--serve-while-stale=86400" in update
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


def test_cutover_then_rollback_imports_captured_importable_map_byte_for_byte(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "edge-rollback")
    state_dir = tmp_path / "durable-state"
    edge_env = {
        "TR_PUBLIC_EDGE_STATE_DIR": str(state_dir),
        "TR_PUBLIC_EDGE_CAPTURED_AT": "2026-08-22T12:34:56Z",
    }

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)
    captured = _capture_path(state_dir)
    assert _captured_source(captured) == IMPORTABLE_LIVE_MAP_BYTES
    capture_manifest = json.loads(captured.read_text())
    assert capture_manifest["captured_at"] == "2026-08-22T12:34:56Z"
    assert capture_manifest["source_fingerprint"] == "source-fingerprint"
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
    assert Path(rollback_source).name == "trusted-router-control-map.rollback-source.json"
    assert Path(rollback_source).read_bytes() == IMPORTABLE_LIVE_MAP_BYTES


def test_captured_map_passes_the_real_import_schema_contract(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "importable-capture")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)

    assert cutover.returncode == 0, summarise(cutover)
    captured_source = json.loads(_captured_source(_capture_path(state_dir)))
    assert set(captured_source) == {"defaultService", "fingerprint", "name"}
    assert any(
        "url-maps" in call
        and "validate" in call
        and any("rollback-validation.json" in item for item in call)
        for call in cutover.calls
    )
    rollback = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    assert rollback.returncode == 0, summarise(rollback)
    assert any(
        "url-maps" in call and "validate" in call for call in rollback.calls
    )


def test_extract_normalizes_a_capture_from_the_blocked_describe_implementation(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "legacy-describe-capture")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}
    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)

    capture_path = _capture_path(state_dir)
    capture = json.loads(capture_path.read_text())
    legacy_source = json.loads(_captured_source(capture_path))
    legacy_source.update(
        {
            "creationTimestamp": "2026-08-22T12:00:00.000-07:00",
            "id": "1234567890123456789",
            "kind": "compute#urlMap",
            "selfLink": "https://example.invalid/output-only",
        }
    )
    legacy_bytes = (json.dumps(legacy_source, separators=(",", ":")) + "\n").encode()
    capture["source_json_base64"] = base64.b64encode(legacy_bytes).decode("ascii")
    capture["source_sha256"] = hashlib.sha256(legacy_bytes).hexdigest()
    capture_path.write_text(json.dumps(capture))

    rollback = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    assert rollback.returncode == 0, summarise(rollback)


def test_cutover_recaptures_after_interrupted_capture_missing_digest(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "interrupted-capture")
    state_dir = tmp_path / "durable-state"
    state_dir.mkdir()
    interrupted_map = (
        state_dir / "trusted-router-control-map.pre-public-cutover.json"
    )
    interrupted_map.write_bytes(b'{"stale":"partial"}\n')
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)

    rollback = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    assert rollback.returncode == 0, summarise(rollback)
    rollback_import = next(
        call for call in rollback.calls if "url-maps" in call and "import" in call
    )
    rollback_source = next(
        item.removeprefix("--source=")
        for item in rollback_import
        if item.startswith("--source=")
    )
    assert Path(rollback_source).read_bytes() == IMPORTABLE_LIVE_MAP_BYTES


def test_corrupted_capture_digest_refuses_rollback(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "corrupted-capture")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}
    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)

    capture_path = _capture_path(state_dir)
    capture = json.loads(capture_path.read_text())
    capture["source_sha256"] = "0" * 64
    capture_path.write_text(json.dumps(capture))

    rollback = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    assert rollback.returncode != 0
    assert "stale or corrupt" in rollback.stderr
    assert not any("url-maps" in call and "import" in call for call in rollback.calls)

    recutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert recutover.returncode != 0
    assert "existing rollback capture is invalid" in recutover.stderr


def test_stale_capture_refuses_to_clobber_a_changed_live_map(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "stale-capture")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}
    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)

    live_state = harness.root / "url-map-state.json"
    live_map = json.loads(live_state.read_text())
    live_map["description"] = "unrelated operator change"
    live_map["fingerprint"] = "changed-after-cutover"
    live_state.write_text(json.dumps(live_map))

    rollback = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    assert rollback.returncode != 0
    assert "matches neither the captured source nor candidate" in rollback.stderr
    assert "stale or corrupt" in rollback.stderr
    assert not any("url-maps" in call and "import" in call for call in rollback.calls)

    recutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert recutover.returncode != 0
    assert "existing armed rollback capture" in recutover.stderr
    assert _captured_source(_capture_path(state_dir)) == IMPORTABLE_LIVE_MAP_BYTES


def test_import_transport_failure_after_apply_leaves_rollback_armed(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "failed-import")
    state_dir = tmp_path / "durable-state"
    edge_env = {
        "TR_PUBLIC_EDGE_STATE_DIR": str(state_dir),
        "HARNESS_URL_MAP_IMPORT_FAIL_AFTER_APPLY": "1",
    }

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode != 0
    assert "status is unknown; rollback remains armed" in cutover.stderr
    capture = json.loads(_capture_path(state_dir).read_text())
    assert capture["phase"] == "armed"
    assert _captured_source(_capture_path(state_dir)) == IMPORTABLE_LIVE_MAP_BYTES

    rollback = harness.run(
        SCRIPT,
        args=("rollback",),
        extra_env={"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)},
    )
    assert rollback.returncode == 0, summarise(rollback)
    assert any("url-maps" in call and "import" in call for call in rollback.calls)


def test_rollback_waits_for_an_accepted_pending_restore_import(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "pending-rollback-import")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}
    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)

    rollback = harness.run(
        SCRIPT,
        args=("rollback",),
        extra_env={
            **edge_env,
            "HARNESS_URL_MAP_ROLLBACK_PENDING_READS": "1",
            "TR_PUBLIC_EDGE_ROLLBACK_CONFIRM_SECONDS": "0",
        },
    )

    assert rollback.returncode == 0, summarise(rollback)
    assert "rollback import result is failure or unknown" in rollback.stderr
    rollback_describes = [
        call
        for call in rollback.calls
        if "url-maps" in call and "describe" in call and "--format=json" in call
    ]
    assert len(rollback_describes) >= 3
    assert json.loads(_capture_path(state_dir).read_text())["phase"] == "restored"


def test_import_failure_before_apply_rollback_is_a_safe_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SCRIPT_FIXTURES[SCRIPT]
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        replace(
            original,
            failures=(*original.failures, r"compute url-maps import"),
        ),
    )
    harness = DeployScriptHarness(tmp_path / "failed-before-apply")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode != 0
    assert json.loads(_capture_path(state_dir).read_text())["phase"] == "armed"

    rollback = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    assert rollback.returncode == 0, summarise(rollback)
    assert "already live" in rollback.stderr
    assert not any("url-maps" in call and "import" in call for call in rollback.calls)
    assert json.loads(_capture_path(state_dir).read_text())["phase"] == "restored"


def test_kill_after_import_keeps_the_pre_cutover_capture_rollbackable(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "killed-after-import")
    state_dir = tmp_path / "durable-state"
    edge_env = {
        "TR_PUBLIC_EDGE_STATE_DIR": str(state_dir),
        "HARNESS_URL_MAP_KILL_AFTER_APPLY": "1",
    }

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode != 0
    assert _captured_source(_capture_path(state_dir)) == IMPORTABLE_LIVE_MAP_BYTES

    rollback = harness.run(
        SCRIPT,
        args=("rollback",),
        extra_env={"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)},
    )
    assert rollback.returncode == 0, summarise(rollback)


def test_describe_failure_after_import_keeps_capture_rollbackable(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "describe-failed-after-import")
    state_dir = tmp_path / "durable-state"
    edge_env = {
        "TR_PUBLIC_EDGE_STATE_DIR": str(state_dir),
        "HARNESS_URL_MAP_POST_IMPORT_DESCRIBE_FAIL": "1",
    }

    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode != 0
    assert _captured_source(_capture_path(state_dir)) == IMPORTABLE_LIVE_MAP_BYTES

    rollback = harness.run(
        SCRIPT,
        args=("rollback",),
        extra_env={"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)},
    )
    assert rollback.returncode == 0, summarise(rollback)


def test_recutover_refuses_to_destroy_the_original_armed_capture(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "recutover")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}

    first = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert first.returncode == 0, summarise(first)
    original_capture = _capture_path(state_dir).read_bytes()

    second = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert second.returncode != 0
    assert "existing armed rollback capture" in second.stderr
    assert _capture_path(state_dir).read_bytes() == original_capture


def test_rollback_is_idempotent(tmp_path: Path) -> None:
    harness = DeployScriptHarness(tmp_path / "rollback-twice")
    state_dir = tmp_path / "durable-state"
    edge_env = {"TR_PUBLIC_EDGE_STATE_DIR": str(state_dir)}
    cutover = harness.run(SCRIPT, args=("cutover",), extra_env=edge_env)
    assert cutover.returncode == 0, summarise(cutover)

    first = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)
    second = harness.run(SCRIPT, args=("rollback",), extra_env=edge_env)

    assert first.returncode == 0, summarise(first)
    assert second.returncode == 0, summarise(second)


def test_harness_url_map_validation_rejects_a_malformed_candidate(
    tmp_path: Path,
) -> None:
    harness = DeployScriptHarness(tmp_path / "malformed-candidate")
    harness.write_script(
        "scripts/deploy/service_surface_url_map.py",
        """from pathlib import Path
import sys
output = Path(sys.argv[sys.argv.index('--output') + 1])
output.write_text('{}\\n')
""",
    )

    run = harness.run(
        SCRIPT,
        args=("cutover",),
        extra_env={"TR_PUBLIC_EDGE_STATE_DIR": str(tmp_path / "state")},
    )

    assert run.returncode != 0
    assert "wrong or missing name" in run.stderr
    assert not any("url-maps" in call and "import" in call for call in run.calls)
