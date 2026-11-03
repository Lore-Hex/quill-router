"""Exercise the installer's actual backup loop from an unrelated SSH directory."""

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class ObserverBackupTest(unittest.TestCase):
    def test_existing_installation_is_backed_up_from_login_directory(self) -> None:
        cp = shutil.which("gcp") or shutil.which("cp")
        if cp is None or "GNU coreutils" not in subprocess.run(  # noqa: S603 - local coreutils binary
            [cp, "--version"], capture_output=True, text=True, check=False,
        ).stdout:
            self.skipTest("The production installer uses GNU coreutils")
        installer = (Path(__file__).resolve().parents[1]
                     / "scripts/ops/install_price_refresh_observer.sh").read_text()
        start = installer.index("for path in /opt/tr-price-refresh ")
        end = installer.index("\ndone", start) + len("\ndone")
        loop = installer[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, backup = root / "login", root / "backup"
            home.mkdir()
            backup.mkdir(mode=0o700)
            paths = (
                "/opt/tr-price-refresh",
                "/etc/systemd/system/price-refresh-observer.service",
                "/etc/systemd/system/price-refresh-observer.timer",
                "/etc/tr-price-refresh/ingest.dsn",
            )
            expected = []
            for index, path in enumerate(paths):
                source = root / "source" / path.lstrip("/")
                source.parent.mkdir(parents=True, exist_ok=True)
                if index == 0:
                    source.mkdir()
                    source_file = source / "price_refresh_alerts.py"
                else:
                    source_file = source
                source_file.write_text(f"backup-fixture-{index}\n")
                source_file.chmod(0o600 if index == 3 else 0o644)
                os.utime(source_file, (1_700_000_000, 1_700_000_000))
                expected.append(source_file)
                loop = loop.replace(path, shlex.quote(str(source)))
            shell = (
                "set -euo pipefail\n"
                f"backup={shlex.quote(str(backup))}\n"
                f"cp() {{ command {shlex.quote(cp)} \"$@\"; }}\n"
                + loop
            )
            result = subprocess.run(  # noqa: S603 - repository loop and quoted fixture paths
                ["/bin/bash", "-c", shell], cwd=home, capture_output=True, text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for source in expected:
                copy = backup / str(source).lstrip("/")
                self.assertEqual(copy.read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(copy.stat().st_mode),
                                 stat.S_IMODE(source.stat().st_mode))
                self.assertEqual(copy.stat().st_mtime_ns, source.stat().st_mtime_ns)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
