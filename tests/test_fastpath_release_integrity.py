"""The fast path's release integrity: what a node and the load generator
install is checked against the release's SHA256SUMS and against the digests
infra/fastpath.tf pins beside the release, before anything is installed;
the release workflow never overwrites a published object and builds every
binary from an unmodified tree; the bucket keeps earlier generations.

Each check runs the real script (the startup template's install block, the
load generator's script, the workflow's steps) under bash with stubs for
gcloud, the metadata server and systemd-cat, against a directory standing
in for the bucket, and each refusal is paired with the case that passes."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TF = ROOT / "infra" / "fastpath.tf"
SCRIPT = ROOT / "infra" / "fastpath_node_startup.sh.tftpl"
WORKFLOW = ROOT / ".github" / "workflows" / "fastpath-release.yml"
RUNBOOK = ROOT / "docs" / "runbooks" / "fastpath-deploy.md"

RELEASE = "0123456789abcdef0123456789abcdef01234567"
FILES = ("fastpath", "fastpathctl", "loadgen", "fastpathwatch", "load-mix.json")
TOOLS = ("loadgen", "fastpathwatch", "load-mix.json")
SHA256 = re.compile(r"[0-9a-f]{64}")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sums(files: dict[str, bytes]) -> bytes:
    """SHA256SUMS as the workflow writes it: sha256sum's text-mode lines."""
    return "".join(f"{_sha(data)}  {name}\n" for name, data in files.items()).encode()


def _release(**overrides: bytes) -> dict[str, bytes]:
    files = {name: f"{name} of {RELEASE}\n".encode() for name in FILES}
    files.update(overrides)
    return files


def _block(tf: str, kind: str, name: str) -> str:
    start = tf.index(f'resource "{kind}" "{name}" {{')
    end = tf.find("\n}\n", start)
    assert end > start
    return tf[start:end]


def _bash() -> str:
    bash = shutil.which("bash")
    assert bash
    return bash


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp), "TMPDIR": str(tmp), **extra}


# --- The pinned digests (infra/fastpath.tf) ----------------------------------


def _digests(tf: str) -> dict[str, dict[str, str]]:
    """local.fastpath_release_digests: each release's fastpath and
    sha256sums values, as written."""
    start = tf.index("  fastpath_release_digests = {\n")
    end = tf.index("\n  }\n", start)
    entries = re.findall(r'^\s*"([^"]*)"\s*=\s*\{\n(.*?)^\s*\}$', tf[start:end], re.M | re.S)
    return {release: dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]*)"$', body, re.M)) for release, body in entries}


def _digest_problems(tf: str) -> list[str]:
    """What is wrong with the pin's digests: none when the pinned release has
    an entry and every entry carries a well-formed fastpath and sha256sums."""
    pinned = re.search(r'^\s*fastpath_release\s*=\s*"([^"]*)"$', tf, re.M)
    if not pinned:
        return ["no fastpath_release pin"]
    digests = _digests(tf)
    problems = []
    if pinned.group(1) not in digests:
        problems.append(f"the pinned release {pinned.group(1)} has no digests")
    for release, entry in digests.items():
        if not re.fullmatch(r"[0-9a-f]{40}", release):
            problems.append(f"{release!r} is not a 40-character commit")
        if set(entry) != {"fastpath", "sha256sums"}:
            problems.append(f"{release} carries {sorted(entry)}, not fastpath and sha256sums")
        for name, digest in entry.items():
            if not SHA256.fullmatch(digest):
                problems.append(f"{release} {name} {digest!r} is not a sha256 digest")
    return problems


def test_the_pin_carries_well_formed_digests_for_the_pinned_release() -> None:
    tf = _read(TF)
    assert _digest_problems(tf) == []
    pinned = re.search(r'^\s*fastpath_release\s*=\s*"([0-9a-f]{40})"$', tf, re.M)
    assert pinned and pinned.group(1) in _digests(tf)


