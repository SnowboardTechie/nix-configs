#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALLER="$SCRIPT_DIR/install-hermes-intel-desktop.sh"
TESTS=0
FAILURES=0

report() {
  TESTS=$((TESTS + 1))
  if "$@"; then
    printf 'ok %d - %s\n' "$TESTS" "$DESCRIPTION"
  else
    printf 'not ok %d - %s\n' "$TESTS" "$DESCRIPTION"
    FAILURES=$((FAILURES + 1))
  fi
}

make_fake_tool() {
  local path=$1
  shift
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' 'set -euo pipefail'
    printf '%s\n' "$@"
  } >"$path"
  chmod +x "$path"
}

setup_case() {
  CASE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/hermes-installer-test.XXXXXX")
  HOME_DIR="$CASE_ROOT/home"
  FAKE_BIN="$CASE_ROOT/bin"
  FIXTURE_APP="$CASE_ROOT/Hermes.app"
  LOG="$CASE_ROOT/commands.log"
  mkdir -p "$HOME_DIR" "$FAKE_BIN" "$FIXTURE_APP/Contents/MacOS" \
    "$FIXTURE_APP/Contents/Resources/app.asar.unpacked/node_modules/node-pty/build/Release" \
    "$FIXTURE_APP/Contents/Resources/app.asar.unpacked/node_modules/node-pty/prebuilds/darwin-x64"
  : >"$LOG"
  cat >"$FIXTURE_APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleExecutable</key><string>Hermes</string></dict></plist>
PLIST
  : >"$FIXTURE_APP/Contents/MacOS/Hermes"
  : >"$FIXTURE_APP/Contents/Resources/app.asar.unpacked/node_modules/node-pty/build/Release/pty.node"
  : >"$FIXTURE_APP/Contents/Resources/app.asar.unpacked/node_modules/node-pty/prebuilds/darwin-x64/spawn-helper"
  chmod +x "$FIXTURE_APP/Contents/MacOS/Hermes" \
    "$FIXTURE_APP/Contents/Resources/app.asar.unpacked/node_modules/node-pty/prebuilds/darwin-x64/spawn-helper"

  make_fake_tool "$FAKE_BIN/uname" 'case "${1:-}" in -s) echo "${FAKE_OS:-Darwin}" ;; -m) echo "${FAKE_ARCH:-x86_64}" ;; *) echo "Darwin" ;; esac'
  make_fake_tool "$FAKE_BIN/sw_vers" 'echo "${FAKE_MACOS_VERSION:-13.6}"'
  make_fake_tool "$FAKE_BIN/xcode-select" 'exit 0'
  make_fake_tool "$FAKE_BIN/lipo" 'case "$*" in *pty.node*) echo "${FAKE_NODE_ARCHS:-x86_64}" ;; *spawn-helper*) echo "${FAKE_HELPER_ARCHS:-x86_64}" ;; *) echo "${FAKE_MAIN_ARCHS:-x86_64}" ;; esac'
  make_fake_tool "$FAKE_BIN/codesign" 'printf "codesign %s\n" "$*" >>"$TEST_COMMAND_LOG"; if [[ "$1" == "--verify" && "${FAKE_CODESIGN_VERIFY_FAIL:-0}" == 1 ]]; then exit 1; fi'
  make_fake_tool "$FAKE_BIN/ditto" 'printf "ditto %s\n" "$*" >>"$TEST_COMMAND_LOG"; cp -R "$1" "$2"'
  make_fake_tool "$FAKE_BIN/git" 'printf "git %s\n" "$*" >>"$TEST_COMMAND_LOG"; if [[ "${1:-}" == clone ]]; then mkdir -p "$3/.git" "$3/apps/desktop"; printf lock >"$3/package-lock.json"; elif [[ "${1:-}" == remote && "${2:-}" == get-url ]]; then echo "https://github.com/NousResearch/hermes-agent.git"; elif [[ "${1:-}" == rev-parse ]]; then echo "${TEST_SOURCE_REF:-586aae4bf13c20c3f2966cad590b27946b227bbb}"; elif [[ "${1:-}" == status ]]; then printf "%s" "${FAKE_GIT_STATUS:-}"; elif [[ "${1:-}" == clean && "${2:-}" == -ffdx && "$#" == 2 ]]; then rm -f apps/desktop/.env.production.local; fi; exit 0'
  make_fake_tool "$FAKE_BIN/npm" 'printf "npm %s\n" "$*" >>"$TEST_COMMAND_LOG"; if [[ "${FAKE_REQUIRE_CLEAN:-0}" == 1 && -e apps/desktop/.env.production.local ]]; then exit 1; fi; if [[ "$*" == *"run builder"* ]]; then mkdir -p apps/desktop/release/mac-x64; cp -R "$TEST_FIXTURE_APP" apps/desktop/release/mac-x64/Hermes.app; fi; exit 0'
  make_fake_tool "$FAKE_BIN/find" '/usr/bin/find "$@"; exit "${FAKE_FIND_EXIT:-0}"'
  make_fake_tool "$FAKE_BIN/node" 'echo v22.12.0'
  make_fake_tool "$FAKE_BIN/curl" 'exit 0'
}

run_installer() {
  HOME="$HOME_DIR" PATH="$FAKE_BIN:/usr/bin:/bin" TEST_COMMAND_LOG="$LOG" TEST_FIXTURE_APP="$FIXTURE_APP" \
    "$INSTALLER" "$@" >"$CASE_ROOT/stdout" 2>"$CASE_ROOT/stderr"
}

cleanup_case() {
  rm -rf "$CASE_ROOT"
}

