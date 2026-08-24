from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import make_fake_store
from trusted_router.bedrock_group_buy import (
    BEDROCK_GROUP_BUY_FOUNDING_BUYERS,
    BEDROCK_GROUP_BUY_FOUNDING_MICRODOLLARS,
    pledge_from_mapping,
    public_snapshot,
)
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.money import MICRODOLLARS_PER_DOLLAR
from trusted_router.storage import STORE, InMemoryStore
from trusted_router.storage_group_buy import bedrock_group_buy_shard
from trusted_router.storage_models import BedrockGroupBuyPledge
from trusted_router.storage_postgres_group_buy import PostgresBedrockGroupBuy


def _payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "full_name": "Avery Private",
        "title": "VP Infrastructure",
        "company_name": "Private Compute Labs",
        "company_url": "https://private-compute.example",
        "monthly_minimum": "27341.73",
        "expected_bedrock_monthly": "41873.29",
        "expected_all_llm_monthly": "67412.91",
        "last_month_llm_spend": "18392.47",
        "last_month_spend_source_bedrock": "1",
        "last_month_spend_source_anthropic_direct": "1",
        "authorized": "1",
        "terms_accepted": "1",
        "publish_message": "1",
        "public_message_confirmed": "1",
        "public_message": "Founders should be able to negotiate cloud pricing together.",
    }
    values.update(overrides)
    return values


def _pledge(user_id: str, *, minimum: int, message: str = "") -> BedrockGroupBuyPledge:
    return BedrockGroupBuyPledge(
        user_id=user_id,
        workspace_id=f"ws-{user_id}",
        full_name=f"Private {user_id}",
        title="CTO",
        company_name=f"Company {user_id}",
        company_url="https://example.test",
        monthly_minimum_microdollars=minimum,
        expected_bedrock_monthly_microdollars=minimum + 10,
        expected_all_llm_monthly_microdollars=minimum + 20,
        aggregate_shard=bedrock_group_buy_shard(user_id),
        public_message=message,
        publish_message=bool(message),
    )


def test_public_snapshot_starts_with_founding_commitments_only() -> None:
    snapshot = public_snapshot(STORE.bedrock_group_buy_aggregate(), [])

    assert snapshot.buyer_count == BEDROCK_GROUP_BUY_FOUNDING_BUYERS
    assert snapshot.monthly_minimum_microdollars == BEDROCK_GROUP_BUY_FOUNDING_MICRODOLLARS
    assert snapshot.progress_basis_points == 4_000
    assert snapshot.goal_remaining_microdollars == 600_000 * MICRODOLLARS_PER_DOLLAR
    assert snapshot.annual_savings_microdollars == 480_000 * MICRODOLLARS_PER_DOLLAR


def test_pledge_validation_uses_decimal_money_and_allows_zero_withdrawal() -> None:
    pledge = pledge_from_mapping(
        _payload(
            monthly_minimum="1000.000001",
            expected_bedrock_monthly="1200.000002",
            expected_all_llm_monthly="1300.000003",
        ),
        user_id="user-1",
        workspace_id="workspace-1",
    )

    assert pledge is not None
    assert pledge.monthly_minimum_microdollars == 1_000_000_001
    assert pledge.expected_bedrock_monthly_microdollars == 1_200_000_002
    assert pledge.expected_all_llm_monthly_microdollars == 1_300_000_003
    assert pledge.last_month_llm_spend_microdollars == 18_392_470_000
    assert pledge.last_month_spend_sources == ("bedrock", "anthropic_direct")
    assert (
        pledge_from_mapping(
            {"monthly_minimum": "0"},
            user_id="user-1",
            workspace_id="workspace-1",
        )
        is None
    )


def test_positive_actual_spend_requires_a_known_source() -> None:
    with pytest.raises(ValueError, match="Select where"):
        pledge_from_mapping(
            _payload(
                last_month_spend_source_bedrock="",
                last_month_spend_source_anthropic_direct="",
            ),
            user_id="user-1",
            workspace_id="workspace-1",
        )

    with pytest.raises(ValueError, match="only the listed"):
        pledge_from_mapping(
            _payload(last_month_spend_sources=["bedrock", "made_up_vendor"]),
            user_id="user-1",
            workspace_id="workspace-1",
        )


