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
# it and the rules below alert on the service, its bots, their accounts,
# unanswered messages, the job clock and failed jobs. With
# `alerts.sggPreviewWatchdog`, they also cover the SGG preview watchdog, a job
# whose script writes sgg-preview-watchdog.prom into the same directory.
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

        # The job clock (pi-bots src/clock.ts) ticks every 10 s and records
        # the time of its last good tick; the file is written every 15 s. A
        # stale tick in a fresh file is a clock that has stopped while the
        # rest of the service runs. 0 means no tick since the service started.
        {
          alert = "PiBotsClockSilent";
          expr = "time() - pi_bots_clock_last_tick_timestamp_seconds > 60 ${serviceUp}";
          "for" = "5m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The bot service's job clock has stopped";
            description = "The bot service runs, but its job clock has not completed a tick for over 6 minutes (it ticks every 10 s), so no scheduled job runs. See ${errorLog}.";
          };
        }
        {
          # Kept across restarts (clock.db) and cleared by the job's next run
          # that does not fail. The `for` usually lets a job that runs every
          # 5 minutes fail once without an alert (not when its next run is a
          # long one); a daily job's failure stands until its next run. A job
          # disabled after a failure keeps its last run, so the alert stays
          # until its file is removed or it runs again.
          alert = "PiBotsJobFailed";
          expr = "pi_bots_job_last_run_failed == 1 ${serviceUp}";
          "for" = "10m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "Job {{ $labels.job_id }} failed";
            description = "The latest run of the bot service's job {{ $labels.job_id }} failed. `pi-bots job runs {{ $labels.job_id }}` shows why. The alert clears when its next run does not fail.";
          };
        }
        {
          alert = "PiBotsJobWaiting";
          expr = "pi_bots_job_waiting == 1 ${serviceUp}";
          "for" = "30m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "Job {{ $labels.job_id }} is not running";
            description = "The bot service has held job {{ $labels.job_id }} back for 30 minutes because it does not bridge the job's bot (no account, or its key store could not be opened). See ${errorLog}.";
          };
        }
        {
          alert = "PiBotsJobFileUnusable";
          expr = "pi_bots_clock_bad_job_files > 0 ${serviceUp}";
          "for" = "15m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "A job file cannot be used";
            description = "The bot service's clock cannot use {{ $value | printf \"%.0f\" }} job file(s), so those jobs never run. `pi-bots job list` names them and why.";
          };
        }
      ];

      # The SGG preview watchdog (pi-bots `jobs/sgg-preview-watchdog.json`,
      # every 5 minutes, no bot) restarts persistent previews that fail and
      # writes what it found to sgg-preview-watchdog.prom in metricsDir.
      watchdogMetric = "sgg_preview_watchdog_last_run_timestamp_seconds";
      # A gate for a series with no instance label (an absent() result).
      serviceUpAnywhere = "and on () (pi_bots_service_up == 1 and time() - pi_bots_metrics_written_timestamp_seconds < 120)";
      # The watchdog's file outlives the job: once it is stale its other
      # series are old news, and SggPreviewWatchdogSilent speaks for them.
      watchdogFresh = "and on (instance, job) (time() - ${watchdogMetric} <= 1200)";
      watchdogRules = [
        {
          # One run may take up to about 14 minutes (the watchdog's own limit
          # and the preview list's), and the clock skips a slot while it runs,
          # so run ends can be about 19 minutes apart; a gap of 20 minutes is
          # a job that has stopped. A stopped service or clock has its own
          # alert, so this holds only while the service reports itself up and
          # the clock ticks.
          alert = "SggPreviewWatchdogSilent";
          expr = "((time() - ${watchdogMetric} > 1200 ${serviceUp}) or (absent(${watchdogMetric}) ${serviceUpAnywhere})) unless on () (time() - pi_bots_clock_last_tick_timestamp_seconds > 60)";
          "for" = "5m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The SGG preview watchdog has stopped running";
            description = "The SGG preview watchdog (the bot service's job sgg-preview-watchdog, every 5 minutes) has not finished a run for over 25 minutes, so failed persistent previews are not restarted. `pi-bots job runs sgg-preview-watchdog` shows its runs.";
          };
        }
        {
          alert = "SggPreviewWatchdogBlocked";
          expr = "sgg_preview_watchdog_blocked == 1 ${watchdogFresh}";
          "for" = "30m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The SGG preview watchdog is blocked ({{ $labels.reason }})";
            description = "For 30 minutes the SGG preview watchdog has been waiting for Tailscale (reason tailscale) or unable to read the preview registry (reason registry), so it restarts nothing.";
          };
        }
        {
          # A preview reads failed or expired for a moment while the watchdog
          # restarts it, hence the `for`. Written only while the preview list
          # could be read; SggPreviewWatchdogBlocked and PiBotsJobFailed cover
          # the times it could not. keep_firing_for rides out a short gap in
          # the list rather than sending a resolved notice for a preview that
          # is still broken; a longer one is the other alerts'.
          alert = "SggPreviewNeedsAttention";
          expr = "sgg_preview_persistent_preview_needs_attention == 1 ${watchdogFresh}";
          "for" = "15m";
          keep_firing_for = "15m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "SGG preview {{ $labels.preview }} needs attention ({{ $labels.status }})";
            description = "Persistent SGG preview {{ $labels.preview }} has been {{ $labels.status }} for 15 minutes and the watchdog has not brought it back. cleanup_failed needs manual recovery; see the sgg-preview checkout's runbook.";
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

        alerts.sggPreviewWatchdog = lib.mkEnableOption ''
          alert rules for the SGG preview watchdog, a pi-bots job whose script
          writes sgg-preview-watchdog.prom into metricsDir (needs the
          monitoring module)'';

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
            extraAlertRules = rules ++ lib.optionals cfg.alerts.sggPreviewWatchdog watchdogRules;
          };
        })
      ]);
    };

  # NixOS aspect - not implemented (the bot service runs on the Studio only).
  flake.modules.nixos.pi-bots = { ... }: {
    # TODO: systemd user service equivalent if a NixOS host ever runs the bot service.
  };
}
