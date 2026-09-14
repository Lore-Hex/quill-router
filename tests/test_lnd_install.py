from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.lightning import lnd_install as lnd


@pytest.mark.parametrize("missing", list(lnd.SIGNERS))
def test_every_pinned_builder_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.setattr(lnd, "download", lambda *args: None)

    def run(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--import" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        signature = args[-2]
        name = next(name for name in lnd.SIGNERS if f"manifest-{name}-" in signature)
        fingerprint = lnd.SIGNERS[name]
        return subprocess.CompletedProcess(
            args, 1 if name == missing else 0,
            f"[GNUPG:] VALIDSIG {fingerprint} 2026-09-01 1788267600 0 4 0 1 8 00 {fingerprint}", "",
        )

    monkeypatch.setattr(lnd, "run", run)
    with pytest.raises(ValueError, match="signature verification failed"):
        lnd.verify(tmp_path)


def test_lnd_install_does_not_initialize_wallet_or_open_network_ports() -> None:
    source = Path(lnd.__file__).read_text()
    assert len(lnd.SIGNERS) == 5
    assert len(set(lnd.SIGNERS.values())) == 5
    assert '"--version"' in source
    assert '"create"' not in source
    assert '"openchannel"' not in source
    assert '"--listen"' not in source