@pytest.mark.parametrize(
    "message",
    [
        "Email me at buyer@example.com because this should happen.",
        "Read more at https://example.com/group-buy today.",
        "Message @privatebuyer because this should happen.",
        "Call +1 (305) 555-0123 because this should happen.",
        "Private Compute Labs should lead this buying group.",
        "<strong>This should happen for every founder.</strong>",
    ],
)
def test_public_message_rejects_identifiers_and_markup(message: str) -> None:
    with pytest.raises(ValueError):
        pledge_from_mapping(
            _payload(public_message=message),
            user_id="user-1",
            workspace_id="workspace-1",
        )


def test_in_memory_edit_replaces_aggregate_and_withdraw_removes_message() -> None:
    store = InMemoryStore()
    first = store.upsert_bedrock_group_buy_pledge(
        _pledge("alice", minimum=10_000_000, message="Buying together gives startups leverage.")
    )
    assert first.public_message_id.startswith("bgm_")
    assert store.bedrock_group_buy_aggregate().active_pledge_count == 1
    assert store.bedrock_group_buy_aggregate().monthly_minimum_microdollars == 10_000_000

    updated = store.upsert_bedrock_group_buy_pledge(
        _pledge("alice", minimum=25_000_000, message="A group agreement gives builders leverage.")
    )
    assert updated.public_message_id == first.public_message_id
    assert store.bedrock_group_buy_aggregate().active_pledge_count == 1
    assert store.bedrock_group_buy_aggregate().monthly_minimum_microdollars == 25_000_000
    assert [row.message for row in store.list_bedrock_group_buy_public_messages()] == [
        "A group agreement gives builders leverage."
    ]

    assert store.withdraw_bedrock_group_buy_pledge("alice") is True
    assert store.withdraw_bedrock_group_buy_pledge("alice") is False
    assert store.bedrock_group_buy_aggregate().active_pledge_count == 0
    assert store.bedrock_group_buy_aggregate().monthly_minimum_microdollars == 0
    assert store.list_bedrock_group_buy_public_messages() == []


def test_in_memory_concurrent_edits_leave_one_pledge_and_exact_total() -> None:
    store = InMemoryStore()

    def update(value: int) -> None:
        store.upsert_bedrock_group_buy_pledge(_pledge("alice", minimum=value))

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(update, range(1, 101)))

    saved = store.get_bedrock_group_buy_pledge("alice")
    aggregate = store.bedrock_group_buy_aggregate()
    assert saved is not None
    assert aggregate.active_pledge_count == 1
    assert aggregate.monthly_minimum_microdollars == saved.monthly_minimum_microdollars


def test_spanner_fake_edit_and_withdraw_keep_shard_and_projection_atomic() -> None:
    store, database, _ = make_fake_store()
    first = store.upsert_bedrock_group_buy_pledge(
        _pledge(
            "spanner-user",
            minimum=100,
            message="A shared cloud contract helps small teams compete.",
        )
    )
    store.upsert_bedrock_group_buy_pledge(
        _pledge(
            "spanner-user", minimum=250, message="Shared purchasing gives small teams fair terms."
        )
    )

    assert store.bedrock_group_buy_aggregate().active_pledge_count == 1
    assert store.bedrock_group_buy_aggregate().monthly_minimum_microdollars == 250
    assert store.list_bedrock_group_buy_public_messages()[0].message.startswith("Shared purchasing")
    public_body = database.rows[("bedrock_group_buy_public_message", first.public_message_id)].body
    assert "spanner-user" not in public_body
    assert "Company" not in public_body
    assert "workspace" not in public_body

    assert store.withdraw_bedrock_group_buy_pledge("spanner-user") is True
    assert store.bedrock_group_buy_aggregate().active_pledge_count == 0
    assert store.list_bedrock_group_buy_public_messages() == []