def test_a_pin_without_its_digests_is_found() -> None:
    """The control: a pin moved without an entry, a short digest, an
    uppercase one and an entry missing sha256sums each fail the check."""
    tf = _read(TF)
    pinned = re.search(r'^(\s*fastpath_release\s*=\s*")([0-9a-f]{40})"$', tf, re.M)
    assert pinned
    entry = _digests(tf)[pinned.group(2)]
    moved = tf.replace(pinned.group(0), f'{pinned.group(1)}{"f" * 40}"')
    assert any("has no digests" in p for p in _digest_problems(moved)), _digest_problems(moved)
    short = tf.replace(entry["fastpath"], entry["fastpath"][:63])
    assert any("fastpath" in p and "not a sha256 digest" in p for p in _digest_problems(short))
    upper = tf.replace(entry["sha256sums"], entry["sha256sums"].upper())
    assert any("sha256sums" in p and "not a sha256 digest" in p for p in _digest_problems(upper))
    dropped = re.sub(rf'\n\s*sha256sums\s*=\s*"{entry["sha256sums"]}"', "", tf, count=1)
    assert any("not fastpath and sha256sums" in p for p in _digest_problems(dropped))


def test_the_digests_reach_the_instances_and_a_missing_one_fails_the_plan() -> None:
    """The node and the load generator take the pinned digests as instance
    attributes, looked up by the pinned release, and a precondition on each
    refuses a plan whose release has no well-formed pair."""
    tf = _read(TF)
    assert re.search(
        r"^\s*fastpath_release_sha256\s*=\s*try\(local\.fastpath_release_digests\[local\.fastpath_release\]\.fastpath, \"\"\)$",
        tf, re.M)
    assert re.search(
        r"^\s*fastpath_release_sums_sha256\s*=\s*try\(local\.fastpath_release_digests\[local\.fastpath_release\]\.sha256sums, \"\"\)$",
        tf, re.M)
    ok = tf[tf.index("fastpath_release_digests_ok = alltrue(["):]
    ok = ok[:ok.index("])")]
    for name in ("fastpath_release_sha256", "fastpath_release_sums_sha256"):
        assert f'can(regex("^[0-9a-f]{{64}}$", local.{name}))' in ok, name
    node = _block(tf, "google_compute_instance", "fastpath_node")
    loadgen = _block(tf, "google_compute_instance", "fastpath_loadgen")
    assert re.search(r"^\s*fastpath-release-sha256\s*=\s*local\.fastpath_release_sha256$", node, re.M)
    for block in (node, loadgen):
        assert re.search(r"^\s*fastpath-release-sums-sha256\s*=\s*local\.fastpath_release_sums_sha256$", block, re.M)
        assert re.search(r"precondition \{\n\s*condition\s*=\s*local\.fastpath_release_digests_ok\n", block)
        # Neither script reaches a running instance by a plan: a change to it
        # would replace the instance, which infra-apply refuses.
        assert "ignore_changes = [metadata_startup_script]" in block
    script = _read(SCRIPT)
    assert '"$(attribute fastpath-release-sha256)"' in script
    assert '"$(attribute fastpath-release-sums-sha256)"' in script
    assert '"$(attribute fastpath-release-sums-sha256 || true)"' in loadgen


def test_the_bucket_keeps_earlier_generations_and_every_live_release() -> None:
    """Versioning keeps what an overwrite or a delete replaced, and no rule
    deletes a live object: a release any pin may name stays readable."""
    bucket = _block(_read(TF), "google_storage_bucket", "fastpath_releases")
    assert re.search(r"versioning \{\n\s*enabled = true\n\s*\}", bucket)
    rules = re.findall(r"lifecycle_rule \{\n(.*?)\n  \}", bucket, re.S)
    assert rules
    for rule in rules:
        assert re.search(r'with_state\s*=\s*"ARCHIVED"', rule), rule
        days = re.search(r"days_since_noncurrent_time = (\d+)", rule)
        assert days and int(days.group(1)) >= 90, rule
    assert "retention_policy" not in bucket


