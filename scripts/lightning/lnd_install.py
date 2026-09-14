"""Install authenticated LND binaries only; never create a wallet or spend BTC."""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
from pathlib import Path

from scripts.lightning.bitcoin_node import download, run, valid_signers

VERSION = "v0.21.3-beta"
REVISION = "572b561bf05f03dfe6135110970c4d858c3482dc"
ARCHIVE = f"lnd-linux-amd64-{VERSION}.tar.gz"
SHA256 = "aad62005d25bb0d974c5c1b135decc269d8f3e69ee9cde8bb6b32998100bc3fd"
# Five independent builders, matching the upstream release verification floor.
SIGNERS = {
    "boris": "BCEE34B0F9CD832214CE53005EA98470361ACB4F",
    "georgetsagk": "1583B601BB57CC7CD2DF8A87E08DEA9B12B66AF6",
    "gijswijs": "7530B54D5E45A68760E68926019A44857735FD20",
    "hieblmi": "32F7EA1E7A0339F7D37164B9F82D456EA023C9BF",
    "suheb": "3E9BD4436C288039CA827A9200C9E2BC2E45666F",
}


def verify(directory: Path) -> None:
    manifest = directory / f"manifest-{VERSION}.txt"
    home = directory / "gnupg"
    home.mkdir(mode=0o700)
    base = f"https://github.com/lightningnetwork/lnd/releases/download/{VERSION}"
    download(base + "/" + manifest.name, manifest, 1_000_000)
    for signer, fingerprint in SIGNERS.items():
        key = directory / f"{signer}.asc"
        signature = directory / f"manifest-{signer}-{VERSION}.sig"
        download(f"https://raw.githubusercontent.com/lightningnetwork/lnd/{REVISION}/scripts/keys/{signer}.asc", key, 1_000_000)
        download(base + "/" + signature.name, signature, 1_000_000)
        run("gpg", "--homedir", str(home), "--batch", "--import", str(key))
        result = run("gpg", "--homedir", str(home), "--batch", "--status-fd", "1",
                     "--verify", str(signature), str(manifest), check=False)
        if result.returncode or fingerprint not in valid_signers(result.stdout):
            raise ValueError("LND release signature verification failed: " + signer)
    entries = [line.split() for line in manifest.read_text().splitlines()]
    if [SHA256, ARCHIVE] not in entries:
        raise ValueError("LND pinned archive checksum absent from signed manifest")
    with (directory / ARCHIVE).open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != SHA256:
            raise ValueError("LND archive checksum mismatch")


def install() -> None:
    target = Path("/opt/lnd-" + VERSION)
    marker = target / "verified-sha256"
    if marker.exists() and marker.read_text().strip() == SHA256 and all((target / name).is_file() for name in ("lnd", "lncli")):
        print("LND verified binaries already installed; wallet unchanged")
        return
    with tempfile.TemporaryDirectory(prefix="lnd-release-") as temp:
        directory = Path(temp)
        download(f"https://github.com/lightningnetwork/lnd/releases/download/{VERSION}/{ARCHIVE}", directory / ARCHIVE)
        verify(directory)
        target.mkdir(mode=0o755, exist_ok=True)
        with tarfile.open(directory / ARCHIVE) as archive:
            for name in ("lnd", "lncli"):
                member = archive.getmember(f"lnd-linux-amd64-{VERSION}/{name}")
                if not member.isfile():
                    raise ValueError("LND binary must be a regular file")
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("LND binary missing")
                with source, (target / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
                (target / name).chmod(0o755)
        marker.write_text(SHA256 + "\n")
        print(run(str(target / "lnd"), "--version").stdout.strip())
        print("LND release verified by five pinned builders. Wallet not created; no payments enabled.")


if __name__ == "__main__":
    install()
