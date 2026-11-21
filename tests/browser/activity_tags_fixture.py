"""Render the Observability page and its tag endpoints from synthetic workspaces.

One workspace sends tags and one does not. The page HTML and every JSON answer
come from the real routes over the in-memory store, so the browser test drives
the page script against what the server returns. Every tag here is invented.
"""

import datetime as dt
import itertools
import json
from typing import Any

from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE, Generation

# A value no request could carry today. If the page ever treated tag text as
# markup, this would run.
MARKUP = '<img src=x onerror="window.__tagMarkupRan=1">'
RANGES = ("30d", "7d")
FILTERS = (
    (None, None),
    ("team", None),
    ("team", "search"),
    ("team", MARKUP),
    ("environment", "production"),
)


def _generation(workspace_id: str, index: int, tags: dict[str, str], cost: int,
                elapsed: int | None, age: dt.timedelta) -> Generation:
    created = dt.datetime.now(dt.UTC) - age
    return Generation(
        id=f"gen-{workspace_id[:6]}-{index}", request_id=f"req-{index}", workspace_id=workspace_id,
        key_hash="key-browser-tags", model="test/model", provider_name="Test Provider",
        app="browser-tags", tokens_prompt=1200 + index, tokens_completion=300 + index,
        total_cost_microdollars=cost, usage_type="Credits", speed_tokens_per_second=40.0,
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


def render() -> dict[str, Any]:
    app = create_app(
        Settings(environment="test", storage_backend="memory", sentry_dsn=None),
        init_observability=False,
    )
    tagged, tagged_workspace = _session(app, "tags-browser@example.com")
    plain, plain_workspace = _session(app, "no-tags-browser@example.com")
    minutes = dt.timedelta(minutes=1)
    for index, (tags, cost, elapsed, age) in enumerate([
        ({"team": "search", "environment": "production"}, 1_250_000, 820, 2 * minutes),
        ({"team": "search"}, 400_000, 640, 5 * minutes),
        ({"team": "ads", "environment": "staging"}, 120_000, 910, 9 * minutes),
        ({}, 50_000, 700, 12 * minutes),
        ({"team": MARKUP}, 10_000, None, 15 * minutes),
        ({"team": "search", "environment": "production"}, 2_000_000, 760, dt.timedelta(days=20)),
    ]):
        STORE.add_generation(_generation(tagged_workspace, index, tags, cost, elapsed, age))
    STORE.add_generation(_generation(plain_workspace, 0, {}, 75_000, 500, 3 * minutes))

    summaries: list[dict[str, Any]] = [
        {"range": range_, "group_by": group_by, "tag_key": key, "tag_value": value}
        for range_, group_by, (key, value) in itertools.product(
            RANGES, (None, "team", "environment"), FILTERS
        )
    ]
    lists: list[dict[str, Any]] = [
        {"range": range_, "tag_key": key, "tag_value": value}
        for range_, (key, value) in itertools.product(RANGES, FILTERS)
        if key is not None
    ]
    usage = {
        range_: tagged.get(f"/console/activity/usage.json?range={range_}&by_model=1").json()
        for range_ in RANGES
    }
    page = tagged.get("/console/activity")
    plain_page = plain.get("/console/activity")
    assert page.status_code == plain_page.status_code == 200
    return {
        "markup": MARKUP,
        "tagged": {
            "page": page.text,
            "usage": usage,
            "tags": _answers(tagged, "/console/activity/tags.json", summaries),
            "requests": _answers(tagged, "/console/activity/requests.json", lists),
        },
        "plain": {
            "page": plain_page.text,
            "tags": _answers(
                plain, "/console/activity/tags.json",
                [{"range": range_, "group_by": None, "tag_key": None, "tag_value": None}
                 for range_ in RANGES],
            ),
        },
    }


if __name__ == "__main__":
    print(json.dumps(render()))
