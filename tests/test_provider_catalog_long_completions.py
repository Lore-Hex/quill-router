from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from scripts.pricing.provider_contract_catalog import discover_provider_contract_catalog
from tests.test_hixtube_provider import catalog
from trusted_router.provider_contract import PROVIDER_CATALOG_V2_SCHEMA


@pytest.mark.parametrize("version,seconds,valid", [
    ("2.0", 900, True), ("2.0", 901, False), ("2.0", 3540, False),
    ("2.1", 3540, True), ("2.1", 3600, True), ("2.1", 3601, False),
    ("2.1", 0, False), ("2.1", True, False), ("2.1", "3540", False),
    ("2.2", 120, False),
])
def test_schema_and_ingestion_share_versioned_completion_bounds(version, seconds, valid):
    payload = catalog()
    payload["contract_version"] = version
    payload["data"][0]["reliability"]["completion_timeout_seconds"] = seconds
    schema = Draft202012Validator(PROVIDER_CATALOG_V2_SCHEMA)
    if valid:
        schema.validate(payload)
        _, rows = discover_provider_contract_catalog(payload, upstream_id_map={})
        assert rows[payload["data"][0]["id"]]["reliability"]["completion_timeout_seconds"] == seconds
    else:
        with pytest.raises(ValidationError):
            schema.validate(payload)
        with pytest.raises(RuntimeError):
            discover_provider_contract_catalog(payload, upstream_id_map={})


@pytest.mark.parametrize("field", ["first_token_timeout_seconds", "stream_idle_timeout_seconds"])
def test_long_completions_do_not_weaken_progress_bounds(field):
    payload = catalog()
    payload["contract_version"] = "2.1"
    payload["data"][0]["reliability"].update(completion_timeout_seconds=3540)
    payload["data"][0]["reliability"][field] = 301
    with pytest.raises(ValidationError):
        Draft202012Validator(PROVIDER_CATALOG_V2_SCHEMA).validate(payload)
    with pytest.raises(RuntimeError):
        discover_provider_contract_catalog(payload, upstream_id_map={})


def test_public_schema_is_valid_and_v20_example_is_unchanged():
    from trusted_router.provider_contract import PROVIDER_CATALOG_V2_EXAMPLE

    Draft202012Validator.check_schema(PROVIDER_CATALOG_V2_SCHEMA)
    before = deepcopy(PROVIDER_CATALOG_V2_EXAMPLE)
    Draft202012Validator(PROVIDER_CATALOG_V2_SCHEMA).validate(before)
    assert before["contract_version"] == "2.0"


def test_actual_neocloud_million_catalog_parses_without_invented_discount():
    # Emitted by nib scripts/fixtures/pilot_catalog, not a reconstructed row.
    payload = json.loads((Path(__file__).parent / "fixtures/nib_million_catalog_v21.json").read_text())
    Draft202012Validator(PROVIDER_CATALOG_V2_SCHEMA).validate(payload)
    prices, rows = discover_provider_contract_catalog(payload, upstream_id_map={})
    model = "dealignai/deepseek-v4.1-flash-1017-uncensored-fp8"
    row = rows[model]
    assert row["context_length"] == 1048576
    assert row["max_output_tokens"] == 393216
    assert row["reliability"]["completion_timeout_seconds"] == 3540
    assert payload["data"][0]["pricing"]["cached_input"] == payload["data"][0]["pricing"]["input"]
    assert model in prices
    assert payload["data"][0]["capabilities"]["reasoning"] is False
