# DwarfStar serves an independently pinned, user-built model on Studio.
# Only Tailscale Serve can forward the loopback-only API to other trusted hosts.
{ inputs, ... }:
{
  flake.modules.darwin.dwarfstar = { config, lib, pkgs, ... }:
    let
      cfg = config.services.dwarfstar;
    in
    {
      options.services.dwarfstar = {
        enable = lib.mkEnableOption "DwarfStar local inference server";
        directory = lib.mkOption {
          type = lib.types.str;
          default = "/Users/${config.system.primaryUser}/code/ds4";
          description = "Pinned DwarfStar source and locally built binaries";
        };
        modelPath = lib.mkOption {
          type = lib.types.str;
          default = "${cfg.directory}/gguf/DeepSeek-V4.1-Flash-Q2.gguf";
          description = "Verified DeepSeek V4.1 Flash Q2 GGUF";
        };
        host = lib.mkOption {
          type = lib.types.str;
          default = "127.0.0.1";
          description = "DwarfStar bind address";
        };
        port = lib.mkOption {
          type = lib.types.port;
          default = 8000;
          description = "DwarfStar API port";
        };
        contextLength = lib.mkOption {
          type = lib.types.int;
          default = 65536;
          description = "Per-session context budget";
        };
        tailscaleServe = lib.mkEnableOption "tailnet-only TCP forwarding for trusted coding clients";
      };

      config = lib.mkIf cfg.enable {
        assertions = lib.optional cfg.tailscaleServe {
          assertion = config.services.tailscale.enable && cfg.host == "127.0.0.1";
          message = "services.dwarfstar.tailscaleServe requires Tailscale and a loopback bind";
        };
        launchd.user.agents.dwarfstar = {
          serviceConfig = {
            ProgramArguments = [
              "${cfg.directory}/ds4-server"
              "-m" cfg.modelPath
              "--ssd-streaming"
              "--ctx" (toString cfg.contextLength)
              "--host" cfg.host
              "--port" (toString cfg.port)
            ];
            WorkingDirectory = cfg.directory;
            RunAtLoad = true;
            KeepAlive = false;
            StandardOutPath = "/tmp/dwarfstar.log";
            StandardErrorPath = "/tmp/dwarfstar.error.log";
          };
        };
        system.activationScripts.extraActivation.text = lib.mkAfter ''
          ${lib.optionalString cfg.tailscaleServe ''
            if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
              ${config.services.tailscale.package}/bin/tailscale serve \
                --bg --yes \
                --tcp=${toString cfg.port} \
                tcp://${cfg.host}:${toString cfg.port}; then
              echo "Failed to configure Tailscale Serve for DwarfStar on port ${toString cfg.port}" >&2
              exit 1
            fi
          ''}
        '';
      };
    };

  flake.modules.nixos.dwarfstar = { ... }: {
    # Studio-only source build and macOS launchd service.
  };
}