# --- The node's install block (infra/fastpath_node_startup.sh.tftpl) ---------

_GCLOUD_DOWNLOAD = (
    'gcloud() {\n'
    '  [ "$1 $2" = "storage cp" ] || return 2\n'
    '  printf "%s\\n" "$3" >> "$CALLS"\n'
    '  local object="$BUCKET_DIR/${3#gs://}"\n'
    '  [ -f "$object" ] || { echo "no such object $3" >&2; return 1; }\n'
    '  cp "$object" "$4"\n'
    '}\n'
    "systemd-cat() { cat >&2; }\n"
)


def _install_block() -> str:
    """The node script's install block, rendered as Terraform renders it:
    from the install directory to the release file it writes."""
    script = _read(SCRIPT)
    start = script.index("install -d /opt/fastpath\n")
    end = script.index('echo "$release" > /opt/fastpath/release\n', start)
    block = script[start:end + len('echo "$release" > /opt/fastpath/release\n')]
    # The block's only interpolation is the bucket's name.
    assert set(re.findall(r"(?<!\$)\$\{(\w+)\}", block)) == {"release_bucket"}
    assert "%{" not in block.replace("%%{", "")
    return block.replace("${release_bucket}", "bucket").replace("$${", "${").replace("%%{", "%{")


class Node:
    def __init__(self, tmp: Path, files: dict[str, bytes], sums: bytes | None) -> None:
        self.tmp = tmp
        self.bucket = tmp / "bucket"
        self.opt = tmp / "opt"
        prefix = self.bucket / "bucket" / "fastpath" / RELEASE
        prefix.mkdir(parents=True)
        for name, data in files.items():
            (prefix / name).write_bytes(data)
        if sums is not None:
            (prefix / "SHA256SUMS").write_bytes(sums)
        self.calls = tmp / "calls"

    def preinstall(self) -> dict[str, bytes]:
        """A rebooted node's earlier install, which a refusal must leave."""
        self.opt.mkdir(parents=True, exist_ok=True)
        old = {name: f"old {name}\n".encode() for name in ("fastpath", "fastpathctl", "loadgen", "release")}
        for name, data in old.items():
            (self.opt / name).write_bytes(data)
        return old

    def installed(self) -> dict[str, bytes]:
        if not self.opt.exists():
            return {}
        return {p.name: p.read_bytes() for p in self.opt.iterdir() if p.is_file()}

    def run(self, release: str = RELEASE, **attributes: str) -> subprocess.CompletedProcess[str]:
        block = _install_block().replace("/opt/fastpath", str(self.opt))
        stub = (
            'attribute() { local name="A_${1//-/_}"; printf "%s" "${!name:-}"; }\n'
            + _GCLOUD_DOWNLOAD
            + f'release="{release}"\n'
        )
        env = _env(self.tmp, BUCKET_DIR=str(self.bucket), CALLS=str(self.calls),
                   **{"A_" + name.replace("-", "_"): value for name, value in attributes.items()})
        return subprocess.run(  # noqa: S603 - bash on the repository's own script
            [_bash(), "-c", "set -euo pipefail\n" + stub + block],
            capture_output=True, text=True, env=env, check=False,
        )


def _pins(files: dict[str, bytes], sums: bytes) -> dict[str, str]:
    return {"fastpath-release-sha256": _sha(files["fastpath"]), "fastpath-release-sums-sha256": _sha(sums)}


def _refused(ran: subprocess.CompletedProcess[str], why: str, release: str = RELEASE) -> None:
    assert ran.returncode == 1, (ran.stdout, ran.stderr)
    assert f"fastpath: release {release} failed verification: {why}" in ran.stderr, ran.stderr
    assert "; nothing installed, the unit not written or started" in ran.stderr, ran.stderr