def test_postgres_sql_edit_and_withdraw_keep_shard_and_projection_atomic() -> None:
    connection = sqlite_postgres_conn()
    store = postgres_store_on(connection)
    store.bedrock_group_buy_store = PostgresBedrockGroupBuy(
        run_transaction=store._run_transaction,
        read_entity_tx=store._read_entity_tx,
        write_entity_tx=store._write_entity_tx,
        delete_entity_tx=store._delete_entity_tx,
        read_entity=store._read_entity,
        list_entities=store._list_entities,
    )

    first = store.upsert_bedrock_group_buy_pledge(
        _pledge("postgres-user", minimum=100, message="A shared contract helps small teams.")
    )
    updated = store.upsert_bedrock_group_buy_pledge(
        _pledge("postgres-user", minimum=275, message="Group purchasing gives builders leverage.")
    )

    assert updated.public_message_id == first.public_message_id
    assert store.bedrock_group_buy_aggregate().active_pledge_count == 1
    assert store.bedrock_group_buy_aggregate().monthly_minimum_microdollars == 275
    assert [item.message for item in store.list_bedrock_group_buy_public_messages()] == [
        "Group purchasing gives builders leverage."
    ]
    assert store.withdraw_bedrock_group_buy_pledge("postgres-user") is True
    assert store.bedrock_group_buy_aggregate().active_pledge_count == 0
    assert connection.count_entities("bedrock_group_buy_pledge") == 0
    assert connection.count_entities("bedrock_group_buy_public_message") == 0


def test_public_page_and_json_show_aggregate_but_never_private_fields(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/bedrock-group-buy/pledge",
        headers=user_headers,
        data=_payload(),
        follow_redirects=False,
    )
    assert response.status_code == 303

    public_page = client.get("/bedrock-group-buy")
    public_json = client.get("/v1/bedrock-group-buy")
    assert public_page.status_code == public_json.status_code == 200
    assert public_page.headers["cache-control"].startswith("public,")
    assert public_json.headers["cache-control"].startswith("public,")
    assert "$425,000" in public_page.text
    assert "11 buyers" in public_page.text
    assert "Founders should be able to negotiate cloud pricing together." in public_page.text
    serialized = public_page.text + json.dumps(public_json.json())
    for secret in (
        "Avery Private",
        "VP Infrastructure",
        "Private Compute Labs",
        "private-compute.example",
        "alice@example.com",
        "27341.73",
        "41873.29",
        "67412.91",
        "18392.47",
    ):
        assert secret not in serialized
    assert set(public_json.json()) == {
        "buyer_count",
        "monthly_minimum_microdollars",
        "monthly_minimum_usd",
        "expected_bedrock_monthly_microdollars",
        "expected_bedrock_monthly_usd",
        "expected_all_llm_monthly_microdollars",
        "expected_all_llm_monthly_usd",
        "annual_minimum_microdollars",
        "annual_minimum_usd",
        "annual_savings_microdollars",
        "annual_savings_usd",
        "goal_microdollars",
        "goal_usd",
        "goal_remaining_microdollars",
        "goal_remaining_usd",
        "progress_basis_points",
        "goal_reached",
        "messages",
    }
    assert public_json.json()["messages"] == [
        {"message": "Founders should be able to negotiate cloud pricing together."}
    ]
    private = STORE.list_bedrock_group_buy_private_pledges()[0]
    assert private.monthly_minimum_microdollars == 27_341_730_000
    assert private.last_month_llm_spend_microdollars == 18_392_470_000
    assert private.last_month_spend_sources == ("bedrock", "anthropic_direct")
    # Public totals are deliberately coarsened, so subtracting the known
    # founding baseline cannot recover the exact private commitment.
    assert public_json.json()["monthly_minimum_microdollars"] == 425_000_000_000


