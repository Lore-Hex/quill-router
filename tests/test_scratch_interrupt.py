"""TEMPORARY, scratch PR only: a shard that stops early must make the shadow comparison unavailable."""


def test_scratch_deliberate_failure() -> None:
    assert 1 == 2, "deliberate failure"


def test_scratch_interrupts_its_shard() -> None:
    raise KeyboardInterrupt
