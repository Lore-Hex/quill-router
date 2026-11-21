"""/console/activity request tags: the page, its two JSON endpoints, and who may read.

Tag keys and values in this file are invented.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from trusted_router import storage_activity_tags
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.request_tags import MAX_TAG_VALUE_CHARS
from trusted_router.routes.console.activity import USAGE_RANGE_PRESETS
from trusted_router.routes.console.activity_tags import checked_tag, window_start
from trusted_router.storage import STORE, Generation, InMemoryStore
from trusted_router.types import UsageType

TAGS = "/console/activity/tags.json"
REQUESTS = "/console/activity/requests.json"
TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "src" / "trusted_router" / "templates" / "console" / "activity.html"
)
# Text a tag could only hold if it predates validation or bypassed it. The
# page must still show it as text.
MARKUP_VALUE = '<img src=x onerror="alert(1)">'
MARKUP_KEY = "o'wner\"<b>"


def _app() -> FastAPI:
    # Building the app configures a fresh store, so a test with several
    # sessions builds it once and signs each of them in to the same app.
    return create_app(Settings(environment="test"), init_observability=False)


def _signed_in(
    email: str, *, app: FastAPI | None = None, workspace_id: str | None = None
) -> tuple[TestClient, str]:
    client = TestClient(app or _app(), raise_server_exceptions=False)
    user = STORE.ensure_user(email)
    workspace = workspace_id or STORE.list_workspaces_for_user(user.id)[0].id
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label=email,
        ttl_seconds=3600,
        workspace_id=workspace,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, workspace


def _list_rows(page: str) -> str:
    start = page.index("<tbody data-activity-rows>")
    return page[start : page.index("</tbody>", start)]


def _add(
    workspace_id: str,
    generation_id: str,
    tags: dict[str, str],
    *,
    cost: int = 100,
    minutes_ago: int = 5,
    elapsed: int | None = 200,
) -> None:
    created = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
    STORE.add_generation(
        Generation(
            id=generation_id,
            request_id=f"req-{generation_id}",
            workspace_id=workspace_id,
            key_hash="key-console-tags",
            model="test/model",
            provider_name="Test Provider",
            app="console-tags-test",
            tokens_prompt=10,
            tokens_completion=5,
            total_cost_microdollars=cost,
            usage_type=UsageType.CREDITS,
            speed_tokens_per_second=10.0,
            finish_reason="stop",
            status="success",
            streamed=False,
            elapsed_milliseconds=elapsed,
            tags=tags,
            created_at=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# ------------------------------------------------------------- the window


def test_window_start_is_the_usage_charts_window() -> None:
    now = dt.datetime(2026, 11, 21, 17, 30, 45, tzinfo=dt.UTC)

    assert window_start("1h", now=now) == "2026-11-21T16:30:45Z"
    assert window_start("6h", now=now) == "2026-11-21T11:30:45Z"
    assert window_start("24h", now=now) == "2026-11-20T17:30:45Z"
    # Ranges drawn in day buckets start at midnight UTC of their first day.
    assert window_start("7d", now=now) == "2026-11-15T00:00:00Z"
    assert window_start("30d", now=now) == "2026-10-23T00:00:00Z"
    assert window_start("90d", now=now) == "2026-08-24T00:00:00Z"
    with pytest.raises(HTTPException) as refused:
        window_start("2h", now=now)
    assert (refused.value.status_code, refused.value.detail) == (400, "invalid range")


@pytest.mark.parametrize("range_", sorted(USAGE_RANGE_PRESETS))
def test_window_start_agrees_with_what_the_usage_chart_counts(range_: str) -> None:
    """One request just inside the window and one just outside it.

    The tag panel must count exactly the requests the usage chart above it
    counts for the same range, whatever that range's bucket size.
    """
    store = InMemoryStore()
    window_minutes, granularity = USAGE_RANGE_PRESETS[range_]
    start = dt.datetime.fromisoformat(window_start(range_).replace("Z", "+00:00"))
    for generation_id, moment in (
        ("inside", start + dt.timedelta(seconds=2)),
        ("outside", start - dt.timedelta(seconds=2)),
    ):
        store.add_generation(
            Generation(
                id=generation_id,
                request_id=f"req-{generation_id}",
                workspace_id="ws-window",
                key_hash="key-window",
                model="test/model",
                provider_name="Test Provider",
                app="window-test",
                tokens_prompt=1,
                tokens_completion=1,
                total_cost_microdollars=1,
                usage_type=UsageType.CREDITS,
                speed_tokens_per_second=1.0,
                finish_reason="stop",
                status="success",
                streamed=False,
                tags={"team": "search"},
                created_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )

    chart = store.usage_series("ws-window", window_minutes=window_minutes, granularity=granularity)
    panel = store.activity_tag_summary("ws-window", start_at=window_start(range_))

    assert sum(bucket["requests"] for bucket in chart["buckets"]) == 1
    assert [row["requests"] for row in panel["breakdown"]] == [1]


# ------------------------------------------------------------ input limits


def test_console_input_is_held_to_the_limits_tags_are_accepted_under() -> None:
    checked_tag(None)
    checked_tag("team", "search")
    checked_tag("k" * 128, "v" * 256)
    checked_tag("team", "")  # an empty value is allowed
    checked_tag("équipe", "recherche 1")  # letters, digits and spaces of any script

    def refused(key: str | None, value: str | None = None) -> str:
        with pytest.raises(HTTPException) as error:
            checked_tag(key, value)
        assert error.value.status_code == 400
        # A refusal names the rule, never the text it refused.
        if key:
            assert key not in str(error.value.detail)
        if value:
            assert value not in str(error.value.detail)
        return str(error.value.detail)

    assert refused(None, "search") == "tag_value requires tag_key"
    assert refused("") == "tag key must contain 1 to 128 characters"
    assert refused("k" * 129) == "tag key must contain 1 to 128 characters"
    assert refused("team", "v" * 257) == "tag value must contain at most 256 characters"
    assert refused("trustedrouter:tier") == "tag key uses a reserved prefix"
    assert refused("AWS:region") == "tag key uses a reserved prefix"
    assert refused(MARKUP_KEY) == "tag key contains unsupported characters"
    assert refused("team", MARKUP_VALUE) == "tag value contains unsupported characters"
    assert refused("team", "line\nbreak") == "tag value contains unsupported characters"


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        (TAGS, {"range": "2h"}, "invalid range"),
        (TAGS, {"tag_value": "search"}, "tag_value requires tag_key"),
        (TAGS, {"group_by": "k" * 129}, "tag key must contain 1 to 128 characters"),
        (TAGS, {"group_by": "trustedrouter:tier"}, "tag key uses a reserved prefix"),
        (TAGS, {"tag_key": "team", "tag_value": MARKUP_VALUE},
         "tag value contains unsupported characters"),
        (TAGS, {"tag_key": 7}, "tag_key: Input should be a valid string"),
        (TAGS, {"workspace_id": "ws-someone-else"}, "workspace_id: Extra inputs are not permitted"),
        (REQUESTS, {"range": "24h"}, "tag_key: Field required"),
        (REQUESTS, {"range": "bad", "tag_key": "team"}, "invalid range"),
        (REQUESTS, {"tag_key": MARKUP_KEY}, "tag key contains unsupported characters"),
        (REQUESTS, {"tag_key": "team", "tag_value": "v" * 257},
         "tag value must contain at most 256 characters"),
        (REQUESTS, {"tag_key": "team", "limit": 5000}, "limit: Extra inputs are not permitted"),
    ],
)
def test_endpoints_refuse_bad_input_before_any_read(
    monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any], message: str
) -> None:
    client, _workspace = _signed_in("tags-input@example.com")

    def no_read(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("refused input must not reach the store")

    monkeypatch.setattr(InMemoryStore, "activity_tag_summary", no_read)
    monkeypatch.setattr(InMemoryStore, "activity_tag_events", no_read)

    response = client.post(path, json=body)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"] == message
    for value in body.values():
        if isinstance(value, str) and len(value) > 3 and value not in message:
            assert value not in response.text


def test_endpoints_take_a_body_and_ignore_the_query_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tag in a URL would be written to request logs, so none is read from one."""
    client, workspace = _signed_in("tags-body@example.com")
    _add(workspace, "body-1", {"team": "search"})
    seen: list[dict[str, Any]] = []
    original = InMemoryStore.activity_tag_summary

    def spy(self: InMemoryStore, workspace_id: str, **kwargs: Any) -> dict[str, Any]:
        seen.append({"workspace_id": workspace_id, **kwargs})
        return original(self, workspace_id, **kwargs)

    monkeypatch.setattr(InMemoryStore, "activity_tag_summary", spy)

    assert client.get(TAGS).status_code == 405
    assert client.get(f"{REQUESTS}?tag_key=team").status_code == 405
    response = client.post(f"{TAGS}?tag_key=team&tag_value=search&group_by=team", json={})

    assert response.status_code == 200
    assert response.json()["filter"] is None
    assert [(call["group_by"], call["tag_key"], call["tag_value"]) for call in seen] == [
        (None, None, None)
    ]
    assert response.headers["cache-control"] == "private, no-store"


