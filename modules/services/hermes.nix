# Hermes Agent client packages and macOS service supervision.
#
# The upstream Nix package makes the CLI/Desktop reproducible on all client
# hosts. On macOS, Matrix is intentionally excluded from that package upstream,
# so Studio server processes use isolated per-user managed venvs while
# nix-darwin owns their launchd definitions and lifecycle. The Nix package
# remains the interactive client package, not a server runtime.
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
      headlessInstanceModule =
        { name
        , config
        , ...
        }:
        {
          options = {
            user = lib.mkOption {
              type = lib.types.str;
              default = name;
              description = "macOS user that owns and runs this Hermes instance.";
            };

            homeDirectory = lib.mkOption {
              type = lib.types.str;
              default = "/Users/${config.user}";
              description = "Home directory for the instance user.";
            };

            secureHome = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Restrict the user's home directory to mode 0700.";
            };

            runtimePython = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              description = "Python executable from this user's managed Hermes installation.";
            };

            autoUpdate = {
              enable = lib.mkOption {
                type = lib.types.bool;
                default = false;
                description = "Update this managed Hermes runtime with a guarded launchd job.";
              };
              calendar = lib.mkOption {
                type = lib.types.attrsOf lib.types.int;
                default = { Hour = 4; Minute = 0; };
                description = "launchd StartCalendarInterval for managed Hermes updates.";
              };
            };

            gateway.enable = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Run this user's messaging gateway as a boot-time LaunchDaemon.";
            };

            serve = {
              enable = lib.mkOption {
                type = lib.types.bool;
                default = false;
                description = "Run this user's Desktop backend as a boot-time LaunchDaemon.";
              };
              host = lib.mkOption {
                type = lib.types.str;
                default = "127.0.0.1";
                description = "Address passed to hermes serve. Non-loopback addresses require Hermes authentication.";
              };
              port = lib.mkOption {
                type = lib.types.port;
                default = 9119;
                description = "Port passed to hermes serve.";
              };
              tailscale = {
                enable = lib.mkOption {
                  type = lib.types.bool;
                  default = false;
                  description = "Expose a loopback-bound backend through Tailscale Serve HTTPS.";
                };
                httpsPort = lib.mkOption {
                  type = lib.types.port;
                  default = config.serve.port;
                  description = "Tailnet-only HTTPS port exposed by Tailscale Serve.";
                };
                proxyPort = lib.mkOption {
                  type = lib.types.port;
                  default = config.serve.port + 1;
                  description = "Loopback reverse-proxy port used to normalize the Host header for Hermes.";
                };
              };
            };
          };
        };
      enabledHeadlessInstances = lib.filterAttrs
        (_: instance: instance.gateway.enable || instance.serve.enable)
        cfg.headlessInstances;
      enabledHeadlessServeInstances = lib.filterAttrs
        (_: instance: instance.serve.enable)
        enabledHeadlessInstances;
      enabledHeadlessGatewayInstances = lib.filterAttrs
        (_: instance: instance.gateway.enable)
        enabledHeadlessInstances;
      enabledTailscaleServeInstances = lib.filterAttrs
        (_: instance: instance.serve.tailscale.enable)
        enabledHeadlessServeInstances;
      enabledHeadlessUpdateInstances = lib.filterAttrs
        (_: instance: instance.autoUpdate.enable)
        enabledHeadlessInstances;
      headlessUpdateLogDirectory = "/var/log/hermes-updates";
      primaryTailscaleServeEnabled = cfg.dashboard.enable && cfg.dashboard.tailscale.enable;
      localServePorts = lib.optional cfg.dashboard.enable cfg.dashboard.port
        ++ lib.mapAttrsToList (_: instance: instance.serve.port) enabledHeadlessServeInstances;
      localProxyPorts = lib.optional primaryTailscaleServeEnabled cfg.dashboard.tailscale.proxyPort
        ++ lib.mapAttrsToList
        (_: instance: instance.serve.tailscale.proxyPort)
        enabledTailscaleServeInstances;
      tailscaleHttpsPorts = lib.optional primaryTailscaleServeEnabled cfg.dashboard.tailscale.httpsPort
        ++ lib.mapAttrsToList
        (_: instance: instance.serve.tailscale.httpsPort)
        enabledTailscaleServeInstances;
      headlessRuntimeVenv = instance: "${instance.homeDirectory}/.hermes/hermes-agent/venv";
      headlessRuntimePython = instance:
        if instance.runtimePython != null
        then instance.runtimePython
        else "${headlessRuntimeVenv instance}/bin/python";
      headlessEnvironment = instance: {
        HOME = instance.homeDirectory;
        HERMES_HOME = "${instance.homeDirectory}/.hermes";
        VIRTUAL_ENV = headlessRuntimeVenv instance;
        PATH = lib.concatStringsSep ":" [
          "${headlessRuntimeVenv instance}/bin"
          "${instance.homeDirectory}/.local/bin"
          "${cfg.package}/bin"
          "/run/current-system/sw/bin"
          "${config.homebrew.prefix}/bin"
          "/usr/bin"
          "/bin"
          "/usr/sbin"
          "/sbin"
        ];
      };
      # System LaunchDaemons are loaded before the Nix volume is guaranteed to
      # be mounted. Start through a system shell so launchd does not put the job
      # in its penalty box when the real executable is temporarily unavailable.
      waitForNixStoreProgram = programArguments: [
        "/bin/sh"
        "-c"
        ''
          while [ ! -x "$1" ]; do
            /bin/sleep 1
          done
          exec "$@"
        ''
        "wait-for-nix-store"
      ] ++ programArguments;
      waitForManagedProgram = programArguments: [
        "/bin/sh"
        "-c"
        ''
          attempts=0
          while [ ! -x "$1" ] && [ "$attempts" -lt 120 ]; do
            attempts=$((attempts + 1))
            /bin/sleep 1
          done
          if [ ! -x "$1" ]; then
            echo "Managed Hermes executable unavailable after 120 seconds: $1" >&2
            exit 75
          fi
          exec "$@"
        ''
        "wait-for-managed-hermes"
      ] ++ programArguments;
      mkHermesUpdater =
        { name
        , user
        , updaterHome
        , updaterRuntimePython
        , healthUrl
        , supervisedServiceLabel ? null
        }:
        let
          installDirectory = "${updaterHome}/.hermes/hermes-agent";
          updaterPath = lib.concatStringsSep ":" [
            "${installDirectory}/venv/bin"
            "${updaterHome}/.local/bin"
            "/run/current-system/sw/bin"
            "${config.homebrew.prefix}/bin"
            "/usr/bin"
            "/bin"
            "/usr/sbin"
            "/sbin"
          ];
          servicePlist =
            if supervisedServiceLabel != null
            then "/Library/LaunchDaemons/${supervisedServiceLabel}.plist"
            else "";
        in
        pkgs.writeShellScript "update-hermes-${name}" ''
          set -eu

          export HOME=${lib.escapeShellArg updaterHome}
          export HERMES_HOME=${lib.escapeShellArg "${updaterHome}/.hermes"}
          export PATH=${lib.escapeShellArg updaterPath}

          as_user() {
            if [ "$(/usr/bin/id -u)" -eq 0 ]; then
              /usr/bin/sudo -u ${lib.escapeShellArg user} -H \
                /usr/bin/env HOME="$HOME" HERMES_HOME="$HERMES_HOME" PATH="$PATH" "$@"
            else
              "$@"
            fi
          }

          lock_dir="$HERMES_HOME/.auto-update-lock"
          write_lock_pid() {
            # Positional arguments expand in the child shell.
            # shellcheck disable=SC2016
            as_user /bin/sh -c \
              'umask 077; printf "%s\n" "$1" > "$2/pid"' \
              write-lock "$$" "$lock_dir"
          }
          if as_user /bin/mkdir "$lock_dir" 2>/dev/null; then
            if ! write_lock_pid; then
              as_user /bin/rmdir "$lock_dir" >/dev/null 2>&1 || true
              echo "Could not record Hermes update lock for ${name}." >&2
              exit 1
            fi
          else
            if ! as_user /bin/test -d "$lock_dir"; then
              echo "Could not create Hermes update lock for ${name}." >&2
              exit 1
            fi
            lock_pid=$(as_user /bin/cat "$lock_dir/pid" 2>/dev/null || true)
            lock_mtime=$(as_user /usr/bin/stat -f %m "$lock_dir" 2>/dev/null || true)
            now=$(/bin/date +%s)
            lock_active=0
            case "$lock_pid:$lock_mtime" in
              *[!0-9:]* | :* | *:)
                ;;
              *)
                if [ $((now - lock_mtime)) -le 21600 ] \
                  && /bin/kill -0 "$lock_pid" 2>/dev/null; then
                  lock_active=1
                fi
                ;;
            esac
            if [ "$lock_active" -eq 1 ]; then
              echo "Hermes update already running for ${name}; skipping."
              exit 0
            fi
            if ! as_user /bin/rm -f "$lock_dir/pid" \
              || ! as_user /bin/rmdir "$lock_dir" \
              || ! as_user /bin/mkdir "$lock_dir" \
              || ! write_lock_pid; then
              echo "Could not recover stale Hermes update lock for ${name}." >&2
              exit 1
            fi
            echo "Recovered stale Hermes update lock for ${name}."
          fi

          ${lib.optionalString (supervisedServiceLabel != null) "service_stopped=0"}
          cleanup() {
            ${lib.optionalString (supervisedServiceLabel != null) ''
              if [ "$service_stopped" -eq 1 ]; then
                /bin/launchctl bootstrap system ${lib.escapeShellArg servicePlist} >/dev/null 2>&1 || true
              fi
            ''}
            lock_pid=$(as_user /bin/cat "$lock_dir/pid" 2>/dev/null || true)
            if [ "$lock_pid" = "$$" ]; then
              as_user /bin/rm -f "$lock_dir/pid" >/dev/null 2>&1 || true
              as_user /bin/rmdir "$lock_dir" >/dev/null 2>&1 || true
            fi
          }
          trap cleanup EXIT HUP INT TERM

          if [ ! -x ${lib.escapeShellArg updaterRuntimePython} ]; then
            echo "Managed Hermes runtime is missing for ${name}: ${updaterRuntimePython}" >&2
            exit 1
          fi
          if [ ! -d ${lib.escapeShellArg "${installDirectory}/.git"} ]; then
            echo "Managed Hermes checkout is missing for ${name}: ${installDirectory}" >&2
            exit 1
          fi

          branch=$(as_user ${pkgs.git}/bin/git -C ${lib.escapeShellArg installDirectory} branch --show-current)
          if [ "$branch" != main ]; then
            echo "Refusing to update ${name}: checkout is on branch '$branch', not main." >&2
            exit 1
          fi
          if [ -n "$(as_user ${pkgs.git}/bin/git -C ${lib.escapeShellArg installDirectory} status --porcelain)" ]; then
            echo "Refusing to update ${name}: managed checkout is dirty." >&2
            exit 1
          fi

          # The supported check fetches the target branch and records the
          # updater's own read-only assessment before we inspect divergence.
          as_user ${lib.escapeShellArg updaterRuntimePython} -m hermes_cli.main update --check
          counts=$(as_user ${pkgs.git}/bin/git -C ${lib.escapeShellArg installDirectory} rev-list --left-right --count HEAD...origin/main)
          ahead="''${counts%%[[:space:]]*}"
          behind="''${counts##*[[:space:]]}"
          if [ "$ahead" -ne 0 ]; then
            echo "Refusing to update ${name}: checkout is $ahead commit(s) ahead of origin/main." >&2
            exit 1
          fi
          if [ "$behind" -eq 0 ]; then
            exit 0
          fi

          # Unlike the updater's best-effort internal snapshot, this explicit
          # quick backup is a hard gate: no successful backup, no update.
          backup_label="auto-update-$(/bin/date -u +%Y%m%dT%H%M%SZ)"
          as_user ${lib.escapeShellArg updaterRuntimePython} - "$backup_label" <<'PY'
          import sys
          from hermes_cli.backup import create_quick_snapshot

          snapshot_id = create_quick_snapshot(label=sys.argv[1])
          if not snapshot_id:
              raise SystemExit("Hermes pre-update state snapshot produced no backup")
          print(f"Hermes pre-update state snapshot: {snapshot_id}")
          PY

          ${lib.optionalString (supervisedServiceLabel != null) ''
            # A system LaunchDaemon would immediately respawn a stopped serve
            # process during the code swap. Unload it first, then restore it on
            # every exit path. The updater itself still runs as the instance user.
            if /bin/launchctl print system/${lib.escapeShellArg supervisedServiceLabel} >/dev/null 2>&1; then
              /bin/launchctl bootout system/${lib.escapeShellArg supervisedServiceLabel}
            fi
            service_stopped=1
          ''}

          as_user ${lib.escapeShellArg updaterRuntimePython} -m hermes_cli.main update --yes --no-backup --keep-stash

          ${lib.optionalString (supervisedServiceLabel != null) ''
            /bin/launchctl bootstrap system ${lib.escapeShellArg servicePlist}
            service_stopped=0
          ''}

          if [ -n "$(as_user ${pkgs.git}/bin/git -C ${lib.escapeShellArg installDirectory} status --porcelain)" ]; then
            echo "Hermes update left ${name}'s managed checkout dirty." >&2
            exit 1
          fi
          counts=$(as_user ${pkgs.git}/bin/git -C ${lib.escapeShellArg installDirectory} rev-list --left-right --count HEAD...origin/main)
          ahead="''${counts%%[[:space:]]*}"
          behind="''${counts##*[[:space:]]}"
          if [ "$ahead" -ne 0 ] || [ "$behind" -ne 0 ]; then
            echo "Hermes update left ${name}'s checkout out of sync with origin/main." >&2
            exit 1
          fi

          as_user ${lib.escapeShellArg updaterRuntimePython} - ${lib.escapeShellArg healthUrl} <<'PY'
          import json
          import sys
          import time
          import urllib.request
          from importlib.metadata import version

          expected = version("hermes-agent")
          url = sys.argv[1]
          error = None
          for _ in range(60):
              try:
                  with urllib.request.urlopen(url, timeout=5) as response:
                      actual = str(json.load(response).get("version", ""))
                  if actual == expected:
                      print(f"Hermes backend healthy at {url} (v{actual})")
                      raise SystemExit(0)
                  error = f"running v{actual or 'unknown'}, expected v{expected}"
              except Exception as exc:
                  error = str(exc)
              time.sleep(1)
          raise SystemExit(f"Hermes backend verification failed at {url}: {error}")
          PY
        '';
      primaryUpdater = mkHermesUpdater {
        name = cfg.user;
        user = cfg.user;
        updaterHome = homeDirectory;
        updaterRuntimePython = runtimePython;
        healthUrl = "http://${cfg.dashboard.host}:${toString cfg.dashboard.port}/api/status";
      };
      headlessServeDaemons = lib.mapAttrs'
        (name: instance:
          lib.nameValuePair "hermes-${name}-serve" {
            serviceConfig = {
              Label = "ai.hermes.serve-${name}";
              UserName = instance.user;
              ProgramArguments = waitForManagedProgram [
                (headlessRuntimePython instance)
                "-m"
                "hermes_cli.main"
                "serve"
                "--host"
                instance.serve.host
                "--port"
                (toString instance.serve.port)
                "--skip-build"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = instance.homeDirectory;
              EnvironmentVariables = headlessEnvironment instance;
              StandardOutPath = "${instance.homeDirectory}/.hermes/logs/serve.log";
              StandardErrorPath = "${instance.homeDirectory}/.hermes/logs/serve.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          })
        enabledHeadlessServeInstances;
      headlessGatewayDaemons = lib.mapAttrs'
        (name: instance:
          lib.nameValuePair "hermes-${name}-gateway" {
            serviceConfig = {
              Label = "ai.hermes.gateway-${name}";
              UserName = instance.user;
              ProgramArguments = waitForManagedProgram [
                (headlessRuntimePython instance)
                "-m"
                "hermes_cli.main"
                "gateway"
                "run"
                "--replace"
                "--external-supervisor"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = instance.homeDirectory;
              EnvironmentVariables = headlessEnvironment instance;
              StandardOutPath = "${instance.homeDirectory}/.hermes/logs/gateway.log";
              StandardErrorPath = "${instance.homeDirectory}/.hermes/logs/gateway.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          })
        enabledHeadlessGatewayInstances;
      headlessTailscaleProxyDaemons = lib.mapAttrs'
        (name: instance:
          let
            caddyConfig = pkgs.writeText "hermes-${name}-tailscale-proxy.json" (builtins.toJSON {
              admin.disabled = true;
              apps.http.servers.hermes = {
                listen = [ "127.0.0.1:${toString instance.serve.tailscale.proxyPort}" ];
                automatic_https.disable = true;
                routes = [
                  {
                    handle = [
                      {
                        handler = "reverse_proxy";
                        headers.request.set.Host = [ "127.0.0.1:${toString instance.serve.port}" ];
                        upstreams = [{ dial = "127.0.0.1:${toString instance.serve.port}"; }];
                      }
                    ];
                  }
                ];
              };
            });
          in
          lib.nameValuePair "hermes-${name}-tailscale-proxy" {
            serviceConfig = {
              Label = "ai.hermes.serve-${name}-tailscale-proxy";
              UserName = instance.user;
              ProgramArguments = waitForNixStoreProgram [
                "${pkgs.caddy}/bin/caddy"
                "run"
                "--config"
                "${caddyConfig}"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = instance.homeDirectory;
              EnvironmentVariables = headlessEnvironment instance;
              StandardOutPath = "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.log";
              StandardErrorPath = "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          })
        enabledTailscaleServeInstances;
      headlessUpdateDaemons = lib.mapAttrs'
        (name: instance:
          let
            updater = mkHermesUpdater {
              inherit name;
              user = instance.user;
              updaterHome = instance.homeDirectory;
              updaterRuntimePython = headlessRuntimePython instance;
              healthUrl = "http://${instance.serve.host}:${toString instance.serve.port}/api/status";
              supervisedServiceLabel = "ai.hermes.serve-${name}";
            };
          in
          lib.nameValuePair "hermes-${name}-updater" {
            serviceConfig = {
              Label = "ai.hermes.update-${name}";
              ProgramArguments = [ "${updater}" ];
              StartCalendarInterval = instance.autoUpdate.calendar;
              StandardOutPath = "${headlessUpdateLogDirectory}/${name}.log";
              StandardErrorPath = "${headlessUpdateLogDirectory}/${name}.error.log";
              ProcessType = "Background";
            };
          })
        enabledHeadlessUpdateInstances;
      primaryTailscaleProxyConfig = pkgs.writeText "hermes-dashboard-tailscale-proxy.json" (builtins.toJSON {
        admin.disabled = true;
        apps.http.servers.hermes = {
          listen = [ "127.0.0.1:${toString cfg.dashboard.tailscale.proxyPort}" ];
          automatic_https.disable = true;
          routes = [
            {
              handle = [
                {
                  handler = "reverse_proxy";
                  headers.request.set = {
                    Host = [ "${cfg.dashboard.host}:${toString cfg.dashboard.port}" ];
                    "X-Forwarded-Proto" = [ "https" ];
                  };
                  upstreams = [{ dial = "${cfg.dashboard.host}:${toString cfg.dashboard.port}"; }];
                }
              ];
            }
          ];
        };
      });
      serviceEnvironment = {
        HOME = homeDirectory;
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

        secureHome = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Restrict the primary Hermes user's home directory to mode 0700.";
        };

        headlessInstances = lib.mkOption {
          type = lib.types.attrsOf (lib.types.submodule headlessInstanceModule);
          default = { };
          description = ''
            Additional per-user Hermes instances supervised as LaunchDaemons.
            These start at boot without a GUI login and execute from isolated
            per-user managed Hermes installations.
          '';
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

        autoUpdate = {
          enable = lib.mkOption {
            type = lib.types.bool;
            default = false;
            description = "Update the primary managed Hermes runtime with a guarded launchd job.";
          };
          calendar = lib.mkOption {
            type = lib.types.attrsOf lib.types.int;
            default = { Hour = 4; Minute = 0; };
            description = "launchd StartCalendarInterval for primary managed Hermes updates.";
          };
        };

        gateway.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Run the canonical Hermes messaging gateway at login.";
        };

        dashboard = {
          enable = lib.mkOption {
            type = lib.types.bool;
            default = false;
            description = "Expose the authenticated Hermes browser dashboard and remote backend through launchd.";
          };
          host = lib.mkOption {
            type = lib.types.str;
            default = "127.0.0.1";
            description = "Address passed to hermes dashboard; use a Tailscale address for remote clients.";
          };
          port = lib.mkOption {
            type = lib.types.port;
            default = 9119;
            description = "Port passed to hermes dashboard.";
          };
          tailscale = {
            enable = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Expose the authenticated primary dashboard through Tailscale Serve HTTPS.";
            };
            httpsPort = lib.mkOption {
              type = lib.types.port;
              default = 443;
              description = "Tailnet-only HTTPS port exposed by Tailscale Serve.";
            };
            proxyPort = lib.mkOption {
              type = lib.types.port;
              default = cfg.dashboard.port + 1;
              description = "Loopback reverse-proxy port used to bridge Tailscale Serve HTTPS to the authenticated Hermes dashboard.";
            };
          };
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          environment.systemPackages = [ cfg.package ]
            ++ lib.optional cfg.desktop.enable upstreamDesktop;

          assertions = [
            {
              assertion = builtins.length (localServePorts ++ localProxyPorts)
                == builtins.length (lib.unique (localServePorts ++ localProxyPorts));
              message = "services.hermes dashboard/serve/proxy ports must be unique";
            }
            {
              assertion = builtins.length tailscaleHttpsPorts
                == builtins.length (lib.unique tailscaleHttpsPorts);
              message = "services.hermes Tailscale Serve HTTPS ports must be unique";
            }
            {
              assertion = !cfg.dashboard.tailscale.enable
                || (cfg.dashboard.enable
                && cfg.dashboard.host != "127.0.0.1"
                && cfg.dashboard.host != "localhost"
                && cfg.dashboard.host != "::1"
                && config.services.tailscale.enable);
              message = "services.hermes.dashboard.tailscale requires an authenticated non-loopback dashboard host and services.tailscale";
            }
          ] ++ lib.mapAttrsToList
            (name: instance: {
              assertion = instance.gateway.enable || instance.serve.enable;
              message = "services.hermes.headlessInstances.${name} must enable gateway or serve";
            })
            cfg.headlessInstances
          ++ lib.mapAttrsToList
            (name: instance: {
              assertion = !instance.autoUpdate.enable || instance.serve.enable;
              message = "services.hermes.headlessInstances.${name}.autoUpdate requires serve.enable";
            })
            cfg.headlessInstances
          ++ lib.mapAttrsToList
            (name: instance: {
              assertion = !instance.serve.tailscale.enable
                || (instance.serve.enable
                && instance.serve.host == "127.0.0.1"
                && config.services.tailscale.enable);
              message = "services.hermes.headlessInstances.${name}.serve.tailscale requires a loopback serve host and services.tailscale";
            })
            cfg.headlessInstances;

          launchd.daemons = headlessServeDaemons
            // headlessGatewayDaemons
            // headlessTailscaleProxyDaemons
            // headlessUpdateDaemons;

          system.activationScripts.extraActivation.text = lib.mkAfter (
            lib.optionalString cfg.secureHome ''
              # === Hermes primary-user home isolation ===
              /bin/chmod 0700 ${lib.escapeShellArg homeDirectory}
            ''
            + lib.optionalString (enabledHeadlessUpdateInstances != { }) ''
              # === Root-owned Hermes updater logs ===
              /bin/mkdir -p ${lib.escapeShellArg headlessUpdateLogDirectory}
              ${lib.concatMapStrings
                (name: ''
                  /usr/bin/touch \
                    ${lib.escapeShellArg "${headlessUpdateLogDirectory}/${name}.log"} \
                    ${lib.escapeShellArg "${headlessUpdateLogDirectory}/${name}.error.log"}
                '')
                (lib.attrNames enabledHeadlessUpdateInstances)}
              /usr/sbin/chown -R root:wheel ${lib.escapeShellArg headlessUpdateLogDirectory}
              /bin/chmod 0750 ${lib.escapeShellArg headlessUpdateLogDirectory}
              /bin/chmod 0640 ${lib.escapeShellArg headlessUpdateLogDirectory}/*.log
            ''
            + lib.concatMapStrings
              (instance: ''
                # === Hermes headless instance: ${instance.user} ===
                ${lib.optionalString instance.secureHome "/bin/chmod 0700 ${lib.escapeShellArg instance.homeDirectory}"}
                ${lib.optionalString instance.gateway.enable ''
                  # Remove the per-user service created by `hermes gateway
                  # install`; the Nix-owned system LaunchDaemon is canonical.
                  legacy_uid=$(/usr/bin/id -u ${lib.escapeShellArg instance.user} 2>/dev/null || true)
                  if [ -n "$legacy_uid" ]; then
                    /bin/launchctl bootout "user/$legacy_uid/ai.hermes.gateway" >/dev/null 2>&1 || true
                    /bin/launchctl bootout "gui/$legacy_uid/ai.hermes.gateway" >/dev/null 2>&1 || true
                  fi
                  /bin/rm -f ${lib.escapeShellArg "${instance.homeDirectory}/Library/LaunchAgents/ai.hermes.gateway.plist"}
                ''}
                /bin/mkdir -p ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs"}
                /usr/bin/touch \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.error.log"}
                /usr/sbin/chown ${lib.escapeShellArg instance.user}:staff \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.error.log"}
                /bin/chmod 0700 ${lib.escapeShellArg "${instance.homeDirectory}/.hermes"}
                /bin/chmod 0600 ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/"}*.log
              '')
              (lib.attrValues enabledHeadlessInstances)
            + lib.optionalString primaryTailscaleServeEnabled ''
              # === Hermes primary dashboard via Tailscale Serve ===
              if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
                ${config.services.tailscale.package}/bin/tailscale serve \
                  --bg \
                  --yes \
                  --https=${toString cfg.dashboard.tailscale.httpsPort} \
                  http://127.0.0.1:${toString cfg.dashboard.tailscale.proxyPort}; then
                echo "Failed to configure Tailscale Serve for the primary Hermes dashboard on port ${toString cfg.dashboard.tailscale.httpsPort}" >&2
                exit 1
              fi
            ''
            + lib.concatMapStrings
              (instance: lib.optionalString instance.serve.tailscale.enable ''
                # === Hermes headless backend via Tailscale Serve ===
                if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
                  ${config.services.tailscale.package}/bin/tailscale serve \
                    --bg \
                    --yes \
                    --https=${toString instance.serve.tailscale.httpsPort} \
                    http://127.0.0.1:${toString instance.serve.tailscale.proxyPort}; then
                  echo "Failed to configure Tailscale Serve for Hermes on port ${toString instance.serve.tailscale.httpsPort}" >&2
                  exit 1
                fi
              '')
              (lib.attrValues enabledHeadlessServeInstances)
          );
        }

        (lib.mkIf (cfg.gateway.enable || cfg.dashboard.enable) {
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
              assertion = !cfg.gateway.enable && !cfg.dashboard.enable;
              message = "services.hermes.clientOnly conflicts with gateway.enable or dashboard.enable";
            }
            {
              assertion = enabledHeadlessInstances == { };
              message = "services.hermes.clientOnly conflicts with headlessInstances";
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
              /bin/launchctl bootout "user/$hermes_uid/ai.hermes.dashboard" >/dev/null 2>&1 || true
              /bin/launchctl bootout "gui/$hermes_uid/ai.hermes.dashboard" >/dev/null 2>&1 || true
            fi
            /bin/rm -f \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.gateway.plist"} \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.serve.plist"} \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.dashboard.plist"}
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
                "--external-supervisor"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = homeDirectory;
              EnvironmentVariables = serviceEnvironment;
              StandardOutPath = "${hermesHome}/logs/gateway.log";
              StandardErrorPath = "${hermesHome}/logs/gateway.error.log";
              SoftResourceLimits.NumberOfFiles = 65536;
              HardResourceLimits.NumberOfFiles = 65536;
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          };
        })

        (lib.mkIf cfg.autoUpdate.enable {
          assertions = [
            {
              assertion = cfg.gateway.enable || cfg.dashboard.enable;
              message = "services.hermes.autoUpdate requires a primary gateway or dashboard";
            }
          ];

          launchd.user.agents.hermes-updater = {
            serviceConfig = {
              Label = "ai.hermes.update";
              ProgramArguments = [ "${primaryUpdater}" ];
              StartCalendarInterval = cfg.autoUpdate.calendar;
              StandardOutPath = "/tmp/hermes-${cfg.user}-updater.log";
              StandardErrorPath = "/tmp/hermes-${cfg.user}-updater.error.log";
              ProcessType = "Background";
            };
          };
        })

        (lib.mkIf cfg.dashboard.enable {
          launchd.user.agents.hermes-dashboard = {
            serviceConfig = {
              Label = "ai.hermes.dashboard";
              ProgramArguments = [
                runtimePython
                "-m"
                "hermes_cli.main"
                "dashboard"
                "--host"
                cfg.dashboard.host
                "--port"
                (toString cfg.dashboard.port)
                "--no-open"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = homeDirectory;
              EnvironmentVariables = serviceEnvironment;
              StandardOutPath = "${hermesHome}/logs/dashboard.log";
              StandardErrorPath = "${hermesHome}/logs/dashboard.error.log";
              SoftResourceLimits.NumberOfFiles = 65536;
              HardResourceLimits.NumberOfFiles = 65536;
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          };
        })

        (lib.mkIf primaryTailscaleServeEnabled {
          launchd.user.agents.hermes-dashboard-tailscale-proxy = {
            serviceConfig = {
              Label = "ai.hermes.dashboard-tailscale-proxy";
              ProgramArguments = [
                "${pkgs.caddy}/bin/caddy"
                "run"
                "--config"
                "${primaryTailscaleProxyConfig}"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = homeDirectory;
              EnvironmentVariables = serviceEnvironment;
              StandardOutPath = "${hermesHome}/logs/dashboard-tailscale-proxy.log";
              StandardErrorPath = "${hermesHome}/logs/dashboard-tailscale-proxy.error.log";
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
