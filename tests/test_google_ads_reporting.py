from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from trusted_router.google_ads_reporting import (
    GoogleAdsReportingClient,
    GoogleAdsReportingConfig,
    GoogleAdsReportingError,
    GoogleAdsSpendReport,
    build_google_ads_spend_query,
    google_ads_reporting_window,
    parse_google_ads_search_stream,
)


def _credential() -> str:
    return "a" * 22


def _payload(*, duplicate: bool = False) -> list[dict[str, object]]:
    row = {
        "customer": {
            "currencyCode": "USD",
            "timeZone": "America/Los_Angeles",
        },
        "campaign": {"id": "123", "name": "OpenRouter alternative"},
        "adGroup": {"id": "456", "name": "OpenRouter migration"},
        "adGroupAd": {
            "ad": {
                "id": "789",
                "finalUrls": [
                    "https://trustedrouter.com/openrouter-alternative/test/"
                    "g3_or_migrate_attest_key?utm_campaign=google_search_messages_v3"
                    "&utm_content=g3_or_migrate_attest_key"
                    "&tr_exp=google_search_messages_v3"
                    "&tr_cell=g3_or_migrate_attest_key"
                ],
            }
        },
        "segments": {"date": "2026-08-23"},
        "metrics": {"impressions": "1000", "clicks": "30", "costMicros": "12345678"},
    }
    rows: list[dict[str, object]] = [row]
    if duplicate:
        rows.append(dict(row))
    return [{"results": rows, "requestId": "request-1"}]


def test_reporting_config_normalizes_ids_and_validates_time_zone() -> None:
    config = GoogleAdsReportingConfig.from_environment(
        {
            "TR_GOOGLE_ADS_REPORTING_CUSTOMER_ID": "123-456-7890",
            "TR_GOOGLE_ADS_REPORTING_LOGIN_CUSTOMER_ID": "999-888-7777",
            "TR_GOOGLE_ADS_DEVELOPER_TOKEN": _credential(),
            "TR_GOOGLE_ADS_REPORTING_TIME_ZONE": "America/Los_Angeles",
        }
    )

    assert config.customer_id == "1234567890"
    assert config.login_customer_id == "9998887777"
    assert config.developer_token == _credential()

    with pytest.raises(ValueError, match="TIME_ZONE"):
        GoogleAdsReportingConfig.from_environment(
            {
                "TR_GOOGLE_ADS_REPORTING_CUSTOMER_ID": "1234567890",
                "TR_GOOGLE_ADS_DEVELOPER_TOKEN": _credential(),
                "TR_GOOGLE_ADS_REPORTING_TIME_ZONE": "not/a-zone",
            }
        )


