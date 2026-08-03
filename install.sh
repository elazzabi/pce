#!/bin/sh

set -u

PROGRAM=pce-installer
VERSION=
EXPECTED_VERSION=
PREFIX=
PREFIX_SUPPLIED=0
RELEASE_BASE_URL_EXPLICIT=${PCE_RELEASE_BASE_URL+x}
RELEASES_URL=${PCE_RELEASE_BASE_URL:-https://github.com/elazzabi/pce/releases}
GITHUB_API_URL=${PCE_GITHUB_API_URL:-https://api.github.com/repos/elazzabi/pce}
PAYLOAD_FILES='SKILL.md
references/storage-model.md
scripts/pce.py
scripts/pce_ui.py'
PAYLOAD_DIRECTORIES='references scripts'

usage() {
  cat <<'EOF'
Usage: install.sh [--version X.Y.Z] [--prefix PATH]

Install the latest stable PCE release, an exact stable version, or a local source checkout.
The default prefix is $HOME/.local. No knowledge data is copied or changed.
EOF
}

fail() {
  printf '%s: %s\n' "$PROGRAM" "$1" >&2
  exit 1
}

path_exists() {
  [ -e "$1" ] || [ -L "$1" ]
}

absolute_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$PWD" "$1" ;;
  esac
}

assert_owned_link_or_absent() {
  if ! path_exists "$1"; then
    return
  fi
  if [ ! -L "$1" ] || [ "$(readlink "$1")" != "$2" ]; then
    fail "refusing to overwrite unrelated target: $1"
  fi
}

validate_version_tree() {
  version_root=$1
  expected_version=$2
  compare_with_source=$3
  mismatch_message="refusing to replace an existing PCE version that differs: $version_root"

  [ -d "$version_root" ] && [ ! -L "$version_root" ] || fail "$mismatch_message"
  for relative in $PAYLOAD_DIRECTORIES; do
    [ -d "$version_root/$relative" ] && [ ! -L "$version_root/$relative" ] ||
      fail "$mismatch_message"
  done
  for relative in $PAYLOAD_FILES; do
    [ -f "$version_root/$relative" ] && [ ! -L "$version_root/$relative" ] ||
      fail "$mismatch_message"
  done

  unexpected_entry=$(find "$version_root" \
    ! -path "$version_root" \
    ! -path "$version_root/SKILL.md" \
    ! -path "$version_root/references" \
    ! -path "$version_root/references/storage-model.md" \
    ! -path "$version_root/scripts" \
    ! -path "$version_root/scripts/pce.py" \
    ! -path "$version_root/scripts/pce_ui.py" \
    ! -path "$version_root/scripts/__pycache__" \
    ! -path "$version_root/scripts/__pycache__/*" \
    -print -quit) || fail "$mismatch_message"
  [ -z "$unexpected_entry" ] || fail "$mismatch_message"

  bytecode_root=$version_root/scripts/__pycache__
  if path_exists "$bytecode_root"; then
    [ -d "$bytecode_root" ] && [ ! -L "$bytecode_root" ] || fail "$mismatch_message"
    unexpected_bytecode=$(find "$bytecode_root" \
      ! -path "$bytecode_root" \
      \( ! -type f -o ! -name '*.pyc' \) \
      -print -quit) || fail "$mismatch_message"
    [ -z "$unexpected_bytecode" ] || fail "$mismatch_message"
  fi

  installed_version=$(sed -n 's/^PCE_VERSION = "\([^"]*\)"/\1/p' "$version_root/scripts/pce.py")
  [ "$installed_version" = "$expected_version" ] || fail "$mismatch_message"
  [ -x "$version_root/scripts/pce.py" ] || fail "$mismatch_message"
  if [ "$compare_with_source" -eq 1 ]; then
    for relative in $PAYLOAD_FILES; do
      cmp -s "$SOURCE_ROOT/$relative" "$version_root/$relative" || fail "$mismatch_message"
    done
  fi
}

validate_managed_upgrade_root() {
  [ -d "$VERSIONS_ROOT" ] && [ ! -L "$VERSIONS_ROOT" ] ||
    fail "refusing to adopt an existing unmanaged directory: $MANAGED_ROOT"
  [ -L "$CURRENT_PATH" ] ||
    fail "refusing to adopt an existing unmanaged directory: $MANAGED_ROOT"

  previous_target=$(readlink "$CURRENT_PATH")
  case "$previous_target" in
    versions/*) previous_version=${previous_target#versions/} ;;
    *) fail "refusing to overwrite unrelated target: $CURRENT_PATH" ;;
  esac
  case "$previous_version" in
    ''|*[!0-9.]*|*/*) fail "refusing to overwrite unrelated target: $CURRENT_PATH" ;;
  esac
  validate_version_tree "$MANAGED_ROOT/$previous_target" "$previous_version" 0

  unexpected_managed_entry=$(find "$MANAGED_ROOT" \
    ! -path "$MANAGED_ROOT" \
    ! -path "$CURRENT_PATH" \
    ! -path "$VERSIONS_ROOT" \
    ! -path "$VERSIONS_ROOT/*" \
    -print -quit) ||
    fail "refusing to adopt an existing unmanaged directory: $MANAGED_ROOT"
  [ -z "$unexpected_managed_entry" ] ||
    fail "refusing to adopt an existing unmanaged directory: $MANAGED_ROOT"
}

