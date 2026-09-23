# Summit Point concept preview — loopback-only static review service.
#
# Serves one immutable, commit-addressed static build of the Summit Point
# website concept preview (repository: bryan/summit-point-site) to the
# existing remotely managed Cloudflare Tunnel.
#
# Exactly one public hostname reaches this origin: summitpoint.thompson.codes.
# Any request carrying another Host header gets a 404, so a stale tunnel route
# or DNS record can never serve the preview under a different name.
#
# Port owner: this module owns 127.0.0.1:4321. Astro's dev server uses 4322
# and must NEVER be tunneled.
#
# Release root (written only by scripts/publish-review.sh in the site repo):
#   ~/Library/Application Support/Thompson Codes/Summit Point Gallery/
#     releases/<sha>/    immutable builds, never deleted automatically
#     current            symlink to the served release
#
# Publish:  scripts/publish-review.sh
# Rollback: scripts/publish-review.sh --rollback <previous-sha>
#           (atomic symlink change; no rebuild, no activation)
#
# This is an unlisted, public-by-link concept preview. It is NOT a Summit
# Point website and carries no approved company content.
{ inputs, ... }:
{
  flake.modules.darwin.summit-point-gallery =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.summit-point-gallery;

      hostname = "summitpoint.${cfg.domain}";

      # Review-phase response safety. Applied to every response, 404s included:
      # the preview must not be indexed, archived, cached, sniffed, or allowed
      # to reach the network for anything.
      securityHeaders = {
        handler = "headers";
        response.set = {
          "X-Robots-Tag" = [ "noindex, nofollow, noarchive" ];
          "Cache-Control" = [ "no-store" ];
          "X-Content-Type-Options" = [ "nosniff" ];
          "Referrer-Policy" = [ "no-referrer" ];
          "Permissions-Policy" = [ "camera=(), microphone=(), geolocation=()" ];
          # Static-site CSP. Nothing on the site frames or is framed, so
          # framing is refused both ways.
          "Content-Security-Policy" = [
            ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
              + "script-src 'self'; frame-src 'none'; frame-ancestors 'none'; "
              + "base-uri 'none'; form-action 'none'")
          ];
        };
      };

      caddyConfig = pkgs.writeText "summit-point-gallery-caddy.json" (builtins.toJSON {
        admin.disabled = true;
        apps.http.servers.summit-point-gallery = {
          listen = [ "${cfg.host}:${toString cfg.port}" ];
          # The tunnel terminates TLS; this origin is plain HTTP on loopback.
          automatic_https.disable = true;
          routes = [
            { handle = [ securityHeaders ]; }
            {
              match = [{ host = [ hostname ]; }];
              handle = [{
                handler = "file_server";
                root = cfg.root;
                # An unknown path must 404, never fall back to another page.
                pass_thru = false;
              }];
              terminal = true;
            }
            {
              # Any other Host. Caddy answers a request no route handles with
              # an empty 200, so the 404 has to be explicit.
              handle = [{
                handler = "static_response";
                status_code = 404;
              }];
              terminal = true;
            }
          ];
        };
      });
    in
    {
      options.services.summit-point-gallery = {
        enable = lib.mkEnableOption "loopback-only Summit Point concept preview service";

        root = lib.mkOption {
          type = lib.types.str;
          default =
            "/Users/${config.system.primaryUser}/Library/Application Support/Thompson Codes/Summit Point Gallery/current";
          description = ''
            Directory served by the review service. Defaults to the `current`
            symlink maintained by the site repository's publish-review.sh,
            which points at an immutable releases/<sha>/ directory. Never
            point this at a working tree's dist/.
          '';
        };

        host = lib.mkOption {
          type = lib.types.str;
          default = "127.0.0.1";
          description = ''
            Listen address. Loopback only: the Cloudflare Tunnel is the sole
            public path to this service, and the macOS firewall is
            deliberately not opened for it.
          '';
        };

        port = lib.mkOption {
          type = lib.types.port;
          default = 4321;
          description = ''
            Listen port for the stable review build. Astro's dev server uses
            4322; that port must never be tunneled.
          '';
        };

        domain = lib.mkOption {
          type = lib.types.str;
          default = "thompson.codes";
          description = ''
            Parent domain for the one review hostname, summitpoint.<domain>.
            Requests for any other host are answered with a 404.
          '';
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [
          {
            assertion = cfg.port != 4322;
            message = ''
              services.summit-point-gallery.port must not be 4322: that is the
              Astro dev server's HMR port and must never be exposed.
            '';
          }
          {
            assertion = cfg.host == "127.0.0.1" || cfg.host == "::1";
            message = ''
              services.summit-point-gallery.host must stay on loopback. The
              review build is reachable only through the Cloudflare Tunnel.
            '';
          }
        ];

        launchd.user.agents.summit-point-gallery = {
          serviceConfig = {
            ProgramArguments = [
              "${pkgs.caddy}/bin/caddy"
              "run"
              "--config"
              "${caddyConfig}"
            ];
            RunAtLoad = true;
            KeepAlive = true;
            StandardOutPath = "/tmp/summit-point-gallery.log";
            StandardErrorPath = "/tmp/summit-point-gallery.error.log";
          };
        };

        # Deliberately NO firewall registration. Unlike dashy (which binds a
        # Tailscale address), this listener is loopback-only, so macOS never
        # prompts and nothing should be unblocked.
      };
    };

  # NixOS stub per repository convention: this is a Studio-only review service.
  flake.modules.nixos.summit-point-gallery = { };
}
