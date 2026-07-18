#!/usr/bin/env bash
set -euo pipefail

readonly REPO_URL="https://github.com/NousResearch/hermes-agent.git"
readonly VERIFIED_REF="586aae4bf13c20c3f2966cad590b27946b227bbb"
readonly REMOTE_URL="http://100.121.238.48:9119"
readonly BUILD_ROOT="${HOME}/Library/Caches/hermes-intel-desktop"
readonly SOURCE_DIR="${BUILD_ROOT}/source"
readonly INSTALL_DIR="${HOME}/Applications"
readonly INSTALL_APP="${INSTALL_DIR}/Hermes.app"
readonly ROLLBACK_APP="${INSTALL_DIR}/Hermes.rollback.app"

usage() {
  cat <<'EOF'
Usage:
  install-hermes-intel-desktop [--ref FULL_COMMIT_SHA] [--keep-build]
  install-hermes-intel-desktop --verify-only /path/to/Hermes.app
EOF
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

require_intel_macos() {
  [[ "$(uname -s)" == Darwin ]] || fail "this installer runs only on macOS"
  [[ "$(uname -m)" == x86_64 ]] || fail "this installer runs only on x86_64 macOS"

  local major
  major=$(sw_vers -productVersion | cut -d. -f1)
  [[ "$major" =~ ^[0-9]+$ ]] || fail "could not determine the macOS version"
  ((major >= 12)) || fail "macOS 12 or newer is required"
}

require_prerequisites() {
  local command
  for command in codesign ditto find git lipo mktemp mv node npm python3 rm sw_vers uname xcode-select; do
    require_command "$command"
  done
  xcode-select -p >/dev/null 2>&1 || fail "Xcode Command Line Tools are required"

  local node_major
  node_major=$(node --version | sed -E 's/^v([0-9]+).*/\1/')
  [[ "$node_major" =~ ^[0-9]+$ ]] || fail "could not determine the Node.js version"
  ((node_major >= 22)) || fail "Node.js 22 or newer is required"
}

read_bundle_executable() {
  local app=$1
  local plist="$app/Contents/Info.plist"
  [[ -f "$plist" ]] || fail "missing Info.plist in $app"
  python3 - "$plist" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "rb") as handle:
    value = plistlib.load(handle).get("CFBundleExecutable")
if not isinstance(value, str) or not value:
    raise SystemExit("CFBundleExecutable is missing")
print(value)
PY
}

require_x86_64() {
  local binary=$1
  local archs
  [[ -f "$binary" ]] || fail "missing native binary: $binary"
  archs=$(lipo -archs "$binary") || fail "could not inspect architecture: $binary"
  case " $archs " in
    *" x86_64 "*) ;;
    *) fail "native binary lacks x86_64: $binary ($archs)" ;;
  esac
}

verify_app_architectures() {
  local app=$1
  [[ -d "$app" ]] || fail "app bundle not found: $app"

  local executable main_binary artifact
  executable=$(read_bundle_executable "$app") || fail "could not read CFBundleExecutable from $app"
  main_binary="$app/Contents/MacOS/$executable"
  require_x86_64 "$main_binary"

  local node_count=0
  local node_pty_count=0
  local node_list helper_list
  node_list=$(mktemp "${TMPDIR:-/tmp}/hermes-intel-nodes.XXXXXX")
  helper_list=$(mktemp "${TMPDIR:-/tmp}/hermes-intel-helpers.XXXXXX")
  if ! find "$app" -type f -name '*.node' -print >"$node_list"; then
    rm -f "$node_list" "$helper_list"
    fail "could not enumerate native .node artifacts"
  fi
  if ! find "$app" -type f -name spawn-helper -perm +111 -print >"$helper_list"; then
    rm -f "$node_list" "$helper_list"
    fail "could not enumerate executable spawn-helper artifacts"
  fi
  while IFS= read -r artifact; do
    [[ -n "$artifact" ]] || continue
    node_count=$((node_count + 1))
    case "$artifact" in *node-pty*) node_pty_count=$((node_pty_count + 1)) ;; esac
    require_x86_64 "$artifact"
  done <"$node_list"

  local helper_count=0
  local node_pty_helper_count=0
  while IFS= read -r artifact; do
    [[ -n "$artifact" ]] || continue
    helper_count=$((helper_count + 1))
    case "$artifact" in *node-pty*) node_pty_helper_count=$((node_pty_helper_count + 1)) ;; esac
    require_x86_64 "$artifact"
  done <"$helper_list"
  rm -f "$node_list" "$helper_list"

  ((node_count > 0 && node_pty_count > 0)) || fail "expected node-pty .node payload was not found"
  ((helper_count > 0 && node_pty_helper_count > 0)) || fail "expected executable node-pty spawn-helper was not found"
  printf '%s\n' "$(lipo -archs "$main_binary")"
}

