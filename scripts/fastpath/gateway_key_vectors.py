"""Golden key and workspace facts for the Go edge (E2 G3, fastpath/internal/gwstate).

Each case is a stored entity body, an API key's or a workspace's, as the
gateway authorize reads it from tr_entities. The body is decoded with
Python's own reader (``storage_models.decode_auth_record`` into the real
``ApiKey``/``Workspace`` dataclasses), and each fact the edge's cohort
gate needs is computed by the expression or function the authorize route
uses:

* disabled / expired: ``api_key.disabled or is_api_key_expired(api_key.expires_at)``
  (401), with ``auth.is_api_key_expired``'s clock pinned to the case's instant;
* scope: ``gateway._assert_gateway_key_scope`` (403);
* federated: ``getattr(api_key, "federated_home", "")``, the federated branch;
* tags: ``request_tags.merge_tags(api_key.tags, None)`` (400 on InvalidTags);
* app: ``gateway._oauth_app_terms_for_key`` called, an app key being one
  that reaches the app's provenance;
* the reads that decide nothing but can raise (the route's 500):
  ``enforced_window_limits`` and ``storage_gcp_counters.key_usage_shard_count``;
* limit / window / strict: the pilot's ``key_capped`` and ``budget_strict``
  (``limit_microdollars is not None``, ``spend_windows.key_window_limits``,
  ``budget_strict``);
* workspace: absent or ``deleted`` (403), ``workspace_billing_paused`` and
  ``assert_workspace_billing_active`` (403/503), ``federated_home``, and the
  id Python bills (the body's), which must be the key's workspace_id.

ROUTE_READS lists every key and workspace read the route makes from the key
read to ``_fastpath_admission``; the Python test extracts the same set from
the route's source and fails when they differ. The checks run over recording
subclasses of ApiKey/Workspace, and the file's ``consulted_fields`` lists the
fields they read, which the Go facts must each account for.

A case's ``reason`` is the first fact that keeps the key out of the edge's
cohort, in the order FACT_ORDER (the Go facts' order), or null when none
does; a decode or a predicate that raises is ``error``. ``exact`` cases are
well-formed bodies on which Go must give the same reason and tags; the
others (wrong JSON types, timestamps in ISO forms Python accepts and Go does
not parse, non-ASCII tags, Python errors) Go may pass to Python, and when it
does not it must agree. Ids are synthetic.

    uv run python scripts/fastpath/gateway_key_vectors.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import random
import sys
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Settings must not take inputs from the shell that runs this.
for _name in [name for name in os.environ if name.startswith("TR_")]:
    del os.environ[_name]

from trusted_router import auth  # noqa: E402
from trusted_router.auth import is_api_key_expired  # noqa: E402
from trusted_router.errors import assert_workspace_billing_active  # noqa: E402
from trusted_router.request_tags import InvalidTags, merge_tags  # noqa: E402
from trusted_router.routes.internal import gateway  # noqa: E402
from trusted_router.routes.internal.gateway import (  # noqa: E402
    _assert_gateway_key_scope,
    _oauth_app_terms_for_key,
)
from trusted_router.spend_windows import enforced_window_limits, key_window_limits  # noqa: E402
from trusted_router.storage_codec import json_body  # noqa: E402
from trusted_router.storage_gcp_counters import key_usage_shard_count  # noqa: E402
from trusted_router.storage_models import (  # noqa: E402
    ApiKey,
    Workspace,
    decode_auth_record,
    workspace_billing_paused,
)

OUTPUT = ROOT / "fastpath" / "testdata" / "gateway_key_vectors_v1.json"

FACT_ORDER = (
    "key_disabled", "key_expired", "key_scope", "key_federated", "key_tags", "key_app",
    "key_limit", "key_window_limit", "key_budget_strict",
)
WORKSPACE_ORDER = ("workspace_unavailable", "workspace_paused", "workspace_federated")

# Every read of the key or the workspace that gateway._authorize_gateway_sync_impl
# makes from the key read to its _fastpath_admission call: an attribute read
# (``api_key.x``, ``getattr(api_key, "x", ...)``) or a call given the key or
# the workspace. tests/test_fastpath_gateway_key_vectors.py extracts the same
# set from the route's source and fails when the two differ, so a key or
# workspace field the route starts to consult cannot be left out of the
# oracle. Each says how key_facts / workspace_facts covers it.
ROUTE_READS: dict[str, str] = {
    "api_key.disabled": "key_disabled",
    "api_key.expires_at": "key_expired (is_api_key_expired at the case instant)",
    "_assert_gateway_key_scope": "key_scope (called)",
    "api_key.workspace_id": "the key's workspace; returned, and the workspace body's id must equal it",
    "api_key.lookup_hash": "returned; the boot binding and the monitor key are the edge policy's (G10)",
    "api_key.federated_home": "key_federated",
    "workspace.id": "returned (billed_workspace_id): Go bills the key's workspace_id, so they must agree",
    "workspace_billing_paused": "workspace_paused (called)",
    "assert_workspace_billing_active": "workspace_paused (called)",
    "api_key.tags": "key_tags (merge_tags called with no request tags)",
    "_oauth_app_terms_for_key": "key_app (called; reaching the app's provenance is an app key)",
    "api_key.budget_strict": "key_budget_strict; also read by Stage D eligibility (called as truthiness)",
    "api_key.include_byok_in_limit": "read as the route reads it; it never raises, and only a BYOK request uses it",
    "enforced_window_limits": "called; a raise is an error",
    "key_usage_shard_count": "called; a raise is an error",
    "api_key.limit_microdollars": "key_limit",
    "key_window_limits": "key_window_limit (called)",
    "api_key.hash": "returned (the authorization's key_hash)",
    "workspace.federated_home": "workspace_federated",
    "speculation_shadow.resolved": "not a check: an isolated shadow observation",
}

AT = dt.datetime(2026, 10, 23, 12, 0, 0, 250000, tzinfo=dt.UTC)
AT_WHOLE = dt.datetime(2026, 10, 23, 12, 0, 0, tzinfo=dt.UTC)


@contextmanager
def clock(at: dt.datetime) -> Iterator[None]:
    """Pin ``auth.is_api_key_expired``'s now() to ``at``; nothing else changes."""

    class Pinned(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> Pinned:  # type: ignore[override]
            moment = at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)
            return cls.combine(moment.date(), moment.timetz())

    shim = types.SimpleNamespace(datetime=Pinned, UTC=dt.UTC, timezone=dt.timezone, timedelta=dt.timedelta)
    with mock.patch.object(auth, "dt", shim):
        yield