@pytest.mark.parametrize(
    "values, message",
    (
        ({"TR_GOOGLE_ADS_DEVELOPER_TOKEN": _credential()}, "customer ID"),
        ({"TR_GOOGLE_ADS_REPORTING_CUSTOMER_ID": "123"}, "DEVELOPER_TOKEN"),
    ),
)
def test_reporting_config_fails_closed_when_credentials_are_missing(
    values: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        GoogleAdsReportingConfig.from_environment(values)


def test_spend_query_is_bounded_and_metadata_only() -> None:
    query = build_google_ads_spend_query(
        dt.date(2026, 8, 17),
        dt.date(2026, 8, 23),
    )

    assert "segments.date BETWEEN '2026-08-17' AND '2026-08-23'" in query
    assert "metrics.cost_micros" in query
    assert "metrics.clicks" in query
    assert "metrics.impressions" in query
    assert "ad_group_ad.ad.final_urls" in query
    assert "FROM ad_group_ad" in query
    for forbidden in ("search_term", "email", "user_id", "prompt", "output"):
        assert forbidden not in query.lower()

    with pytest.raises(ValueError, match="precedes"):
        build_google_ads_spend_query(dt.date(2026, 8, 23), dt.date(2026, 8, 17))


def test_reporting_window_uses_account_local_calendar_days() -> None:
    start, end, start_at = google_ads_reporting_window(
        days=7,
        time_zone="America/Los_Angeles",
        now=dt.datetime(2026, 8, 23, 15, 30, tzinfo=dt.UTC),
    )

    assert start == dt.date(2026, 8, 17)
    assert end == dt.date(2026, 8, 23)
    assert start_at == dt.datetime(2026, 8, 17, 7, 0, tzinfo=dt.UTC)


def test_search_stream_parser_preserves_integer_micros() -> None:
    rows, currency, time_zone = parse_google_ads_search_stream(_payload())

    assert currency == "USD"
    assert time_zone == "America/Los_Angeles"
    assert len(rows) == 1
    assert rows[0].spend_microdollars == 12_345_678
    assert isinstance(rows[0].spend_microdollars, int)
    assert rows[0].clicks == 30
    assert rows[0].ad_id == "789"
    assert rows[0].experiment_id == "google_search_messages_v3"
    assert rows[0].experiment_cell_id == "g3_or_migrate_attest_key"
    assert rows[0].utm_campaign == "google_search_messages_v3"
    assert rows[0].landing_path.endswith("g3_or_migrate_attest_key")


def test_spend_report_filters_to_exact_experiment_cell() -> None:
    rows, currency, time_zone = parse_google_ads_search_stream(_payload())
    report = GoogleAdsSpendReport(
        customer_id="1234567890",
        currency_code=currency,
        time_zone=time_zone,
        start_date="2026-08-23",
        end_date="2026-08-23",
        rows=tuple(rows),
    )

    matching = report.filtered(
        campaign="google_search_messages_v3",
        experiment_id="google_search_messages_v3",
        experiment_cell_id="g3_or_migrate_attest_key",
    )
    missing = report.filtered(experiment_cell_id="g3_sec_privacy_source_key")

    assert matching.spend_microdollars == 12_345_678
    assert matching.clicks == 30
    assert matching.spend_by_experiment_cell() == {
        "g3_or_migrate_attest_key": 12_345_678
    }
    assert missing.rows == ()
    assert missing.spend_microdollars == 0


def test_parser_rejects_conflicting_or_partial_experiment_urls() -> None:
    payload = _payload()
    result = payload[0]["results"]
    assert isinstance(result, list)
    row = result[0]
    assert isinstance(row, dict)
    ad_group_ad = row["adGroupAd"]
    assert isinstance(ad_group_ad, dict)
    ad = ad_group_ad["ad"]
    assert isinstance(ad, dict)
    ad["finalUrls"] = [
        "https://trustedrouter.com/openrouter-alternative?tr_exp=google_search_messages_v3"
    ]

    with pytest.raises(GoogleAdsReportingError, match="identity"):
        parse_google_ads_search_stream(payload)


def test_parser_keeps_unattributed_legacy_ads_in_account_totals() -> None:
    payload = _payload()
    result = payload[0]["results"]
    assert isinstance(result, list)
    row = result[0]
    assert isinstance(row, dict)
    ad_group_ad = row["adGroupAd"]
    assert isinstance(ad_group_ad, dict)
    ad = ad_group_ad["ad"]
    assert isinstance(ad, dict)
    del ad["finalUrls"]

    rows, _, _ = parse_google_ads_search_stream(payload)

    assert rows[0].spend_microdollars == 12_345_678
    assert rows[0].experiment_cell_id == ""
    assert rows[0].landing_path == ""


def test_search_stream_parser_rejects_duplicates_and_bad_money() -> None:
    with pytest.raises(GoogleAdsReportingError, match="duplicate"):
        parse_google_ads_search_stream(_payload(duplicate=True))

    payload = _payload()
    result = payload[0]["results"]
    assert isinstance(result, list)
    row = result[0]
    assert isinstance(row, dict)
    row["metrics"] = {"impressions": "1", "clicks": "1", "costMicros": "-1"}
    with pytest.raises(GoogleAdsReportingError, match="negative"):
        parse_google_ads_search_stream(payload)


def test_client_sends_only_aggregate_query_and_required_auth_headers() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_payload())

    config = GoogleAdsReportingConfig(
        customer_id="1234567890",
        login_customer_id="9998887777",
        developer_token=_credential(),
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = GoogleAdsReportingClient(
            config=config,
            client=client,
            token_provider=lambda: "oauth-token",
        ).fetch_spend(
            start_date=dt.date(2026, 8, 17),
            end_date=dt.date(2026, 8, 23),
        )

    assert captured["url"] == (
        "https://googleads.googleapis.com/v25/customers/1234567890/googleAds:searchStream"
    )
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["developer-token"] == _credential()
    assert headers["login-customer-id"] == "9998887777"
    assert headers["authorization"] == "Bearer oauth-token"
    assert report.spend_microdollars == 12_345_678
    assert report.as_dict()["spend_microdollars"] == 12_345_678


def test_client_error_is_safe_and_does_not_echo_google_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            403,
            json={"error": {"message": "secret account detail", "status": "PERMISSION_DENIED"}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reporting = GoogleAdsReportingClient(
            config=GoogleAdsReportingConfig(
                customer_id="1234567890",
                developer_token=_credential(),
            ),
            client=client,
            token_provider=lambda: "oauth-token",
        )
        with pytest.raises(GoogleAdsReportingError) as caught:
            reporting.fetch_spend(
                start_date=dt.date(2026, 8, 17),
                end_date=dt.date(2026, 8, 23),
            )

    assert str(caught.value) == "Google Ads reporting returned HTTP 403"
    assert "secret account detail" not in str(caught.value)