# ----------------------------------------------------------------- access


@pytest.mark.parametrize("path", [TAGS, REQUESTS])
def test_endpoints_require_the_console_session(path: str) -> None:
    anonymous = TestClient(_app())

    response = anonymous.post(
        path, json={"range": "24h", "tag_key": "team"}, follow_redirects=False
    )

    # The same answer the page's other JSON endpoints give without a session.
    reference = anonymous.get("/console/activity/usage.json", follow_redirects=False)
    assert response.status_code == reference.status_code == 302
    assert response.headers["location"] == reference.headers["location"] == "/?reason=signin"
    assert "team" not in response.text

    anonymous.cookies.set("tr_session", "not-a-session")
    assert anonymous.post(path, json={"tag_key": "team"}, follow_redirects=False).status_code == 302


def test_a_user_reads_only_their_own_workspaces_tags() -> None:
    app = _app()
    alice, alice_workspace = _signed_in("tags-alice@example.com", app=app)
    bob, bob_workspace = _signed_in("tags-bob@example.com", app=app)
    assert alice_workspace != bob_workspace
    _add(alice_workspace, "alice-1", {"team": "search", "shared-key": "alice-only"})
    _add(bob_workspace, "bob-1", {"project": "apollo", "shared-key": "bob-only"})

    alice_tags = alice.post(TAGS, json={"range": "24h"}).json()
    bob_tags = bob.post(TAGS, json={"range": "24h", "group_by": "shared-key"}).json()

    assert sorted(item["key"] for item in alice_tags["keys"]) == ["shared-key", "team"]
    assert sorted(item["key"] for item in bob_tags["keys"]) == ["project", "shared-key"]
    assert [row["value"] for row in bob_tags["breakdown"]] == ["bob-only"]
    # Asking by name for a key or value only the other workspace uses finds
    # nothing, in the summary and in the request list alike.
    probe = alice.post(
        TAGS, json={"range": "24h", "group_by": "project", "tag_key": "project"}
    ).json()
    assert probe["group_by"] != "project"
    assert probe["filter"]["values"] == []
    for body in (
        {"range": "24h", "tag_key": "project"},
        {"range": "24h", "tag_key": "shared-key", "tag_value": "bob-only"},
    ):
        found = alice.post(REQUESTS, json=body)
        assert found.status_code == 200
        assert found.json()["data"] == []
    own = alice.post(REQUESTS, json={"range": "24h", "tag_key": "shared-key"}).json()
    assert [item["tags"]["shared-key"] for item in own["data"]] == ["alice-only"]
    for text in (alice.get("/console/activity").text, str(alice_tags), str(probe)):
        assert "apollo" not in text and "bob-only" not in text