def test_the_node_installs_a_release_that_matches_its_sums_and_pins(tmp_path: Path) -> None:
    files = _release()
    sums = _sums(files)
    node = Node(tmp_path, files, sums)
    ran = node.run(**_pins(files, sums))
    assert ran.returncode == 0, ran.stderr
    installed = node.installed()
    assert installed == {**files, "release": f"{RELEASE}\n".encode()}
    for name in FILES:
        assert (node.opt / name).stat().st_mode & 0o777 == 0o755, name
    # SHA256SUMS is read first, then each file; nothing is left staged.
    calls = node.calls.read_text().split()
    assert calls[0] == f"gs://bucket/fastpath/{RELEASE}/SHA256SUMS"
    assert calls[1:] == [f"gs://bucket/fastpath/{RELEASE}/{name}" for name in FILES]
    assert not list(node.opt.glob(".stage.*"))


def test_the_node_refuses_a_binary_its_sums_do_not_name(tmp_path: Path) -> None:
    """The bucket's binary overwritten, its SHA256SUMS not: refused, and a
    rebooted node's earlier install is left as it was."""
    files = _release()
    sums = _sums(files)
    node = Node(tmp_path, {**files, "fastpath": b"overwritten\n"}, sums)
    old = node.preinstall()
    ran = node.run(**_pins(files, sums))
    _refused(ran, f"fastpath has sha256 {_sha(b'overwritten' + bytes([10]))}, not SHA256SUMS's {_sha(files['fastpath'])}")
    assert node.installed() == old
    assert not list(node.opt.glob(".stage.*"))


def test_the_node_refuses_a_release_overwritten_with_new_sums(tmp_path: Path) -> None:
    """The release prefix overwritten whole, binary and a SHA256SUMS naming
    it: the pinned digest of SHA256SUMS refuses it."""
    files = _release()
    sums = _sums(files)
    forged = _release(fastpath=b"overwritten\n")
    node = Node(tmp_path, forged, _sums(forged))
    old = node.preinstall()
    ran = node.run(**_pins(files, sums))
    _refused(ran, f"its SHA256SUMS has sha256 {_sha(_sums(forged))}, not the pinned {_sha(sums)}")
    assert node.installed() == old


def test_the_node_holds_the_binary_to_its_own_pin(tmp_path: Path) -> None:
    """With SHA256SUMS's pin matching, the binary's own pin still refuses a
    fastpath other than the one pinned."""
    files = _release()
    sums = _sums(files)
    node = Node(tmp_path, files, sums)
    ran = node.run(**{**_pins(files, sums), "fastpath-release-sha256": "e" * 64})
    _refused(ran, f"fastpath has sha256 {_sha(files['fastpath'])}, not the pinned {'e' * 64}")
    assert node.installed() == {}


def test_the_node_refuses_a_release_without_readable_sums(tmp_path: Path) -> None:
    files = _release()
    sums = _sums(files)
    node = Node(tmp_path, files, None)
    old = node.preinstall()
    ran = node.run(**_pins(files, sums))
    _refused(ran, "its SHA256SUMS could not be read")
    assert node.installed() == old
    # Only SHA256SUMS was asked for.
    assert node.calls.read_text().split() == [f"gs://bucket/fastpath/{RELEASE}/SHA256SUMS"]


def test_the_node_refuses_a_listed_file_it_cannot_read_or_that_differs(tmp_path: Path) -> None:
    """A tool SHA256SUMS lists is checked like the binaries: missing or
    different, nothing is installed, the binaries included."""
    files = _release()
    sums = _sums(files)
    missing = {name: data for name, data in files.items() if name != "fastpathwatch"}
    node = Node(tmp_path / "missing", missing, sums)
    _refused(node.run(**_pins(files, sums)), "its SHA256SUMS lists fastpathwatch but fastpathwatch could not be read")
    assert node.installed() == {}
    node = Node(tmp_path / "differs", {**files, "loadgen": b"other\n"}, sums)
    _refused(node.run(**_pins(files, sums)), f"loadgen has sha256 {_sha(b'other' + bytes([10]))}")
    assert node.installed() == {}


