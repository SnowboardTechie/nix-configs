# The bot service (bryan/pi-bots): one long-running process that holds every
# bot's Matrix account, bridges the bots' rooms to their Pis, runs the job
# clock and keeps the `bots` Rex session (one tab per bot) alive.
#
# Like Pi Durable's agents it runs from a checkout, not the store: a code
# change there needs a restart of this agent (`launchctl kickstart -k`), not a
# rebuild. It runs on Homebrew's node, so base/homebrew.nix re-pins (and so
# restarts) it on every activation; the service stops cleanly on SIGTERM and
# the bots' Pis keep running in their Rex tabs meanwhile.
#
# One service per state directory: the service holds `<stateDir>/service.lock`
# and a second one exits at once. KeepAlive then retries it at launchd's
# throttle, so this agent takes over within seconds of another copy stopping.
#
# Health: the service writes pi-bots.prom to `metricsDir` every 15 s and once
# more as it stops. With the monitoring module on the host, node_exporter reads
# it and the rules below alert on the service, its bots, their accounts and
# unanswered messages.
#
# The command: `pi-bots` in the system profile (/run/current-system/sw/bin, on
# the PATH of Pi Durable's agents, this one and interactive shells) runs the
# checkout's command line on Homebrew's node. It sets PI_BOTS_HOMESERVER and
# PI_BOTS_STATE_DIR to this service's unless the caller has, so a caller that
# passes only PATH and HOME (Pi Durable's notifier) reaches the running
# service's socket and needs no --homeserver.
{ inputs, ... }:
{
  flake.modules.darwin.pi-bots = { config, lib, options, pkgs, ... }:
    let
      cfg = config.services.pi-bots;
      command = pkgs.writeShellScriptBin "pi-bots" ''
        if [ -z "''${PI_BOTS_HOMESERVER:-}" ]; then PI_BOTS_HOMESERVER=${lib.escapeShellArg cfg.homeserver}; fi
        if [ -z "''${PI_BOTS_STATE_DIR:-}" ]; then PI_BOTS_STATE_DIR=${lib.escapeShellArg cfg.stateDir}; fi
        export PI_BOTS_HOMESERVER PI_BOTS_STATE_DIR
        exec ${lib.escapeShellArg "${config.homebrew.prefix}/bin/node"} ${lib.escapeShellArg "${cfg.checkout}/src/cli.ts"} "$@"
      '';
      homeDir = "/Users/${config.system.primaryUser}";
      logFile = "/tmp/pi-bots.log";
      errorLog = "/tmp/pi-bots.error.log";
      # A series about a bot or an account is only current while the service
      # writes the file: after a clean stop it says so, and after a crash the
      # file goes stale. Either way the service rules speak for them.
      serviceUp = "and on (instance, job) (pi_bots_service_up == 1 and time() - pi_bots_metrics_written_timestamp_seconds < 120)";

      rules = [
        {
          alert = "PiBotsServiceDown";
          expr = "pi_bots_service_up == 0";
          "for" = "5m";
          labels = { severity = "critical"; };
          annotations = {
            summary = "The bot service is down";
            description = "The bot service stopped cleanly and has not come back for 5 minutes: no bot answers in Matrix and no job runs. Check `launchctl print gui/$(id -u)/org.nixos.pi-bots` and ${errorLog}.";
          };
        }
        {
          # A crash writes nothing, so the file goes stale instead. A clean
          # stop is PiBotsServiceDown's.
          alert = "PiBotsServiceSilent";
          expr = "(time() - pi_bots_metrics_written_timestamp_seconds > 120 unless on (instance, job) pi_bots_service_up == 0) or absent(pi_bots_metrics_written_timestamp_seconds)";
          "for" = "5m";
          labels = { severity = "critical"; };
          annotations = {
            summary = "The bot service has stopped reporting";
            description = "The bot service has not written its health file in over 2 minutes (it writes every 15 s), so it has crashed or hangs: no bot answers in Matrix and no job runs. See ${errorLog}.";
          };
        }
        {
          # The product is 0 when the bot's runner (its Rex tab) or its Pi is down.
          alert = "PiBotsBotDown";
          expr = "pi_bots_bot_runner_up * pi_bots_bot_up == 0 ${serviceUp}";
          "for" = "5m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "Bot {{ $labels.bot }} is down";
            description = "Bot {{ $labels.bot }}'s Pi has not answered on its control socket for 5 minutes, though the service keeps restarting it. Look at its tab in the `bots` Rex session.";
          };
        }
        {
          # A Pi that keeps dying is restarted by its runner, so it only
          # flaps and PiBotsBotDown's `for` never runs out. Down for most of
          # 15 minutes and dropping more than once (resets counts each 1 -> 0)
          # is a crash loop; one outage, ended or not, is PiBotsBotDown's.
          alert = "PiBotsBotCrashLooping";
          expr = "avg_over_time(pi_bots_bot_up[15m]) < 0.5 and resets(pi_bots_bot_up[15m]) > 1 ${serviceUp}";
          labels = { severity = "warning"; };
          annotations = {
            summary = "Bot {{ $labels.bot }} keeps restarting";
            description = "Bot {{ $labels.bot }}'s Pi was down for most of the last 15 minutes, coming back and dying again. Look at its tab in the `bots` Rex session.";
          };
        }
        {
          alert = "PiBotsAccountNotSyncing";
          expr = "pi_bots_account_syncing == 0 ${serviceUp}";
          "for" = "10m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "Bot {{ $labels.bot }} is not syncing with the homeserver";
            description = "Bot {{ $labels.bot }}'s Matrix account has not finished a sync for over 10 minutes, so it neither sees nor answers messages. If every bot shows this, the homeserver is unreachable.";
          };
        }
        {
          alert = "PiBotsMessagesWaiting";
          expr = "pi_bots_bridge_messages{status=\"received\"} > 0 ${serviceUp}";
          "for" = "30m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "Messages to bot {{ $labels.bot }} are waiting";
            description = "Messages from Bryan to bot {{ $labels.bot }} have been waiting for an answer for 30 minutes ({{ $value | printf \"%.0f\" }} waiting now). The bridge retries while the bot's Pi or the homeserver is down.";
          };
        }
      ];
    in
    {
      options.services.pi-bots = {
        enable = lib.mkEnableOption "the bot service (bryan/pi-bots) as a launchd user agent";

        checkout = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/code/pi-bots";
          description = "The pi-bots checkout the service runs from (its main branch).";
        };

        homeserver = lib.mkOption {
          type = lib.types.strMatching "https://[A-Za-z0-9.-]+(:[0-9]+)?/?";
          example = "https://matrix.example.org";
          description = "The Matrix homeserver's client URL (PI_BOTS_HOMESERVER).";
        };

        stateDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/.local/state/pi-bots";
          description = "The service's state directory (key stores, journals, sessions, locks).";
        };

        metricsDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${cfg.stateDir}/metrics";
          defaultText = lib.literalExpression ''"''${stateDir}/metrics"'';
          description = "Directory the service writes pi-bots.prom into for node_exporter's textfile collector.";
        };

        package = lib.mkOption {
          type = lib.types.package;
          readOnly = true;
          default = command;
          defaultText = lib.literalMD "a `pi-bots` wrapper running the checkout's `src/cli.ts` on Homebrew's node";
          description = ''
            The `pi-bots` command, installed in the system profile
            (/run/current-system/sw/bin/pi-bots: the absolute path for callers
            under launchd). It defaults PI_BOTS_HOMESERVER and PI_BOTS_STATE_DIR
            to this service's, so it reaches the running service.
          '';
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          assertions = [
            {
              assertion = config.services.pi-bots-backup.enable or false
                -> config.services.pi-bots-backup.stateDir == cfg.stateDir;
              message = "services.pi-bots.stateDir must be the directory services.pi-bots-backup backs up.";
            }
          ];

          environment.systemPackages = [ cfg.package ];

          launchd.user.agents.pi-bots.serviceConfig = {
            ProgramArguments = [
              "${config.homebrew.prefix}/bin/node"
              "${cfg.checkout}/src/cli.ts"
              "service"
              "run"
            ];
            WorkingDirectory = cfg.checkout;
            EnvironmentVariables = {
              HOME = homeDir;
              # Job scripts run with this PATH. The bots' tabs get the login
              # shell's from Rex, and the Rex command line is found by pi-bots.
              PATH = lib.concatStringsSep ":" [
                "${homeDir}/.local/bin"
                "/run/current-system/sw/bin"
                "${config.homebrew.prefix}/bin"
                "/usr/local/bin"
                "/usr/bin"
                "/bin"
                "/usr/sbin"
                "/sbin"
              ];
              PI_BOTS_HOMESERVER = cfg.homeserver;
              PI_BOTS_STATE_DIR = cfg.stateDir;
              PI_BOTS_METRICS_DIR = cfg.metricsDir;
            };
            RunAtLoad = true;
            KeepAlive = true;
            # A stop ends running job scripts (SIGTERM, SIGKILL 5 s later) and
            # writes the last health file before exiting.
            ExitTimeOut = 30;
            StandardOutPath = logFile;
            StandardErrorPath = errorLog;
          };
        }

        # Only hosts that import the monitoring module have these options.
        (lib.optionalAttrs (options.services ? monitoring) {
          services.monitoring = {
            nodeExporter.textfileDirectories = [ cfg.metricsDir ];
            extraAlertRules = rules;
          };
        })
      ]);
    };

  # NixOS aspect - not implemented (the bot service runs on the Studio only).
  flake.modules.nixos.pi-bots = { ... }: {
    # TODO: systemd user service equivalent if a NixOS host ever runs the bot service.
  };
}