def test_the_workspace_comes_from_the_session_and_cannot_be_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    owner = STORE.ensure_user("tags-two@example.com")
    first = STORE.list_workspaces_for_user(owner.id)[0]
    second = STORE.create_workspace(owner.id, "Second workspace", trial_credit_microdollars=0)
    _add(first.id, "first-1", {"team": "search"})
    _add(second.id, "second-1", {"team": "ads"})
    stranger, stranger_workspace = _signed_in("tags-stranger@example.com", app=app)
    calls: list[tuple[str, str]] = []
    summary, events = InMemoryStore.activity_tag_summary, InMemoryStore.activity_tag_events

    def spy_summary(self: InMemoryStore, workspace_id: str, **kwargs: Any) -> Any:
        calls.append(("summary", workspace_id))
        return summary(self, workspace_id, **kwargs)

    def spy_events(self: InMemoryStore, workspace_id: str, **kwargs: Any) -> Any:
        calls.append(("events", workspace_id))
        return events(self, workspace_id, **kwargs)

    monkeypatch.setattr(InMemoryStore, "activity_tag_summary", spy_summary)
    monkeypatch.setattr(InMemoryStore, "activity_tag_events", spy_events)

    for workspace, value in ((first.id, "search"), (second.id, "ads")):
        member, selected = _signed_in("tags-two@example.com", app=app, workspace_id=workspace)
        assert selected == workspace
        tags = member.post(TAGS, json={"range": "24h"}).json()
        assert [row["value"] for row in tags["breakdown"]] == [value]
        found = member.post(REQUESTS, json={"range": "24h", "tag_key": "team"}).json()
        assert [item["tags"] for item in found["data"]] == [{"team": value}]

    # A session cannot point either endpoint at a workspace: not one its user
    # belongs to, and not anyone else's.
    for body in ({"workspace_id": first.id}, {"workspace": first.id}, {"tenant_id": first.id}):
        assert stranger.post(TAGS, json=body).status_code == 400
        assert stranger.post(REQUESTS, json={"tag_key": "team", **body}).status_code == 400
    assert stranger.post(TAGS, json={"range": "24h"}).json()["keys"] == []

    assert calls == [
        ("summary", first.id), ("events", first.id),
        ("summary", second.id), ("events", second.id),
        ("summary", stranger_workspace),
    ]