class _AppKey(Exception):
    """_oauth_app_terms_for_key went past its no-app guard to the app's provenance."""


def _raise_app_key(_api_key: Any) -> tuple[bool, int, str]:
    raise _AppKey


# Which dataclass fields the checks read, over every case: the Go facts must
# read each one or say why it need not (gwstate's consultedKeyFields /
# consultedWorkspaceFields hold the file's lists).
_CONSULTED: dict[str, set[str]] = {"api_key": set(), "workspace": set()}


def _recording(cls: type[Any], kind: str) -> type[Any]:
    names = {f.name for f in dataclasses.fields(cls)}

    class Recording(cls):  # type: ignore[misc, valid-type]
        def __getattribute__(self, name: str) -> Any:
            if name in names:
                _CONSULTED[kind].add(name)
            return super().__getattribute__(name)

    Recording.__name__ = cls.__name__
    return Recording


_RecordingApiKey = _recording(ApiKey, "api_key")
_RecordingWorkspace = _recording(Workspace, "workspace")


def _refuses(check: Callable[[Any], Any], value: Any, *statuses: int) -> bool:
    try:
        check(value)
    except Exception as exc:
        if getattr(exc, "status_code", None) in statuses:
            return True
        raise
    return False


def _app_key(key: Any) -> bool:
    with mock.patch.object(gateway, "_oauth_app_provenance", _raise_app_key):
        try:
            assert _oauth_app_terms_for_key(key) == (0, "")
        except _AppKey:
            return True
    return False


def _tags(key: Any) -> dict[str, str] | None:
    try:
        return merge_tags(key.tags, None)
    except InvalidTags:
        return None


def _read(value: Any) -> bool:
    """A route read that decides nothing for the cohort; it may still raise."""
    return False


