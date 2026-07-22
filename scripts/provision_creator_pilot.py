"""Provision capped, inference-only workspaces for a creator pilot.

The command is read-only unless ``--apply`` is supplied. New raw API keys are
written to a local ``*.private`` JSON file with mode 0600 and are never printed.

Example:
  uv run python scripts/provision_creator_pilot.py \
    --owner-email operator@example.com \
    --creator theo_t3gg \
    --secrets-file ~/.trustedrouter_creator_pilot.private \
    --apply
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.money import format_money_precise
from trusted_router.security import new_api_key
from trusted_router.storage import create_store

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    REPO_ROOT / "docs/marketing/creator-program/creator-pilot-202607.json"
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_CODE_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")


@dataclass(frozen=True)
class CreatorSpec:
    slug: str
    display_name: str
    concept: str
    viewer_code: str
    creator_credit_microdollars: int
    daily_limit_microdollars: int

    @property
    def workspace_name(self) -> str:
        return f"Creator Pilot: {self.display_name}"


@dataclass(frozen=True)
class PilotManifest:
    campaign: str
    landing_path: str
    key_ttl_days: int
    creators: tuple[CreatorSpec, ...]


def load_manifest(path: Path) -> PilotManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    campaign = _required_string(payload, "campaign")
    landing_path = _required_string(payload, "landing_path")
    if not landing_path.startswith("/") or landing_path.startswith("//"):
        raise ValueError("landing_path must be an absolute site path")
    key_ttl_days = _required_positive_int(payload, "key_ttl_days")
    raw_creators = payload.get("creators")
    if not isinstance(raw_creators, list) or not raw_creators:
        raise ValueError("manifest creators must be a non-empty array")

    creators: list[CreatorSpec] = []
    slugs: set[str] = set()
    codes: set[str] = set()
    for index, raw in enumerate(raw_creators):
        if not isinstance(raw, dict):
            raise ValueError(f"creators[{index}] must be an object")
        spec = CreatorSpec(
            slug=_required_string(raw, "slug"),
            display_name=_required_string(raw, "display_name"),
            concept=_required_string(raw, "concept"),
            viewer_code=_required_string(raw, "viewer_code"),
            creator_credit_microdollars=_required_positive_int(
                raw, "creator_credit_microdollars"
            ),
            daily_limit_microdollars=_required_positive_int(
                raw, "daily_limit_microdollars"
            ),
        )
        if not _SLUG_RE.fullmatch(spec.slug):
            raise ValueError(f"invalid creator slug: {spec.slug}")
        if not _SLUG_RE.fullmatch(spec.concept):
            raise ValueError(f"invalid creator concept: {spec.concept}")
        if not _CODE_RE.fullmatch(spec.viewer_code):
            raise ValueError(f"invalid viewer code: {spec.viewer_code}")
        if spec.daily_limit_microdollars > spec.creator_credit_microdollars:
            raise ValueError(f"daily limit exceeds total credit for {spec.slug}")
        if spec.slug in slugs or spec.viewer_code in codes:
            raise ValueError("creator slugs and viewer codes must be unique")
        slugs.add(spec.slug)
        codes.add(spec.viewer_code)
        creators.append(spec)
    return PilotManifest(campaign, landing_path, key_ttl_days, tuple(creators))


def tracking_url(manifest: PilotManifest, creator: CreatorSpec) -> str:
    query = urlencode(
        {
            "utm_source": "creator",
            "utm_medium": "sponsorship",
            "utm_campaign": manifest.campaign,
            "utm_content": f"{creator.slug}_{creator.concept}",
            "utm_term": creator.viewer_code.lower(),
        }
    )
    return f"https://trustedrouter.com{manifest.landing_path}?{query}"


def provision_creator(
    store: Any,
    *,
    owner_user: Any,
    manifest: PilotManifest,
    creator: CreatorSpec,
    apply: bool,
    raw_key: str | None = None,
) -> dict[str, Any]:
    matching = [
        workspace
        for workspace in store.list_workspaces_for_user(owner_user.id)
        if workspace.name == creator.workspace_name
        and workspace.owner_user_id == owner_user.id
    ]
    if len(matching) > 1:
        raise ValueError(f"multiple workspaces match {creator.workspace_name}")
    workspace = matching[0] if matching else None
    existing_key = None
    if workspace is not None:
        keys = store.list_keys(workspace.id)
        existing = [key for key in keys if key.name == _key_name(manifest)]
        if len(existing) > 1:
            raise ValueError(f"multiple pilot keys exist for {creator.slug}")
        existing_key = existing[0] if existing else None

    result: dict[str, Any] = {
        "slug": creator.slug,
        "display_name": creator.display_name,
        "tracking_url": tracking_url(manifest, creator),
        "viewer_code": creator.viewer_code,
        "workspace_id": workspace.id if workspace else None,
        "key_id": existing_key.hash if existing_key else None,
        "credit_microdollars": creator.creator_credit_microdollars,
        "created_workspace": False,
        "created_key": False,
        "credited": False,
    }
    if not apply:
        return result

    if workspace is None:
        workspace = store.create_workspace(
            owner_user.id,
            creator.workspace_name,
            trial_credit_microdollars=0,
        )
        result["workspace_id"] = workspace.id
        result["created_workspace"] = True

    event_id = f"{manifest.campaign}:{creator.slug}:creator_funding_v1"
    result["credited"] = store.credit_workspace_typed_direct(
        workspace.id,
        creator.creator_credit_microdollars,
        event_id,
    )

    keys = store.list_keys(workspace.id)
    existing = [key for key in keys if key.name == _key_name(manifest)]
    if len(existing) > 1:
        raise ValueError(f"multiple pilot keys exist for {creator.slug}")
    if existing:
        result["key_id"] = existing[0].hash
        return result

    expires_at = (
        dt.datetime.now(dt.UTC) + dt.timedelta(days=manifest.key_ttl_days)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    raw_key, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name=_key_name(manifest),
        creator_user_id=owner_user.id,
        management=False,
        raw_key=raw_key,
        limit_microdollars=creator.creator_credit_microdollars,
        limit_daily_microdollars=creator.daily_limit_microdollars,
        limit_monthly_microdollars=creator.creator_credit_microdollars,
        budget_alert_only=False,
        expires_at=expires_at,
        tags={
            "campaign": manifest.campaign,
            "creator": creator.slug,
            "purpose": "sponsored_test",
        },
    )
    result.update(
        {
            "key_id": api_key.hash,
            "api_key": raw_key,
            "created_key": True,
            "expires_at": expires_at,
        }
    )
    return result


def _key_name(manifest: PilotManifest) -> str:
    return f"Creator test: {manifest.campaign}"


def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _required_positive_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _load_secret_document(path: Path, campaign: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "campaign": campaign, "credentials": {}}
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ValueError(f"secrets file must not be group/world accessible: {oct(mode)}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("campaign") != campaign:
        raise ValueError("secrets file campaign does not match manifest")
    credentials = payload.get("credentials")
    if not isinstance(credentials, dict):
        raise ValueError("secrets file credentials must be an object")
    return payload


def _write_secret_document(path: Path, payload: dict[str, Any]) -> None:
    if path.suffix != ".private":
        raise ValueError("secrets file must end in .private so git ignores it")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None, *, store: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--creator", action="append", default=[])
    parser.add_argument("--secrets-file", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if args.apply and os.environ.get("TR_STORAGE_BACKEND") != "spanner-bigtable":
        print("ERROR: --apply requires TR_STORAGE_BACKEND=spanner-bigtable", file=sys.stderr)
        return 2
    if args.apply and args.secrets_file is None:
        print("ERROR: --apply requires --secrets-file", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(args.manifest)
        selected = set(args.creator)
        unknown = selected - {creator.slug for creator in manifest.creators}
        if unknown:
            raise ValueError(f"unknown creator slug(s): {', '.join(sorted(unknown))}")
        creators = [
            creator for creator in manifest.creators if not selected or creator.slug in selected
        ]
        active_store = create_store(Settings()) if store is None else store
        owner_user = active_store.find_user_by_email(args.owner_email)
        if owner_user is None:
            raise ValueError("owner email does not match an existing user")
        secrets = (
            _load_secret_document(args.secrets_file, manifest.campaign)
            if args.apply
            else None
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    total = sum(creator.creator_credit_microdollars for creator in creators)
    print(
        f"campaign={manifest.campaign} creators={len(creators)} "
        f"maximum_funded={format_money_precise(total)}"
    )
    try:
        for creator in creators:
            prepared_raw_key = None
            if args.apply:
                assert args.secrets_file is not None and secrets is not None
                credentials = secrets["credentials"]
                credential = credentials.get(creator.slug)
                if credential is not None and not isinstance(credential, dict):
                    raise ValueError(f"invalid credential record for {creator.slug}")
                prepared_raw_key = (
                    str(credential.get("api_key"))
                    if isinstance(credential, dict) and credential.get("api_key")
                    else new_api_key()
                )
                if not isinstance(credential, dict):
                    credentials[creator.slug] = {
                        "workspace_id": None,
                        "key_id": None,
                        "api_key": prepared_raw_key,
                        "expires_at": None,
                        "state": "prepared",
                    }
                    # Persist the recoverable raw key before creating its
                    # one-way hash in Spanner. A crash can then safely retry
                    # with the exact same key instead of orphaning access.
                    _write_secret_document(args.secrets_file, secrets)
            result = provision_creator(
                active_store,
                owner_user=owner_user,
                manifest=manifest,
                creator=creator,
                apply=args.apply,
                raw_key=prepared_raw_key,
            )
            action = "existing" if result["workspace_id"] else "would-create"
            if result["created_workspace"]:
                action = "created"
            funding = "would-credit"
            if args.apply:
                funding = "applied" if result["credited"] else "existing"
            key_action = "would-create"
            if args.apply:
                key_action = "created" if result["created_key"] else "existing"
            print(
                f"{creator.slug}: workspace={action} "
                f"funding={funding} key={key_action} "
                f"credit={format_money_precise(creator.creator_credit_microdollars)}"
            )
            print(f"  {result['tracking_url']}")
            result.pop("api_key", None)
            if args.apply:
                assert secrets is not None
                credentials = secrets["credentials"]
                credential = credentials[creator.slug]
                assert isinstance(credential, dict)
                stored_raw_key = str(credential["api_key"])
                persisted_key = active_store.get_key_by_raw(stored_raw_key)
                if persisted_key is None or persisted_key.hash != result["key_id"]:
                    raise ValueError(
                        f"private key material does not match pilot key for {creator.slug}"
                    )
                credentials[creator.slug] = {
                    "workspace_id": result["workspace_id"],
                    "key_id": result["key_id"],
                    "api_key": stored_raw_key,
                    "expires_at": result.get("expires_at") or credential.get("expires_at"),
                    "state": "active",
                }
                _write_secret_document(args.secrets_file, secrets)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.apply:
        assert args.secrets_file is not None and secrets is not None
        try:
            _write_secret_document(args.secrets_file, secrets)
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"Raw keys written only to {args.secrets_file}")
    else:
        print("DRY-RUN: no workspace, credit, or key changes were made")
    return 0


if __name__ == "__main__":
    sys.exit(main())
