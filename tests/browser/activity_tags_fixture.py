"""Render the Observability page and its tag endpoints from synthetic workspaces.

One workspace sends tags, one does not, and one tags every request with its
own id. The page HTML and every JSON answer come from the real routes over the
in-memory store, so the browser test drives the page script against what the
server returns. Every tag here is invented.
"""

import datetime as dt
import itertools
import json
from typing import Any

from fastapi.testclient import TestClient

from trusted_router import storage_activity_tags
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, Generation
from trusted_router.types import UsageType

# A value no request could carry today. If the page ever treated tag text as
# markup, this would run.
MARKUP = '<img src=x onerror="window.__tagMarkupRan=1">'
RANGES = ("1h", "24h", "7d", "30d", "90d")
FILTERS = (
    (None, None),
    ("team", None),
    ("team", "search"),
    ("team", MARKUP),
    ("environment", "production"),
)
# The id-per-request workspace is answered with a small distinct-value limit,
# so that 55 ids are "too many" without a fixture of half a million rows.
MANY_DISTINCT_LIMIT = 40


def _generation(workspace_id: str, index: int, tags: dict[str, str], cost: int,
                elapsed: int | None, age: dt.timedelta) -> Generation:
    created = dt.datetime.now(dt.UTC) - age
    return Generation(
        id=f"gen-{workspace_id[:6]}-{index}", request_id=f"req-{index}", workspace_id=workspace_id,
        key_hash="key-browser-tags", model="test/model", provider_name="Test Provider",
        app="browser-tags", tokens_prompt=1200 + index, tokens_completion=300 + index,
        total_cost_microdollars=cost, usage_type=UsageType.CREDITS, speed_tokens_per_second=40.0,
        finish_reason="stop", status="success", streamed=False, elapsed_milliseconds=elapsed,
        tags=tags, created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _key(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _session(app: Any, email: str) -> tuple[TestClient, str]:
    client = TestClient(app)
    user = STORE.ensure_user(email, trial_credit_microdollars=0)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw, _ = STORE.create_auth_session(
        user_id=user.id, provider="google", label=email, ttl_seconds=3600, state="active",
    )
    client.cookies.set("tr_session", raw)
    return client, workspace.id


def _answers(client: TestClient, path: str, bodies: list[dict[str, Any]]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for body in bodies:
        response = client.post(path, json=body)
        assert response.status_code in {200, 400}, response.text
        answers[_key(body)] = {"status": response.status_code, "body": response.json()}
    return answers


def _summaries(group_keys: tuple[str | None, ...], filters: tuple[tuple[str | None, str | None], ...],
               ranges: tuple[str, ...] = RANGES) -> list[dict[str, Any]]:
    return [
        {"range": range_, "group_by": group_by, "tag_key": key, "tag_value": value}
        for range_, group_by, (key, value) in itertools.product(ranges, group_keys, filters)
    ]


def _lists(filters: tuple[tuple[str | None, str | None], ...],
           ranges: tuple[str, ...] = RANGES) -> list[dict[str, Any]]:
    return [
        {"range": range_, "tag_key": key, "tag_value": value}
        for range_, (key, value) in itertools.product(ranges, filters)
        if key is not None
    ]


def render() -> dict[str, Any]:
    app = create_app(
        Settings(environment="test", storage_backend="memory", sentry_dsn=None),
        init_observability=False,
    )
    tagged, tagged_workspace = _session(app, "tags-browser@example.com")
    plain, plain_workspace = _session(app, "no-tags-browser@example.com")
    many, many_workspace = _session(app, "many-tags-browser@example.com")
    hours = dt.timedelta(hours=1)
    # Nothing in the last hour, five requests in the last day, one more in the
    # last thirty days.
    for index, (tags, cost, elapsed, age) in enumerate([
        ({"team": "search", "environment": "production"}, 1_250_000, 820, 2 * hours),
        ({"team": "search"}, 400_000, 640, 3 * hours),
        ({"team": "ads", "environment": "staging"}, 120_000, 910, 5 * hours),
        ({}, 50_000, 700, 8 * hours),
        ({"team": MARKUP}, 10_000, None, 10 * hours),
        ({"team": "search", "environment": "production"}, 2_000_000, 760, dt.timedelta(days=20)),
    ]):
        STORE.add_generation(_generation(tagged_workspace, index, tags, cost, elapsed, age))
    STORE.add_generation(_generation(plain_workspace, 0, {}, 75_000, 500, 3 * hours))
    for index in range(55):
        STORE.add_generation(_generation(
            many_workspace, index, {"request": f"req-{index:02d}", "team": "search"},
            10_000 + index, 400 + index, dt.timedelta(hours=2, minutes=index),
        ))

    usage = {
        range_: tagged.get(f"/console/activity/usage.json?range={range_}&by_model=1").json()
        for range_ in RANGES
    }
    page = tagged.get("/console/activity")
    plain_page = plain.get("/console/activity")
    many_page = many.get("/console/activity")
    assert page.status_code == plain_page.status_code == many_page.status_code == 200
    result: dict[str, Any] = {
        "markup": MARKUP,
        "tagged": {
            "page": page.text,
            "usage": usage,
            "tags": _answers(
                tagged, "/console/activity/tags.json",
                _summaries((None, "team", "environment"), FILTERS),
            ),
            "requests": _answers(tagged, "/console/activity/requests.json", _lists(FILTERS)),
        },
        "plain": {
            "page": plain_page.text,
            "tags": _answers(plain, "/console/activity/tags.json", _summaries((None,), ((None, None),))),
        },
    }
    many_filters = ((None, None), ("request", None), ("request", "req-07"), ("request", "req-54"))
    storage_activity_tags.TAG_DISTINCT_LIMIT = MANY_DISTINCT_LIMIT
    result["many"] = {
        "page": many_page.text,
        "distinct_limit": MANY_DISTINCT_LIMIT,
        "tags": _answers(
            many, "/console/activity/tags.json",
            _summaries((None, "request", "team"), many_filters, ("30d",)),
        ),
        "requests": _answers(many, "/console/activity/requests.json", _lists(many_filters, ("30d",))),
    }
    return result


if __name__ == "__main__":
    print(json.dumps(render()))
