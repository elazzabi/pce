#!/usr/bin/env python3
"""Build and verify the platform-independent PCE GitHub Release payload."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = (
    "SKILL.md",
    "references/storage-model.md",
    "scripts/pce.py",
    "scripts/pce_ui.py",
)
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def pce_version() -> str:
    source = (ROOT / "scripts" / "pce.py").read_text(encoding="utf-8")
    match = re.search(r'^PCE_VERSION = "([^"]+)"$', source, re.MULTILINE)
    if not match or not VERSION_PATTERN.fullmatch(match.group(1)):
        raise ValueError("scripts/pce.py must declare a stable PCE_VERSION")
    return match.group(1)


def validate_tag(tag: str) -> None:
    expected = f"v{pce_version()}"
    if tag != expected:
        raise ValueError(f"release tag must be {expected}; received {tag}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output: Path, source_revision: str) -> Path:
    if not REVISION_PATTERN.fullmatch(source_revision):
        raise ValueError("source revision must be a full lowercase Git commit hash")
    version = pce_version()
    output.mkdir(parents=True, exist_ok=True)
    filename = f"pce-v{version}.tar.gz"
    archive = output / filename
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as package:
                for relative in PAYLOAD:
                    source = ROOT / relative
                    info = package.gettarinfo(str(source), arcname=relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mtime = 0
                    info.mode = 0o755 if relative == "scripts/pce.py" else 0o644
                    with source.open("rb") as contents:
                        package.addfile(info, contents)
    artifact = {
        "filename": filename,
        "sha256": sha256(archive),
        "size": archive.stat().st_size,
    }
    manifest = {
        "schemaVersion": 1,
        "version": version,
        "sourceRevision": source_revision,
        "artifact": artifact,
    }
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text(
        f"{artifact['sha256']}  {filename}\n", encoding="utf-8"
    )
    return manifest_path


def verify(manifest_path: Path) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("release manifest is not valid JSON") from error
    version = manifest.get("version")
    revision = manifest.get("sourceRevision")
    artifact = manifest.get("artifact")
    if manifest.get("schemaVersion") != 1 or not isinstance(version, str):
        raise ValueError("release manifest metadata is invalid")
    if not VERSION_PATTERN.fullmatch(version) or not REVISION_PATTERN.fullmatch(str(revision)):
        raise ValueError("release manifest version or revision is invalid")
    if not isinstance(artifact, dict):
        raise ValueError("release manifest artifact is invalid")
    expected_filename = f"pce-v{version}.tar.gz"
    if artifact.get("filename") != expected_filename:
        raise ValueError("release artifact filename is invalid")
    archive = manifest_path.parent / expected_filename
    if not archive.is_file() or archive.stat().st_size != artifact.get("size"):
        raise ValueError("release artifact size mismatch")
    if sha256(archive) != artifact.get("sha256"):
        raise ValueError("release artifact SHA-256 mismatch")
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        names = [member.name for member in members]
        if names != list(PAYLOAD) or any(not member.isfile() for member in members):
            raise ValueError("release artifact contains an unexpected payload")
        for member in members:
            expected_mode = 0o755 if member.name == "scripts/pce.py" else 0o644
            if member.mode != expected_mode:
                raise ValueError(f"release artifact mode is invalid: {member.name}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    tag = commands.add_parser("validate-tag")
    tag.add_argument("--tag", required=True)
    create = commands.add_parser("build")
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--source-revision", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--manifest", required=True, type=Path)
    return root


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.command == "validate-tag":
            validate_tag(arguments.tag)
        elif arguments.command == "build":
            build(arguments.output, arguments.source_revision)
        else:
            verify(arguments.manifest)
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"pce-release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
