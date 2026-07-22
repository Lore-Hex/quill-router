from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scripts import provision_creator_pilot
from tests.fakes.spanner import make_fake_store
from trusted_router.acquisition import (
    ATTRIBUTION_COOKIE_NAME,
    decode_attribution_cookie,
)
from trusted_router.typed_balance import live_credit_summary


def _owner(store: Any, email: str = "operator@example.com") -> Any:
    return store.ensure_user(email)


def test_creator_quickstart_is_public_and_campaign_attributed(
    client: TestClient,
) -> None:
    response = client.get(
        "/for-developers"
        "?utm_source=creator&utm_medium=sponsorship"
        "&utm_campaign=creator_pilot_202607"
        "&utm_content=theo_t3gg_cost_per_completed_job"
        "&utm_term=theo-tr"
    )

    assert response.status_code == 200
    assert "Try TrustedRouter in 60 Seconds" in response.text
    assert 'base_url="https://api.trustedrouter.com/v1"' in response.text
    assert "max_tokens=128" in response.text
    assert "https://trust.trustedrouter.com" in response.text
    assert "https://status.trustedrouter.com" in response.text
    assert 'href="/v1/models"' in response.text
    assert "does not make every downstream provider end-to-end encrypted" in response.text

    encoded = client.cookies.get(ATTRIBUTION_COOKIE_NAME)
    context = decode_attribution_cookie(encoded, client.app.state.settings)
    assert context is not None
    assert context.last_touch["utm_source"] == "creator"
    assert context.last_touch["utm_medium"] == "sponsorship"
    assert context.last_touch["utm_campaign"] == "creator_pilot_202607"
    assert context.last_touch["utm_term"] == "theo-tr"
    assert context.last_touch["landing_path"] == "/for-developers"


def test_creator_quickstart_is_in_core_sitemap(client: TestClient) -> None:
    response = client.get("/sitemap-core.xml")

    assert response.status_code == 200
    assert "<loc>https://trustedrouter.com/for-developers</loc>" in response.text


def test_manifest_is_valid_unique_and_integer_only() -> None:
    manifest = provision_creator_pilot.load_manifest(
        provision_creator_pilot.DEFAULT_MANIFEST
    )

    assert manifest.campaign == "creator_pilot_202607"
    assert manifest.creators[0].slug == "theo_t3gg"
    assert len(manifest.creators) == 13
    assert len({creator.slug for creator in manifest.creators}) == 13
    assert len({creator.viewer_code for creator in manifest.creators}) == 13
    assert all(
        isinstance(creator.creator_credit_microdollars, int)
        for creator in manifest.creators
    )


def test_tracking_url_is_stable_and_contains_no_secret() -> None:
    manifest = provision_creator_pilot.load_manifest(
        provision_creator_pilot.DEFAULT_MANIFEST
    )
    theo = manifest.creators[0]

    assert provision_creator_pilot.tracking_url(manifest, theo) == (
        "https://trustedrouter.com/for-developers"
        "?utm_source=creator&utm_medium=sponsorship"
        "&utm_campaign=creator_pilot_202607"
        "&utm_content=theo_t3gg_cost_per_completed_job"
        "&utm_term=theo-tr"
    )
    assert "sk-tr-v1-" not in provision_creator_pilot.tracking_url(manifest, theo)


def test_dry_run_does_not_create_workspace_credit_key_or_secret(
    tmp_path: Path,
) -> None:
    store, _database, _ = make_fake_store()
    owner = _owner(store)
    before = list(store.list_workspaces_for_user(owner.id))
    secret_path = tmp_path / "pilot.private"

    result = provision_creator_pilot.main(
        [
            "--owner-email",
            "operator@example.com",
            "--creator",
            "theo_t3gg",
            "--secrets-file",
            str(secret_path),
        ],
        store=store,
    )

    assert result == 0
    assert store.list_workspaces_for_user(owner.id) == before
    assert not secret_path.exists()


def test_apply_is_idempotent_capped_non_management_and_keeps_raw_key_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, _database, _ = make_fake_store()
    owner = _owner(store)
    secret_path = tmp_path / "pilot.private"
    argv = [
        "--owner-email",
        "operator@example.com",
        "--creator",
        "theo_t3gg",
        "--secrets-file",
        str(secret_path),
        "--apply",
    ]

    assert provision_creator_pilot.main(argv, store=store) == 0
    first_output = capsys.readouterr().out
    assert provision_creator_pilot.main(argv, store=store) == 0
    second_output = capsys.readouterr().out

    creator_workspaces = [
        workspace
        for workspace in store.list_workspaces_for_user(owner.id)
        if workspace.name == "Creator Pilot: Theo, t3.gg"
    ]
    assert len(creator_workspaces) == 1
    workspace = creator_workspaces[0]
    keys = store.list_keys(workspace.id)
    assert len(keys) == 1
    key = keys[0]
    assert key.management is False
    assert key.limit_microdollars == 250_000_000
    assert key.limit_daily_microdollars == 50_000_000
    assert key.limit_monthly_microdollars == 250_000_000
    assert key.budget_alert_only is False
    assert key.expires_at is not None
    assert key.tags == {
        "campaign": "creator_pilot_202607",
        "creator": "theo_t3gg",
        "purpose": "sponsored_test",
    }

    balance = live_credit_summary(workspace.id, store=store)
    assert balance is not None
    assert balance["total_credits"] == 250_000_000

    payload = json.loads(secret_path.read_text(encoding="utf-8"))
    credential = payload["credentials"]["theo_t3gg"]
    raw_key = credential["api_key"]
    assert raw_key.startswith("sk-tr-v1-")
    assert credential["key_id"] == key.hash
    assert credential["state"] == "active"
    assert store.get_key_by_raw(raw_key) == key
    assert raw_key not in first_output
    assert raw_key not in second_output
    assert "funding=applied key=created" in first_output
    assert "funding=existing key=existing" in second_output
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_apply_requires_private_suffix_before_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_STORAGE_BACKEND", "spanner-bigtable")
    store, _database, _ = make_fake_store()
    owner = _owner(store)

    result = provision_creator_pilot.main(
        [
            "--owner-email",
            "operator@example.com",
            "--creator",
            "theo_t3gg",
            "--secrets-file",
            str(tmp_path / "pilot.json"),
            "--apply",
        ],
        store=store,
    )

    assert result == 1
    assert all(
        workspace.name != "Creator Pilot: Theo, t3.gg"
        for workspace in store.list_workspaces_for_user(owner.id)
    )


def test_manifest_rejects_float_money(tmp_path: Path) -> None:
    manifest = {
        "campaign": "test",
        "landing_path": "/for-developers",
        "key_ttl_days": 30,
        "creators": [
            {
                "slug": "creator",
                "display_name": "Creator",
                "concept": "test",
                "viewer_code": "CREATOR-TR",
                "creator_credit_microdollars": 1.5,
                "daily_limit_microdollars": 1,
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="positive integer"):
        provision_creator_pilot.load_manifest(path)
