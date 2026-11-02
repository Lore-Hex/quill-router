from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.merge_shard_durations import main


def test_merge_downloaded_shards(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    current = tmp_path / ".test_durations"
    original = {"unseen": 7, "slower": 1, "faster": 20, "same": 3}
    current.write_text(json.dumps(original))
    artifacts = tmp_path / "artifacts"
    for name, data in (
        ("default", {"new": 4, "slower": 9, "faster": 8, "same": 3}),
        ("post-cutover", {"new": 6, "slower": 2, "faster": 5}),
    ):
        folder = artifacts / f"durations-{name}-1"
        folder.mkdir(parents=True)
        (folder / "durations-1.json").write_text(json.dumps(data))
    output = tmp_path / "merged.json"

    main([str(artifacts), str(current), str(output)])

    expected = {"faster": 8, "new": 6, "same": 3, "slower": 9, "unseen": 7}
    assert output.read_text() == json.dumps(expected, indent=4) + "\n"
    assert json.loads(current.read_text()) == original
    assert capsys.readouterr().out == "Added 1 IDs, updated 2 IDs, kept 2 IDs\n"


def test_missing_artifacts_leave_output_untouched(tmp_path: Path) -> None:
    current = tmp_path / ".test_durations"
    current.write_text('{"test": 1}')
    with pytest.raises(ValueError, match="no durations"):
        main([str(tmp_path / "missing"), str(current), str(current)])
    assert current.read_text() == '{"test": 1}'


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "slow"])
def test_invalid_durations_leave_output_untouched(tmp_path: Path, value: object) -> None:
    current = tmp_path / ".test_durations"
    current.write_text('{"test": 1}')
    (tmp_path / "durations-1.json").write_text(json.dumps({"test": value}))
    with pytest.raises(ValueError, match="finite, nonnegative"):
        main([str(tmp_path), str(current), str(current)])
    assert current.read_text() == '{"test": 1}'


def test_profile_uses_independent_runs_and_does_not_mix_clocks(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text('{"unobserved": 7}')
    for run, value in enumerate((10, 12, 1000)):
        for clock, duration in (("default", value), ("post-cutover", 1)):
            directory = tmp_path / str(run) / f"durations-{clock}-1"
            directory.mkdir(parents=True)
            (directory / "durations-1.json").write_text(json.dumps({
                "test@conformance-postgres": duration,
            }))
    output = tmp_path / "profile.json"
    main([str(tmp_path), str(current), str(output), "--profile", "default"])
    assert json.loads(output.read_text()) == {"unobserved": 7, "test@conformance-postgres": 12}
    main([str(tmp_path), str(current), str(output), "--profile", "post-cutover"])
    assert json.loads(output.read_text())["test@conformance-postgres"] == 1


def test_profile_refuses_single_run_and_duplicate_shards(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text('{}')
    directory = tmp_path / "run1" / "durations-default-1"
    directory.mkdir(parents=True)
    (directory / "durations-1.json").write_text('{"test": 1}')
    args = [str(tmp_path), str(current), str(current), "--profile", "default"]
    with pytest.raises(ValueError, match="at least three"):
        main(args)
    (directory / "durations-2.json").write_text('{"test": 1}')
    with pytest.raises(ValueError, match="duplicate"):
        main(args)
    assert current.read_text() == '{}'
