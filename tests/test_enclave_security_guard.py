from pathlib import Path

import pytest
import yaml

from scripts.check_enclave_sentry import check_enclave


def _enclave(root: Path) -> Path:
    for name in ("go.mod", "cmd/enclave/main.go", "sidecar/go.mod", "Dockerfile.enclave.gcp.multi"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// fixture\n")
    return root


def test_missing_checkout_is_a_failure(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="Missing enclave source"):
        check_enclave(tmp_path / "absent")


@pytest.mark.parametrize("name", ["cmd/enclave/main.go", "sidecar/go.mod", "go.sum", "Dockerfile.enclave.gcp.multi"])
def test_sentry_in_code_or_dependencies_is_rejected(tmp_path: Path, name: str) -> None:
    root = _enclave(tmp_path)
    (root / name).write_text("github.com/getsentry/sentry-go v1.0.0\n")
    with pytest.raises(AssertionError, match="Forbidden Sentry"):
        check_enclave(root)


def test_invalid_source_encoding_is_not_silently_ignored(tmp_path: Path) -> None:
    root = _enclave(tmp_path)
    (root / "cmd/enclave/main.go").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        check_enclave(root)


def test_clean_source_passes(tmp_path: Path) -> None:
    check_enclave(_enclave(tmp_path))


def test_prose_and_unrelated_identifiers_are_not_sdk_imports(tmp_path: Path) -> None:
    root = _enclave(tmp_path)
    (root / "cmd/enclave/main.go").write_text("// No Sentry SDK.\nconst publicModelEndpointsEntryMaxBytes = 1024\n")
    check_enclave(root)


def test_source_symlinks_cannot_escape_the_scan(tmp_path: Path) -> None:
    root = _enclave(tmp_path / "enclave")
    (root / "external").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AssertionError, match="refuses symlink"):
        check_enclave(root)


def test_every_ci_suite_shard_fetches_and_scans_the_enclave() -> None:
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text())
    suites = [job for job in workflow["jobs"].values() if "Tests and coverage" in str(job) or "Tests with every scheduled cutover" in str(job)]
    assert len(suites) == 2
    for job in suites:
        steps = job["steps"]
        checkout = next(i for i, step in enumerate(steps) if step.get("with", {}).get("repository") == "Lore-Hex/quill-cloud-proxy")
        guard = next(i for i, step in enumerate(steps) if "python -m scripts.check_enclave_sentry" in step.get("run", ""))
        suite = next(i for i, step in enumerate(steps) if "uv run pytest" in step.get("run", ""))
        assert checkout < guard < suite
        assert not steps[checkout].get("continue-on-error")
        assert not steps[guard].get("continue-on-error")
        assert "GITHUB_ENV" in steps[guard]["run"]


def test_attestation_docs_distinguish_inspection_from_verification() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    assert "raw Google-signed JWT" in readme
    assert "submods.container.image_digest" in readme
    assert "GCP evidence has no pcrs field" in readme
    assert "Saving or decoding a JWT is **not verification**" in readme
    assert "tools/verify-attestation.py" in readme
    assert "| jq ." not in readme.split("Inspect the GCP evidence", 1)[1].split("</details>", 1)[0]
    security = (root / "docs/security.md").read_text()
    assert "GitHub Pages" in security
    assert "publish-trust-page.yml" in security
    assert "control-plane/trust hosting" not in security
