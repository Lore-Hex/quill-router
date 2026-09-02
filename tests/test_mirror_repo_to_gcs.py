from pathlib import Path

SCRIPT = Path("scripts/deploy/mirror_repo_to_gcs.sh")


def test_mirror_runtime_does_not_require_service_admin() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "services enable" not in script
    assert 'storage buckets describe "gs://${MIRROR_BUCKET}"' in script
    assert 'storage buckets update "gs://${MIRROR_BUCKET}" --versioning' in script
