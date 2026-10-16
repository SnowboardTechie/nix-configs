# Pi Durable SGG PR pre-review: the workspace's launchd agent, its egress
# proxy and the standing page's Tailscale route.
#
# Application and runbook: ~/code/durable-pi-workspace
# (workspaces/sgg/OPERATIONS.md). The service starts paused and holds every
# side effect until its own commissioning gates are recorded; this module only
# supervises it. Nothing here reads or writes a secret: the service reads its
# 0600 secrets file and deploy key itself, so neither enters the store or a plist.
#
# Fail closed by construction. `enable` alone runs the paused agent and its
# loopback page. The proxy runs only once its listen address and client subnet
# (from `container network create --internal`) are set; the page is routed only
# once an HTTPS port is set, and activation refuses a port that already serves
# anything else. A configured proxy is not proof that the VM cannot bypass it:
# that is the application's live sandbox gate.
#
# Not installed here: Apple `container`. The application targets 1.5.0, which
# is a prerequisite candidate for the security review, not an approved install
# (nixpkgs pins 1.1.0; Homebrew would float it at activation). Once approved,
# the operator installs it and the service's config names it as `sandbox.cli`.
{ inputs, ... }:
{
  flake.modules.darwin.pi-durable =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.pi-durable;
      home = "/Users/${config.system.primaryUser}";
      # The application's notices, plugin path and submit flow all name this
      # checkout, so it is the deployment path, never a worktree.
      checkout = "${home}/code/durable-pi-workspace";
      tinyproxy = "${pkgs.tinyproxy}/bin/tinyproxy";

      # Canonical dotted-quad only (octets 0-255, no leading zeros, whitespace
      # or newline), so a value can never widen or add a tinyproxy directive.
      # This validates configuration syntax, not the VM's network isolation.
      octet = "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])";
      ipv4 = "${octet}\\.${octet}\\.${octet}\\.${octet}";
      isAddress = s: builtins.match ipv4 s != null && s != "0.0.0.0";
      isClient = s: isAddress s || builtins.match "${ipv4}/([1-9]|[12][0-9]|3[0-2])" s != null;

      # Exact hostnames only (fnmatch of a name without wildcards is equality);
      # FilterDefaultDeny makes the list the only reachable destinations.
      proxyConfig = pkgs.writeText "pi-durable-tinyproxy.conf" ''
        Listen ${toString cfg.proxy.listenAddress}
        Port ${toString cfg.proxy.port}
        ${lib.concatMapStrings (client: "Allow ${client}\n") cfg.proxy.allowedClients}
        ConnectPort 443
        FilterType fnmatch
        FilterDefaultDeny Yes
        Filter "${pkgs.writeText "pi-durable-proxy-allowlist" (lib.concatLines cfg.proxy.allowedDomains)}"
      '';
    in
    {
      options.services.pi-durable = {
        enable = lib.mkEnableOption ''
          the Pi Durable SGG pre-review agent. It starts paused and
          uncommissioned; see the application's OPERATIONS.md before enabling'';

        page = {
          port = lib.mkOption {
            type = lib.types.port;
            default = 8790;
            description = "Loopback port of the standing page. Must equal `page.port` in the service's config.";
          };
          tailscaleHttpsPort = lib.mkOption {
            type = lib.types.nullOr lib.types.port;
            default = null;
            description = ''
              Tailnet-only HTTPS port that Tailscale Serve forwards to the
              page. Null: no route. Activation refuses a port that already
              serves another route.
            '';
          };
        };

        proxy = {
          listenAddress = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = ''
              Gateway IPv4 address of the sandbox's internal container
              network, where tinyproxy listens. Null: no proxy, so the VM has
              no egress.
            '';
          };
          port = lib.mkOption {
            type = lib.types.port;
            default = 8888;
            description = "tinyproxy port; the service's `sandbox.proxy` is http://<listenAddress>:<port>.";
          };
          allowedClients = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ ];
            description = "Addresses or CIDR ranges allowed to use the proxy: the internal network's subnet.";
          };
          allowedDomains = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            # The application contract's starting allowlist. Unverified until
            # the live sandbox gate passes with it.
            default = [
              "github.com"
              "api.github.com"
              "codeload.github.com"
              "objects.githubusercontent.com"
              "registry.npmjs.org"
              "pypi.org"
              "files.pythonhosted.org"
            ];
            description = "Exact lowercase hostnames the VM may reach through the proxy; every other destination is refused.";
          };
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          assertions = [
            {
              assertion = cfg.page.tailscaleHttpsPort == null || config.services.tailscale.enable;
              message = "services.pi-durable.page.tailscaleHttpsPort requires services.tailscale";
            }
            {
              assertion = lib.all (domain: builtins.match "[a-z0-9.-]+" domain != null) cfg.proxy.allowedDomains;
              message = "services.pi-durable.proxy.allowedDomains takes exact lowercase hostnames, no wildcards";
            }
          ];

          launchd.user.agents.pi-durable-sgg.serviceConfig = {
            ProgramArguments = [
              "${config.homebrew.prefix}/bin/node"
              "${checkout}/dist/workspaces/sgg/src/main.js"
              "run"
              "--config"
              "${home}/.config/pi-durable/sgg.json"
            ];
            # No secret: only what finds node, git, claude, hermes and container
            # (Apple's installer puts it in /usr/local/bin).
            EnvironmentVariables = {
              HOME = home;
              PATH = lib.concatStringsSep ":" [
                "${home}/.local/bin"
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
            # SIGTERM stops reviews, the store and the page in order.
            ExitTimeOut = 60;
            StandardOutPath = "/tmp/pi-durable-sgg.log";
            StandardErrorPath = "/tmp/pi-durable-sgg.error.log";
          };
        }

        (lib.mkIf (cfg.proxy.listenAddress != null) {
          assertions = [
            {
              assertion = isAddress cfg.proxy.listenAddress;
              message = "services.pi-durable.proxy.listenAddress must be the internal network's gateway as one canonical IPv4 address, not a wildcard, name or anything else";
            }
            {
              assertion = cfg.proxy.allowedClients != [ ] && lib.all isClient cfg.proxy.allowedClients;
              message = "services.pi-durable.proxy.allowedClients must name the internal network's subnet as canonical IPv4 addresses or CIDR ranges with a /1 to /32 prefix";
            }
          ];

          launchd.user.agents.pi-durable-proxy.serviceConfig = {
            ProgramArguments = [ tinyproxy "-d" "-c" "${proxyConfig}" ];
            RunAtLoad = true;
            # Until the internal network exists, its gateway cannot be bound:
            # tinyproxy exits and launchd retries.
            KeepAlive = true;
            StandardOutPath = "/tmp/pi-durable-proxy.log";
            StandardErrorPath = "/tmp/pi-durable-proxy.error.log";
          };

          system.activationScripts.extraActivation.text = lib.mkAfter ''
            # === pi-durable proxy firewall ===
            /usr/libexec/ApplicationFirewall/socketfilterfw --add ${tinyproxy} >/dev/null 2>&1 || true
            /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${tinyproxy} >/dev/null 2>&1 || true
          '';
        })

        (lib.mkIf (cfg.page.tailscaleHttpsPort != null) {
          system.activationScripts.extraActivation.text = lib.mkAfter ''
            # === pi-durable standing page via Tailscale Serve ===
            if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
              /bin/bash ${./pi-durable-page-route.sh} \
                ${config.services.tailscale.package}/bin/tailscale ${pkgs.jq}/bin/jq \
                ${toString cfg.page.tailscaleHttpsPort} http://127.0.0.1:${toString cfg.page.port}; then
              echo "Failed to route the Pi Durable standing page through Tailscale Serve on port ${toString cfg.page.tailscaleHttpsPort}" >&2
              exit 1
            fi
          '';
        })
      ]);
    };

  # NixOS stub per repository convention: the pilot runs on Studio only.
  flake.modules.nixos.pi-durable = { };
}