def key_facts(body: str, at: dt.datetime) -> dict[str, Any]:
    """Python's verdict on a stored key body at an instant.

    The checks run in the route's order, each the route's own expression or
    function; a check that raises is the route's 500 (``error``)."""
    failure = {"error": None, "reason": "error", "tags": None, "hash": None, "workspace_id": None,
               "lookup_hash": None}
    try:
        key = decode_auth_record(body, _RecordingApiKey)
    except Exception as exc:
        return {**failure, "error": type(exc).__name__}
    checks: list[tuple[str | None, Callable[[], bool]]] = [
        # api_key.disabled or is_api_key_expired(api_key.expires_at): 401.
        ("key_disabled", lambda: bool(key.disabled)),
        ("key_expired", lambda: is_api_key_expired(key.expires_at)),
        ("key_scope", lambda: _refuses(_assert_gateway_key_scope, key, 403)),
        # _gateway_authorize_metadata's federated branch; the route reads it
        # again for async settlement.
        ("key_federated", lambda: bool(getattr(key, "federated_home", ""))),
        # merge_tags(api_key.tags, body.tags): 400 on InvalidTags.
        ("key_tags", lambda: _tags(key) is None),
        ("key_app", lambda: _app_key(key)),
        # Stage D eligibility: `not api_key.budget_strict`.
        (None, lambda: _read(not key.budget_strict)),
        # `{} if is_byok_request and not api_key.include_byok_in_limit else
        # enforced_window_limits(api_key)`, then key_usage_shard_count.
        (None, lambda: _read(not key.include_byok_in_limit)),
        (None, lambda: _read(enforced_window_limits(key))),
        (None, lambda: _read(key_usage_shard_count(key))),
        # The pilot's key_capped and budget_strict.
        ("key_limit", lambda: key.limit_microdollars is not None),
        ("key_window_limit", lambda: bool(key_window_limits(key))),
        ("key_budget_strict", lambda: bool(key.budget_strict)),
    ]
    assert tuple(name for name, _ in checks if name is not None) == FACT_ORDER
    with clock(at):
        for name, check in checks:
            try:
                failed = check()
            except Exception as exc:
                return {**failure, "error": type(exc).__name__}
            if failed:
                return {**failure, "reason": name}
        tags = _tags(key)
    assert tags is not None
    def text(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    return {"error": None, "reason": None, "tags": [[k, v] for k, v in tags.items()], "hash": text(key.hash),
            "workspace_id": text(key.workspace_id), "lookup_hash": text(key.lookup_hash)}


def workspace_facts(body: str | None) -> dict[str, Any]:
    """Python's verdict on a stored workspace body (None: no row), in the route's order."""
    failure = {"error": None, "reason": "error", "pause_causes": None, "billed_workspace_id": None}
    try:
        workspace = decode_auth_record(body, _RecordingWorkspace) if body is not None else None
    except Exception as exc:
        return {**failure, "error": type(exc).__name__}
    # storage_gcp.gateway_api_key_auth_context: a deleted workspace is none (403).
    if workspace is not None and workspace.deleted:
        workspace = None
    if workspace is None:
        return {**failure, "reason": "workspace_unavailable"}
    try:
        causes = list(workspace.billing_pause_causes)
        paused = workspace_billing_paused(workspace)
        # 403 for an abuse pause, 503 for the others.
        refused = _refuses(assert_workspace_billing_active, workspace, 403, 503)
        assert paused == refused
        federated = bool(workspace.federated_home)
        billed = workspace.id
    except Exception as exc:
        return {**failure, "error": type(exc).__name__}
    reason = "workspace_paused" if paused else "workspace_federated" if federated else None
    return {"error": None, "reason": reason, "pause_causes": causes,
            "billed_workspace_id": billed if isinstance(billed, str) else None}


def _iso(at: dt.datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _key(**changes: Any) -> ApiKey:
    key = ApiKey(
        hash="kh_synthetic_0001", salt="salt-synthetic",
        secret_hash="secret-hash-synthetic",  # noqa: S106 - synthetic test vector, no credential
        lookup_hash="lh_synthetic_0001", name="synthetic key", label="synthetic-label-0001",
        workspace_id="ws_synthetic_0001", creator_user_id="user_synthetic_0001",
        created_at="2026-01-01T00:00:00Z",
    )
    return dataclasses.replace(key, **changes)


def _stored(**changes: Any) -> str:
    """A key body as production stores it (storage_codec.json_body)."""
    return json_body(_key(**changes))


def _raw(base: dict[str, Any] | None = None, drop: tuple[str, ...] = (), **changes: Any) -> str:
    """A key body with arbitrary JSON values, keys in insertion order."""
    data = json.loads(_stored()) if base is None else dict(base)
    for name in drop:
        data.pop(name, None)
    data.update(changes)
    return json.dumps(data, separators=(",", ":"))


def _with_tags_text(tags_json: str) -> str:
    """A key body whose tags are the given JSON text verbatim (duplicates, order)."""
    body = _stored()
    assert body.endswith("}")
    return body[:-1] + ',"tags":' + tags_json + "}"


def key_cases() -> list[dict[str, Any]]:
    cases: list[tuple[str, str, dt.datetime, bool]] = []

    def add(name: str, body: str, at: dt.datetime = AT, exact: bool = True) -> None:
        cases.append((name, body, at, exact))

    add("eligible", _stored())
    add("eligible_minimal_fields", _raw(drop=("created_at", "usage_shard_count")))
    add("eligible_unknown_fields_ignored", _raw(future_field={"x": 1}, another=[1, 2]))
    add("disabled", _stored(disabled=True))
    add("disabled_and_expired", _stored(disabled=True, expires_at="2020-01-01T00:00:00Z"))
    add("not_disabled_explicit", _stored(disabled=False))

    # Expiry: at, around and away from the instant, in the forms Python writes.
    for label, delta in [("past_day", dt.timedelta(days=-1)), ("past_us", dt.timedelta(microseconds=-1)),
                         ("at", dt.timedelta(0)), ("future_us", dt.timedelta(microseconds=1)),
                         ("future_day", dt.timedelta(days=1)), ("future_year", dt.timedelta(days=365))]:
        moment = AT + delta
        add(f"expires_{label}_z", _stored(expires_at=_iso(moment)))
        add(f"expires_{label}_offset_zero", _stored(expires_at=moment.isoformat()))
        add(f"expires_{label}_naive", _stored(expires_at=moment.replace(tzinfo=None).isoformat()))
        east = moment.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
        add(f"expires_{label}_offset_east", _stored(expires_at=east.isoformat()))
        west = moment.astimezone(dt.timezone(dt.timedelta(hours=-5)))
        add(f"expires_{label}_offset_west", _stored(expires_at=west.isoformat()))
        add(f"expires_{label}_space", _stored(expires_at=moment.isoformat(sep=" ")))
    for label, delta in [("past_s", dt.timedelta(seconds=-1)), ("at", dt.timedelta(0)),
                         ("future_s", dt.timedelta(seconds=1))]:
        moment = AT_WHOLE + delta
        add(f"expires_whole_{label}_z", _stored(expires_at=_iso(moment)), at=AT_WHOLE)
        add(f"expires_whole_{label}_minutes", _stored(expires_at=moment.strftime("%Y-%m-%dT%H:%M")), at=AT_WHOLE)
    for digits in ["2", "25", "250", "2500", "25000", "250000", "2500001", "249999999", "250000999"]:
        add(f"expires_fraction_{digits}", _stored(expires_at=f"2026-10-23T12:00:00.{digits}Z"))
    add("expires_date_only_past", _stored(expires_at="2026-10-23"))
    add("expires_date_only_future", _stored(expires_at="2026-10-24"))
    add("expires_empty_string", _stored(expires_at=""))
    add("expires_null", _raw(expires_at=None))
    add("expires_leap_day", _stored(expires_at="2028-02-29T00:00:00Z"))
    add("expires_offset_max", _stored(expires_at="2026-10-24T12:00:00+23:59"))
    for text in ["never", "2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "2026-10-23T24:00:00Z",
                 "2026-10-23T12:60:00Z", "2026-10-23T12:00:60Z", "0000-01-01T00:00:00Z",
                 "2026-10-23T12:00:00+24:00", "2026-10-23T12:00:00Zjunk", "2026-10-23T12:00:00ZZ"]:
        # Python cannot parse these and counts them expired; Go may pass.
        add(f"expires_invalid_{text}", _stored(expires_at=text), exact=False)
    for text in ["20261024T000000", "2026-W44-1", "2026-10-24T00", "2026-10-24T00:00:00+05",
                 "2026-10-24T00:00:00+0530", "2026-10-24T00:00:00,5", "2026-10-24x00:00:00",
                 "2026-10-24T00:00:00+05:30:15", "2026-10-24T00:00:00.5+00:00", "2026-10-24T00:00:00z"]:
        # ISO forms Python parses and production never writes.
        add(f"expires_odd_{text}", _stored(expires_at=text), exact=False)

    # Scope.
    for label, scopes in [("empty", []), ("inference", ["inference"]), ("inference_and_more", ["keys:read", "inference"]),
                          ("other_only", ["keys:read"]), ("wrong_case", ["Inference"]),
                          ("duplicate", ["inference", "inference"]), ("blank", [""])]:
        add(f"scope_{label}", _raw(scopes=scopes))

    add("federated", _stored(federated_home="aws"))
    add("federated_empty", _raw(federated_home=""))
    add("app", _stored(app_id="app_synthetic"))
    add("app_federated_terms", _stored(app_id="app_synthetic", federated_home="aws",
                                       federated_app_markup_basis_points=100))
    for value in [0, 1, 10**15]:
        add(f"limit_{value}", _stored(limit_microdollars=value))
    add("limit_null", _raw(limit_microdollars=None))
    add("limit_with_reset", _stored(limit_microdollars=5, limit_reset="monthly"))
    for window in ("daily", "weekly", "monthly"):
        for value in (0, 500):
            add(f"window_{window}_{value}", _stored(**{f"limit_{window}_microdollars": value}))
        add(f"window_{window}_null", _raw(**{f"limit_{window}_microdollars": None}))
    add("window_alert_only", _stored(limit_daily_microdollars=10, budget_alert_only=True))
    add("alert_only_no_window", _stored(budget_alert_only=True))
    add("budget_strict", _stored(budget_strict=True))
    add("budget_strict_false", _raw(budget_strict=False))
    add("usage_counters_not_consulted", _stored(usage_microdollars=99, byok_usage_microdollars=4, reserved_microdollars=3))
    # key_usage_shard_count(api_key): absent is 1; 1 to 64 pass, anything it
    # cannot read as that raises (the route's 500).
    for count in (1, 2, 8, 63, 64):
        add(f"usage_shard_count_{count}", _stored(usage_shard_count=count))
    add("usage_shard_count_absent", _raw(drop=("usage_shard_count",)))
    for value in [0, -1, 65, 1000, True, False, None, [], {}, "abc", "", "5", 2.5, 1.0, "1"]:
        add(f"usage_shard_count_bad_{json.dumps(value)}", _raw(usage_shard_count=value), exact=False)
    add("usage_shard_count_8_strict", _stored(usage_shard_count=8, budget_strict=True), exact=False)
    add("usage_shard_count_1_strict", _stored(usage_shard_count=1, budget_strict=True))
    add("usage_shard_count_0_disabled", _raw(usage_shard_count=0, disabled=True), exact=False)
    # Repeated fields: Python's json.loads keeps the last value.
    stored = _stored()
    add("duplicate_workspace_id_last_wins", '{"workspace_id":"ws_synthetic_other",' + stored[1:])
    add("duplicate_hash_last_wins", '{"hash":"kh_synthetic_other",' + stored[1:])
    add("duplicate_workspace_id_other_last", stored[:-1] + ',"workspace_id":"ws_synthetic_other"}')
    add("duplicate_disabled_last_wins", stored[:-1] + ',"disabled":true}')
    add("management_irrelevant", _stored(management=True))

    # Tags, valid.
    add("tags_sorted_as_stored", _stored(tags={"team": "a", "env": "prod", "Cost Center": "R and D"}))
    add("tags_order_kept", _with_tags_text('{"zeta":"1","alpha":"2","mid":"3"}'))
    add("tags_duplicate_last_value_first_place", _with_tags_text('{"b":"1","a":"2","b":"3"}'))
    add("tags_portable_punctuation", _stored(tags={"k+-=._:/@": "v+-=._:/@ x"}))
    add("tags_empty_value", _stored(tags={"k": ""}))
    add("tags_space_key", _stored(tags={" ": " "}))
    add("tags_key_128", _stored(tags={"k" * 128: "v"}))
    add("tags_value_256", _stored(tags={"k": "v" * 256}))
    add("tags_50", _stored(tags={f"k{i:02d}": "v" for i in range(50)}))
    add("tags_null", _raw(tags=None))
    add("tags_list", _raw(tags=["a", "b"]))
    add("tags_string", _raw(tags="team=a"))
    add("tags_number", _raw(tags=5))
    add("tags_reserved_lookalike", _stored(tags={"awsx": "v", "trustedrouterx": "v", "aws": "v"}))
    add("tags_escaped_ascii", _with_tags_text('{"\\u0061":"\\u0062"}'))
    # At and past the 4096-byte cap: 2 + n*(len(k)+len(v)+5) + (n-1).
    sized: dict[str, str] = {f"{i:03d}" + "k" * 125: "v" * 125 for i in range(15)}
    used = 2 + sum(len(k) + len(v) + 5 for k, v in sized.items()) + len(sized) - 1
    last = 4096 - used - 1 - 5 - 128
    add("tags_bytes_at_cap", _stored(tags={**sized, "z" * 128: "v" * last}))
    add("tags_bytes_past_cap", _stored(tags={**sized, "z" * 128: "v" * (last + 1)}))
    # Tags, invalid (Python's merge raises InvalidTags: 400).
    add("tags_51", _stored(tags={f"k{i:02d}": "v" for i in range(51)}))
    add("tags_key_129", _stored(tags={"k" * 129: "v"}))
    add("tags_value_257", _stored(tags={"k": "v" * 257}))
    add("tags_empty_key", _stored(tags={"": "v"}))
    for key in ["aws:x", "AWS:x", "Aws:", "trustedrouter:y", "TrustedRouter:y"]:
        add(f"tags_reserved_{key}", _stored(tags={key: "v"}))
    for bad in ["a!b", "a\tb", "a\nb", "a#b", "a,b", "a*b", "a\"b", "a\\b", "a<b", "a\x7fb", "a\x00b"]:
        add(f"tags_bad_key_{bad!r}", _stored(tags={bad: "v"}))
        add(f"tags_bad_value_{bad!r}", _stored(tags={"k": bad}))
    add("tags_value_number", _raw(tags={"k": 5}))
    add("tags_value_null", _raw(tags={"k": None}))
    add("tags_value_bool", _raw(tags={"k": True}))
    add("tags_value_object", _raw(tags={"k": {"x": "y"}}))
    add("tags_value_list", _raw(tags={"k": ["x"]}))
    add("tags_invalid_after_valid", _with_tags_text('{"a":"1","b":"2","c!":"3"}'))

    # Several facts at once, seeded: the first in FACT_ORDER is the reason.
    rng = random.Random(3)  # noqa: S311 - reproducible test vectors, not secrets.
    for i in range(300):
        changes: dict[str, Any] = {}
        if rng.random() < 0.2:
            changes["disabled"] = True
        if rng.random() < 0.3:
            changes["expires_at"] = _iso(AT + dt.timedelta(seconds=rng.choice([-86400, -1, 0, 1, 86400])))
        if rng.random() < 0.2:
            changes["scopes"] = rng.choice([["inference"], ["keys:read"], ["keys:read", "inference"]])
        if rng.random() < 0.15:
            changes["federated_home"] = "azure"
        if rng.random() < 0.3:
            changes["tags"] = rng.choice([{"a": "1"}, {"aws:x": "1"}, {"b": "2", "a": "1"}, {"k": "v!"}])
        if rng.random() < 0.15:
            changes["app_id"] = "app_seeded"
        if rng.random() < 0.15:
            changes["limit_microdollars"] = rng.choice([0, 7])
        if rng.random() < 0.15:
            changes[f"limit_{rng.choice(['daily', 'weekly', 'monthly'])}_microdollars"] = rng.choice([0, 9])
        if rng.random() < 0.15:
            changes["budget_strict"] = True
        add(f"seeded_{i:03d}", _stored(**changes))

    # Bodies Python decodes without checking types, or cannot decode: Go may pass.
    base = json.loads(_stored())
    for field in ["hash", "salt", "secret_hash", "lookup_hash", "name", "label", "workspace_id", "creator_user_id"]:
        add(f"missing_{field}", _raw(base, drop=(field,)), exact=False)
    add("creator_null", _raw(base, creator_user_id=None))
    add("body_list", "[]", exact=False)
    add("body_number", "1", exact=False)
    add("body_null", "null", exact=False)
    add("body_not_json", "{", exact=False)
    for field, values in {
        "disabled": [1, 0, "yes", "", None, [], {}],
        "scopes": ["inference", None, [1], [None], [["inference"]], {"inference": 1}, ["inference", 1]],
        "federated_home": [1, None, True, [], 0],
        "app_id": [5, None, False, 0, []],
        "budget_strict": [1, 0, "true", None],
        "expires_at": [5, True, False, 0, [], {}],
        "limit_daily_microdollars": ["abc", "5", 1.5, True, [], {}],
        "limit_microdollars": ["x", 1.5, False, []],
        "hash": [5, None, "", "kh_é"],
        "lookup_hash": [5, None, ""],
        "workspace_id": [5, None, "", "ws_é", "ws\ud800"],
    }.items():
        for value in values:
            add(f"type_{field}_{json.dumps(value)}", _raw(base, **{field: value}), exact=False)
    for tags in [{"café": "1"}, {"k": "日本"}, {"k": " "}, {"k": "a b"}, {"Ａ": "1"},
                 {"k": "ok", "é": "é"}, {"ok": "1", "bad!": "é"}]:
        add(f"tags_non_ascii_{json.dumps(tags)}", _stored(tags=tags), exact=False)
    add("tags_lone_surrogate", _with_tags_text('{"k":"\\ud800"}'), exact=False)

    out = []
    names: set[str] = set()
    for name, body, at, exact in cases:
        assert name not in names, name
        names.add(name)
        out.append({"name": name, "body": body, "at": _iso(at), "exact": exact, "python": key_facts(body, at)})
    return out


def _workspace(**changes: Any) -> str:
    ws = Workspace(id="ws_synthetic_0001", name="synthetic", owner_user_id="user_synthetic_0001",
                   created_at="2026-01-01T00:00:00Z")
    return json_body(dataclasses.replace(ws, **changes))


def _workspace_raw(drop: tuple[str, ...] = (), **changes: Any) -> str:
    data = json.loads(_workspace())
    for name in drop:
        data.pop(name, None)
    data.update(changes)
    return json.dumps(data, separators=(",", ":"))


KEY_WORKSPACE = "ws_synthetic_0001"


def workspace_cases() -> list[dict[str, Any]]:
    workspace = _workspace()
    cases: list[tuple[str, str | None, bool]] = [
        ("eligible", _workspace(), True),
        ("absent", None, True),
        ("deleted", _workspace(deleted=True), True),
        ("deleted_and_paused", _workspace(deleted=True, billing_pause_causes=["manual"]), True),
        ("paused_cause", _workspace(billing_pause_causes=["manual"]), True),
        ("paused_causes_sorted", _workspace(billing_pause_causes=["zeta", "abuse", "zeta"]), True),
        ("paused_raw_causes_unsorted", _workspace_raw(billing_pause_causes=["zeta", "abuse", "zeta"]), True),
        ("paused_legacy_flag", _workspace_raw(billing_paused=True, billing_pause_causes=[]), True),
        ("paused_legacy_flag_no_causes_field", _workspace_raw(drop=("billing_pause_causes",), billing_paused=True), True),
        ("paused_flag_false_causes", _workspace_raw(billing_paused=False, billing_pause_causes=["manual"]), True),
        ("paused_flag_and_causes", _workspace_raw(billing_paused=True, billing_pause_causes=["abuse"]), True),
        ("not_paused_flag_false", _workspace_raw(billing_paused=False, billing_pause_causes=[]), True),
        ("federated", _workspace(federated_home="aws"), True),
        ("federated_and_paused", _workspace(federated_home="aws", billing_pause_causes=["federation"]), True),
        ("content_storage_irrelevant", _workspace(content_storage_enabled=True), True),
        ("unknown_fields_ignored", _workspace_raw(future={"x": 1}), True),
        ("minimal_fields", _workspace_raw(drop=("created_at",)), True),
        ("not_deleted_explicit", _workspace_raw(deleted=False), True),
        ("missing_id", _workspace_raw(drop=("id",)), False),
        ("missing_name", _workspace_raw(drop=("name",)), False),
        ("missing_owner", _workspace_raw(drop=("owner_user_id",)), False),
        ("causes_null", _workspace_raw(billing_pause_causes=None), False),
        ("causes_string", _workspace_raw(billing_pause_causes="abuse"), False),
        ("causes_number_items", _workspace_raw(billing_pause_causes=[1]), False),
        ("causes_mixed_items", _workspace_raw(billing_pause_causes=["a", 1]), False),
        ("causes_empty_string_item", _workspace_raw(billing_pause_causes=[""]), True),
        ("paused_flag_string", _workspace_raw(billing_paused="yes"), False),
        ("paused_flag_number", _workspace_raw(billing_paused=1), False),
        ("deleted_number", _workspace_raw(deleted=1), False),
        ("deleted_string", _workspace_raw(deleted="no"), False),
        ("deleted_null", _workspace_raw(deleted=None), False),
        ("federated_number", _workspace_raw(federated_home=5), False),
        ("federated_null", _workspace_raw(federated_home=None), False),
        ("body_list", "[]", False),
        ("body_not_json", "{", False),
        ("causes_non_ascii", _workspace(billing_pause_causes=["causé"]), False),
        # The workspace Python bills is the body's id; Go bills the key's
        # workspace_id, so a body whose id is another is not Go's to answer.
        ("id_mismatch", _workspace(id="ws_synthetic_other"), False),
        ("id_mismatch_case", _workspace(id=KEY_WORKSPACE.upper()), False),
        ("id_mismatch_prefix", _workspace(id=KEY_WORKSPACE + "#x"), False),
        ("id_empty", _workspace_raw(id=""), False),
        ("id_number", _workspace_raw(id=5), False),
        ("id_null", _workspace_raw(id=None), False),
        ("id_duplicate_last_matches", '{"id":"ws_synthetic_other",' + workspace[1:], True),
        ("id_duplicate_last_differs", workspace[:-1] + ',"id":"ws_synthetic_other"}', False),
        ("id_duplicate_both_match", workspace[:-1] + ',"id":"' + KEY_WORKSPACE + '"}', True),
        ("id_mismatch_and_paused", _workspace(id="ws_synthetic_other", billing_pause_causes=["manual"]), False),
        ("deleted_duplicate_last_wins", workspace[:-1] + ',"deleted":true}', True),
    ]
    return [{"name": name, "body": body, "key_workspace_id": KEY_WORKSPACE, "exact": exact,
             "python": workspace_facts(body)} for name, body, exact in cases]


def render() -> str:
    """The file's text: one case a line, so a change shows as the cases it changes."""
    keys = [json.dumps(c, sort_keys=True, separators=(",", ":")) for c in key_cases()]
    workspaces = [json.dumps(c, sort_keys=True, separators=(",", ":")) for c in workspace_cases()]
    # Read after every case has run: the fields the route's checks consulted.
    consulted = {kind: sorted(names) for kind, names in _CONSULTED.items()}
    return (
        "{\"generator\": \"scripts/fastpath/gateway_key_vectors.py\",\n"
        + " \"fact_order\": " + json.dumps(list(FACT_ORDER)) + ",\n"
        + " \"workspace_order\": " + json.dumps(list(WORKSPACE_ORDER)) + ",\n"
        + " \"route_reads\": " + json.dumps(ROUTE_READS, sort_keys=True) + ",\n"
        + " \"consulted_fields\": " + json.dumps(consulted, sort_keys=True) + ",\n"
        + " \"key_cases\": [\n  " + ",\n  ".join(keys) + "\n ],\n"
        + " \"workspace_cases\": [\n  " + ",\n  ".join(workspaces) + "\n ]}\n"
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(render())
    else:
        OUTPUT.write_text(render())
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
