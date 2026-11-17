# The voice service (bryan/pi-bots-voice): a Python service on Pipecat that
# hears speech, hands each turn to a Pi bot and speaks the reply. It listens on
# loopback only and is reached from the phone through Tailscale Serve.
#
# Like pi-bots it runs from a checkout, not the store: a code change there
# needs `cd service && uv sync --frozen` and a restart of this agent
# (`launchctl kickstart -k gui/$(id -u)/org.nixos.pi-bots-voice`), not a
# rebuild. Its settings are the checkout's `config.local.yaml` (ignored by
# Git), read by the service itself: its port, its Hugging Face cache and
# whether it is offline, its agents. This module sets none of them.
#
# The interpreter is the checkout's own `service/.venv/bin/python`, never
# `uv run` or Homebrew's Python: WebRTC audio arrives over UDP on the Mac's
# own addresses, and the macOS application firewall lets it through only for
# an interpreter it has been told to allow. That interpreter is uv's Python,
# whose path changes when uv updates it, so each activation resolves the
# venv's interpreter and allows it again.
{ inputs, ... }:
{
  flake.modules.darwin.pi-bots-voice = { config, lib, options, pkgs, ... }:
    let
      cfg = config.services.pi-bots-voice;
      homeDir = "/Users/${config.system.primaryUser}";
      python = "${cfg.checkout}/service/.venv/bin/python";
      firewall = "/usr/libexec/ApplicationFirewall/socketfilterfw";
    in
    {
      options.services.pi-bots-voice = {
        enable = lib.mkEnableOption "the voice service (bryan/pi-bots-voice) as a launchd user agent";

        checkout = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/code/pi-bots-voice";
          defaultText = lib.literalExpression ''"/Users/''${config.system.primaryUser}/code/pi-bots-voice"'';
          description = "The pi-bots-voice checkout the service runs from; its `service/.venv` must be synced (`uv sync --frozen`).";
        };

        port = lib.mkOption {
          type = lib.types.port;
          default = 7860;
          description = ''
            The service's loopback port, used here only for the Serve route and
            the content probe. The service reads its own port from its config
            (`server.port`, default 7860), so the two must agree.
          '';
        };

        tailscaleHttpsPort = lib.mkOption {
          type = lib.types.nullOr lib.types.port;
          default = null;
          example = 7860;
          description = ''
            Tailnet-only HTTPS port that Tailscale Serve forwards to the
            service (never Funnel). Requires services.tailscale.
          '';
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          assertions = [
            {
              assertion = cfg.tailscaleHttpsPort == null || config.services.tailscale.enable;
              message = "services.pi-bots-voice.tailscaleHttpsPort requires services.tailscale";
            }
          ];

          launchd.user.agents.pi-bots-voice.serviceConfig = {
            ProgramArguments = [ python "-m" "pi_bots_voice" ];
            WorkingDirectory = "${cfg.checkout}/service";
            EnvironmentVariables = {
              HOME = homeDir;
              # The plain-Pi agent starts `pi`, which needs Homebrew's node.
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
            };
            RunAtLoad = true;
            KeepAlive = true;
            # A broken checkout or venv fails at once; don't spin on it.
            ThrottleInterval = 30;
            StandardOutPath = "/tmp/pi-bots-voice.log";
            StandardErrorPath = "/tmp/pi-bots-voice.error.log";
          };

          system.activationScripts.extraActivation.text = lib.mkAfter (''

            # === pi-bots-voice: firewall exception for the venv's interpreter ===
            pi_bots_voice_python=$(${pkgs.coreutils}/bin/realpath -e ${lib.escapeShellArg python} 2>/dev/null || true)
            if [ -n "$pi_bots_voice_python" ]; then
              ${firewall} --add "$pi_bots_voice_python" >/dev/null 2>&1 || true
              ${firewall} --unblockapp "$pi_bots_voice_python" >/dev/null 2>&1 || true
            else
              echo "pi-bots-voice: no interpreter at ${python} (run uv sync --frozen in the checkout's service directory); firewall left as it is" >&2
            fi
          '' + lib.optionalString (cfg.tailscaleHttpsPort != null) ''

            # === pi-bots-voice: service via Tailscale Serve ===
            if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
              ${config.services.tailscale.package}/bin/tailscale serve \
                --bg \
                --yes \
                --https=${toString cfg.tailscaleHttpsPort} \
                http://127.0.0.1:${toString cfg.port}; then
              echo "Failed to configure Tailscale Serve for pi-bots-voice on port ${toString cfg.tailscaleHttpsPort}" >&2
              exit 1
            fi
          '');
        }

        # Only hosts that import the monitoring module have this option. A
        # loopback request needs no login; the status always names its state.
        (lib.optionalAttrs (options.services ? monitoring) {
          services.monitoring.blackbox.contentProbes."Pi Bots Voice" = {
            url = "http://127.0.0.1:${toString cfg.port}/v1/status";
            bodyMatches = "\"state\":\"(ready|loading)\"";
          };
        })
      ]);
    };

  # NixOS aspect - not implemented (the voice service runs on the Studio only).
  flake.modules.nixos.pi-bots-voice = { ... }: {
    # TODO: systemd user service equivalent if a NixOS host ever runs the voice service.
  };
}
