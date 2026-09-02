from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qs, urlsplit

from scripts.build_google_ads_experiment_matrix import cell_record
from trusted_router.marketing_experiments import (
    GOOGLE_SEARCH_CELLS,
    GOOGLE_SEARCH_CELLS_PER_WAVE,
    GOOGLE_SEARCH_EXPERIMENT_ID,
    GOOGLE_SEARCH_WAVE_COUNT,
    PROMISES,
    assigned_google_search_cell,
    google_search_wave,
    valid_experiment_identity,
)


def test_catalog_has_hundreds_of_unique_valid_cells() -> None:
    assert len(GOOGLE_SEARCH_CELLS) == 384
    assert len({cell.cell_id for cell in GOOGLE_SEARCH_CELLS}) == 384
    assert all(
        valid_experiment_identity(cell.experiment_id, cell.cell_id)
        for cell in GOOGLE_SEARCH_CELLS
    )


def test_controlled_waves_cover_every_cell_once_and_balance_promises() -> None:
    waves = [google_search_wave(index) for index in range(GOOGLE_SEARCH_WAVE_COUNT)]

    assert GOOGLE_SEARCH_WAVE_COUNT == 96
    assert all(len(wave) == GOOGLE_SEARCH_CELLS_PER_WAVE for wave in waves)
    assert {cell.cell_id for wave in waves for cell in wave} == {
        cell.cell_id for cell in GOOGLE_SEARCH_CELLS
    }
    assert all(len(Counter(cell.promise.code for cell in wave)) == 4 for wave in waves)
    assert Counter(
        cell.promise.code for wave in waves for cell in wave
    ) == Counter({promise.code: 48 for promise in PROMISES})


def test_assignment_is_sticky_and_reasonably_balanced() -> None:
    assignments = [
        assigned_google_search_cell(f"person-{index}").cell_id
        for index in range(8_000)
    ]
    counts = Counter(assignments)

    assert set(counts) == {cell.cell_id for cell in google_search_wave(0)}
    assert all(1_800 <= count <= 2_200 for count in counts.values())
    assert assigned_google_search_cell("stable").cell_id == (
        assigned_google_search_cell("stable").cell_id
    )


def test_export_record_has_exact_cell_identity_and_google_length_limits() -> None:
    cell = google_search_wave(0)[0]
    record = cell_record(cell, wave=0)
    final_url = urlsplit(str(record["final_url"]))
    query = parse_qs(final_url.query)

    assert query["tr_exp"] == [GOOGLE_SEARCH_EXPERIMENT_ID]
    assert query["tr_cell"] == [cell.cell_id]
    assert query["utm_content"] == [cell.cell_id]
    assert final_url.path.endswith(cell.cell_id)
    assert all(len(str(record[name])) <= 30 for name in ("headline_1", "headline_2", "headline_3"))
    assert all(len(str(record[name])) <= 90 for name in ("description_1", "description_2"))


def test_experiment_identity_rejects_partial_or_unsafe_values() -> None:
    assert not valid_experiment_identity(GOOGLE_SEARCH_EXPERIMENT_ID, "")
    assert not valid_experiment_identity("", "g3_or_migrate_attest_key")
    assert not valid_experiment_identity("google-search", "cell")
    assert not valid_experiment_identity("google_search", "cell?secret=value")
    assert not valid_experiment_identity(GOOGLE_SEARCH_EXPERIMENT_ID, "g3_fake_cell")
