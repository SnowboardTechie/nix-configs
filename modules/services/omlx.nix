# oMLX: an on-demand MLX inference server for trials of models larger than RAM
# (MoE expert offload from SSD), beside Ollama rather than replacing it.
#
# Review, install route and runbook: docs/omlx-glm53-trial.md. Nix supervises
# one foreground `omlx serve` as a launchd user agent; it installs nothing.
# The runtime is a user-managed venv built from a pinned source revision under
# `stateDir`: Homebrew's tap floats at every activation (`onActivation.upgrade`)
# and zaps undeclared installs, and the macOS app runs its own server and
# auto-updater.
#
# Fail closed: `omlx-preflight.sh` refuses to start, downloading nothing,
# until that runtime exists and `stateDir/settings.json` was commissioned
# with an API key and `cors_origins = []` (oMLX's defaults let any web page
# in the operator's browser claim the admin key), and while a model is
# pinned (pinned models load at startup) or a model directory carries code
# (the VLM loader imports `model_file` whatever `trust_remote_code` says).
#
# Loopback only, by construction: no firewall registration, Tailscale route,
# MCP config, Hugging Face endpoint or proxy, and no credential in the store or
# plist; the key lives in oMLX's 0600 settings.json, written by the operator.
# Hugging Face is offline for the process, with its state under `stateDir`.
# Per-model settings (expert offload, MTP) are oMLX's own mutable state, set
# through its admin API during commissioning; activation never writes them.
# `omlx serve` persists the flags below into `stateDir/settings.json` at every
# start, so these values win on restart.
{ inputs, ... }:
{
  flake.modules.darwin.omlx = { config, lib, pkgs, ... }:
    let
      cfg = config.services.omlx;
      home = "/Users/${config.system.primaryUser}";
      executable = "${cfg.stateDir}/runtime/venv/bin/omlx";
      ollamaPort = if config.services ? ollama && config.services.ollama.enable then config.services.ollama.port else null;
    in
    {
      options.services.omlx = {
        enable = lib.mkEnableOption ''
          the oMLX inference server on loopback. Install the reviewed runtime
          first; see docs/omlx-glm53-trial.md'';

        port = lib.mkOption {
          type = lib.types.port;
          default = 8000;
          description = "Loopback port. Unprivileged, and never Ollama's.";
        };

        stateDir = lib.mkOption {
          type = lib.types.str;
          default = "${home}/.local/share/omlx";
          description = ''
            oMLX's base path: settings, logs, `models/`, `cache/` (the paged
            SSD KV cache) and the runtime venv at `runtime/venv`.
          '';
        };

        memoryGuard = lib.mkOption {
          # `off` disables the guard; `aggressive` may compress half of other
          # apps' memory (Ollama's included); `custom` is a static ceiling
          # that ignores what other apps hold.
          type = lib.types.enum [ "safe" "balanced" ];
          default = "safe";
          description = ''
            oMLX memory guard tier. `safe` keeps 20% of RAM (6-16 GB) free and
            never pushes other apps' memory into the compressor.
          '';
        };

        ssdCacheGB = lib.mkOption {
          type = lib.types.ints.positive;
          default = 20;
          description = ''
            Fixed size of the paged SSD KV/prefix cache in GiB (upstream's
            default, `auto`, takes half of the free space). Expert offload
            reads the checkpoint itself and is not part of this cache.
          '';
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [
          {
            assertion = cfg.port >= 1024 && cfg.port != ollamaPort;
            message = "services.omlx.port must be an unprivileged port (1024 or above) and must not be Ollama's";
          }
          {
            # Absolute, no trailing slash or `..`, nothing that needs quoting.
            assertion = builtins.match "(/[A-Za-z0-9._-]+)+" cfg.stateDir != null
              && builtins.match ".*/\\.\\.?(/.*)?" cfg.stateDir == null;
            message = "services.omlx.stateDir must be a plain absolute path without `..`, whitespace or a trailing slash";
          }
        ];

        launchd.user.agents.omlx.serviceConfig = {
          ProgramArguments = [
            "${pkgs.bash}/bin/bash"
            "${./omlx-preflight.sh}"
            "${pkgs.jq}/bin/jq"
            cfg.stateDir
            executable
            "serve"
            "--base-path"
            cfg.stateDir
            "--model-dir"
            "${cfg.stateDir}/models"
            "--host"
            "127.0.0.1"
            "--port"
            (toString cfg.port)
            "--max-concurrent-requests"
            "1"
            "--memory-guard"
            cfg.memoryGuard
            "--paged-ssd-cache-dir"
            "${cfg.stateDir}/cache"
            "--paged-ssd-cache-max-size"
            "${toString cfg.ssdCacheGB}GB"
            # Only `--model-dir` is discovered, not ~/.cache/huggingface.
            "--no-hf-cache"
            # The inference and admin APIs without the web UI.
            "--headless"
            "--log-level"
            "info"
          ];
          EnvironmentVariables = {
            HOME = home;
            PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
            HF_HOME = "${cfg.stateDir}/huggingface";
            HF_HUB_OFFLINE = "1";
            HF_HUB_DISABLE_TELEMETRY = "1";
            TRANSFORMERS_OFFLINE = "1";
          };
          RunAtLoad = true;
          # A crash restarts it (no model loads at startup); a refused
          # preflight or a configuration error stays down instead of looping.
          KeepAlive.Crashed = true;
          StandardOutPath = "/tmp/omlx.log";
          StandardErrorPath = "/tmp/omlx.error.log";
        };
      };
    };

  # NixOS stub per repository convention: oMLX runs on Apple Silicon macOS only.
  flake.modules.nixos.omlx = { };
}
