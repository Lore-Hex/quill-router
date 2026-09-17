"""Export the public LightningRouter mirror from committed, allowlisted files only."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

APP = "experiments/lightning_router"
TEMPLATES = "scripts/lightning/public_repository"
PATHS = (
    APP, "scripts/lightning", "docs/bitcoin-node.md",
    "docs/lightning-receiving-node.md", "docs/lightning-funding-production.md",
    "docs/lightning-btcpay.md", "docs/lightning-liquidity.md", "docs/lightning-lexe.md",
    ".github/workflows/lightning-experiment.yml", "tsconfig.json",
)
BLOCKED = {".private", ".env", ".venv", "node_modules", "__pycache__", "test-results"}
SUFFIXES = {".db", ".sqlite", ".pem", ".key", ".macaroon", ".wallet", ".pyc"}
BRAND_MARK = f"{APP}/web/lightningrouter-mark.webp"
SOCIAL_IMAGE = f"{APP}/web/lightningrouter-og.jpg"
SOCIAL_IMAGE_SHA256 = "698c899d0bb009795f1a816a520d59aae2f30cac6e3b3eb97f3d2ea716276718"


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])  # noqa: S603, S607 -- local Git plumbing, no shell


def export(root: Path, ref: str, output: Path) -> dict[str, str]:
    if output.exists():
        raise ValueError("export destination must not exist")
    commit = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    entries = git(root, "ls-tree", "-rz", commit, "--", *PATHS).split(b"\0")
    files: dict[str, tuple[bytes, int]] = {}
    for entry in filter(None, entries):
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        name = raw_name.decode("utf-8")
        path = PurePosixPath(name)
        if (mode not in {"100644", "100755"} or kind != "blob"
                or path.is_absolute() or ".." in path.parts
                or BLOCKED.intersection(path.parts) or path.suffix in SUFFIXES):
            raise ValueError(f"unsafe public source path: {name}")
        target = name.removeprefix(TEMPLATES + "/") if name.startswith(TEMPLATES + "/") else name
        content = git(root, "cat-file", "blob", oid)
        if name == BRAND_MARK:
            # Reviewed brand assets are the only binary publication exceptions.
            if (not 20 <= len(content) <= 100_000 or content[:4] != b"RIFF"
                    or content[8:12] != b"WEBP"
                    or content[12:16] not in {b"VP8 ", b"VP8L", b"VP8X"}
                    or int.from_bytes(content[4:8], "little") != len(content) - 8):
                raise ValueError("invalid public brand image")
        elif name == SOCIAL_IMAGE:
            if (not 1000 <= len(content) <= 500_000 or not content.startswith(b"\xff\xd8\xff")
                    or not content.endswith(b"\xff\xd9")
                    or hashlib.sha256(content).hexdigest() != SOCIAL_IMAGE_SHA256):
                raise ValueError("invalid public social image")
        else:
            content.decode("utf-8")
        if path.suffix == ".json":
            json.loads(content)
        if target in files:
            raise ValueError(f"duplicate public path: {target}")
        files[target] = content, int(mode[-3:], 8)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES"):
        files[name] = files[f"{APP}/{name}"]
    hashes = {name: hashlib.sha256(content).hexdigest() for name, (content, _) in sorted(files.items())}
    manifest = {"source_repository": "https://github.com/Lore-Hex/quill-router",
                "source_commit": commit, "files_sha256": hashes}
    output.mkdir(parents=True)
    for name, (content, mode) in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(mode)
    (output / "SOURCE.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    hashes = export(Path(__file__).resolve().parents[2], args.ref, args.output)
    print(f"Exported {len(hashes)} committed files to {args.output}")


if __name__ == "__main__":
    main()