# ------------------------------------------------------- what the page shows


def test_page_has_the_tag_panel_between_reliability_and_the_request_list() -> None:
    client, _workspace = _signed_in("tags-page@example.com")

    page = client.get("/console/activity").text

    assert (
        page.index("<h2>Usage</h2>")
        < page.index("<h2>Client-observed reliability</h2>")
        < page.index("<h2>Tags</h2>")
        < page.index("<h2>Recent activity</h2>")
    )
    assert "data-tags-panel" in page
    assert '"/console/activity/tags.json"' in page
    assert '"/console/activity/requests.json"' in page
    assert "<th>Finish</th><th>Tags</th>" in page
    # Eight columns: the list scrolls inside its panel like the page's other tables.
    section = page[page.index("<h2>Recent activity</h2>") :]
    assert '<div class="table-scroll">' in section[: section.index("<tbody data-activity-rows>")]
    assert '<td class="empty" colspan="8">No activity yet.' in page
    # The state most workspaces see: what tags are for, one example, the docs.
    empty = page[page.index("data-tags-empty") : page.index("<h2>Recent activity</h2>")]
    assert "Tags label each request with your own keys and values" in empty
    assert "splits requests, cost, tokens and generation time by tag" in empty
    assert '<code>"tags": {"team": "search"}</code>' in empty
    assert '<a href="/docs/tagging">Read the tagging docs.</a>' in empty
    assert client.get("/docs/tagging").status_code == 200


