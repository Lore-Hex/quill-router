"""One-shot scheduled Google Ads Data Manager uploader."""

from __future__ import annotations

import logging

import httpx

from trusted_router.config import get_settings
from trusted_router.services.google_data_manager import (
    GoogleDataManagerClient,
    GoogleDataManagerConfig,
    MetadataAccessTokenProvider,
    run_google_data_manager_once,
)
from trusted_router.storage_gcp_google_ads import create_google_ads_delivery_store


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.google_data_manager_enabled:
        logging.getLogger(__name__).info("google_data_manager.disabled")
        return 0

    store = create_google_ads_delivery_store(settings)
    config = GoogleDataManagerConfig.from_settings(settings)
    timeout = httpx.Timeout(settings.google_data_manager_timeout_seconds)
    with httpx.Client(timeout=timeout) as client:
        result = run_google_data_manager_once(
            store=store,
            settings=settings,
            client=GoogleDataManagerClient(
                config=config,
                client=client,
                token_provider=MetadataAccessTokenProvider(client),
            ),
        )
    logging.getLogger(__name__).info(
        "google_data_manager.run_complete claimed=%d submitted=%d "
        "failed=%d repaired=%d google_request_id=%s",
        result.claimed,
        result.submitted,
        result.failed,
        result.repaired,
        result.request_id or "",
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
