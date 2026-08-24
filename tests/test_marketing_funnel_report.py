from __future__ import annotations

import json

import pytest

from trusted_router.google_ads_reporting import GoogleAdsSpendReport, GoogleAdsSpendRow
from trusted_router.marketing_funnel import (
    aggregate_funnel_rows,
    build_axiom_funnel_query,
    microdollars_to_usd,
    parse_axiom_json_lines,
    percentage,
    render_markdown,
    render_measurement_markdown,
    summarize_measurement,
)


def _record(
    event: str,
    *,
    people: int,
    events: int | None = None,
    revenue_microdollars: int = 0,
    google_ads_click_people: int = 0,
    google_ads_persisted_people: int = 0,
    source: str = "google",
    campaign: str = "high_intent",
    creative: str = "privacy_a",
    landing_path: str = "/openrouter-alternative",
) -> dict[str, object]:
    return {
        "event": event,
        "people": people,
        "events": people if events is None else events,
        "revenue_microdollars": revenue_microdollars,
        "google_ads_click_people": google_ads_click_people,
        "google_ads_persisted_people": google_ads_persisted_people,
        "utm_source": source,
        "utm_medium": "paid_search",
        "utm_campaign": campaign,
        "utm_content": creative,
        "landing_path": landing_path,
    }


def test_funnel_query_is_metadata_only_and_covers_every_stage() -> None:
    query = build_axiom_funnel_query("trusted-router-logs")

    for event in (
        "acquisition.landing_engaged",
        "acquisition.sign_in_opened",
        "acquisition.signup_completed",
        "acquisition.first_successful_api_call",
        "acquisition.free_credit_exhausted",
        "acquisition.checkout_started",
        "acquisition.payment_method_saved",
        "acquisition.credit_purchase_completed",
        "acquisition.retained_api_usage_7d",
    ):
        assert event in query
    assert "dcount(anonymous_fingerprint)" in query
    assert "dcountif(anonymous_fingerprint" in query
    assert "has_gclid == true" in query
    assert "has_gbraid == true" in query
    assert "has_wbraid == true" in query
    assert "column_ifexists('google_ads_click_persisted', false) == true" in query
    assert "sum(amount_microdollars)" in query
    assert "landing_path" in query
    for forbidden in (
        "prompt",
        "output",
        "email",
        "workspace_id",
        "api_key",
        "request_body",
    ):
        assert forbidden not in query.lower()


@pytest.mark.parametrize(
    "dataset",
    ("bad dataset", "trusted-router-logs']; delete table", ""),
)
def test_funnel_query_rejects_unsafe_dataset_names(dataset: str) -> None:
    with pytest.raises(ValueError, match="unsupported characters"):
        build_axiom_funnel_query(dataset)


def test_aggregate_funnel_rows_pivots_creative_and_preserves_integer_money() -> None:
    records = [
        _record("acquisition.landing_engaged", people=40, events=63),
        _record("acquisition.sign_in_opened", people=10),
        _record("acquisition.signup_completed", people=7),
        _record("acquisition.first_successful_api_call", people=4),
        _record("acquisition.free_credit_exhausted", people=3),
        _record("acquisition.checkout_started", people=3),
        _record("acquisition.payment_method_saved", people=2),
        _record(
            "acquisition.credit_purchase_completed",
            people=2,
            events=3,
            revenue_microdollars=12_345_678,
        ),
        _record("acquisition.retained_api_usage_7d", people=3),
    ]

    rows = aggregate_funnel_rows(records)

    assert len(rows) == 1
    row = rows[0]
    assert row.engaged_visitors == 40
    assert row.sign_in_visitors == 10
    assert row.signups == 7
    assert row.activated_users == 4
    assert row.free_credit_exhausted_users == 3
    assert row.checkout_started_users == 3
    assert row.payment_method_saved_users == 2
    assert row.purchasers == 2
    assert row.purchase_events == 3
    assert row.retained_users_7d == 3
    assert row.revenue_microdollars == 12_345_678
    assert row.landing_path == "/openrouter-alternative"
    assert row.as_dict()["revenue_usd"] == "12.345678"
    assert row.as_dict()["signup_rate"] == "17.5%"
    assert row.as_dict()["activation_rate"] == "57.1%"
    assert row.as_dict()["activation_per_engaged_rate"] == "10.0%"
    assert row.as_dict()["checkout_rate"] == "42.9%"
    assert row.as_dict()["payment_method_rate"] == "28.6%"
    assert row.as_dict()["purchase_per_engaged_rate"] == "5.0%"


