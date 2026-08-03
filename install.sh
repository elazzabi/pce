#!/bin/sh

set -u

PROGRAM=pce-installer
PREFIX=
PREFIX_SUPPLIED=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [--prefix PATH]

Install PCE from this source checkout. The default prefix is $HOME/.local.
This private-preview installer performs no network access and copies no knowledge data.
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

replace_link() {
  python3 - "$1" "$2" <<'PY'
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
}

while [ "$#" -gt 0 ]; do
  case "$1" in
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

SOURCE_ROOT=$(CDPATH= cd "$(dirname "$0")" && pwd -P) ||
  fail "could not resolve the source checkout"
PAYLOAD_FILES='SKILL.md
references/storage-model.md
scripts/pce.py
scripts/pce_ui.py'
PAYLOAD_DIRECTORIES='references scripts'
for relative in $PAYLOAD_FILES; do
  [ -f "$SOURCE_ROOT/$relative" ] ||
    fail "source checkout is incomplete: $SOURCE_ROOT/$relative"
done

PCE_VERSION=$(sed -n 's/^PCE_VERSION = "\([^"]*\)"/\1/p' "$SOURCE_ROOT/scripts/pce.py")
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
if path_exists "$MANAGED_ROOT" && [ ! -d "$MANAGED_ROOT" ]; then
  fail "refusing to overwrite unrelated target: $MANAGED_ROOT"
fi
if path_exists "$VERSION_ROOT"; then
  [ -d "$VERSION_ROOT" ] && [ ! -L "$VERSION_ROOT" ] ||
    fail "refusing to overwrite unrelated target: $VERSION_ROOT"
  expected_entries=0
  for _entry in $PAYLOAD_DIRECTORIES $PAYLOAD_FILES; do
    expected_entries=$((expected_entries + 1))
  done
  entry_count=$(find "$VERSION_ROOT" ! -path "$VERSION_ROOT" | wc -l | tr -d '[:space:]')
  [ "$entry_count" = "$expected_entries" ] ||
    fail "refusing to replace an existing PCE version that differs: $VERSION_ROOT"
  for relative in $PAYLOAD_FILES; do
    cmp -s "$SOURCE_ROOT/$relative" "$VERSION_ROOT/$relative" ||
      fail "refusing to replace an existing PCE version that differs: $VERSION_ROOT"
  done
  [ -x "$VERSION_ROOT/scripts/pce.py" ] ||
    fail "refusing to replace an existing PCE version that differs: $VERSION_ROOT"
elif path_exists "$MANAGED_ROOT"; then
  fail "refusing to adopt an existing unmanaged directory: $MANAGED_ROOT"
fi

assert_owned_link_or_absent "$CURRENT_PATH" "$CURRENT_TARGET"
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

STAGE=
CURRENT_TEMP=$MANAGED_ROOT/.current.$$
EXECUTABLE_TEMP=$PREFIX/bin/.pce.$$
SKILL_TEMP=$CODEX_ROOT/skills/.personal-compound.$$
cleanup() {
  [ -z "$STAGE" ] || rm -rf "$STAGE"
  [ ! -L "$CURRENT_TEMP" ] || rm "$CURRENT_TEMP"
  [ ! -L "$EXECUTABLE_TEMP" ] || rm "$EXECUTABLE_TEMP"
  [ ! -L "$SKILL_TEMP" ] || rm "$SKILL_TEMP"
}
trap cleanup 0
trap 'cleanup; exit 1' HUP INT TERM

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