def test_page_and_endpoints_treat_tag_text_as_text() -> None:
    client, workspace = _signed_in("tags-markup@example.com")
    _add(workspace, "markup-1", {"team": MARKUP_VALUE, MARKUP_KEY: "search"})

    rows = _list_rows(client.get("/console/activity").text)

    assert "<img" not in rows and "<b>" not in rows and "onerror=\"" not in rows
    assert (
        "<span data-tag-chip-value>&lt;img src=x onerror=&#34;alert(1)&#34;&gt;</span>" in rows
    )
    assert "<span data-tag-chip-key>o&#39;wner&#34;&lt;b&gt;</span>" in rows
    # The server never writes tag text into an attribute, and the page script
    # never builds markup from it.
    assert "data-tag-chip>" in rows and 'data-tag-chip="' not in rows

    tags = client.post(TAGS, json={"range": "24h"})
    assert tags.headers["content-type"] == "application/json"
    payload = tags.json()
    # Stored text comes back as JSON data, character for character, and the
    # page script writes it with textContent.
    assert [item["key"] for item in payload["keys"]] == [MARKUP_KEY, "team"]
    assert payload["group_by"] == MARKUP_KEY
    assert [row["value"] for row in payload["breakdown"]] == ["search"]
    by_team = client.post(TAGS, json={"range": "24h", "group_by": "team"}).json()
    assert [row["value"] for row in by_team["breakdown"]] == [MARKUP_VALUE]
    # It cannot be used as a filter: it is outside what a tag may contain.
    refused = client.post(REQUESTS, json={"range": "24h", "tag_key": "team",
                                          "tag_value": MARKUP_VALUE})
    assert refused.status_code == 400
    assert "img" not in refused.text


def test_page_script_writes_tag_text_with_textcontent_only() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    script = template[template.index("// Request tags.") : template.index("{% endblock %}")]

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in template
    assert "textContent" in script
    # Attributes set from script are fixed names with empty values.
    for line in script.splitlines():
        if "setAttribute(" in line:
            assert line.strip().endswith('", "");'), line
    # Tag text goes out in a JSON body, never in a URL.
    assert 'method: "POST"' in script and "body: JSON.stringify(body)" in script
    assert "URLSearchParams" not in script and "?${" not in script
    for storage in ("localStorage", "sessionStorage", "location.", "history."):
        assert storage not in script


def test_request_list_shows_tags_and_the_filter_endpoint_returns_list_columns() -> None:
    client, workspace = _signed_in("tags-list@example.com")
    _add(workspace, "list-1", {"team": "search", "environment": "production"}, cost=1, minutes_ago=3)
    _add(workspace, "list-2", {"team": "ads"}, cost=2, minutes_ago=2)
    _add(workspace, "list-3", {}, cost=3, minutes_ago=1)
    _add(workspace, "list-4", {f"key-{index:02d}": "x" for index in range(11)}, minutes_ago=4)
    _add(workspace, "list-old", {"team": "search"}, minutes_ago=3 * 24 * 60)

    rows = _list_rows(client.get("/console/activity").text)

    chip = (
        '<button type="button" class="tag-chip" title="Filter requests by this tag" '
        "data-tag-chip><span data-tag-chip-key>{}</span>: "
        "<span data-tag-chip-value>{}</span></button>"
    )
    # Chips are sorted by key; a request without tags has an empty cell.
    assert chip.format("environment", "production") + chip.format("team", "search") in rows
    assert chip.format("team", "ads") in rows
    assert rows.count("<td></td>") == 1
    # At most eight chips per request, then a count of the rest.
    assert chip.format("key-07", "x") in rows and chip.format("key-08", "x") not in rows
    assert '<span class="tag-chip-more">+3 more</span>' in rows

    found = client.post(
        REQUESTS, json={"range": "24h", "tag_key": "team", "tag_value": "search"}
    ).json()
    assert found["truncated"] is False and found["limit"] == 50
    [item] = found["data"]  # the three-day-old request is outside the range
    assert item == {
        "created_at": item["created_at"],
        "model": "test/model",
        "provider_name": "Test Provider",
        "input_tokens": 10,
        "output_tokens": 5,
        "finish_reason": "stop",
        "cost_display": "$0.000001",
        "tags": {"team": "search", "environment": "production"},
    }
    wider = client.post(REQUESTS, json={"range": "7d", "tag_key": "team", "tag_value": "search"})
    assert len(wider.json()["data"]) == 2