def test_aggregate_filters_without_placing_user_input_in_apl() -> None:
    records = [
        _record("acquisition.landing_engaged", people=40),
        _record(
            "acquisition.landing_engaged",
            people=20,
            source="x",
            campaign="openrouter_conquest",
            creative="reliability_b",
            landing_path="/private-llm-api",
        ),
    ]

    rows = aggregate_funnel_rows(
        records,
        source="x",
        creative="reliability_b",
        landing_path="/private-llm-api",
    )

    assert [(row.source, row.creative, row.landing_path) for row in rows] == [
        ("x", "reliability_b", "/private-llm-api")
    ]


def test_aggregate_sorts_by_deepest_outcome_then_volume() -> None:
    records = [
        _record(
            "acquisition.landing_engaged",
            people=100,
            creative="many_visits",
        ),
        _record(
            "acquisition.signup_completed",
            people=10,
            creative="many_visits",
        ),
        _record(
            "acquisition.landing_engaged",
            people=20,
            creative="activated",
        ),
        _record(
            "acquisition.signup_completed",
            people=5,
            creative="activated",
        ),
        _record(
            "acquisition.first_successful_api_call",
            people=1,
            creative="activated",
        ),
    ]

    rows = aggregate_funnel_rows(records)

    assert [row.creative for row in rows] == ["activated", "many_visits"]


def test_duplicate_axiom_summary_rows_fail_instead_of_double_counting() -> None:
    record = _record("acquisition.signup_completed", people=2)
    with pytest.raises(ValueError, match="Duplicate Axiom summary row"):
        aggregate_funnel_rows([record, dict(record)])


def test_null_dimensions_are_explicit_and_unknown_events_are_ignored() -> None:
    rows = aggregate_funnel_rows(
        [
            {
                "event": "acquisition.landing_engaged",
                "people": 1,
                "events": 1,
                "revenue_microdollars": None,
                "utm_source": None,
                "utm_medium": None,
                "utm_campaign": None,
                "utm_content": None,
            },
            {"event": "acquisition.not_a_real_stage", "people": 99},
        ]
    )

    assert len(rows) == 1
    assert rows[0].source == "(direct)"
    assert rows[0].medium == "(none)"
    assert rows[0].campaign == "(none)"
    assert rows[0].creative == "(none)"
    assert rows[0].landing_path == "(unknown)"


def test_parse_axiom_json_lines_is_strict() -> None:
    payload = "\n".join(
        (
            json.dumps({"event": "acquisition.signup_completed", "people": 1}),
            "",
            json.dumps({"event": "acquisition.landing_engaged", "people": 4}),
        )
    )
    assert len(parse_axiom_json_lines(payload)) == 2
    with pytest.raises(ValueError, match="line 1"):
        parse_axiom_json_lines("{not-json}")
    with pytest.raises(ValueError, match="not an object"):
        parse_axiom_json_lines("[]")


def test_report_rendering_escapes_markdown_and_handles_empty_denominators() -> None:
    row = aggregate_funnel_rows(
        [
            _record(
                "acquisition.signup_completed",
                people=1,
                campaign="campaign|unsafe",
            )
        ]
    )[0]

    rendered = render_markdown([row])

    assert "campaign\\|unsafe" in rendered
    assert "/openrouter-alternative" in rendered
    assert "n/a" in rendered
    assert percentage(1, 3) == "33.3%"
    assert microdollars_to_usd(1) == "0.000001"


@pytest.mark.parametrize("value", (-1, True, "not-an-int"))
def test_invalid_counts_fail_closed(value: object) -> None:
    with pytest.raises(ValueError):
        aggregate_funnel_rows(
            [
                {
                    "event": "acquisition.landing_engaged",
                    "people": value,
                }
            ]
        )


def test_google_measurement_holds_when_utm_visits_have_no_click_ids_or_spend() -> None:
    rows = aggregate_funnel_rows(
        [
            _record("acquisition.landing_engaged", people=304),
            _record("acquisition.signup_completed", people=31),
            _record("acquisition.first_successful_api_call", people=7),
            _record("acquisition.checkout_started", people=2),
        ]
    )

    summary = summarize_measurement(
        rows,
        source="google",
        spend=None,
        spend_error="native_spend_not_configured",
    )

    assert summary.hold_scale is True
    assert summary.blockers == (
        "google_click_ids_missing",
        "native_spend_not_configured",
    )
    assert summary.as_dict()["purchase_cac_microdollars"] is None
    assert summary.as_dict()["roas"] is None
    rendered = render_measurement_markdown(summary)
    assert "**Scale decision:** HOLD" in rendered
    assert "304 engaged -> 31 signup -> 7 first call -> 2 checkout" in rendered