replace_link() {
  python3 - "$1" "$2" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
}

download_file() {
  destination=$1
  url=$2
  accept=${3:-}
  set -- -fL --retry 2 --connect-timeout 10 --max-time 300 \
    --speed-limit 1024 --speed-time 30 -o "$destination"
  if [ -n "$GITHUB_AUTH_HEADER_FILE" ]; then
    set -- "$@" -H "@$GITHUB_AUTH_HEADER_FILE"
  fi
  if [ -n "$accept" ]; then
    set -- "$@" -H "Accept: $accept"
  fi
  curl "$@" "$url"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      [ "$#" -ge 2 ] || fail "--version requires X.Y.Z"
      VERSION=$2
      shift 2
      ;;
    --prefix)
      [ "$#" -ge 2 ] || fail "--prefix requires a path"
      PREFIX=$2
      PREFIX_SUPPLIED=1
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

if [ "$PREFIX_SUPPLIED" -eq 0 ]; then
  [ -n "${HOME:-}" ] || fail "HOME is not set; pass --prefix PATH"
  PREFIX=$HOME/.local
fi
[ -n "$PREFIX" ] || fail "installation prefix cannot be empty"
[ -n "${CODEX_HOME:-${HOME:-}}" ] ||
  fail "HOME is not set; set CODEX_HOME or HOME for the skill link"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

TEMPORARY_ROOT=
STAGE=
CURRENT_TEMP=
EXECUTABLE_TEMP=
SKILL_TEMP=
cleanup() {
  [ -z "$TEMPORARY_ROOT" ] || rm -rf "$TEMPORARY_ROOT"
  [ -z "$STAGE" ] || rm -rf "$STAGE"
  [ -z "$CURRENT_TEMP" ] || [ ! -L "$CURRENT_TEMP" ] || rm "$CURRENT_TEMP"
  [ -z "$EXECUTABLE_TEMP" ] || [ ! -L "$EXECUTABLE_TEMP" ] || rm "$EXECUTABLE_TEMP"
  [ -z "$SKILL_TEMP" ] || [ ! -L "$SKILL_TEMP" ] || rm "$SKILL_TEMP"
}
trap cleanup 0
trap 'cleanup; exit 1' HUP INT TERM

SOURCE_ROOT=
if [ -z "$VERSION" ]; then
  case "$0" in
    install.sh|*/install.sh)
      source_candidate=$(CDPATH= cd "$(dirname "$0")" && pwd -P) ||
        fail "could not resolve the source checkout"
      if [ -f "$source_candidate/scripts/pce.py" ]; then
        SOURCE_ROOT=$source_candidate
      fi
      ;;
  esac
fi

