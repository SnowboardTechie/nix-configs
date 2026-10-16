#!/usr/bin/env bash
# Preflight for the oMLX trial agent (nix-configs modules/services/omlx.nix).
#
#   omlx-preflight.sh <jq> <state-dir> <omlx> serve [args...]
#
# Refuses to start, with EX_CONFIG (78) and one line on stderr, unless:
# - the reviewed runtime is installed;
# - settings.json was commissioned. oMLX's defaults (no API key, CORS `*`)
#   let any web page in the operator's browser claim the admin key through
#   the unauthenticated first-key setup route and use /v1 without one;
# - no model is pinned (pinned models load at startup);
# - no model directory carries code. mlx-vlm, which loads GLM-5.3-Flash,
#   imports config.json's `model_file` whatever `trust_remote_code` says.
# Otherwise execs the command unchanged. It creates, installs and downloads
# nothing, never prints the key, and uses only shell builtins and the given jq.
set -u

jq=$1
state_dir=$2
shift 2
omlx=$1
runbook="see docs/omlx-glm53-trial.md in nix-configs"

refuse() {
  echo "omlx: $1; not starting ($runbook)" >&2
  exit 78
}

[ -d "$state_dir" ] || refuse "state directory $state_dir does not exist: the reviewed runtime is not installed"
[ -x "$omlx" ] || refuse "runtime executable $omlx is missing or not executable: install the reviewed runtime first"

settings="$state_dir/settings.json"
if ! "$jq" -e '
  (.auth.api_key | type == "string" and length >= 32)
  and .server.cors_origins == []
  and (.auth.skip_api_key_verification // false) == false
  and (.auth.allow_unauthenticated_inference // false) == false
  and (.server.distributed_inference_enabled // false) == false
  and .mcp.config_path == null
' "$settings" >/dev/null 2>&1; then
  refuse "$settings is missing, unreadable or not commissioned: it needs an API key of 32 or more characters, cors_origins [], key verification and authenticated inference on, and cluster and MCP off"
fi

model_settings="$state_dir/model_settings.json"
if [ -e "$model_settings" ] &&
  ! "$jq" -e '[(.models // {})[] | select(.is_pinned == true)] | length == 0' "$model_settings" >/dev/null 2>&1; then
  refuse "$model_settings pins a model or cannot be read; pinned models load at startup, so unpin it first"
fi

for config in "$state_dir"/models/*/config.json; do
  [ -e "$config" ] || continue
  model=${config%/config.json}
  if ! "$jq" -e 'has("model_file") or has("auto_map") | not' "$config" >/dev/null 2>&1 ||
    compgen -G "$model/*.py" >/dev/null; then
    refuse "model directory $model carries code (config.json model_file or auto_map, or *.py) or an unreadable config.json; the loader would run it"
  fi
done

exec "$@"