def test_summary_endpoint_shape() -> None:
    client, workspace = _signed_in("tags-shape@example.com")
    _add(workspace, "shape-1", {"team": "search", "environment": "production"}, cost=300, elapsed=100)
    _add(workspace, "shape-2", {"team": "search"}, cost=200, elapsed=300)
    _add(workspace, "shape-3", {"team": "ads", "environment": "staging"}, cost=50, elapsed=None)
    _add(workspace, "shape-4", {}, cost=7)

    payload = client.post(
        TAGS, json={"range": "24h", "tag_key": "environment", "tag_value": "production"}
    ).json()

    assert payload["range"] == "24h"
    assert payload["keys"] == [
        {"key": "team", "requests": 3},
        {"key": "environment", "requests": 2},
    ]
    assert payload["group_by"] == "team" and payload["value_limit"] == 50
    assert (payload["keys_approximate"], payload["breakdown_too_many_values"]) == (False, False)
    # The in-memory store ranks every value: its choice of rows is never approximate.
    assert payload["breakdown_approximate"] is False
    assert payload["distinct_limit"] == 500_000
    assert payload["breakdown"] == [
        {
            "kind": "value", "value": "search", "values": 1, "requests": 1,
            "cost_microdollars": 300, "byok_microdollars": 0, "prompt_tokens": 10,
            "completion_tokens": 5, "median_elapsed_ms": 100,
        }
    ]
    assert payload["filter"] == {
        "key": "environment",
        "value": "production",
        "values": [{"value": "production", "requests": 1}, {"value": "staging", "requests": 1}],
        "values_truncated": False,
        "values_approximate": False,
    }
    unfiltered = client.post(TAGS, json={"range": "24h"}).json()
    assert [(row["kind"], row["value"], row["requests"]) for row in unfiltered["breakdown"]] == [
        ("value", "search", 2), ("value", "ads", 1), ("untagged", None, 1),
    ]
    assert unfiltered["filter"] is None


def test_a_key_with_too_many_values_gets_an_answer_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace = _signed_in("tags-many@example.com")
    for index in range(4):
        _add(workspace, f"many-{index}", {"request": f"req-{index}", "team": "search"})
    monkeypatch.setattr(storage_activity_tags, "TAG_DISTINCT_LIMIT", 3)

    # Asked for by name, the key is answered for: refused, with the reason.
    asked = client.post(TAGS, json={"range": "24h", "group_by": "request"})
    assert asked.status_code == 200
    payload = asked.json()
    assert (payload["group_by"], payload["breakdown"]) == ("request", [])
    assert payload["breakdown_too_many_values"] is True
    assert payload["distinct_limit"] == 3
    # Not asked for, the next key is tried, so the first view is not a refusal
    # just because every request carries its own id.
    default = client.post(TAGS, json={"range": "24h"}).json()
    assert [item["key"] for item in default["keys"]] == ["request", "team"]
    assert (default["group_by"], default["breakdown_too_many_values"]) == ("team", False)
    assert [row["value"] for row in default["breakdown"]] == ["search"]
    # Its values can still be filtered by, which needs no ranking.
    found = client.post(
        REQUESTS, json={"range": "24h", "tag_key": "request", "tag_value": "req-2"}
    ).json()
    assert [item["tags"]["request"] for item in found["data"]] == ["req-2"]


def test_page_names_the_median_and_offers_a_typed_value() -> None:
    client, _workspace = _signed_in("tags-header@example.com")

    page = client.get("/console/activity").text
    panel = page[page.index("<h2>Tags</h2>") : page.index("<h2>Recent activity</h2>")]

    # The column is the server's generation time, and says it is not the
    # client-observed latency of the panel above.
    assert ">Median generation time</th>" in panel
    assert "Median latency" not in panel
    assert "This is not the client-observed latency in the reliability panel above" in panel
    # A value the list leaves out can be typed. The page states the limit the
    # server enforces and counts characters the way the server does; the
    # field has no maxlength, which counts UTF-16 units and would cut a value
    # of characters outside the basic plane short.
    assert f'data-tags-value-input data-max-characters="{MAX_TAG_VALUE_CHARS}"' in panel
    assert f"(at most {MAX_TAG_VALUE_CHARS} characters)" in panel
    assert "maxlength" not in panel
    assert "data-tags-value-note data-tags-value-form hidden" in panel
    # The empty value is a real one and has a control of its own.
    assert '<button class="btn" type="button" data-tags-value-empty>Filter by the empty value</button>' in panel
    script = TEMPLATE.read_text(encoding="utf-8")
    assert "values_truncated || filter.values_approximate" in script
    assert "Array.from(typed).length" in script
    # A refusal rests on a count that is an estimate in ClickHouse, and an
    # approximate list or ranking is called approximate.
    assert "or more distinct values in" in script and "has more than" not in script
    assert "the list is approximately its most used ones" in script
    assert "the rows are approximately its most expensive ones" in script
    assert "Other (about ${folded} values)" in script


