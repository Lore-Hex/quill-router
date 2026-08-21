"""Executable fake-provider tests for the legacy hardening prerequisite."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/deploy/rollout_legacy_harden.py"
PROJECT = "quill-cloud-proxy"
SERVICE = "trusted-router"
REGIONS = ["us-central1", "us-east4"]
RUNTIME_ACCOUNT = "123456789-compute@developer.gserviceaccount.com"
IMAGE = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/app@sha256:" + (
    "a" * 64
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rollout_legacy_harden", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revision(region: str) -> dict[str, Any]:
    name = f"trusted-router-prior-{region}"
    return {
        "metadata": {"name": name, "annotations": {}},
        "spec": {
            "serviceAccountName": RUNTIME_ACCOUNT,
            "containerConcurrency": 2,
            "timeoutSeconds": 300,
            "containers": [
                {
                    "image": IMAGE,
                    "command": ["python"],
                    "args": ["-m", "trusted_router"],
                    "ports": [{"containerPort": 8080}],
                    "resources": {"limits": {"cpu": "1", "memory": "2Gi"}},
                    "startupProbe": {
                        "httpGet": {"path": "/ready", "port": 8080},
                        "failureThreshold": 18,
                        "periodSeconds": 10,
                        "timeoutSeconds": 10,
                    },
                    "env": [
                        {"name": "TR_REGION", "value": region},
                        {
                            "name": "STRIPE_SECRET_KEY",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": "trustedrouter-stripe-secret-key",
                                    "key": "latest",
                                }
                            },
                        },
                    ],
                }
            ],
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def _service(region: str, revision: dict[str, Any]) -> dict[str, Any]:
    name = revision["metadata"]["name"]
    return {
        "metadata": {
            "name": SERVICE,
            "generation": 7,
            "annotations": {
                "run.googleapis.com/ingress": "all",
                "run.googleapis.com/ingress-status": "all",
                "run.googleapis.com/default-url-disabled": "false",
            },
        },
        "spec": {
            "traffic": [{"latestRevision": True, "percent": 100}],
            "template": {
                "metadata": {
                    "name": name,
                    "annotations": {
                        "run.googleapis.com/vpc-access-egress": "private-ranges-only"
                    },
                },
                "spec": copy.deepcopy(revision["spec"]),
            },
        },
        "status": {
            "observedGeneration": 7,
            "latestCreatedRevisionName": name,
            "latestReadyRevisionName": name,
            "traffic": [{"revisionName": name, "percent": 100}],
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


class FakeCloud:
    def __init__(self) -> None:
        self.services: dict[str, dict[str, Any]] = {}
        self.revisions: dict[tuple[str, str], dict[str, Any]] = {}
        for region in REGIONS:
            revision = _revision(region)
            self.services[region] = _service(region, revision)
            self.revisions[(region, revision["metadata"]["name"])] = revision
        self.events: list[list[str]] = []
        self.nonzero_after_apply = False
        self.fail_before_once: str | None = None

    @staticmethod
    def _option(arguments: tuple[str, ...], name: str) -> str:
        prefix = f"{name}="
        return next(item[len(prefix) :] for item in arguments if item.startswith(prefix))

    def json(self, *arguments: str) -> Any:
        if arguments[:3] == ("run", "services", "describe"):
            return copy.deepcopy(self.services[self._option(arguments, "--region")])
        if arguments[:3] == ("run", "revisions", "describe"):
            key = (self._option(arguments, "--region"), arguments[3])
            if key not in self.revisions:
                raise ValueError("revision absent")
            return copy.deepcopy(self.revisions[key])
        if arguments[:4] == ("run", "services", "get-iam-policy", SERVICE):
            return {
                "bindings": [
                    {"role": "roles/run.invoker", "members": ["allUsers"]},
                    {
                        "role": "roles/run.invoker",
                        "members": [f"serviceAccount:{RUNTIME_ACCOUNT}"],
                    },
                ],
                "etag": "ignored-output-only",
            }
        if arguments[:3] == ("secrets", "versions", "describe"):
            version = "7" if arguments[3] == "latest" else arguments[3]
            return {
                "name": (
                    "projects/quill-cloud-proxy/secrets/"
                    f"trustedrouter-stripe-secret-key/versions/{version}"
                ),
                "state": "ENABLED",
            }
        if arguments[:3] == ("secrets", "get-iam-policy", "trustedrouter-stripe-secret-key"):
            return {
                "bindings": [
                    {
                        "role": "roles/secretmanager.secretAccessor",
                        "members": [f"serviceAccount:{RUNTIME_ACCOUNT}"],
                    }
                ]
            }
        raise AssertionError(f"unexpected read: {arguments!r}")

    def optional_json(self, *arguments: str) -> Any | None:
        try:
            return self.json(*arguments)
        except ValueError:
            return None

    def run(
        self, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del check
        self.events.append(list(arguments))
        region = self._option(arguments, "--region")
        command_text = " ".join(arguments)
        if self.fail_before_once and self.fail_before_once in command_text:
            self.fail_before_once = None
            return subprocess.CompletedProcess(arguments, 23, "", "provider error")
        if arguments[:3] == ("run", "services", "update-traffic"):
            assignments = self._option(arguments, "--to-revisions").split(",")
            traffic = []
            for assignment in assignments:
                revision, percent = assignment.rsplit("=", 1)
                traffic.append({"revisionName": revision, "percent": int(percent)})
            service = self.services[region]
            service["spec"]["traffic"] = copy.deepcopy(traffic)
            service["status"]["traffic"] = copy.deepcopy(traffic)
            service["metadata"]["generation"] += 1
            service["status"]["observedGeneration"] = service["metadata"]["generation"]
        elif arguments[:3] == ("run", "services", "update"):
            service = self.services[region]
            suffix = self._option(arguments, "--revision-suffix")
            candidate = f"{SERVICE}-{suffix}"
            baseline = service["status"]["traffic"][0]["revisionName"]
            spec = copy.deepcopy(self.revisions[(region, baseline)]["spec"])
            spec["serviceAccountName"] = self._option(arguments, "--service-account")
            pins = self._option(arguments, "--set-secrets")
            versions = {
                name: value.rsplit(":", 1)[1]
                for name, value in (item.split("=", 1) for item in pins.split(","))
            }
            for item in spec["containers"][0]["env"]:
                reference = ((item.get("valueFrom") or {}).get("secretKeyRef") or {})
                if reference:
                    reference["key"] = versions[item["name"]]
            revision = {
                "metadata": {"name": candidate, "annotations": {}},
                "spec": spec,
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
            self.revisions[(region, candidate)] = revision
            service["metadata"]["annotations"]["run.googleapis.com/ingress"] = (
                "internal-and-cloud-load-balancing"
            )
            service["metadata"]["annotations"]["run.googleapis.com/ingress-status"] = (
                "internal-and-cloud-load-balancing"
            )
            service["spec"]["template"] = {
                "metadata": {
                    "name": candidate,
                    "annotations": {
                        "run.googleapis.com/vpc-access-egress": "private-ranges-only"
                    },
                },
                "spec": copy.deepcopy(spec),
            }
            service["metadata"]["generation"] += 1
            service["status"]["observedGeneration"] = service["metadata"]["generation"]
            service["status"]["latestCreatedRevisionName"] = candidate
            service["status"]["latestReadyRevisionName"] = candidate
        else:
            raise AssertionError(f"unexpected mutation: {arguments!r}")
        return subprocess.CompletedProcess(
            arguments, 19 if self.nonzero_after_apply else 0, "", "provider error"
        )


def _invoke(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    cloud: FakeCloud,
    artifact: Path,
    *extra: str,
) -> None:
    monkeypatch.setattr(module, "Cloud", lambda project: cloud)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SOURCE),
            "--project",
            PROJECT,
            "--service",
            SERVICE,
            "--regions",
            ",".join(REGIONS),
            "--runtime-service-account",
            RUNTIME_ACCOUNT,
            "--operation-id",
            "legacy-hardener-test-operation",
            *extra,
            str(artifact),
        ],
    )
    module.main()


def test_hardens_latest_legacy_cohort_and_verifies_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    cloud = FakeCloud()
    cloud.nonzero_after_apply = True
    artifact = tmp_path / "legacy.json"

    _invoke(
        module,
        monkeypatch,
        cloud,
        artifact,
        "--revision-suffix",
        "hard1",
        "--artifact",
    )

    state_path = Path(f"{artifact}.state")
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert state_path.stat().st_mode & 0o777 == 0o600
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert {item["state"] for item in state["regions"]} == {"ramp100"}
    assert state["secret_refs"] == [
        {
            "name": "STRIPE_SECRET_KEY",
            "resource": "trustedrouter-stripe-secret-key",
            "version": "7",
        }
    ]
    for region in REGIONS:
        service = cloud.services[region]
        assert service["metadata"]["annotations"]["run.googleapis.com/ingress"] == (
            "internal-and-cloud-load-balancing"
        )
        assert service["spec"]["traffic"] == [
            {"revisionName": "trusted-router-hard1", "percent": 100}
        ]
    updates = [event for event in cloud.events if event[:3] == ["run", "services", "update"]]
    assert len(updates) == len(REGIONS)
    assert all(f"--service-account={RUNTIME_ACCOUNT}" in event for event in updates)
    assert all(
        "--set-secrets=STRIPE_SECRET_KEY=trustedrouter-stripe-secret-key:7" in event
        for event in updates
    )

    before = copy.deepcopy(cloud.events)
    _invoke(module, monkeypatch, cloud, artifact, "--verify-artifact")
    assert cloud.events == before


def test_mid_ramp_failure_rolls_back_then_same_suffix_resume_adopts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    cloud = FakeCloud()
    cloud.fail_before_once = "trusted-router-hard2=50,trusted-router-prior-us-east4=50"
    artifact = tmp_path / "legacy.json"

    with pytest.raises(SystemExit, match="exact baseline traffic was restored"):
        _invoke(
            module,
            monkeypatch,
            cloud,
            artifact,
            "--revision-suffix",
            "hard2",
            "--artifact",
        )

    assert not artifact.exists()
    state_path = Path(f"{artifact}.state")
    assert {item["state"] for item in json.loads(state_path.read_text())["regions"]} == {
        "rolled_back"
    }
    for region in REGIONS:
        assert cloud.services[region]["spec"]["traffic"] == [
            {"revisionName": f"trusted-router-prior-{region}", "percent": 100}
        ]
    deploy_count = sum(
        event[:3] == ["run", "services", "update"] for event in cloud.events
    )

    _invoke(module, monkeypatch, cloud, artifact, "--artifact")
    assert artifact.exists()
    assert sum(
        event[:3] == ["run", "services", "update"] for event in cloud.events
    ) == deploy_count
    assert {item["state"] for item in json.loads(state_path.read_text())["regions"]} == {
        "ramp100"
    }


def test_tampered_state_and_cross_project_secret_fail_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load()
    cloud = FakeCloud()
    artifact = tmp_path / "legacy.json"
    state_path = Path(f"{artifact}.state")
    state_path.write_text('{"unbound":true}\n', encoding="utf-8")
    state_path.chmod(0o600)
    with pytest.raises(SystemExit, match="state fields differ"):
        _invoke(module, monkeypatch, cloud, artifact, "--artifact")
    assert cloud.events == []

    state_path.unlink()
    for revision in cloud.revisions.values():
        reference = revision["spec"]["containers"][0]["env"][1]["valueFrom"][
            "secretKeyRef"
        ]
        reference["name"] = "projects/attacker-project/secrets/stripe-key"
    with pytest.raises(SystemExit, match="not local and canonical"):
        _invoke(
            module,
            monkeypatch,
            cloud,
            artifact,
            "--revision-suffix",
            "hard3",
            "--artifact",
        )
    assert cloud.events == []