def test_google_measurement_uses_integer_spend_and_requires_a_purchase_to_scale() -> None:
    rows = aggregate_funnel_rows(
        [
            _record(
                "acquisition.landing_engaged",
                people=20,
                google_ads_click_people=18,
            ),
            _record(
                "acquisition.signup_completed",
                people=4,
                google_ads_click_people=4,
                google_ads_persisted_people=4,
            ),
            _record(
                "acquisition.first_successful_api_call",
                people=2,
                google_ads_click_people=2,
                google_ads_persisted_people=2,
            ),
        ]
    )
    spend = GoogleAdsSpendReport(
        customer_id="1234567890",
        currency_code="USD",
        time_zone="America/Los_Angeles",
        start_date="2026-08-17",
        end_date="2026-08-23",
        rows=(
            GoogleAdsSpendRow(
                date="2026-08-23",
                campaign_id="42",
                campaign_name="OpenRouter alternative",
                impressions=100,
                clicks=20,
                spend_microdollars=12_345_679,
            ),
        ),
    )

    summary = summarize_measurement(rows, source="google", spend=spend)

    assert summary.blockers == ("no_purchases_in_window",)
    assert summary.spend_microdollars == 12_345_679
    assert summary.as_dict()["signup_cac_microdollars"] == 3_086_420
    assert isinstance(summary.as_dict()["signup_cac_microdollars"], int)


def test_google_measurement_is_ready_only_with_click_evidence_spend_and_purchase() -> None:
    rows = aggregate_funnel_rows(
        [
            _record(
                "acquisition.landing_engaged",
                people=10,
                google_ads_click_people=9,
            ),
            _record(
                "acquisition.signup_completed",
                people=2,
                google_ads_click_people=2,
                google_ads_persisted_people=2,
            ),
            _record(
                "acquisition.first_successful_api_call",
                people=1,
                google_ads_click_people=1,
                google_ads_persisted_people=1,
            ),
            _record(
                "acquisition.credit_purchase_completed",
                people=1,
                revenue_microdollars=20_000_000,
                google_ads_click_people=1,
                google_ads_persisted_people=1,
            ),
        ]
    )
    spend = GoogleAdsSpendReport(
        customer_id="1234567890",
        currency_code="USD",
        time_zone="America/Los_Angeles",
        start_date="2026-08-17",
        end_date="2026-08-23",
        rows=(GoogleAdsSpendRow("2026-08-23", "42", "Privacy", 100, 10, 5_000_000),),
    )

    summary = summarize_measurement(rows, source="google", spend=spend)

    assert summary.hold_scale is False
    assert summary.blockers == ()
    assert summary.as_dict()["decision"] == "ready"
    assert summary.as_dict()["purchase_cac_microdollars"] == 5_000_000
    assert summary.as_dict()["roas"] == "4.0000"


def test_google_measurement_holds_when_click_backed_signups_were_not_persisted() -> None:
    rows = aggregate_funnel_rows(
        [
            _record(
                "acquisition.landing_engaged",
                people=304,
                google_ads_click_people=265,
            ),
            _record(
                "acquisition.signup_completed",
                people=33,
                google_ads_click_people=29,
                google_ads_persisted_people=0,
            ),
        ]
    )

    summary = summarize_measurement(
        rows,
        source="google",
        spend=None,
        spend_error="native_spend_not_configured",
    )

    assert summary.google_ads_click_signups == 29
    assert summary.google_ads_persisted_signups == 0
    assert summary.blockers == (
        "google_click_ids_not_persisted",
        "native_spend_not_configured",
    )
    assert "0 of 29 click-backed signups persisted" in render_measurement_markdown(summary)


def test_non_google_report_does_not_require_google_measurement() -> None:
    rows = aggregate_funnel_rows([_record("acquisition.landing_engaged", people=5, source="x")])

    summary = summarize_measurement(
        rows,
        source="x",
        spend=None,
        spend_error="native_spend_disabled",
    )

    assert summary.hold_scale is False
    assert summary.blockers == ()
