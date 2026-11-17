"""The edge reads a provider.max_price limit only when its literal is the
Go evaluator's plainLimit (fastpath/internal/routingsnapshot/max_price.go):
no sign, no exponent, at most nine digits before the point and six after it.
It then takes the literal's exact decimal value, in microdollars, with no
float. These tests hold that to Python itself: for every such literal,
routing._price_limit_microdollars on what json.loads makes of it (an int,
or a float whose repr Decimal reads) is that exact value; and just past the
grammar Python's value is not the literal's, or Python refuses it, which is
why Go passes those spellings instead.
"""

from __future__ import annotations

import json
import random
import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from trusted_router.routing import _price_limit_microdollars

ROOT = Path(__file__).resolve().parents[1]
GO_SOURCE = ROOT / "fastpath" / "internal" / "routingsnapshot" / "max_price.go"
FIELD = "provider.max_price.prompt"


def _plain_limit() -> re.Pattern[str]:
    """Go's plainLimit, read from the Go source, so the two cannot drift."""
    match = re.search(r"plainLimit = regexp\.MustCompile\(`([^`]+)`\)", GO_SOURCE.read_text(encoding="utf-8"))
    assert match, "plainLimit is not in max_price.go"
    return re.compile(match.group(1))


def _exact(literal: str) -> int:
    microdollars = Decimal(literal) * 1_000_000
    assert microdollars == microdollars.to_integral_value(), literal
    return int(microdollars)


def _pythons(literal: str) -> int:
    return _price_limit_microdollars(json.loads(literal), FIELD)


def test_every_limit_go_reads_is_pythons_exact_value() -> None:
    plain = _plain_limit()
    literals = ["0", "0.0", "1", "1.0", "1.50", "0.95", "0.950000", "0.949999", "3.30", "0.000001", "0.00005",
                "0.0001", "12.66", "999999999", "999999999.999999", "123456789.123456"]
    # Every six-place fraction of the integer part with the most digits, so
    # fifteen significant digits, and of 0, where repr writes the values
    # below 1e-4 with an exponent; then a fixed-seed sample of the rest.
    literals += [f"{whole}.{fraction:06d}" for whole in ("0", "999999999") for fraction in range(1_000_000)]
    rng = random.Random(20261117)  # noqa: S311 - fixed: the sample is a function of this text
    for _ in range(100_000):
        places = rng.randrange(7)
        literal = str(rng.randrange(1_000_000_000))
        literals.append(literal + (f".{rng.randrange(10**places):0{places}d}" if places else ""))
    for literal in literals:
        assert plain.fullmatch(literal), literal
        assert _pythons(literal) == _exact(literal), literal


@pytest.mark.parametrize(
    "literal",
    [
        "9999999999.999999",  # sixteen digits: the double's repr is another decimal
        "0.1000000000000000055511151231257827",  # Python reads 0.1, the literal is not whole microdollars
        "9007199254740993.0",  # past 2**53: the double is 9007199254740992
    ],
)
def test_past_the_grammar_pythons_value_is_not_the_literals(literal: str) -> None:
    assert not _plain_limit().fullmatch(literal)
    exact = Decimal(literal) * 1_000_000
    assert _pythons(literal) != exact


@pytest.mark.parametrize("literal", ["0.0000005", "1.0000005", "0.9500001", "0.30000000000000004"])
def test_a_seventh_place_is_refused_never_rounded(literal: str) -> None:
    assert not _plain_limit().fullmatch(literal)
    with pytest.raises(HTTPException) as refusal:
        _pythons(literal)
    assert refusal.value.status_code == 400
    assert "at most six decimal places" in refusal.value.detail["error"]["message"]


@pytest.mark.parametrize(
    ("literal", "microdollars"),
    # Spellings Go passes though Python reads them: trailing zeros past the
    # sixth place, an exponent, ten integer digits, a string, negative zero.
    [("0.95000000", 950_000), ("1E0", 1_000_000), ("1234567890", 1_234_567_890_000_000), ('"0.95"', 950_000),
     ("-0.0", 0)],
)
def test_go_passes_some_spellings_python_reads(literal: str, microdollars: int) -> None:
    assert not _plain_limit().fullmatch(literal)
    assert _pythons(literal) == microdollars
