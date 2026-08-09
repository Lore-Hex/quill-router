from __future__ import annotations

import json

import pytest

from trusted_router.marketing_funnel import (
    aggregate_funnel_rows,
    build_axiom_funnel_query,
    microdollars_to_usd,
    parse_axiom_json_lines,
    percentage,
    render_markdown,
)


def _record(
    event: str,
    *,
    people: int,
    events: int | None = None,
    revenue_microdollars: int = 0,
    source: str = "google",
    campaign: str = "high_intent",
    creative: str = "privacy_a",
) -> dict[str, object]:
    return {
        "event": event,
        "people": people,
        "events": people if events is None else events,
        "revenue_microdollars": revenue_microdollars,
        "utm_source": source,
        "utm_medium": "paid_search",
        "utm_campaign": campaign,
        "utm_content": creative,
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
    assert "sum(amount_microdollars)" in query
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
    assert row.as_dict()["revenue_usd"] == "12.345678"
    assert row.as_dict()["signup_rate"] == "17.5%"
    assert row.as_dict()["activation_rate"] == "57.1%"
    assert row.as_dict()["checkout_rate"] == "42.9%"
    assert row.as_dict()["payment_method_rate"] == "28.6%"


def test_aggregate_filters_without_placing_user_input_in_apl() -> None:
    records = [
        _record("acquisition.landing_engaged", people=40),
        _record(
            "acquisition.landing_engaged",
            people=20,
            source="x",
            campaign="openrouter_conquest",
            creative="reliability_b",
        ),
    ]

    rows = aggregate_funnel_rows(records, source="x", creative="reliability_b")

    assert [(row.source, row.creative) for row in rows] == [
        ("x", "reliability_b")
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
