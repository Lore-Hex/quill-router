"""Periodically probe online user-provided models.

Dry-run is the default and only prints the models that would be probed. Pass
``--apply`` to send the signed canaries and persist their probe status.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os

os.environ.setdefault("TR_STORAGE_BACKEND", "spanner-bigtable")
os.environ.setdefault("TR_GCP_PROJECT_ID", "quill-cloud-proxy")
os.environ.setdefault("TR_SPANNER_INSTANCE_ID", "trusted-router-nam6")
os.environ.setdefault("TR_SPANNER_DATABASE_ID", "trusted-router")
os.environ.setdefault("TR_BIGTABLE_INSTANCE_ID", "trusted-router-logs")
os.environ.setdefault("TR_BIGTABLE_GENERATION_TABLE", "trustedrouter-generations")

from trusted_router.config import Settings
from trusted_router.services.user_model_reprobe import reprobe_user_models
from trusted_router.storage import create_store


def _positive_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("limit must be at least 1")
    return value


def _emit(record: dict[str, object]) -> None:
    print(json.dumps(record, separators=(",", ":"), sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=_positive_limit, default=100)
    parser.add_argument("--kind", choices=("machine", "agent", "human"))
    parser.add_argument("--apply", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    store = create_store(settings)
    report = await reprobe_user_models(
        settings,
        store=store,
        limit=args.limit,
        kind=args.kind,
        apply=args.apply,
    )
    for record in report.records:
        _emit({"type": "model", **dataclasses.asdict(record)})
    _emit(
        {
            "type": "summary",
            "scanned": report.scanned,
            "attempted": report.attempted,
            "passed": report.passed,
            "failed": report.failed,
            "dry_run": report.dry_run,
        }
    )
    return 1 if report.failed else 0


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