def test_page_script_follows_the_range_without_reading_the_chart() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    script = template[template.index("// Request tags.") : template.index("{% endblock %}")]

    # The published range at start, then the usage panel's announcement.
    assert 'usage.getAttribute("data-usage-range")' in script
    assert script.count('document.addEventListener("activity:range-change"') == 1
    # Nothing else: not the label the chart rewrites after a successful
    # request, not the radios or the "Show ..." control, whose changes the
    # usage panel already announces, and no range event of its own.
    assert "data-usage-range-label" not in script and "MutationObserver" not in script
    assert "data-usage-expand-range" not in script
    assert 'document.addEventListener("change"' not in script
    assert "dispatchEvent" not in script
    # The usage panel's side of the contract, which this panel depends on.
    usage = template[: template.index("// Request tags.")]
    assert 'new CustomEvent("activity:range-change", { detail: { range } })' in usage
    assert "panel.dataset.usageRange = range" in usage
    # A range change keeps the viewer's filter.
    change = script[script.index("function applyRange(range)") : script.index("function initTagsPanel()")]
    assert "filterKey = null" not in change and "filterValue = null" not in change


# --------------------------------------------------- outages and what is logged


@pytest.mark.parametrize(
    ("path", "method"),
    [(TAGS, "activity_tag_summary"), (REQUESTS, "activity_tag_events")],
)
def test_an_analytics_outage_is_a_retryable_503_and_a_defect_stays_a_500(
    monkeypatch: pytest.MonkeyPatch, path: str, method: str
) -> None:
    client, _workspace = _signed_in("tags-outage@example.com")
    body = {"range": "24h", "tag_key": "team", "tag_value": "search"}

    def unreachable(*_args: Any, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(InMemoryStore, method, unreachable)
    outage = client.post(path, json=body)
    assert outage.status_code == 503
    assert outage.headers["retry-after"] == "30"
    assert outage.json()["error"]["message"] == "Tag data is temporarily unavailable; retry shortly."

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("ClickHouse response did not contain data rows")

    monkeypatch.setattr(InMemoryStore, method, broken)
    assert client.post(path, json=body).status_code == 500


def test_no_log_record_carries_a_tag_key_or_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, workspace = _signed_in("tags-logs@example.com")
    _add(workspace, "logs-1", {"zq-log-key": "zq-log-value"})
    body = {"range": "24h", "group_by": "zq-log-key", "tag_key": "zq-log-key",
            "tag_value": "zq-log-value"}

    with caplog.at_level(logging.DEBUG):
        assert client.get("/console/activity").status_code == 200
        assert client.post(TAGS, json=body).status_code == 200
        assert client.post(REQUESTS, json=body | {"group_by": None}).status_code == 400
        assert client.post(
            REQUESTS, json={"range": "24h", "tag_key": "zq-log-key", "tag_value": "zq-log-value"}
        ).status_code == 200
        # Refused input, with the text the validator refused.
        assert client.post(TAGS, json={"tag_key": "zq-log-key<", "tag_value": "x"}).status_code == 400
        assert client.post(TAGS, json={"tag_key": "zq-log-key", "tag_value": 3}).status_code == 400

        def unreachable(*_args: Any, **_kwargs: Any) -> Any:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(InMemoryStore, "activity_tag_summary", unreachable)
        assert client.post(TAGS, json=body).status_code == 503

    assert any("operational_analytics.unavailable" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert "zq-log" not in record.getMessage(), record.name
        assert "zq-log" not in str(record.__dict__), record.name
