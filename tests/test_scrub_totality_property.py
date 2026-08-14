"""Property tests for scrub totality.

`_scrub` is the last barrier on the Axiom path: `_AxiomScrubFilter` runs every
log-record attribute through it and hands the record straight to the shipper.
The law:

    for every value v built from any Python container shape,
        no declared secret fragment or prefix survives in the scrubbed output

It was false. `_scrub` recursed into dict, list and str and returned everything
else unchanged, so:

    _scrub(("a", "sk-tr-v1-SECRET"))  ->  ('a', 'sk-tr-v1-SECRET')

and a tuple is serialised by Axiom's ujson as a JSON array, preserving the
string. The old tests all passed because every one of them embedded the secret
in a str inside a dict or list — the two branches that already worked.

The property generates *shapes*, not values: canaries are planted at random
leaf positions inside recursively-built containers, and the assertion is over
the serialised image. That is what makes it able to find a container type
nobody thought to write a test for, which is exactly how the tuple hole
survived.

Scope limit worth stating plainly: this quantifies only over the declared
SENSITIVE_* sets and the key blocklist. A secret in a format the blocklist does
not recognise is out of scope for any blocklist scrubber, here or anywhere.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trusted_router.axiom_config import _AxiomScrubFilter
from trusted_router.sentry_config import (
    SENSITIVE_KEYS,
    SENSITIVE_STRING_FRAGMENTS,
    SENSITIVE_STRING_PREFIXES,
    _scrub,
)

CANARY_SUFFIX = "CANARY9f3a2b"
SECRETS = [f"{fragment}{CANARY_SUFFIX}" for fragment in SENSITIVE_STRING_FRAGMENTS] + [
    f"{prefix}{CANARY_SUFFIX}" for prefix in SENSITIVE_STRING_PREFIXES
]


def _leaked(scrubbed: Any) -> bool:
    """Did a canary survive into the serialised image?

    Serialising with default=str is the faithful check: it is what a shipper
    does to a value it does not natively understand, so a secret that survives
    only inside an object's string form still counts as leaked.
    """
    return CANARY_SUFFIX in json.dumps(scrubbed, default=str, ensure_ascii=False)


# A recursive strategy over every container shape a log extra can take. The
# tuple/set/frozenset/bytes arms are the ones that used to pass secrets through.
leaves = st.one_of(
    st.sampled_from(SECRETS),
    st.sampled_from(SECRETS).map(lambda s: f"context {s} trailing"),
    st.sampled_from(SECRETS).map(str.encode),
    st.text(max_size=8),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
)

# Mapping KEYS carry secrets too. The first version of this module generated
# keys only from st.text(), so every canary lived in a value — and a real leak
# through {"sk-tr-v1-...": 3} survived the whole suite until an external review
# pointed at it. That shape is not exotic: a per-key counter or a cache-hit
# tally is exactly a dict keyed by the credential.
keys = st.one_of(st.text(max_size=6), st.sampled_from(SECRETS))

values = st.recursive(
    leaves,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.lists(children, max_size=4).map(tuple),
        st.dictionaries(keys, children, max_size=4),
        st.frozensets(st.sampled_from(SECRETS) | st.text(max_size=6), max_size=3),
    ),
    max_leaves=12,
)


# ----------------------------------------------------------------- the law ---


@given(value=values)
@settings(max_examples=1500)
def test_no_declared_secret_survives_any_container_shape(value: Any) -> None:
    assert not _leaked(_scrub(value)), f"secret survived _scrub of {value!r}"


@given(value=values)
@settings(max_examples=500)
def test_scrubbing_is_idempotent(value: Any) -> None:
    """A second pass must be a no-op, or the output is not a fixed point and a
    re-scrubbed record could differ from the one that shipped."""
    once = _scrub(value)
    assert _scrub(once) == once


@given(
    key=st.sampled_from(sorted(SENSITIVE_KEYS)),
    payload=values,
)
@settings(max_examples=300)
def test_a_sensitive_key_redacts_its_whole_subtree(key: str, payload: Any) -> None:
    """Key-based redaction must not depend on the shape of what is under it."""
    scrubbed = _scrub({key: payload})
    assert scrubbed[key] == "[Filtered]"


# ------------------------------------------------- termination and limits ---


def test_a_cycle_terminates() -> None:
    cyclic: dict[str, Any] = {"secret_holder": "sk-tr-v1-" + CANARY_SUFFIX}
    cyclic["self"] = cyclic
    scrubbed = _scrub(cyclic)
    assert scrubbed["self"] == "[Filtered-cycle]"
    assert not _leaked(scrubbed)


def test_deep_nesting_terminates() -> None:
    deep: Any = "sk-tr-v1-" + CANARY_SUFFIX
    for _ in range(200):
        deep = {"n": deep}
    scrubbed = _scrub(deep)
    assert not _leaked(scrubbed)


def test_a_huge_container_is_truncated_not_walked_forever() -> None:
    scrubbed = _scrub(list(range(5_000)))
    assert len(scrubbed) <= 501


# -------------------------------------------------------- unknown objects ---


def test_an_unknown_object_is_named_not_repred() -> None:
    """repr() on an arbitrary object can execute application code, raise,
    recurse, or expose a value the blocklist does not recognise. The type name
    keeps the diagnostic signal without any of that."""

    class Custom:
        def __repr__(self) -> str:  # pragma: no cover - must never be called
            raise AssertionError("_scrub must not call repr on unknown objects")

    assert _scrub(Custom()) == "[Filtered-Custom]"


def test_structurally_safe_types_survive_for_diagnostics() -> None:
    """UUIDs and timestamps cannot carry a secret and are common in extras, so
    they keep their value rather than being replaced by a type name."""
    import datetime
    import uuid

    identifier = uuid.uuid4()
    assert _scrub(identifier) == str(identifier)
    assert _scrub(datetime.date(2026, 8, 13)) == "2026-08-13"


def test_bytes_are_redacted_wholesale() -> None:
    """Decoding and blocklist-scrubbing bytes would still ship a key that
    arrived base64'd, hex'd, or in an unrecognised framing."""
    assert _scrub(b"sk-tr-v1-" + CANARY_SUFFIX.encode()) == "[Filtered-bytes]"
    assert _scrub(b"harmless") == "[Filtered-bytes]"


# ------------------------------------------------------ the Axiom filter ---


@given(value=values)
@settings(max_examples=400)
def test_the_axiom_filter_ships_no_canary_in_an_extra(value: Any) -> None:
    """End to end at the boundary that actually ships: the filter mutates the
    record in place and axiom-py serialises it immediately afterwards."""
    record = logging.LogRecord(
        name="trusted_router.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event happened",
        args=(),
        exc_info=None,
    )
    record.context = value  # type: ignore[attr-defined]

    assert _AxiomScrubFilter().filter(record) is True
    assert not _leaked(
        {k: v for k, v in record.__dict__.items() if not k.startswith("_")}
    )


@pytest.mark.parametrize("secret", SECRETS)
def test_a_bare_secret_in_the_message_is_scrubbed(secret: str) -> None:
    """The message path applied only the `key=value` and e-mail regexes, so a
    bare token in ordinary prose — "rotating sk-tr-v1-…" — shipped verbatim.
    It now goes through the same declared blocklist as the structured fields."""
    record = logging.LogRecord(
        name="trusted_router.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="rotating %s now",
        args=(secret,),
        exc_info=None,
    )

    assert _AxiomScrubFilter().filter(record) is True
    assert CANARY_SUFFIX not in record.getMessage()


def test_a_secret_used_as_a_mapping_key_is_scrubbed() -> None:
    """The concrete shape the generator missed.

    `_scrub` checked whether a key NAME was sensitive ("api_key") but never
    scrubbed the key's own text, so a credential used as a key survived intact
    while the value beside it was filtered. Found by external review, not by
    this module — which is why the generator above now produces secret keys.
    """
    secret = f"sk-tr-v1-{CANARY_SUFFIX}"
    assert not _leaked(_scrub({secret: 1}))
    assert not _leaked(_scrub({"nested": {secret: 1}}))
    assert not _leaked(_scrub([{secret: 1}]))
    assert not _leaked(_scrub({secret: secret}))


def test_scrubbing_a_key_does_not_disturb_a_harmless_one() -> None:
    """The bound must not rename ordinary keys, or every structured log field
    changes name and dashboards break."""
    scrubbed = _scrub({"request_id": "abc", "count": 3})
    assert scrubbed == {"request_id": "abc", "count": 3}
