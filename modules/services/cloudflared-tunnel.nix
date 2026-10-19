# Cloudflare Tunnel connector for a remotely managed tunnel.
#
# Runs `cloudflared tunnel run --token-file <tokenFile>` as a root LaunchDaemon
# (org.nixos.cloudflared-tunnel). The token lives only in a root-owned 0600
# file: never in the store, the plist or the process arguments. This replaces
# `cloudflared service install <token>`, which writes the token into a
# world-readable /Library/LaunchDaemons plist and into argv, where every local
# account (and anything running as one) can read it and run its own connector.
#
# Ingress rules (which hostnames reach which local origin) are managed in the
# Cloudflare dashboard, not here. Requires `cloudflared` via Homebrew.
#
# The daemon stays down, exiting 0 so KeepAlive does not loop, until the token
# file exists. One-time migration from the `service install` daemon:
#   1. update-system
#   2. Cloudflare dashboard → the tunnel → refresh (rotate) its token; copy it.
#   3. sudo /bin/bash -c 'umask 077; IFS= read -rs t; printf %s "$t" > /etc/cloudflared/tunnel-token'
#      (paste the token, press Enter), then
#      sudo launchctl kickstart -k system/org.nixos.cloudflared-tunnel
#   4. sudo cloudflared service uninstall
{ inputs, ... }:
{
  flake.modules.darwin.cloudflared-tunnel = { config, lib, ... }:
    let
      cfg = config.services.cloudflared-tunnel;
      tokenFile = lib.escapeShellArg cfg.tokenFile;
    in
    {
      options.services.cloudflared-tunnel = {
        enable = lib.mkEnableOption "the Cloudflare Tunnel connector, reading its token from a root-only file";

        tokenFile = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "/etc/cloudflared/tunnel-token";
          description = ''
            Root-owned 0600 file holding the remotely managed tunnel's token.
            Activation creates its directory as root-only and tightens the
            file's ownership and mode when it exists.
          '';
        };
      };

      config = lib.mkIf cfg.enable {
        # The launcher is inline /bin/sh and the binary is Homebrew's, so the
        # boot-time daemon never waits on the Nix volume.
        launchd.daemons.cloudflared-tunnel.serviceConfig = {
          ProgramArguments = [
            "/bin/sh"
            "-c"
            ''
              if [ ! -s "$1" ]; then
                echo "cloudflared-tunnel: $1 is missing or empty; staying down until it is written and the job is kickstarted" >&2
                exit 0
              fi
              exec "$2" tunnel run --token-file "$1"
            ''
            "cloudflared-tunnel"
            cfg.tokenFile
            "${config.homebrew.prefix}/bin/cloudflared"
          ];
          RunAtLoad = true;
          KeepAlive.SuccessfulExit = false;
          ThrottleInterval = 30;
          StandardOutPath = "/var/log/cloudflared-tunnel.log";
          StandardErrorPath = "/var/log/cloudflared-tunnel.log";
        };

        # No firewall rule: the connector only dials out to Cloudflare.
        system.activationScripts.extraActivation.text = lib.mkAfter ''
          # === cloudflared-tunnel: root-only token ===
          /usr/bin/install -d -m 0700 -o root -g wheel ${lib.escapeShellArg (dirOf cfg.tokenFile)}
          if [ -e ${tokenFile} ]; then
            /usr/sbin/chown root:wheel ${tokenFile}
            /bin/chmod 0600 ${tokenFile}
          else
            echo "WARNING: services.cloudflared-tunnel is enabled but ${cfg.tokenFile} is missing." >&2
            echo "         The tunnel connector stays down until it is written; see modules/services/cloudflared-tunnel.nix." >&2
          fi
        '';
      };
    };

  # NixOS aspect - stub for future implementation
  flake.modules.nixos.cloudflared-tunnel = { config, lib, ... }: {
    # TODO: Implement NixOS equivalent using the native services.cloudflared module
  };
}