if [[ ! -f "$INSTALLER" ]]; then
  printf 'not ok 1 - installer implementation exists\n'
  exit 1
fi

setup_case
DESCRIPTION='rejects non-Darwin hosts'
FAKE_OS=Linux report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" FAKE_OS=Linux "$3" --verify-only "$4" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
DESCRIPTION='rejects arm64 hosts'
FAKE_ARCH=arm64 report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" FAKE_ARCH=arm64 "$3" --verify-only "$4" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
DESCRIPTION='rejects a moving or abbreviated ref'
report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" "$3" --ref main >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER"
cleanup_case

setup_case
DESCRIPTION='fetches, checks out, and reports the pinned default source commit'
report bash -c 'HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" TEST_FIXTURE_APP="$4" "$3" >"$5" 2>/dev/null && grep -F "git fetch --no-tags origin 586aae4bf13c20c3f2966cad590b27946b227bbb" "$2" >/dev/null && grep -F "git checkout --detach 586aae4bf13c20c3f2966cad590b27946b227bbb" "$2" >/dev/null && grep -F "Source commit: 586aae4bf13c20c3f2966cad590b27946b227bbb" "$5" >/dev/null' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP" "$CASE_ROOT/stdout"
cleanup_case

setup_case
DESCRIPTION='refuses a main executable without x86_64'
FAKE_MAIN_ARCHS=arm64 report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" FAKE_MAIN_ARCHS=arm64 "$3" --verify-only "$4" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
DESCRIPTION='refuses a node-pty native module without x86_64'
FAKE_NODE_ARCHS=arm64 report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" FAKE_NODE_ARCHS=arm64 "$3" --verify-only "$4" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
DESCRIPTION='refuses a node-pty spawn-helper without x86_64'
FAKE_HELPER_ARCHS=arm64 report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" FAKE_HELPER_ARCHS=arm64 "$3" --verify-only "$4" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
DESCRIPTION='fails architecture validation when native-artifact traversal fails'
FAKE_FIND_EXIT=1 report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" FAKE_FIND_EXIT=1 "$3" --verify-only "$4" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
mkdir -p "$HOME_DIR/Library/Caches" "$CASE_ROOT/outside"
printf keep >"$CASE_ROOT/outside/marker"
ln -s "$CASE_ROOT/outside" "$HOME_DIR/Library/Caches/hermes-intel-desktop"
DESCRIPTION='refuses a symlinked build cache without deleting its target'
report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" TEST_FIXTURE_APP="$4" "$3" >/dev/null 2>&1 && test -f "$5/marker"' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP" "$CASE_ROOT/outside"
cleanup_case

setup_case
DESCRIPTION='refuses a dirty source checkout before npm lifecycle scripts run'
FAKE_GIT_STATUS='?? unexpected-file' report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" TEST_FIXTURE_APP="$4" FAKE_GIT_STATUS="?? unexpected-file" "$3" >/dev/null 2>&1 && ! grep -F "npm install" "$2" >/dev/null' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
mkdir -p "$HOME_DIR/Library/Caches/hermes-intel-desktop/source/.git" \
  "$HOME_DIR/Library/Caches/hermes-intel-desktop/source/apps/desktop"
printf lock >"$HOME_DIR/Library/Caches/hermes-intel-desktop/source/package-lock.json"
printf contaminated >"$HOME_DIR/Library/Caches/hermes-intel-desktop/source/apps/desktop/.env.production.local"
DESCRIPTION='removes ignored cached inputs before npm lifecycle and build commands'
report bash -c 'HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" TEST_FIXTURE_APP="$4" FAKE_REQUIRE_CLEAN=1 "$3" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
mkdir -p "$HOME_DIR/Applications/Hermes.app"
printf old >"$HOME_DIR/Applications/Hermes.app/old-marker"
DESCRIPTION='does not replace an existing app when signature verification fails'
FAKE_CODESIGN_VERIFY_FAIL=1 report bash -c '! HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" TEST_FIXTURE_APP="$4" FAKE_CODESIGN_VERIFY_FAIL=1 "$3" >/dev/null 2>&1 && test -f "$0/Applications/Hermes.app/old-marker"' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
mkdir -p "$HOME_DIR/Applications/Hermes.app"
printf old >"$HOME_DIR/Applications/Hermes.app/old-marker"
DESCRIPTION='installs under the user Applications directory and preserves one rollback app'
report bash -c 'HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" TEST_FIXTURE_APP="$4" "$3" >/dev/null 2>&1 && test -d "$0/Applications/Hermes.app" && test -f "$0/Applications/Hermes.rollback.app/old-marker" && test ! -e /Applications/Hermes.app.test-marker' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

setup_case
mkdir -p "$HOME_DIR/.config/hermes"
printf 'connection-state' >"$HOME_DIR/.config/hermes/settings"
DESCRIPTION='leaves connection URL and token state untouched'
report bash -c 'before=$(shasum "$0/.config/hermes/settings"); HOME="$0" PATH="$1:/usr/bin:/bin" TEST_COMMAND_LOG="$2" TEST_FIXTURE_APP="$4" "$3" >/dev/null 2>&1; after=$(shasum "$0/.config/hermes/settings"); test "$before" = "$after" && ! grep -R "HERMES_DESKTOP_REMOTE_URL" "$0/Applications/Hermes.app" >/dev/null 2>&1' "$HOME_DIR" "$FAKE_BIN" "$LOG" "$INSTALLER" "$FIXTURE_APP"
cleanup_case

printf '1..%d\n' "$TESTS"
exit "$FAILURES"