def test_the_node_runs_a_release_without_the_tools_its_sums_do_not_list(tmp_path: Path) -> None:
    """A release from before P1 lists neither tool: the binaries install and
    the journal says the tools are not; the binaries themselves are needed."""
    files = {name: data for name, data in _release().items() if name not in TOOLS}
    sums = _sums(files)
    node = Node(tmp_path / "early", files, sums)
    ran = node.run(**_pins(files, sums))
    assert ran.returncode == 0, ran.stderr
    assert node.installed() == {**files, "release": f"{RELEASE}\n".encode()}
    for tool in TOOLS:
        assert f"release {RELEASE} carries no {tool}; P1's tools are not installed" in ran.stderr
    no_ctl = {name: data for name, data in _release().items() if name != "fastpathctl"}
    node = Node(tmp_path / "noctl", no_ctl, _sums(no_ctl))
    _refused(node.run(**_pins(no_ctl, _sums(no_ctl))), "its SHA256SUMS does not list fastpathctl")


def test_the_node_refuses_a_listing_that_is_not_one_digest(tmp_path: Path) -> None:
    files = _release()
    sums = _sums(files) + f"{'0' * 64}  fastpath\n".encode()
    node = Node(tmp_path, files, sums)
    _refused(node.run(**_pins(files, sums)), "its SHA256SUMS does not list one sha256 digest for fastpath")
    assert node.installed() == {}


def test_the_node_refuses_absent_or_malformed_pins(tmp_path: Path) -> None:
    files = _release()
    sums = _sums(files)
    pins = _pins(files, sums)
    node = Node(tmp_path, files, sums)
    for attribute in pins:
        for value in ("", "e" * 63, "E" * 64, "e" * 64 + " -roles auditor"):
            ran = node.run(**{**pins, attribute: value})
            _refused(ran, f"{attribute} is not a pinned sha256 digest")
    for release in ("", "0123456", RELEASE.upper(), RELEASE + "/../x"):
        _refused(node.run(release=release, **pins), "fastpath-release is not a 40-character commit", release)
    assert node.installed() == {}
    assert not node.calls.exists()


# --- The load generator's script (infra/fastpath.tf) -------------------------


def _loadgen_script() -> str:
    loadgen = _block(_read(TF), "google_compute_instance", "fastpath_loadgen")
    body = re.search(r"metadata_startup_script = <<-EOT\n(.*?)\n\s*EOT\n", loadgen, re.S)
    assert body
    script = textwrap.dedent(body.group(1))
    assert set(re.findall(r"(?<!\$)\$\{([\w.]+)\}", script)) == {"google_storage_bucket.fastpath_releases.name"}
    return script.replace("${google_storage_bucket.fastpath_releases.name}", "bucket").replace("$${", "${")


def _run_loadgen(tmp: Path, files: dict[str, bytes], sums: bytes | None, **attributes: str) -> tuple[
        subprocess.CompletedProcess[str], Path]:
    node = Node(tmp, files, sums)
    curl = (
        'curl() { local url="${*: -1}" name; name="A_${url##*/}"; name="${name//-/_}"; '
        '[ -n "${!name+x}" ] || return 22; printf "%s" "${!name}"; }\n'
    )
    script = _loadgen_script().replace("/opt/fastpath", str(node.opt))
    script = script.replace("#!/bin/bash\n", "#!/bin/bash\n" + curl + _GCLOUD_DOWNLOAD, 1)
    env = _env(tmp, BUCKET_DIR=str(node.bucket), CALLS=str(node.calls),
               **{"A_" + name.replace("-", "_"): value for name, value in attributes.items()})
    ran = subprocess.run(  # noqa: S603 - bash on the repository's own script
        [_bash(), "-c", script], capture_output=True, text=True, env=env, check=False,
    )
    return ran, node.opt