if [ -z "$SOURCE_ROOT" ]; then
  if [ -n "$VERSION" ]; then
    case "$VERSION" in
      *[!0-9.]*|*.*.*.*|.*|*.) fail "expected an exact stable version such as 1.2.3" ;;
    esac
    old_ifs=$IFS
    IFS=.
    set -- $VERSION
    IFS=$old_ifs
    [ "$#" -eq 3 ] && [ -n "$1" ] && [ -n "$2" ] && [ -n "$3" ] ||
      fail "expected an exact stable version such as 1.2.3"
    ASSET_BASE=$RELEASES_URL/download/v$VERSION
  else
    ASSET_BASE=$RELEASES_URL/latest/download
  fi
  command -v curl >/dev/null 2>&1 || fail "curl is required"
  TEMPORARY_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pce-install.XXXXXX") ||
    fail "could not create a temporary directory"
  GITHUB_AUTH_TOKEN=
  GITHUB_AUTH_HEADER_FILE=
  if [ -z "$RELEASE_BASE_URL_EXPLICIT" ]; then
    GITHUB_AUTH_TOKEN=${PCE_GITHUB_TOKEN:-${GH_TOKEN:-${GITHUB_TOKEN:-}}}
    if [ -z "$GITHUB_AUTH_TOKEN" ] && [ "${PCE_USE_GH_AUTH:-0}" = 1 ]; then
      command -v gh >/dev/null 2>&1 ||
        fail "gh is required when PCE_USE_GH_AUTH=1"
      GITHUB_AUTH_TOKEN=$(gh auth token) ||
        fail "could not read a GitHub token from gh"
      [ -n "$GITHUB_AUTH_TOKEN" ] ||
        fail "gh returned an empty GitHub token"
    fi
    if [ -n "$GITHUB_AUTH_TOKEN" ]; then
      GITHUB_AUTH_HEADER_FILE=$TEMPORARY_ROOT/github-authorization-header
      (umask 077 && printf 'Authorization: Bearer %s\n' "$GITHUB_AUTH_TOKEN" > "$GITHUB_AUTH_HEADER_FILE") ||
        fail "could not prepare GitHub authentication"
      chmod 600 "$GITHUB_AUTH_HEADER_FILE" ||
        fail "could not protect GitHub authentication"
      unset PCE_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN
    fi
  else
    unset PCE_GITHUB_TOKEN GH_TOKEN GITHUB_TOKEN
  fi
  MANIFEST_PATH=$TEMPORARY_ROOT/release-manifest.json
  RELEASE_METADATA=
  if [ -n "$GITHUB_AUTH_TOKEN" ]; then
    RELEASE_METADATA=$TEMPORARY_ROOT/github-release.json
    if [ -n "$VERSION" ]; then
      RELEASE_API_ENDPOINT=$GITHUB_API_URL/releases/tags/v$VERSION
    else
      RELEASE_API_ENDPOINT=$GITHUB_API_URL/releases/latest
    fi
    download_file "$RELEASE_METADATA" "$RELEASE_API_ENDPOINT" "application/vnd.github+json" ||
      fail "could not find the selected GitHub Release"
    ASSET_SELECTION=$(python3 - "$RELEASE_METADATA" <<'PY'
import json
import re
import sys

release = json.load(open(sys.argv[1], encoding="utf-8"))
assets = release.get("assets", [])
manifests = [asset for asset in assets if asset.get("name") == "release-manifest.json"]
archives = [asset for asset in assets if re.fullmatch(r"pce-v\d+\.\d+\.\d+\.tar\.gz", str(asset.get("name")))]
if len(manifests) != 1 or len(archives) != 1:
    raise SystemExit(1)
values = (manifests[0].get("url"), archives[0].get("name"), archives[0].get("url"))
if any(not isinstance(value, str) or any(character.isspace() for character in value) for value in values):
    raise SystemExit(1)
print(*values)
PY
    ) || fail "GitHub Release does not contain exactly one manifest and PCE archive"
    set -- $ASSET_SELECTION
    [ "$#" -eq 3 ] || fail "GitHub Release asset selection returned invalid data"
    MANIFEST_URL=$1
    METADATA_ARTIFACT_FILENAME=$2
    ARTIFACT_URL=$3
    download_file "$MANIFEST_PATH" "$MANIFEST_URL" "application/octet-stream" ||
      fail "could not download the release manifest"
  else
    download_file "$MANIFEST_PATH" "$ASSET_BASE/release-manifest.json" ||
      fail "could not download release manifest from $ASSET_BASE"
  fi

  SELECTION=$(python3 - "$MANIFEST_PATH" "$VERSION" <<'PY'
import json
import re
import sys

try:
    manifest = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit("release manifest is not valid JSON")
version = manifest.get("version")
revision = manifest.get("sourceRevision")
artifact = manifest.get("artifact")
requested = sys.argv[2]
if manifest.get("schemaVersion") != 1 or not isinstance(artifact, dict):
    raise SystemExit("release manifest metadata is invalid")
if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
    raise SystemExit("release manifest version is invalid")
if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise SystemExit("release manifest revision is invalid")
if requested and requested != version:
    raise SystemExit(f"requested {requested}, but manifest describes {version}")
filename = artifact.get("filename")
digest = artifact.get("sha256")
size = artifact.get("size")
if filename != f"pce-v{version}.tar.gz" or not re.fullmatch(r"[A-Za-z0-9._-]+", str(filename)):
    raise SystemExit("release artifact filename is invalid")
if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit("release artifact digest is invalid")
if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
    raise SystemExit("release artifact size is invalid")
print(filename, digest, version, size)
PY
  ) || fail "release manifest is malformed"
  set -- $SELECTION
  [ "$#" -eq 4 ] || fail "release manifest selection returned invalid data"
  ARTIFACT_FILENAME=$1
  ARTIFACT_DIGEST=$2
  EXPECTED_VERSION=$3
  ARTIFACT_SIZE=$4
  ARTIFACT_PATH=$TEMPORARY_ROOT/$ARTIFACT_FILENAME
  if [ -n "$RELEASE_METADATA" ]; then
    [ "$METADATA_ARTIFACT_FILENAME" = "$ARTIFACT_FILENAME" ] ||
      fail "GitHub Release archive does not match its manifest"
    download_file "$ARTIFACT_PATH" "$ARTIFACT_URL" "application/octet-stream" ||
      fail "could not download release artifact $ARTIFACT_FILENAME"
  else
    download_file "$ARTIFACT_PATH" "$ASSET_BASE/$ARTIFACT_FILENAME" ||
      fail "could not download release artifact $ARTIFACT_FILENAME"
  fi
  INTEGRITY=$(python3 - "$ARTIFACT_PATH" <<'PY'
import hashlib
import os
import sys

path = sys.argv[1]
digest = hashlib.sha256()
with open(path, "rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
print(os.path.getsize(path), digest.hexdigest())
PY
  ) || fail "could not inspect the release artifact"
  set -- $INTEGRITY
  [ "$#" -eq 2 ] || fail "release artifact integrity check returned invalid data"
  ACTUAL_SIZE=$1
  ACTUAL_DIGEST=$2
  [ "$ACTUAL_SIZE" = "$ARTIFACT_SIZE" ] || fail "release artifact size mismatch"
  [ "$ACTUAL_DIGEST" = "$ARTIFACT_DIGEST" ] || fail "release artifact SHA-256 mismatch"
  SOURCE_ROOT=$TEMPORARY_ROOT/candidate
  mkdir "$SOURCE_ROOT" || fail "could not stage release artifact"
  if ! python3 - "$ARTIFACT_PATH" "$SOURCE_ROOT" "$PAYLOAD_FILES" <<'PY'
import os
import shutil
import sys
import tarfile
from pathlib import Path

archive_path, destination, payload = sys.argv[1:]
expected = payload.splitlines()
with tarfile.open(sys.argv[1], "r:gz") as package:
    members = package.getmembers()
    if [member.name for member in members] != expected:
        raise SystemExit(1)
    if any(not member.isfile() for member in members):
        raise SystemExit(1)
    for member in members:
        expected_mode = 0o755 if member.name == "scripts/pce.py" else 0o644
        if member.mode != expected_mode:
            raise SystemExit(1)
    for member in members:
        target = Path(destination, member.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = package.extractfile(member)
        if source is None:
            raise SystemExit(1)
        with source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        os.chmod(target, member.mode)
PY
  then
    fail "release artifact contains an unsafe payload"
  fi
else
  GITHUB_AUTH_TOKEN=
  GITHUB_AUTH_HEADER_FILE=
fi

for relative in $PAYLOAD_FILES; do
  [ -f "$SOURCE_ROOT/$relative" ] || fail "PCE package is incomplete: $relative"
done

SOURCE_VERSION=$(sed -n 's/^PCE_VERSION = "\([^"]*\)"/\1/p' "$SOURCE_ROOT/scripts/pce.py")
if [ -n "$EXPECTED_VERSION" ] && [ "$SOURCE_VERSION" != "$EXPECTED_VERSION" ]; then
  fail "release package version does not match its manifest"
fi
PCE_VERSION=$SOURCE_VERSION
case "$PCE_VERSION" in
  ''|*[!0-9.]*) fail "could not read a stable PCE_VERSION from scripts/pce.py" ;;
esac

PREFIX=$(absolute_path "$PREFIX")
CODEX_ROOT=$(absolute_path "${CODEX_HOME:-$HOME/.codex}")
MANAGED_ROOT=$PREFIX/lib/pce
VERSIONS_ROOT=$MANAGED_ROOT/versions
VERSION_ROOT=$VERSIONS_ROOT/$PCE_VERSION
CURRENT_PATH=$MANAGED_ROOT/current
EXECUTABLE_PATH=$PREFIX/bin/pce
SKILL_PATH=$CODEX_ROOT/skills/personal-compound
CURRENT_TARGET=versions/$PCE_VERSION
EXECUTABLE_TARGET=../lib/pce/current/scripts/pce.py
SKILL_TARGET=$MANAGED_ROOT/current

if path_exists "$PREFIX" && [ ! -d "$PREFIX" ]; then
  fail "installation prefix is not a directory: $PREFIX"
fi
if path_exists "$MANAGED_ROOT" && { [ ! -d "$MANAGED_ROOT" ] || [ -L "$MANAGED_ROOT" ]; }; then
  fail "refusing to overwrite unrelated target: $MANAGED_ROOT"
fi
CURRENT_VALIDATED=0
if path_exists "$VERSION_ROOT"; then
  validate_version_tree "$VERSION_ROOT" "$PCE_VERSION" 1
elif path_exists "$MANAGED_ROOT"; then
  validate_managed_upgrade_root
  CURRENT_VALIDATED=1
fi

if [ "$CURRENT_VALIDATED" -eq 0 ]; then
  assert_owned_link_or_absent "$CURRENT_PATH" "$CURRENT_TARGET"
fi
assert_owned_link_or_absent "$EXECUTABLE_PATH" "$EXECUTABLE_TARGET"
assert_owned_link_or_absent "$SKILL_PATH" "$SKILL_TARGET"

if path_exists "$VERSION_ROOT" &&
  [ -L "$CURRENT_PATH" ] &&
  [ -L "$EXECUTABLE_PATH" ] &&
  [ -L "$SKILL_PATH" ]; then
  printf 'PCE %s is already current under %s.\n' "$PCE_VERSION" "$PREFIX"
  exit 0
fi

mkdir -p "$VERSIONS_ROOT" "$PREFIX/bin" "$CODEX_ROOT/skills" ||
  fail "could not create installation directories"

CURRENT_TEMP=$MANAGED_ROOT/.current.$$
EXECUTABLE_TEMP=$PREFIX/bin/.pce.$$
SKILL_TEMP=$CODEX_ROOT/skills/.personal-compound.$$

if ! path_exists "$VERSION_ROOT"; then
  STAGE=$(mktemp -d "$VERSIONS_ROOT/.${PCE_VERSION}.XXXXXX") ||
    fail "could not create a version staging directory"
  for relative in $PAYLOAD_DIRECTORIES; do
    mkdir -p "$STAGE/$relative" ||
      fail "could not prepare the version staging directory"
  done
  for relative in $PAYLOAD_FILES; do
    cp "$SOURCE_ROOT/$relative" "$STAGE/$relative" ||
      fail "could not copy $relative"
  done
  chmod 755 "$STAGE/scripts/pce.py" || fail "could not make pce executable"
  for relative in $PAYLOAD_FILES; do
    [ "$relative" = scripts/pce.py ] || chmod 644 "$STAGE/$relative" ||
      fail "could not set program file modes"
  done
  mv "$STAGE" "$VERSION_ROOT" || fail "could not publish PCE $PCE_VERSION"
  STAGE=
fi

! path_exists "$CURRENT_TEMP" ||
  fail "temporary activation path is occupied: $CURRENT_TEMP"
! path_exists "$EXECUTABLE_TEMP" ||
  fail "temporary executable path is occupied: $EXECUTABLE_TEMP"
! path_exists "$SKILL_TEMP" ||
  fail "temporary skill path is occupied: $SKILL_TEMP"

ln -s "$CURRENT_TARGET" "$CURRENT_TEMP" || fail "could not stage current pointer"
ln -s "$EXECUTABLE_TARGET" "$EXECUTABLE_TEMP" || fail "could not stage pce executable"
ln -s "$SKILL_TARGET" "$SKILL_TEMP" || fail "could not stage Codex skill link"
replace_link "$CURRENT_TEMP" "$CURRENT_PATH" || fail "could not activate PCE $PCE_VERSION"
replace_link "$EXECUTABLE_TEMP" "$EXECUTABLE_PATH" || fail "could not activate pce command"
replace_link "$SKILL_TEMP" "$SKILL_PATH" || fail "could not activate personal-compound skill"

printf 'Installed PCE %s under %s.\n' "$PCE_VERSION" "$PREFIX"
printf 'Verify with: %s --version\n' "$EXECUTABLE_PATH"
