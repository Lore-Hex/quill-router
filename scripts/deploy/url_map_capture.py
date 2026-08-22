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

from service_surface_url_map import _strip_output_only


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key != "fingerprint"
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _content_digest(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        _strip_volatile(_strip_output_only(document)),
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
    live, _ = _read_map(live_path, "live URL map")
    candidate, _ = _read_map(candidate_path, "candidate URL map")
    name = live.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("live URL map has no name")
    if candidate.get("name") != name:
        raise ValueError("candidate URL-map name does not match the live map")
    live_fingerprint = _fingerprint(live, "live URL map")
    importable_live = _strip_output_only(live)
    live_raw = (
        json.dumps(importable_live, separators=(",", ":")) + "\n"
    ).encode()
    live_content_digest = _content_digest(live)
    candidate_content_digest = _content_digest(candidate)

    if capture_path.is_file():
        try:
            existing, existing_source = _load_capture(capture_path)
        except (OSError, ValueError) as exc:
            raise ValueError(
                "existing rollback capture is invalid; refusing to overwrite it"
            ) from exc
        if existing.get("url_map_name") != name:
            raise ValueError("existing rollback capture belongs to a different URL map")
        existing_phase = existing.get("phase")
        existing_source_document = _json_object(
            existing_source, "captured URL map"
        )
        if existing_phase in {"armed", "ready"}:
            if existing.get("source_fingerprint") != live_fingerprint:
                raise ValueError(
                    "existing armed rollback capture does not match the current live "
                    "fingerprint; run rollback before another cutover"
                )
            if _content_digest(existing_source_document) != live_content_digest:
                raise ValueError(
                    "existing armed rollback capture fingerprint matches but content differs"
                )
            if existing.get("expected_live_content_sha256") != candidate_content_digest:
                raise ValueError(
                    "existing armed rollback capture targets a different candidate"
                )
            print("preserved")
            return
        if existing_phase != "restored":
            raise ValueError("existing rollback capture has an unsupported phase")
        if _content_digest(existing_source_document) != live_content_digest:
            raise ValueError(
                "restored rollback capture does not match the current live URL map"
            )

    payload = {
        "captured_at": captured_at,
        "expected_live_content_sha256": candidate_content_digest,
        # Armed before import: from this atomic write onward the import may have
        # applied, failed, or returned an ambiguous transport result.
        "phase": "armed",
        "source_fingerprint": live_fingerprint,
        "source_json_base64": base64.b64encode(live_raw).decode("ascii"),
        "source_sha256": hashlib.sha256(live_raw).hexdigest(),
        "url_map_name": name,
        "version": 1,
    }
    _write_atomic(capture_path, payload)
    print("captured")


def verify_candidate(capture_path: Path, live_path: Path) -> None:
    capture, _ = _load_capture(capture_path)
    live, _ = _read_map(live_path, "post-import URL map")
    if live.get("name") != capture["url_map_name"]:
        raise ValueError("post-import URL-map name does not match the capture")
    if _content_digest(live) != capture["expected_live_content_sha256"]:
        raise ValueError("post-import URL map does not match the validated candidate")
    _fingerprint(live, "post-import URL map")


def check_live(capture_path: Path, live_path: Path) -> None:
    capture, source = _load_capture(capture_path)
    if capture.get("phase") not in {"armed", "ready", "restored"}:
        raise ValueError("rollback capture is not armed")
    live, _ = _read_map(live_path, "current live URL map")
    if live.get("name") != capture["url_map_name"]:
        raise ValueError("current URL-map name does not match the capture")
    _fingerprint(live, "current live URL map")
    live_digest = _content_digest(live)
    source_digest = _content_digest(_json_object(source, "captured URL map"))
    if live_digest == source_digest:
        print("source")
        return
    if live_digest == capture["expected_live_content_sha256"]:
        print("candidate")
        return
    raise ValueError(
        "current URL-map content matches neither the captured source nor candidate"
    )


def mark_restored(capture_path: Path, live_path: Path) -> None:
    capture, source = _load_capture(capture_path)
    live, _ = _read_map(live_path, "restored live URL map")
    if live.get("name") != capture["url_map_name"]:
        raise ValueError("restored URL-map name does not match the capture")
    if _content_digest(live) != _content_digest(
        _json_object(source, "captured URL map")
    ):
        raise ValueError("restored live URL map does not match the captured source")
    capture["phase"] = "restored"
    _write_atomic(capture_path, capture)


def extract(capture_path: Path, output_path: Path) -> None:
    capture, source = _load_capture(capture_path)
    if capture.get("phase") not in {"armed", "ready", "restored"}:
        raise ValueError("rollback capture is not armed")
    # Normalize again so captures produced by the blocked describe-based
    # implementation remain recoverable after this fix is deployed.
    importable_source = _strip_output_only(
        _json_object(source, "captured URL map")
    )
    source = (
        json.dumps(importable_source, separators=(",", ":")) + "\n"
    ).encode()
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

    verify_parser = subparsers.add_parser("verify-candidate")
    verify_parser.add_argument("--capture", type=Path, required=True)
    verify_parser.add_argument("--live-map", type=Path, required=True)

    check_parser = subparsers.add_parser("check-live")
    check_parser.add_argument("--capture", type=Path, required=True)
    check_parser.add_argument("--live-map", type=Path, required=True)

    restored_parser = subparsers.add_parser("mark-restored")
    restored_parser.add_argument("--capture", type=Path, required=True)
    restored_parser.add_argument("--live-map", type=Path, required=True)

    extract_parser = subparsers.add_parser("extract")
    extract_parser.add_argument("--capture", type=Path, required=True)
    extract_parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        if args.command == "prepare":
            prepare(args.capture, args.live_map, args.candidate, args.captured_at)
        elif args.command == "verify-candidate":
            verify_candidate(args.capture, args.live_map)
        elif args.command == "check-live":
            check_live(args.capture, args.live_map)
        elif args.command == "mark-restored":
            mark_restored(args.capture, args.live_map)
        elif args.command == "extract":
            extract(args.capture, args.output)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    main()
