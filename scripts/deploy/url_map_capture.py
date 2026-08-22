#!/usr/bin/env python3
"""Create and verify an atomic, stale-safe URL-map rollback capture."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

_OUTPUT_ONLY_KEYS = {
    "creationTimestamp",
    "fingerprint",
    "id",
    "kind",
    "selfLink",
}


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _strip_output_only(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_output_only(item)
            for key, item in value.items()
            if key not in _OUTPUT_ONLY_KEYS
        }
    if isinstance(value, list):
        return [_strip_output_only(item) for item in value]
    return value


def _content_digest(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        _strip_output_only(document),
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _fingerprint(document: dict[str, Any], label: str) -> str:
    fingerprint = document.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ValueError(f"{label} has no URL-map fingerprint")
    return fingerprint


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _load_capture(path: Path) -> tuple[dict[str, Any], bytes]:
    capture = _json_object(path.read_bytes(), "rollback capture")
    required_strings = (
        "captured_at",
        "expected_live_content_sha256",
        "source_fingerprint",
        "source_json_base64",
        "source_sha256",
        "url_map_name",
    )
    for key in required_strings:
        if not isinstance(capture.get(key), str) or not capture[key]:
            raise ValueError(f"rollback capture has no {key}")
    if capture.get("version") != 1:
        raise ValueError("rollback capture has an unsupported version")
    try:
        source = base64.b64decode(capture["source_json_base64"], validate=True)
    except ValueError as exc:
        raise ValueError("rollback capture source is not valid base64") from exc
    if hashlib.sha256(source).hexdigest() != capture["source_sha256"]:
        raise ValueError("rollback capture source digest is corrupted")
    source_document = _json_object(source, "captured URL map")
    if source_document.get("name") != capture["url_map_name"]:
        raise ValueError("captured URL-map name does not match its manifest")
    if _fingerprint(source_document, "captured URL map") != capture["source_fingerprint"]:
        raise ValueError("captured URL-map fingerprint does not match its manifest")
    return capture, source


def _read_map(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    return _json_object(raw, label), raw


def prepare(capture_path: Path, live_path: Path, candidate_path: Path, captured_at: str) -> None:
    live, live_raw = _read_map(live_path, "live URL map")
    candidate, _ = _read_map(candidate_path, "candidate URL map")
    name = live.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("live URL map has no name")
    if candidate.get("name") != name:
        raise ValueError("candidate URL-map name does not match the live map")
    live_fingerprint = _fingerprint(live, "live URL map")
    live_content_digest = _content_digest(live)
    candidate_content_digest = _content_digest(candidate)

    if capture_path.is_file():
        try:
            existing, _ = _load_capture(capture_path)
        except (OSError, ValueError):
            existing = {}
        if (
            existing.get("phase") == "ready"
            and existing.get("url_map_name") == name
            and existing.get("expected_live_fingerprint") == live_fingerprint
            and existing.get("expected_live_content_sha256") == live_content_digest
            and existing.get("expected_live_content_sha256")
            == candidate_content_digest
        ):
            print("preserved")
            return

    payload = {
        "captured_at": captured_at,
        "expected_live_content_sha256": candidate_content_digest,
        "expected_live_fingerprint": None,
        "phase": "prepared",
        "source_fingerprint": live_fingerprint,
        "source_json_base64": base64.b64encode(live_raw).decode("ascii"),
        "source_sha256": hashlib.sha256(live_raw).hexdigest(),
        "url_map_name": name,
        "version": 1,
    }
    _write_atomic(capture_path, payload)
    print("captured")


def arm(capture_path: Path, live_path: Path) -> None:
    capture, _ = _load_capture(capture_path)
    live, _ = _read_map(live_path, "post-import URL map")
    if live.get("name") != capture["url_map_name"]:
        raise ValueError("post-import URL-map name does not match the capture")
    if _content_digest(live) != capture["expected_live_content_sha256"]:
        raise ValueError("post-import URL map does not match the validated candidate")
    capture["expected_live_fingerprint"] = _fingerprint(live, "post-import URL map")
    capture["phase"] = "ready"
    _write_atomic(capture_path, capture)


def check_live(capture_path: Path, live_path: Path) -> None:
    capture, _ = _load_capture(capture_path)
    if capture.get("phase") != "ready":
        raise ValueError("rollback capture was not armed by a verified cutover")
    expected_fingerprint = capture.get("expected_live_fingerprint")
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise ValueError("rollback capture has no expected live fingerprint")
    live, _ = _read_map(live_path, "current live URL map")
    if live.get("name") != capture["url_map_name"]:
        raise ValueError("current URL-map name does not match the capture")
    if _fingerprint(live, "current live URL map") != expected_fingerprint:
        raise ValueError("current URL-map fingerprint changed after cutover")
    if _content_digest(live) != capture["expected_live_content_sha256"]:
        raise ValueError("current URL-map content changed after cutover")


def extract(capture_path: Path, output_path: Path) -> None:
    capture, source = _load_capture(capture_path)
    if capture.get("phase") != "ready":
        raise ValueError("rollback capture was not armed by a verified cutover")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--capture", type=Path, required=True)
    prepare_parser.add_argument("--live-map", type=Path, required=True)
    prepare_parser.add_argument("--candidate", type=Path, required=True)
    prepare_parser.add_argument("--captured-at", required=True)

    arm_parser = subparsers.add_parser("arm")
    arm_parser.add_argument("--capture", type=Path, required=True)
    arm_parser.add_argument("--live-map", type=Path, required=True)

    check_parser = subparsers.add_parser("check-live")
    check_parser.add_argument("--capture", type=Path, required=True)
    check_parser.add_argument("--live-map", type=Path, required=True)

    extract_parser = subparsers.add_parser("extract")
    extract_parser.add_argument("--capture", type=Path, required=True)
    extract_parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "prepare":
            prepare(args.capture, args.live_map, args.candidate, args.captured_at)
        elif args.command == "arm":
            arm(args.capture, args.live_map)
        elif args.command == "check-live":
            check_live(args.capture, args.live_map)
        elif args.command == "extract":
            extract(args.capture, args.output)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    main()
