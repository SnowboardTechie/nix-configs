# Ollama LLM server service
#
# Provides local LLM inference via Ollama. Requires homebrew `ollama` package.
# Default: serves on 127.0.0.1:11434 with flash attention and q8_0 KV cache.
{ inputs, ... }:
{
  # Darwin aspect - full configuration from modules/darwin/services/ollama.nix
  flake.modules.darwin.ollama = { config, lib, pkgs, ... }: {
    options.services.ollama = {
      enable = lib.mkEnableOption "Ollama LLM server";

      host = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1";
        description = "Host address for Ollama to bind to";
      };

      port = lib.mkOption {
        type = lib.types.port;
        default = 11434;
        description = "Port for Ollama to listen on";
      };

      flashAttention = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable flash attention for faster inference";
      };

      kvCacheType = lib.mkOption {
        type = lib.types.str;
        default = "q8_0";
        description = "KV cache quantization type";
      };

       keepAlive = lib.mkOption {
         type = lib.types.str;
         default = "-1";
         description = "How long to keep models loaded (0 = unload immediately, -1 = forever)";
        };

      maxLoadedModels = lib.mkOption {
        type = lib.types.int;
        default = 2;
        description = "Maximum number of models loaded concurrently";
      };

      numParallel = lib.mkOption {
        type = lib.types.int;
        default = 4;
        description = "Maximum number of parallel requests per model";
      };

      contextLength = lib.mkOption {
        type = lib.types.int;
        default = 16384;
        description = "Context window size (ollama defaults to 4096 and silently truncates beyond it)";
      };

      tailscaleServe = lib.mkEnableOption "tailnet-only TCP forwarding through Tailscale Serve";
    };

    config = let
      cfg = config.services.ollama;
    in lib.mkIf cfg.enable {
      assertions = lib.optional cfg.tailscaleServe {
        assertion = config.services.tailscale.enable;
        message = "services.ollama.tailscaleServe requires services.tailscale.enable";
      };

      # Ollama service configuration
      launchd.user.agents.ollama = {
        serviceConfig = {
          ProgramArguments = [ "/opt/homebrew/bin/ollama" "serve" ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/ollama.log";
          StandardErrorPath = "/tmp/ollama.error.log";
          EnvironmentVariables = {
            OLLAMA_HOST = "${cfg.host}:${toString cfg.port}";
            OLLAMA_ORIGINS = "*";
            OLLAMA_FLASH_ATTENTION = if cfg.flashAttention then "1" else "0";
            OLLAMA_KV_CACHE_TYPE = cfg.kvCacheType;
            OLLAMA_KEEP_ALIVE = cfg.keepAlive;
            OLLAMA_MAX_LOADED_MODELS = toString cfg.maxLoadedModels;
            OLLAMA_NUM_PARALLEL = toString cfg.numParallel;
            OLLAMA_CONTEXT_LENGTH = toString cfg.contextLength;
          };
        };
      };

      # Firewall rules + Tailscale Serve, folded into extraActivation because
      # nix-darwin's system.activationScripts only composes a fixed set of named
      # phases into the activate script (custom names like `ollama-firewall` are
      # silently ignored). See services/AGENTS.md for the full footgun writeup.
      #
      # Picking up brew binary upgrades is deliberately NOT handled here:
      # extraActivation runs long before the homebrew phase, so a restart here
      # races the upgrade it is meant to react to. base/homebrew.nix re-pins
      # every brew-backed agent in postActivation instead.
      system.activationScripts.extraActivation.text = lib.mkAfter ''
        # === ollama firewall ===
        /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/bin/ollama >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /opt/homebrew/bin/ollama >/dev/null 2>&1 || true

        ${lib.optionalString cfg.tailscaleServe ''
          # === ollama via Tailscale Serve ===
          if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
            ${config.services.tailscale.package}/bin/tailscale serve \
              --bg --yes \
              --tcp=${toString cfg.port} \
              tcp://${cfg.host}:${toString cfg.port}; then
            echo "Failed to configure Tailscale Serve for Ollama on port ${toString cfg.port}" >&2
            exit 1
          fi
        ''}
      '';
    };
  };

  # NixOS aspect - stub for future implementation
  flake.modules.nixos.ollama = { config, lib, ... }: {
    # TODO: Implement NixOS equivalent using systemd service
    # NixOS has native ollama package and service module
  };
}
