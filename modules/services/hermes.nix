# Hermes Agent client packages and macOS service supervision.
#
# The upstream Nix package makes the CLI/Desktop reproducible on all client
# hosts. On macOS, Matrix is intentionally excluded from that package upstream,
# so the Studio gateway/serve agents continue to use Hermes's managed venv in
# ~/.hermes while nix-darwin owns their launchd definitions and lifecycle.
# Desktop remote URL/token state remains in its per-user settings. Do not inject
# only HERMES_DESKTOP_REMOTE_URL: that env path requires a paired token and
# bypasses the client's already-saved authentication.
{ inputs, ... }:
{
  flake.modules.darwin.hermes =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.hermes;
      system = pkgs.stdenv.hostPlatform.system;
      homeDirectory =
        if cfg.homeDirectory != null then cfg.homeDirectory else "/Users/${cfg.user}";
      hermesHome = "${homeDirectory}/.hermes";
      runtimeVenv = "${hermesHome}/hermes-agent/venv";
      runtimePython =
        if cfg.runtimePython != null then cfg.runtimePython else "${runtimeVenv}/bin/python";
      upstreamDesktop = inputs.hermes-agent.packages.${system}.desktop;
      serviceEnvironment = {
        HERMES_HOME = hermesHome;
        VIRTUAL_ENV = runtimeVenv;
        PATH = lib.concatStringsSep ":" [
          "${runtimeVenv}/bin"
          "${homeDirectory}/.local/bin"
          "${cfg.package}/bin"
          "/run/current-system/sw/bin"
          "${config.homebrew.prefix}/bin"
          "/usr/bin"
          "/bin"
          "/usr/sbin"
          "/sbin"
        ];
      };
    in
    {
      options.services.hermes = {
        enable = lib.mkEnableOption "Hermes Agent client packages";

        package = lib.mkOption {
          type = lib.types.package;
          default = inputs.hermes-agent.packages.${system}.default;
          description = "Nix-built Hermes CLI package installed for interactive client use.";
        };

        user = lib.mkOption {
          type = lib.types.str;
          default = config.system.primaryUser;
          description = "User whose Hermes state and launchd agents are managed.";
        };

        homeDirectory = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Hermes user's home directory; defaults to /Users/<user>.";
        };

        desktop.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Install the Hermes Desktop client.";
        };

        clientOnly = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Remove legacy auto-start gateway/serve agents so this host cannot compete with the primary gateway.";
        };

        runtimePython = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = ''
            Python executable from Hermes's managed macOS installation. The
            upstream Darwin Nix package excludes Matrix, so gateway services use
            this mutable venv while Nix manages launchd supervision.
          '';
        };

        gateway.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Run the canonical Hermes messaging gateway at login.";
        };

        serve = {
          enable = lib.mkOption {
            type = lib.types.bool;
            default = false;
            description = "Expose the Hermes Desktop backend through launchd.";
          };
          host = lib.mkOption {
            type = lib.types.str;
            default = "127.0.0.1";
            description = "Address passed to hermes serve; use a Tailscale address for remote clients.";
          };
          port = lib.mkOption {
            type = lib.types.port;
            default = 9119;
            description = "Port passed to hermes serve.";
          };
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          environment.systemPackages = [ cfg.package ]
            ++ lib.optional cfg.desktop.enable upstreamDesktop;
        }

        (lib.mkIf (cfg.gateway.enable || cfg.serve.enable) {
          assertions = [
            {
              assertion = cfg.user == config.system.primaryUser;
              message = "services.hermes launchd agents must run as system.primaryUser";
            }
          ];
        })

        (lib.mkIf cfg.clientOnly {
          assertions = [
            {
              assertion = !cfg.gateway.enable && !cfg.serve.enable;
              message = "services.hermes.clientOnly conflicts with gateway.enable or serve.enable";
            }
          ];

          system.activationScripts.extraActivation.text = lib.mkAfter ''
            # === Hermes client-only role ===
            # Remove gateway definitions left by a previous manual migration.
            hermes_uid=$(/usr/bin/id -u ${lib.escapeShellArg cfg.user} 2>/dev/null || true)
            if [ -n "$hermes_uid" ]; then
              /bin/launchctl bootout "user/$hermes_uid/ai.hermes.gateway" >/dev/null 2>&1 || true
              /bin/launchctl bootout "gui/$hermes_uid/ai.hermes.gateway" >/dev/null 2>&1 || true
              /bin/launchctl bootout "user/$hermes_uid/ai.hermes.serve" >/dev/null 2>&1 || true
              /bin/launchctl bootout "gui/$hermes_uid/ai.hermes.serve" >/dev/null 2>&1 || true
            fi
            /bin/rm -f \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.gateway.plist"} \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.serve.plist"}
          '';
        })

        (lib.mkIf cfg.gateway.enable {
          launchd.user.agents.hermes-gateway = {
            serviceConfig = {
              # Match the label and plist name used by `hermes gateway install`
              # so nix-darwin adopts the existing service instead of creating a
              # second canonical gateway.
              Label = "ai.hermes.gateway";
              ProgramArguments = [
                runtimePython
                "-m"
                "hermes_cli.main"
                "gateway"
                "run"
                "--replace"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = homeDirectory;
              EnvironmentVariables = serviceEnvironment;
              StandardOutPath = "${hermesHome}/logs/gateway.log";
              StandardErrorPath = "${hermesHome}/logs/gateway.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          };
        })

        (lib.mkIf cfg.serve.enable {
          launchd.user.agents.hermes-serve = {
            serviceConfig = {
              Label = "ai.hermes.serve";
              ProgramArguments = [
                runtimePython
                "-m"
                "hermes_cli.main"
                "serve"
                "--host"
                cfg.serve.host
                "--port"
                (toString cfg.serve.port)
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = homeDirectory;
              EnvironmentVariables = serviceEnvironment;
              StandardOutPath = "${hermesHome}/logs/serve.log";
              StandardErrorPath = "${hermesHome}/logs/serve.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          };
        })
      ]);
    };

  flake.modules.nixos.hermes =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.hermes;
      system = pkgs.stdenv.hostPlatform.system;
      upstreamDesktop = inputs.hermes-agent.packages.${system}.desktop;
      desktopEntry = pkgs.makeDesktopItem {
        name = "hermes-desktop";
        desktopName = "Hermes";
        comment = "Nous Research Hermes Agent desktop client";
        exec = "hermes-desktop";
        icon = ./assets/hermes-icon.png;
        terminal = false;
        categories = [
          "Network"
          "Utility"
        ];
      };
    in
    {
      options.services.hermes = {
        enable = lib.mkEnableOption "Hermes Agent client packages";

        package = lib.mkOption {
          type = lib.types.package;
          default = inputs.hermes-agent.packages.${system}.default;
          description = "Nix-built Hermes CLI package installed for client use.";
        };

        desktop.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Install the Hermes Desktop client and application launcher.";
        };
      };

      config = lib.mkIf cfg.enable {
        environment.systemPackages = [ cfg.package ]
          ++ lib.optionals cfg.desktop.enable [
          upstreamDesktop
          desktopEntry
        ];
      };
    };
}
