from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/gcp_ssh_metadata_hygiene.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gcp_ssh_metadata_hygiene", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prune_ci_keys_preserves_human_and_malformed_entries() -> None:
    module = _load_module()
    value = "\n".join(
        (
            "jperla:ssh-ed25519 AAAA-human jperla@laptop",
            "runner:ssh-rsa AAAA-ci runner@runnervmabc",
            "github-actions:ssh-rsa AAAA-ci-2 gha@runner",
            "a deliberately malformed line that must be preserved",
            "",
        )
    )

    cleaned, removed = module.prune_ci_ssh_keys(value)

    assert removed == 2
    assert "AAAA-human" in cleaned
    assert "malformed line" in cleaned
    assert "AAAA-ci" not in cleaned
    assert cleaned.endswith("\n")


@pytest.mark.parametrize("value", ["TRUE", "true", " True "])
def test_metadata_true_accepts_case_and_whitespace(value: str) -> None:
    module = _load_module()

    assert module.metadata_true(value)


@pytest.mark.parametrize("value", [None, "", "FALSE", "1", "yes"])
def test_metadata_true_rejects_everything_else(value: str | None) -> None:
    module = _load_module()

    assert not module.metadata_true(value)


def test_validate_instance_requires_both_access_controls() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="enable-oslogin"):
        module.validate_instance_metadata({"block-project-ssh-keys": "TRUE"})
    with pytest.raises(ValueError, match="block-project-ssh-keys"):
        module.validate_instance_metadata({"enable-oslogin": "TRUE"})

    module.validate_instance_metadata(
        {"enable-oslogin": "TRUE", "block-project-ssh-keys": "TRUE"}
    )


def test_metadata_limits_reject_the_original_failure_shape() -> None:
    module = _load_module()
    oversized = "\n".join(
        f"user{i}:ssh-rsa {'A' * 600} user{i}@host" for i in range(365)
    )

    with pytest.raises(ValueError, match="metadata remains too large"):
        module.validate_project_ssh_metadata(oversized)