def test_the_load_generator_installs_only_verified_tools(tmp_path: Path) -> None:
    files = _release()
    sums = _sums(files)
    given = {"fastpath-release": RELEASE, "fastpath-release-sums-sha256": _sha(sums)}
    ran, opt = _run_loadgen(tmp_path / "ok", files, sums, **given)
    assert ran.returncode == 0, ran.stderr
    assert {p.name: p.read_bytes() for p in opt.iterdir() if p.is_file()} == {
        **{tool: files[tool] for tool in TOOLS}, "release": f"{RELEASE}\n".encode()}
    refusals = [
        ("differs", {**files, "loadgen": b"other\n"}, sums, given, "loadgen has sha256"),
        ("forged", _release(loadgen=b"other\n"), _sums(_release(loadgen=b"other\n")), given,
         "its SHA256SUMS has sha256"),
        ("nosums", files, None, given, "its SHA256SUMS could not be read"),
        ("unpinned", files, sums, {"fastpath-release": RELEASE},
         "fastpath-release-sums-sha256 is not a pinned sha256 digest"),
        ("short", files, sums, {**given, "fastpath-release-sums-sha256": "e" * 63},
         "fastpath-release-sums-sha256 is not a pinned sha256 digest"),
    ]
    for name, bucket, listed, attributes, why in refusals:
        ran, opt = _run_loadgen(tmp_path / name, bucket, listed, **attributes)
        assert ran.returncode == 1, (name, ran.stderr)
        assert "fastpath-loadgen: release" in ran.stderr and "nothing installed" in ran.stderr, (name, ran.stderr)
        assert why in ran.stderr, (name, ran.stderr)
        assert not opt.exists() or not [p for p in opt.iterdir() if p.is_file()], name


# --- The release workflow (.github/workflows/fastpath-release.yml) ------------


def _steps() -> dict[str, dict[str, object]]:
    workflow = yaml.safe_load(_read(WORKFLOW))
    return {step.get("name", step.get("uses", "")): step for step in workflow["jobs"]["release"]["steps"]}


def test_the_workflow_builds_outside_the_checkout_and_checks_each_binary() -> None:
    build = str(_steps()["Build the node's binaries"]["run"])
    assert "../release" not in _read(WORKFLOW)
    assert 'out="$RUNNER_TEMP/release"' in build
    builds = re.findall(r'go build -trimpath -ldflags="-s -w" -o "\$out/(\w+)" \./cmd/(\w+)', build)
    assert builds == [(name, name) for name in ("fastpath", "fastpathctl", "loadgen", "fastpathwatch")]
    assert "vcs.modified=false" in build and "vcs.revision=%s" in build