def test_signed_in_user_can_edit_then_set_commitment_to_zero(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    client.post("/bedrock-group-buy/pledge", headers=user_headers, data=_payload())
    edited = client.post(
        "/bedrock-group-buy/pledge",
        headers=user_headers,
        data=_payload(
            monthly_minimum="700000",
            expected_bedrock_monthly="750000",
            expected_all_llm_monthly="900000",
            publish_message="",
        ),
        follow_redirects=False,
    )
    assert edited.status_code == 303
    campaign = client.get("/v1/bedrock-group-buy").json()
    assert campaign["buyer_count"] == 11
    assert campaign["monthly_minimum_microdollars"] == 1_100_000 * MICRODOLLARS_PER_DOLLAR
    assert campaign["goal_reached"] is True
    assert campaign["progress_basis_points"] == 11_000
    assert campaign["messages"] == []

    removed = client.post(
        "/bedrock-group-buy/pledge",
        headers=user_headers,
        data={"monthly_minimum": "0"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    campaign = client.get("/v1/bedrock-group-buy").json()
    assert campaign["buyer_count"] == 10
    assert campaign["monthly_minimum_microdollars"] == BEDROCK_GROUP_BUY_FOUNDING_MICRODOLLARS
    assert STORE.list_bedrock_group_buy_private_pledges() == []


def test_post_commit_confirmation_prompts_sharing_and_deeper_group_discount(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/bedrock-group-buy/pledge",
        headers=user_headers,
        data=_payload(),
        follow_redirects=False,
    )
    assert response.headers["location"] == "/bedrock-group-buy/manage?saved=1#share"

    confirmation = client.get(
        "/bedrock-group-buy/manage?saved=1",
        headers=user_headers,
    )
    assert confirmation.status_code == 200
    assert "You are in. Bring one more buyer." in confirmation.text
    assert "may unlock a deeper final discount for everyone" in confirmation.text
    assert "Post on X" in confirmation.text
    assert "Share on LinkedIn" in confirmation.text
    assert "Email a founder" in confirmation.text
    assert confirmation.headers["cache-control"] == "private, no-store"


def test_combined_bridge_preserves_legacy_group_buy_page_and_return_urls(
    test_settings: Settings,
    user_headers: dict[str, str],
) -> None:
    bridge_settings = Settings(
        **{
            **test_settings.model_dump(),
            "allow_deployed_combined_surface": True,
        }
    )
    client = TestClient(create_app(bridge_settings, init_observability=False))

    anonymous_signin = client.post(
        "/bedrock-group-buy/pledge",
        data=_payload(),
        follow_redirects=False,
    )
    saved = client.post(
        "/bedrock-group-buy/pledge",
        headers=user_headers,
        data=_payload(),
        follow_redirects=False,
    )
    personalized = client.get(
        "/bedrock-group-buy?saved=1",
        headers=user_headers,
    )
    withdrawn = client.post(
        "/bedrock-group-buy/withdraw",
        headers=user_headers,
        follow_redirects=False,
    )

    assert anonymous_signin.status_code == 303
    assert anonymous_signin.headers["location"] == (
        "/bedrock-group-buy?reason=signin&next=%2Fbedrock-group-buy"
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/bedrock-group-buy?saved=1#share"
    assert personalized.status_code == 200
    assert personalized.headers["cache-control"] == "private, no-store"
    assert "Avery Private" in personalized.text
    assert "You are in. Bring one more buyer." in personalized.text
    assert withdrawn.status_code == 303
    assert withdrawn.headers["location"] == "/bedrock-group-buy?withdrawn=1"


def test_public_group_buy_page_never_reads_or_varies_on_session(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.post("/bedrock-group-buy/pledge", headers=user_headers, data=_payload())

    def forbidden_principal(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("public group-buy page inspected a user session")

    def forbidden_pledge(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("public group-buy page read a private pledge")

    monkeypatch.setattr(
        "trusted_router.routes.bedrock_group_buy.principal_from_request",
        forbidden_principal,
    )
    monkeypatch.setattr(
        InMemoryStore,
        "get_bedrock_group_buy_pledge",
        forbidden_pledge,
    )

    anonymous = client.get("/bedrock-group-buy?saved=1")
    with_session = client.get("/bedrock-group-buy?saved=1", headers=user_headers)

    assert anonymous.status_code == with_session.status_code == 200
    # The CSP nonce is per-request, not per-session, so the byte comparison
    # runs with nonces normalized. Normalizing alone could mask a nonce
    # mistakenly derived from the session, so first pin every nonce to the
    # 22-char token_urlsafe(16) shape and prove the session render's nonce
    # carries no session material.
    nonce = re.compile(r'nonce="([^"]+)"')
    anonymous_nonces = set(nonce.findall(anonymous.text))
    session_nonces = set(nonce.findall(with_session.text))
    for value in anonymous_nonces | session_nonces:
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", value), value
    session_material = "".join(user_headers.values())
    for value in session_nonces:
        assert value not in session_material and session_material not in value
    assert nonce.sub('nonce=""', anonymous.text) == nonce.sub('nonce=""', with_session.text)
    assert anonymous.headers["cache-control"].startswith("public,")
    assert with_session.headers["cache-control"].startswith("public,")
    assert "You are in. Bring one more buyer." not in anonymous.text
    assert (
        'href="/bedrock-group-buy?reason=signin&amp;next=%2Fbedrock-group-buy%2Fmanage"'
        in anonymous.text
    )


def test_group_buy_manage_page_is_private_and_requires_a_session(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    anonymous = client.get("/bedrock-group-buy/manage", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == (
        "/bedrock-group-buy?reason=signin&next=%2Fbedrock-group-buy%2Fmanage"
    )

    client.post("/bedrock-group-buy/pledge", headers=user_headers, data=_payload())
    personalized = client.get("/bedrock-group-buy/manage", headers=user_headers)
    assert personalized.status_code == 200
    assert personalized.headers["cache-control"] == "private, no-store"
    assert "Avery Private" in personalized.text


def test_private_pledge_api_is_session_scoped_and_api_keys_are_rejected(
    client: TestClient,
    user_headers: dict[str, str],
    inference_headers: dict[str, str],
) -> None:
    client.post("/bedrock-group-buy/pledge", headers=user_headers, data=_payload())
    own = client.get("/v1/bedrock-group-buy/me", headers=user_headers)
    other = client.get(
        "/v1/bedrock-group-buy/me",
        headers={"x-trustedrouter-user": "bob@example.com"},
    )
    key_request = client.get("/v1/bedrock-group-buy/me", headers=inference_headers)

    assert own.status_code == 200
    assert own.json()["pledge"]["company_name"] == "Private Compute Labs"
    assert own.json()["pledge"]["last_month_llm_spend_microdollars"] == 18_392_470_000
    assert own.json()["pledge"]["last_month_spend_sources"] == [
        "bedrock",
        "anthropic_direct",
    ]
    assert other.status_code == 200
    assert other.json() == {"pledge": None}
    assert key_request.status_code == 401


def test_cross_origin_modification_is_rejected(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/bedrock-group-buy/pledge",
        headers={**user_headers, "origin": "https://evil.example"},
        data=_payload(),
    )
    assert response.status_code == 403
    assert STORE.list_bedrock_group_buy_private_pledges() == []


def test_invalid_public_message_never_creates_private_or_public_record(
    client: TestClient,
    user_headers: dict[str, str],
) -> None:
    response = client.post(
        "/bedrock-group-buy/pledge",
        headers=user_headers,
        data=_payload(public_message="Contact buyer@example.com for details."),
    )
    assert response.status_code == 422
    assert STORE.list_bedrock_group_buy_private_pledges() == []
    assert STORE.list_bedrock_group_buy_public_messages() == []


def test_group_buy_is_in_sitemap_and_og_card_has_social_dimensions(client: TestClient) -> None:
    sitemap = client.get("/sitemap-core.xml")
    page = client.get("/bedrock-group-buy")
    image_path = (
        Path(__file__).resolve().parents[1] / "src/trusted_router/static/og/bedrock-group-buy.png"
    )

    assert sitemap.status_code == 200
    assert "https://trustedrouter.com/bedrock-group-buy" in sitemap.text
    assert page.status_code == 200
    assert 'content="https://trustedrouter.com/static/og/bedrock-group-buy.png"' in page.text
    with Image.open(image_path) as image:
        assert image.size == (1200, 630)
    assert image_path.stat().st_size < 350_000