resolve_built_app() {
  local candidates=()
  local candidate
  for candidate in \
    "$SOURCE_DIR/apps/desktop/release/mac-x64/Hermes.app" \
    "$SOURCE_DIR/apps/desktop/release/mac/Hermes.app"; do
    [[ -d "$candidate" ]] && candidates+=("$candidate")
  done
  ((${#candidates[@]} == 1)) || fail "expected exactly one known Electron Builder output, found ${#candidates[@]}"
  printf '%s\n' "${candidates[0]}"
}

safe_remove_build_path() {
  local path=$1
  python3 - "$BUILD_ROOT" "$path" <<'PY' || fail "refusing unsafe cache deletion: $path"
import os
import sys

root = os.path.abspath(sys.argv[1])
target = os.path.abspath(sys.argv[2])
if os.path.commonpath((root, target)) != root or target == root:
    raise SystemExit(1)
if os.path.islink(root):
    raise SystemExit(1)
relative_parent = os.path.relpath(os.path.dirname(target), root)
current = root
if relative_parent != ".":
    for component in relative_parent.split(os.sep):
        current = os.path.join(current, component)
        if os.path.islink(current):
            raise SystemExit(1)
if os.path.commonpath((os.path.realpath(root), os.path.realpath(os.path.dirname(target)))) != os.path.realpath(root):
    raise SystemExit(1)
PY
  rm -rf "$path"
}

requested_ref=$VERIFIED_REF
keep_build=false
verify_only_app=
build_option_seen=false

while (($# > 0)); do
  case "$1" in
    --ref)
      (($# >= 2)) || fail "--ref requires a full commit SHA"
      requested_ref=$2
      build_option_seen=true
      shift 2
      ;;
    --keep-build)
      keep_build=true
      build_option_seen=true
      shift
      ;;
    --verify-only)
      (($# == 2)) || fail "--verify-only requires exactly one app path"
      verify_only_app=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown argument: $1"
      ;;
  esac
done

[[ "$requested_ref" =~ ^[0-9a-fA-F]{40}$ ]] || fail "--ref must be a full 40-character hexadecimal commit SHA"
requested_ref=$(printf '%s' "$requested_ref" | tr 'A-F' 'a-f')
require_intel_macos
require_prerequisites

if [[ -n "$verify_only_app" ]]; then
  [[ "$build_option_seen" == false ]] || fail "--verify-only cannot be combined with build options"
  verify_app_architectures "$verify_only_app" >/dev/null
  codesign --verify --deep --strict --verbose=2 "$verify_only_app"
  printf 'Verified Intel-compatible app: %s\n' "$verify_only_app"
  exit 0
fi

[[ ! -L "$BUILD_ROOT" ]] || fail "build cache must not be a symlink: $BUILD_ROOT"
mkdir -p "$BUILD_ROOT"
[[ ! -L "$SOURCE_DIR" ]] || fail "source cache must not be a symlink: $SOURCE_DIR"
if [[ ! -d "$SOURCE_DIR/.git" ]]; then
  [[ ! -e "$SOURCE_DIR" ]] || fail "source cache exists but is not a Git checkout: $SOURCE_DIR"
  git clone "$REPO_URL" "$SOURCE_DIR"
fi

cd "$SOURCE_DIR"
[[ "$(git remote get-url origin)" == "$REPO_URL" ]] || fail "cached source origin is not the official HTTPS repository"
git fetch --no-tags origin "$requested_ref"
git checkout --detach "$requested_ref"
git reset --hard "$requested_ref"
[[ "$(git rev-parse HEAD)" == "$requested_ref" ]] || fail "checked-out source does not match requested commit"

# SOURCE_DIR is a dedicated cache checkout. Remove every ignored/untracked input
# so lifecycle scripts and Vite cannot consume state outside the pinned commit.
git clean -ffdx
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || fail "source checkout is not clean before dependency installation"
git diff --exit-code -- package-lock.json >/dev/null || fail "package-lock.json differs from the pinned commit"

npm_config_arch=x64 npm_config_ignore_scripts=false npm install --workspace apps/desktop
git diff --exit-code -- package-lock.json >/dev/null || fail "npm install changed package-lock.json"
npm run build --workspace apps/desktop
npm_config_arch=x64 npm run builder --workspace apps/desktop -- --mac --x64 --publish=never

built_app=$(resolve_built_app)
staged_app="$BUILD_ROOT/Hermes.staged.app"
safe_remove_build_path "$staged_app"
ditto "$built_app" "$staged_app"
architecture=$(verify_app_architectures "$staged_app")
codesign --force --deep --sign - "$staged_app"
codesign --verify --deep --strict --verbose=2 "$staged_app"
architecture=$(verify_app_architectures "$staged_app")

mkdir -p "$INSTALL_DIR"
install_stage="$INSTALL_DIR/.Hermes.staged.$$"
rm -rf "$install_stage"
ditto "$staged_app" "$install_stage"
verify_app_architectures "$install_stage" >/dev/null
codesign --verify --deep --strict --verbose=2 "$install_stage"

if [[ -e "$INSTALL_APP" ]]; then
  rm -rf "$ROLLBACK_APP"
  mv "$INSTALL_APP" "$ROLLBACK_APP"
fi
mv "$install_stage" "$INSTALL_APP"
verify_app_architectures "$INSTALL_APP" >/dev/null
codesign --verify --deep --strict --verbose=2 "$INSTALL_APP"

if [[ "$keep_build" != true ]]; then
  safe_remove_build_path "$staged_app"
fi

printf 'Installed app: %s\n' "$INSTALL_APP"
printf 'Source commit: %s\n' "$requested_ref"
printf 'Main executable architectures: %s\n' "$architecture"
if [[ -d "$ROLLBACK_APP" ]]; then
  printf 'Rollback app: %s\n' "$ROLLBACK_APP"
else
  printf 'Rollback app: none (no previous install)\n'
fi
cat <<EOF
Open Hermes → Settings → Gateway → Remote gateway
URL: $REMOTE_URL
Sign in with the existing dashboard credentials.
EOF
