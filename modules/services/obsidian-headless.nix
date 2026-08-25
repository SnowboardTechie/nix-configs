# Obsidian Headless continuous Sync service
{ inputs, ... }:
{
  flake.modules.darwin.obsidian-headless = { config, lib, pkgs, ... }:
    let
      cfg = config.services.obsidian-headless;
      authToken = "${cfg.homeDirectory}/.obsidian-headless/auth_token";
      corePlugins = "${cfg.vaultPath}/.obsidian/core-plugins.json";
      runner = pkgs.writeShellScript "obsidian-headless-sync" ''
        child_pid=""
        stop_sync() {
          if [ -n "$child_pid" ]; then
            /bin/kill -TERM "$child_pid" 2>/dev/null || true
            wait "$child_pid" 2>/dev/null || true
          fi
          exit 0
        }
        core_plugin_sync_excluded() {
          local status
          status=$(${cfg.package}/bin/ob sync-status --json --path ${lib.escapeShellArg cfg.vaultPath} 2>/dev/null) || return 1
          printf '%s' "$status" \
            | ${pkgs.jq}/bin/jq -e '.configs | (type == "array" and (index("core-plugin") == null))' >/dev/null 2>&1
        }
        trap stop_sync INT TERM HUP

        while true; do
          if [ ! -d ${lib.escapeShellArg cfg.vaultPath} ]; then
            echo "obsidian-headless: vault path does not exist: ${cfg.vaultPath}" >&2
          elif ! ${pkgs.jq}/bin/jq -e '.sync == false' ${lib.escapeShellArg corePlugins} >/dev/null 2>&1; then
            echo "obsidian-headless: waiting for desktop Obsidian Sync to be disabled" >&2
          elif [ ! -s ${lib.escapeShellArg authToken} ]; then
            echo "obsidian-headless: waiting for interactive 'ob login' setup" >&2
          elif sync_status=$(${cfg.package}/bin/ob sync-status --json --path ${lib.escapeShellArg cfg.vaultPath} 2>/dev/null); then
            if printf '%s' "$sync_status" | ${pkgs.jq}/bin/jq -e '.configs | (type == "array" and (index("core-plugin") == null))' >/dev/null 2>&1; then
              ${cfg.package}/bin/ob sync --continuous --path ${lib.escapeShellArg cfg.vaultPath} &
              child_pid=$!
              while /bin/kill -0 "$child_pid" 2>/dev/null; do
                /bin/sleep 30
                if ! ${pkgs.jq}/bin/jq -e '.sync == false' ${lib.escapeShellArg corePlugins} >/dev/null 2>&1 \
                  || ! core_plugin_sync_excluded; then
                  echo "obsidian-headless: Sync safety configuration changed; stopping Headless Sync" >&2
                  /bin/kill -TERM "$child_pid" 2>/dev/null || true
                  wait "$child_pid" 2>/dev/null || true
                  child_pid=""
                  break
                fi
              done
              if [ -n "$child_pid" ]; then
                wait "$child_pid"
                status=$?
                child_pid=""
                exit "$status"
              fi
            else
              echo "obsidian-headless: waiting for 'core-plugin' to be excluded with 'ob sync-config'" >&2
            fi
          else
            echo "obsidian-headless: waiting for interactive 'ob sync-setup' for ${cfg.vaultPath}" >&2
          fi
          /bin/sleep ${toString cfg.setupRetrySeconds}
        done
      '';
    in
    {
      options.services.obsidian-headless = {
        enable = lib.mkEnableOption "Obsidian Headless continuous Sync";

        package = lib.mkPackageOption pkgs "obsidian-headless" { };

        user = lib.mkOption {
          type = lib.types.str;
          default = config.system.primaryUser;
          description = "User whose Obsidian account and vault are synchronized";
        };

        homeDirectory = lib.mkOption {
          type = lib.types.str;
          default = "/Users/${cfg.user}";
          description = "Home directory containing machine-local Obsidian Headless credentials";
        };

        vaultPath = lib.mkOption {
          type = lib.types.str;
          description = "Absolute path to the local Obsidian vault";
        };

        logPath = lib.mkOption {
          type = lib.types.str;
          default = "/tmp/obsidian-headless.log";
          description = "Path for standard output";
        };

        errorLogPath = lib.mkOption {
          type = lib.types.str;
          default = "/tmp/obsidian-headless.error.log";
          description = "Path for standard error";
        };

        setupRetrySeconds = lib.mkOption {
          type = lib.types.ints.positive;
          default = 300;
          description = "Seconds to wait before rechecking incomplete interactive setup";
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [{
          assertion = lib.hasPrefix "/" cfg.homeDirectory && lib.hasPrefix "/" cfg.vaultPath;
          message = "services.obsidian-headless homeDirectory and vaultPath must be absolute paths";
        }];

        nixpkgs.overlays = [
          (final: _prev: {
            obsidian-headless = final.callPackage
              (inputs.obsidian-headless-nixpkgs + "/pkgs/by-name/ob/obsidian-headless/package.nix")
              { };
          })
        ];

        environment.systemPackages = [ cfg.package ];

        launchd.user.agents.obsidian-headless = {
          serviceConfig = {
            Label = "md.obsidian.headless-sync";
            ProgramArguments = [ "${runner}" ];
            RunAtLoad = true;
            KeepAlive = true;
            WorkingDirectory = cfg.vaultPath;
            EnvironmentVariables = {
              HOME = cfg.homeDirectory;
            };
            StandardOutPath = cfg.logPath;
            StandardErrorPath = cfg.errorLogPath;
            ProcessType = "Background";
            ThrottleInterval = cfg.setupRetrySeconds;
          };
        };
      };
    };

  flake.modules.nixos.obsidian-headless = { ... }: {
    # TODO: Implement a systemd user service when a NixOS host needs this.
  };
}
