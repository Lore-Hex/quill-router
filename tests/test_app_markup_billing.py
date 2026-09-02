from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from trusted_router.app_markup_billing import (
    app_markup_microdollars,
    app_markup_microdollars_from_charge,
    app_markup_owner_share_microdollars,
    app_markup_payout_event_id,
)
from trusted_router.routes.internal import gateway
from trusted_router.storage import STORE
from trusted_router.storage_models import generation_id_for_authorization


def _mint_app_key(
    client: TestClient, user_headers: dict[str, str], *, app_id: str, bps: int
) -> tuple[Any, str]:
    user = STORE.ensure_user(user_headers["x-trustedrouter-user"])
    STORE.set_user_identity_status(user.id, status="approved", verified_name="App Owner")
    raw_session, _ = STORE.create_auth_session(
        user_id=user.id, provider="google", label="owner", ttl_seconds=3600, state="active"
    )
    client.cookies.set("tr_session", raw_session)
    created = client.post(
        "/v1/oauth/apps",
        headers=user_headers,
        json={
            "id": app_id,
            "name": app_id,
            "redirect_uris": ["https://app.example/callback"],
            "markup_basis_points": bps,
        },
    )
    assert created.status_code == 201, created.text
    consent_page = client.get(
        "/auth",
        params={"client_id": app_id, "callback_url": "https://app.example/callback"},
    )
    assert consent_page.status_code == 200, consent_page.text

    def form_value(name: str) -> str:
        marker = f'name="{name}" value="'
        return consent_page.text.split(marker, 1)[1].split('"', 1)[0]

    approved = client.post(
        "/auth/approve",
        data={"consent": form_value("consent"), "csrf_token": form_value("csrf_token")},
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    exchanged = client.post("/v1/auth/keys", json={"code": code})
    assert exchanged.status_code == 200, exchanged.text
    return user, str(exchanged.json()["key"])


def _authorize(client: TestClient, raw_key: str, request_id: str) -> Any:
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    response = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 100,
            "max_output_tokens": 20,
            "idempotency_key": request_id,
        },
    )
    assert response.status_code == 200, response.text
    auth = STORE.get_gateway_authorization(response.json()["data"]["authorization_id"])
    assert auth is not None
    return auth


def _settle(client: TestClient, auth: Any, *, status: str = "success") -> Any:
    path = "refund" if status != "success" else "settle"
    response = client.post(
        f"/v1/internal/gateway/{path}",
        json={
            "authorization_id": auth.id,
            "actual_input_tokens": 12,
            "actual_output_tokens": 8,
            "request_id": f"req-{auth.id}",
            "status": status,
            "elapsed_seconds": 0.5,
        },
    )
    assert response.status_code == 200, response.text
    return response


def test_app_markup_arithmetic_and_floors() -> None:
    cases = [
        (0, 0, 0),
        (100, 0, 0),
        (0, 30_000, 0),
        (1, 1, 0),
        (1, 30_000, 3),
        (101, 1_000, 10),
        (101, 30_000, 303),
    ]
    for base, basis_points, expected_markup in cases:
        markup = app_markup_microdollars(base, basis_points)
        assert markup == expected_markup
        payout = app_markup_owner_share_microdollars(markup)
        assert payout == markup * 7_000 // 10_000
        assert markup - payout == markup * 3_000 // 10_000 + (markup * 7_000 % 10_000 > 0)


def test_app_markup_payout_event_id_is_authorization_scoped() -> None:
    assert app_markup_payout_event_id("gwa_123") == "app_markup_payout:gwa_123"