def _run_vcs_check(tmp: Path, infos: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """The build step's check of each binary's build information, with go
    version -m answering from the given text."""
    build = str(_steps()["Build the node's binaries"]["run"])
    check = build[build.index("for binary in fastpath fastpathctl loadgen fastpathwatch; do"):]
    (tmp / "info").mkdir(parents=True)
    for name, info in infos.items():
        (tmp / "info" / name).write_text(info)
    stub = 'go() { [ "$1 $2" = "version -m" ] || return 2; cat "$INFO/${3##*/}"; }\nout=/nonexistent\n'
    return subprocess.run(  # noqa: S603 - bash on the repository's own workflow step
        [_bash(), "-c", "set -eo pipefail\n" + stub + check], capture_output=True, text=True,
        env=_env(tmp, INFO=str(tmp / "info"), COMMIT=RELEASE), check=False,
    )


def _info(name: str, revision: str = RELEASE, modified: str = "false") -> str:
    return (f"/x/{name}: go1.26.10\n\tpath\texample/cmd/{name}\n\tbuild\t-trimpath=true\n\tbuild\tvcs=git\n"
            f"\tbuild\tvcs.revision={revision}\n\tbuild\tvcs.time=2026-11-16T00:00:00Z\n\tbuild\tvcs.modified={modified}\n")


def test_a_binary_built_from_a_modified_tree_is_not_released(tmp_path: Path) -> None:
    names = ("fastpath", "fastpathctl", "loadgen", "fastpathwatch")
    clean = _run_vcs_check(tmp_path / "clean", {name: _info(name) for name in names})
    assert clean.returncode == 0, clean.stdout + clean.stderr
    # The control: what fastpathctl reported when it was built into the
    # checkout after fastpath was written there, and a binary of another
    # commit, each stop the step.
    for case, infos in (
        ("modified", {**{name: _info(name) for name in names}, "fastpathctl": _info("fastpathctl", modified="true")}),
        ("revision", {**{name: _info(name) for name in names}, "loadgen": _info("loadgen", revision="f" * 40)}),
    ):
        ran = _run_vcs_check(tmp_path / case, infos)
        assert ran.returncode == 1, (case, ran.stdout)
        assert f"does not report vcs.revision={RELEASE} with vcs.modified=false" in ran.stdout, (case, ran.stdout)


class Upload:
    """The upload step, run with a gcloud that keeps the bucket in a directory
    and honours --if-generation-match=0 as Cloud Storage does."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bucket = tmp / "bucket"
        self.prefix = self.bucket / "tr-fastpath-releases-quill-cloud-proxy" / "fastpath" / RELEASE
        self.release = tmp / "runner" / "release"
        self.release.mkdir(parents=True)
        self.uploads = tmp / "uploads"
        self.files = _release()
        for name, data in self.files.items():
            (self.release / name).write_bytes(data)
        (self.release / "SHA256SUMS").write_bytes(_sums(self.files))

    def publish(self, name: str, data: bytes) -> None:
        self.prefix.mkdir(parents=True, exist_ok=True)
        (self.prefix / name).write_bytes(data)

    def published(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self.prefix.iterdir()} if self.prefix.exists() else {}

    def uploaded(self) -> list[str]:
        return self.uploads.read_text().split() if self.uploads.exists() else []

    def run(self, hide_existing: bool = False) -> subprocess.CompletedProcess[str]:
        """hide_existing: the existence check misses every object, as a run
        racing another for the same commit would, so only the upload's
        precondition stands between the run and an overwrite."""
        stub = (
            'gcloud() {\n'
            '  [ "$1 $2" = "storage cp" ] || return 2\n'
            '  shift 2\n'
            '  local guarded=""\n'
            '  if [ "$1" = "--if-generation-match=0" ]; then guarded=1; shift; fi\n'
            '  case "$1" in\n'
            '    gs://*)\n'
            '      [ -z "$HIDE" ] && [ -f "$BUCKET_DIR/${1#gs://}" ] || return 1\n'
            '      cp "$BUCKET_DIR/${1#gs://}" "$2" ;;\n'
            '    *)\n'
            '      [ -n "$guarded" ] || { echo "an upload without a precondition" >&2; return 3; }\n'
            '      local object="$BUCKET_DIR/${2#gs://}"\n'
            '      [ ! -e "$object" ] || { echo "412 Precondition Failed: $2" >&2; return 1; }\n'
            '      mkdir -p "$(dirname "$object")"\n'
            '      cp "$1" "$object"\n'
            '      printf "%s\\n" "${2##*/}" >> "$UPLOADS" ;;\n'
            '  esac\n'
            '}\n'
        )
        run = str(_steps()["Upload the release"]["run"])
        env = _env(self.tmp, BUCKET_DIR=str(self.bucket), UPLOADS=str(self.uploads), HIDE="1" if hide_existing else "",
                   RUNNER_TEMP=str(self.tmp / "runner"), BUCKET="tr-fastpath-releases-quill-cloud-proxy",
                   COMMIT=RELEASE, GITHUB_STEP_SUMMARY=str(self.tmp / "summary"))
        return subprocess.run(  # noqa: S603 - bash on the repository's own workflow step
            [_bash(), "-c", stub + run], capture_output=True, text=True, env=env, check=False,
        )


def test_the_workflow_publishes_a_new_release_with_its_sums_last(tmp_path: Path) -> None:
    upload = Upload(tmp_path)
    ran = upload.run()
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert upload.uploaded() == [*FILES, "SHA256SUMS"]
    assert upload.published() == {**upload.files, "SHA256SUMS": _sums(upload.files)}
    assert f"published fastpath/{RELEASE}" in (tmp_path / "summary").read_text()


def test_the_workflow_leaves_an_identical_release_alone_and_completes_a_partial_one(tmp_path: Path) -> None:
    again = Upload(tmp_path / "again")
    assert again.run().returncode == 0
    again.uploads.unlink()
    ran = again.run()
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert again.uploaded() == []
    assert ran.stdout.count("byte-identical; left alone") == 6
    assert again.published() == {**again.files, "SHA256SUMS": _sums(again.files)}
    partial = Upload(tmp_path / "partial")
    partial.publish("fastpath", partial.files["fastpath"])
    ran = partial.run()
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert partial.uploaded() == [*FILES[1:], "SHA256SUMS"]


def test_the_workflow_never_overwrites_a_published_object(tmp_path: Path) -> None:
    """A published object that differs fails the run and stays as it was,
    whether the existence check sees it or a race hides it from the check
    (the upload's precondition refuses it then)."""
    for hide in (False, True):
        upload = Upload(tmp_path / f"hide-{hide}")
        upload.publish("fastpathctl", b"published earlier\n")
        ran = upload.run(hide_existing=hide)
        assert ran.returncode == 1, (hide, ran.stdout)
        if hide:
            assert "412 Precondition Failed" in ran.stderr, ran.stderr
        else:
            # The comparison stops the run itself, before any upload is tried.
            assert f"fastpath/{RELEASE}/fastpathctl is already published and differs from this build" in ran.stdout
            assert "412" not in ran.stderr, ran.stderr
        assert (upload.prefix / "fastpathctl").read_bytes() == b"published earlier\n"
        # SHA256SUMS, last, was not published: the nodes refuse the release.
        assert "SHA256SUMS" not in upload.published()
        assert "fastpathctl" not in upload.uploaded()


def test_the_workflow_refuses_a_build_its_own_sums_do_not_match(tmp_path: Path) -> None:
    upload = Upload(tmp_path)
    (upload.release / "loadgen").write_bytes(b"changed after the sums\n")
    ran = upload.run()
    assert ran.returncode == 1
    assert upload.uploaded() == [] and upload.published() == {}


# --- The pin process (docs/runbooks/fastpath-deploy.md) -----------------------


def test_the_runbook_says_how_to_fill_the_digests() -> None:
    runbook = _read(RUNBOOK)
    section = runbook[runbook.index("## Pinning a release"):]
    section = section[:section.index("\n## ")]
    for phrase in ("fastpath_release_digests", "SHA256SUMS", "sha256sums", "fastpath-release-sha256",
                   "fastpath-release-sums-sha256", "gcloud storage cat"):
        assert phrase in section, phrase
    assert "gs://tr-fastpath-releases-quill-cloud-proxy/fastpath/COMMIT/SHA256SUMS" in section
    # The pin process in P0's runbook points at it.
    assert "Pinning a release" in _read(ROOT / "docs" / "runbooks" / "fastpath-p0.md")


def test_the_tests_sums_are_what_sha256sum_writes(tmp_path: Path) -> None:
    """The control for every case above: the SHA256SUMS these tests write is
    byte for byte what the workflow's sha256sum writes."""
    files = _release()
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    written = subprocess.run(  # noqa: S603 - sha256sum on files written here
        [_bash(), "-c", "sha256sum fastpath fastpathctl loadgen fastpathwatch load-mix.json"],
        cwd=tmp_path, capture_output=True, check=True, env=_env(tmp_path),
    ).stdout
    assert written == _sums(files)