def test_hold_inflation_and_legacy_zero_paths(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    baseline = client.post("/v1/keys", headers=user_headers, json={"name": "baseline"})
    assert baseline.status_code == 201
    base_auth = _authorize(client, baseline.json()["key"], "hold-base")
    _owner, marked_key = _mint_app_key(client, user_headers, app_id="hold-marked", bps=500)
    marked_auth = _authorize(client, marked_key, "hold-marked")
    _owner, zero_key = _mint_app_key(client, user_headers, app_id="hold-zero", bps=0)
    zero_auth = _authorize(client, zero_key, "hold-zero")

    markup = app_markup_microdollars(base_auth.estimated_microdollars, 500)
    assert marked_auth.estimated_microdollars == base_auth.estimated_microdollars + markup
    assert zero_auth.estimated_microdollars == base_auth.estimated_microdollars
    assert base_auth.app_markup_basis_points == zero_auth.app_markup_basis_points == 0


def test_frozen_markup_survives_repricing_and_suspension(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    owner, raw_key = _mint_app_key(client, user_headers, app_id="frozen-app", bps=500)
    auth = _authorize(client, raw_key, "frozen-auth")
    assert auth.app_markup_basis_points == 500
    assert auth.app_owner_user_id == owner.id
    changed = client.patch(
        "/v1/oauth/apps/frozen-app",
        headers=user_headers,
        json={"markup_basis_points": 2_000, "suspended": True},
    )
    assert changed.status_code == 200
    _settle(client, auth)
    generation = STORE.get_generation(generation_id_for_authorization(auth.id))
    assert generation is not None
    expected_markup = app_markup_microdollars(
        generation.total_cost_microdollars - generation.app_markup_microdollars, 500
    )
    assert generation.app_markup_microdollars == expected_markup
    payout = app_markup_owner_share_microdollars(expected_markup)
    assert STORE.earnings_summary(owner.id)["total_earned"] == payout


def test_memory_settle_books_charge_payout_share_and_replay_once(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    owner, raw_key = _mint_app_key(client, user_headers, app_id="memory-money", bps=1_000)
    auth = _authorize(client, raw_key, "memory-money")
    before = STORE.credit_money_snapshot(auth.workspace_id)
    assert before is not None
    _settle(client, auth)
    generation = STORE.get_generation(generation_id_for_authorization(auth.id))
    assert generation is not None
    after = STORE.credit_money_snapshot(auth.workspace_id)
    assert after is not None
    markup = generation.app_markup_microdollars
    payout = app_markup_owner_share_microdollars(markup)
    assert after[1] - before[1] == generation.total_cost_microdollars
    assert STORE.earnings_summary(owner.id)["total_earned"] == payout
    assert markup - payout + payout == markup
    _settle(client, auth)
    movements = STORE.list_credit_movements(f"user:{owner.id}")
    assert [m.movement_id for m in movements].count(app_markup_payout_event_id(auth.id)) == 1


def test_regional_lease_clamp_derives_payout_from_final_charge(
    client: TestClient,
    user_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, raw_key = _mint_app_key(
        client, user_headers, app_id="regional-clamp-markup", bps=30_000
    )
    auth = _authorize(client, raw_key, "regional-clamp-markup")
    auth.estimated_microdollars = 400
    auth.settlement = "regional_lease"
    before = STORE.credit_money_snapshot(auth.workspace_id)
    assert before is not None
    monkeypatch.setattr(gateway, "_endpoint_cost_microdollars", lambda *_args, **_kwargs: 200)

    _settle(client, auth)

    generation = STORE.get_generation(generation_id_for_authorization(auth.id))
    assert generation is not None
    after = STORE.credit_money_snapshot(auth.workspace_id)
    assert after is not None
    charge = generation.total_cost_microdollars
    markup = generation.app_markup_microdollars
    payout = STORE.earnings_summary(owner.id)["total_earned"]
    assert charge == 400
    assert markup == 300
    assert payout == 210
    assert charge == (charge - markup) + markup
    assert payout + (markup - payout) == markup
    assert after[1] - before[1] == charge


@pytest.mark.parametrize("base", [0, 1, 2, 17, 101, 200, 999])
@pytest.mark.parametrize("additional", [0, 1, 50, 1_000])
@pytest.mark.parametrize("bps", [1, 500, 10_000, 30_000])
@pytest.mark.parametrize("clamp", [0, 1, 7, 100, 400, 10_000])
def test_final_charge_markup_conservation_property(
    base: int, additional: int, bps: int, clamp: int
) -> None:
    proposed = base + additional
    proposed += app_markup_microdollars(proposed, bps)
    charge = min(proposed, clamp)
    markup = app_markup_microdollars_from_charge(charge, bps)
    payout = app_markup_owner_share_microdollars(markup)
    tr_share = markup - payout
    assert payout + tr_share == markup
    assert markup <= charge


def test_refund_and_zero_cost_write_no_app_payout(
    client: TestClient, user_headers: dict[str, str]
) -> None:
    owner, raw_key = _mint_app_key(client, user_headers, app_id="refund-app", bps=1_000)
    auth = _authorize(client, raw_key, "refund-app")
    _settle(client, auth, status="error")
    assert STORE.earnings_summary(owner.id)["total_earned"] == 0
    assert STORE.list_credit_movements(f"user:{owner.id}") == []


@pytest.mark.parametrize("input_tokens", [0, 20], ids=("additional-only", "mixed"))
def test_additional_cost_is_in_the_settle_markup_base(
    client: TestClient, user_headers: dict[str, str], input_tokens: int
) -> None:
    owner, raw_key = _mint_app_key(
        client, user_headers, app_id=f"additional-markup-{input_tokens}", bps=30_000
    )
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    additional = 1_000_000
    authorized = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-opus-4.7",
            "estimated_input_tokens": input_tokens,
            "max_output_tokens": 1,
            "route_type": "responses.web_search.planner",
            "additional_cost_reservation_microdollars": additional,
            "idempotency_key": f"additional-markup-{input_tokens}",
        },
    )
    assert authorized.status_code == 200, authorized.text
    auth = STORE.get_gateway_authorization(authorized.json()["data"]["authorization_id"])
    assert auth is not None
    settled = client.post(
        "/v1/internal/gateway/settle",
        json={
            "authorization_id": auth.id,
            "actual_input_tokens": input_tokens,
            "actual_output_tokens": 0,
            "request_id": f"req-{auth.id}",
            "route_type": "responses.web_search.planner",
            "additional_cost_microdollars": additional,
            "elapsed_seconds": 0.5,
        },
    )
    assert settled.status_code == 200, settled.text
    generation = STORE.get_generation(generation_id_for_authorization(auth.id))
    assert generation is not None
    base = generation.total_cost_microdollars - generation.app_markup_microdollars
    assert generation.app_markup_microdollars == app_markup_microdollars(base, 30_000)
    assert base >= additional
    if input_tokens == 0:
        assert generation.total_cost_microdollars == 4_000_000
        assert STORE.earnings_summary(owner.id)["total_earned"] == 2_100_000


@pytest.mark.parametrize("constraint", ["lifetime", "window", "balance"])
def test_marked_up_hold_is_seen_by_spend_constraints(
    client: TestClient, user_headers: dict[str, str], constraint: str
) -> None:
    _owner, raw_key = _mint_app_key(
        client, user_headers, app_id=f"cap-markup-{constraint}", bps=30_000
    )
    auth = _authorize(client, raw_key, f"measure-markup-{constraint}")
    marked_hold = auth.estimated_microdollars
    _settle(client, auth, status="error")
    key = STORE.get_key_by_raw(raw_key)
    assert key is not None
    if constraint == "lifetime":
        key.limit_microdollars = marked_hold - 1
    elif constraint == "window":
        key.limit_daily_microdollars = marked_hold - 1
    else:
        money = STORE.credit_money[key.workspace_id]
        money.total_credits_microdollars = money.total_usage_microdollars + marked_hold - 1
    denied = client.post(
        "/v1/internal/gateway/authorize",
        json={
            "api_key_lookup_hash": key.lookup_hash,
            "model": "anthropic/claude-haiku-4.5",
            "estimated_input_tokens": 100,
            "max_output_tokens": 20,
            "idempotency_key": f"blocked-markup-{constraint}",
        },
    )
    assert denied.status_code in {402, 429}, denied.text
