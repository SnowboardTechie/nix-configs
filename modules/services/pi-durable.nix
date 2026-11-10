# Pi Durable SGG PR pre-review: the workspace's launchd agent, its egress
# proxy and the standing page's Tailscale route. Also the shared Claude usage
# sampler (workspaces/sampler/OPERATIONS.md): its agent and its page's route.
# And the model-provider proxy (workspaces/proxies/OPERATIONS.md): its agent on
# Nix's Node, its firewall exception and its port in the sandbox's pf anchor.
# And the GitHub write proxy (the same runbook, §7): its agent on the same Node,
# with Nix's git and openssh on its PATH, and its gateway port (8893: case VMs'
# pushes and signature requests) in the pf anchor, sharing that exception.
# And the replay proxy (durable-pi-workspace#21): a second tinyproxy on the same
# gateway for the evaluation's replay VMs, whose allowlist holds the package
# hosts and no GitHub host, so GitHub context comes only from a case's bundle.
# And planning on VMs (durable-pi-workspace#48): the forge broker (proxies
# runbook §8) on both networks' gateways at 8894; the VM broker (§9) on the
# planning service network's gateway alone at 8895; the planning service's
# launcher and its relay (planning runbook §4.1), the relay's page routed
# through the same Serve guard; and a second pf anchor that confines the
# planning service network to those two brokers. The VM broker's port never
# enters the sandbox's anchor, so an agent VM cannot reach it.
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
# anything else. With the proxy comes a pf anchor that refuses the network
# everything but the proxy. Neither is proof that the VM cannot bypass them:
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
      # Dedicated clean, pushed-main checkout; human/agent work stays in ~/code.
      # Prepare and verify its build before Bryan activates with update-system.
      # Do not substitute a temporary worktree or an installed-plist override.
      checkout = "${home}/.local/share/pi-durable/sgg-deployment";
      tinyproxy = "${pkgs.tinyproxy}/bin/tinyproxy";
      # The proxies' Node: a store path, so the firewall exception below
      # names exactly this binary and Homebrew's node never gets one. The slim
      # package's bin/node is the binary itself; nodejs_24's is a symlink to it,
      # and the firewall would see the target, not the name it was given.
      # Node 24 satisfies the application's engines (>=22.19.0).
      node = "${pkgs.nodejs-slim_24}/bin/node";
      # Each proxy or broker listening on a gateway needs that one exception.
      # The planning relay listens on loopback only and needs none.
      nodeListens = cfg.modelProxy.enable || cfg.githubProxy.enable
        || cfg.forgeBroker.enable || cfg.vmBroker.enable;

      # Canonical dotted-quad only (octets 0-255, no leading zeros, whitespace
      # or newline), so a value can never widen or add a tinyproxy directive.
      # This validates configuration syntax, not the VM's network isolation.
      octet = "(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])";
      ipv4 = "${octet}\\.${octet}\\.${octet}\\.${octet}";
      isAddress = s: builtins.match ipv4 s != null && s != "0.0.0.0";
      isClient = s: isAddress s || builtins.match "${ipv4}/([1-9]|[12][0-9]|3[0-2])" s != null;
      # Lowercase hex and colons with a /1 to /128 prefix: nothing that could
      # end a pf rule or add one. pfctl checks the address itself at load.
      hextet = "[0-9a-f]{0,4}";
      isIpv6Subnet = s: builtins.match "${hextet}(:${hextet}){2,7}/([1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])" s != null;

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

      # The replay proxy: the sandbox proxy's listener and clients on its own
      # port, with its own allowlist. A replay VM sits on the same network as
      # a review's, so pf cannot tell them apart; this filter is what denies a
      # replay GitHub (the application's live replay gate attests it).
      replayProxyConfig = pkgs.writeText "pi-durable-replay-tinyproxy.conf" ''
        Listen ${toString cfg.proxy.listenAddress}
        Port ${toString cfg.replayProxy.port}
        ${lib.concatMapStrings (client: "Allow ${client}\n") cfg.proxy.allowedClients}
        ConnectPort 443
        FilterType fnmatch
        FilterDefaultDeny Yes
        Filter "${pkgs.writeText "pi-durable-replay-proxy-allowlist" (lib.concatLines cfg.replayProxy.allowedDomains)}"
      '';
      isGithubHost = domain:
        domain == "github.com"
        || lib.hasSuffix ".github.com" domain
        || lib.hasSuffix "githubusercontent.com" domain;

      # The sandbox network reaches tinyproxy on its gateway and nothing else:
      # no service of this host on any address (gateway, LAN, tailnet), no
      # LAN, tailnet or internet. Without it, a VM on the network reached every
      # service bound to all interfaces, over IPv4 and IPv6, and the
      # tailnet-only ones (measured 2026-10-16; the application firewall does
      # not filter vmnet bridges).
      #
      # The rules match the network's addresses, not its interface: vmnet
      # names the bridge when the first VM attaches (bridge100 or bridge101,
      # by what else runs), and macOS has no interface groups. lo0 is exempt
      # so this host's own connections from the gateway address still work.
      # Link-local IPv6 carries no address of the network, so TCP and UDP from
      # link-local are refused on every bridge vmnet may use.
      #
      # Each port the network may reach on the gateway: tinyproxy's, then the
      # model proxy's, the write proxy's, the replay proxy's and the forge
      # broker's when each runs. Nothing else is passed, so with all four
      # disabled the anchor is exactly tinyproxy's. Never the VM broker's: an
      # agent VM on this network must not start VMs (an assertion refuses it).
      pfAnchor = "com.apple/100.pi-durable-sgg";
      gatewayPorts = [ cfg.proxy.port ]
        ++ lib.optional cfg.modelProxy.enable cfg.modelProxy.port
        ++ lib.optional cfg.githubProxy.enable cfg.githubProxy.port
        ++ lib.optional cfg.replayProxy.enable cfg.replayProxy.port
        ++ lib.optional cfg.forgeBroker.enable cfg.forgeBroker.port;
      # One network's confinement: its clients reach `address` at `ports` and
      # nothing else on any interface but lo0, and link-local IPv6 is refused
      # on every bridge vmnet may use.
      confinement = { title, clients, address, ports, ipv6Subnet }: ''
        # ${title}: nix-configs modules/services/pi-durable.nix
        ${lib.concatMapStrings (port: "pass in quick inet proto tcp from { ${lib.concatStringsSep " " clients} } to ${toString address} port ${toString port} flags S/SA keep state\n") ports}block return in quick on ! lo0 inet from { ${lib.concatStringsSep " " clients} } to any
        block return in quick on ! lo0 inet6 from ${toString ipv6Subnet} to any
        block return in quick on { ${lib.concatMapStringsSep " " (n: "bridge${toString n}") (lib.range 100 115)} } inet6 proto { tcp udp } from fe80::/10 to any
      '';
      pfRules = pkgs.writeText "pi-durable-sgg.pf" (confinement {
        title = "Pi Durable SGG sandbox isolation";
        clients = cfg.proxy.allowedClients;
        address = cfg.proxy.listenAddress;
        ports = gatewayPorts;
        inherit (cfg.proxy) ipv6Subnet;
      });
      pfLoader = [ "${pkgs.bash}/bin/bash" "${./pi-durable-pf.sh}" "/sbin/pfctl" pfAnchor "${pfRules}" "/var/run/pi-durable-pf.token" ];

      # The planning service network, the same way in its own anchor: the
      # service VM reaches the forge broker and the VM broker on its gateway
      # and nothing else. The same loader keeps it, with its own enable
      # reference, at activation and from its own boot daemon.
      planningPfAnchor = "com.apple/101.pi-durable-planning";
      planningPfRules = pkgs.writeText "pi-durable-planning.pf" (confinement {
        title = "Pi Durable planning service network isolation";
        clients = cfg.planning.allowedClients;
        address = cfg.planning.listenAddress;
        ports = [ cfg.forgeBroker.port cfg.vmBroker.port ];
        inherit (cfg.planning) ipv6Subnet;
      });
      planningPfLoader = [ "${pkgs.bash}/bin/bash" "${./pi-durable-pf.sh}" "/sbin/pfctl" planningPfAnchor "${planningPfRules}" "/var/run/pi-durable-planning-pf.token" ];

      # pf rules do not survive a restart, and nix-darwin does not re-run
      # activation at boot. Retried until the anchor is loaded and checked.
      pfBootDaemon = loader: log: {
        ProgramArguments = [
          "/bin/sh"
          "-c"
          ''
            while [ ! -x "$1" ]; do
              /bin/sleep 1
            done
            exec "$@"
          ''
          "wait-for-nix-store"
        ] ++ loader;
        RunAtLoad = true;
        KeepAlive.SuccessfulExit = false;
        StandardOutPath = log;
        StandardErrorPath = log;
      };

      # The pages' loopback ports: the SGG page's, and the sampler's and the
      # planning relay's when each runs.
      pagePorts = [ cfg.page.port ]
        ++ lib.optional cfg.sampler.enable cfg.sampler.port
        ++ lib.optional cfg.planning.enable cfg.planning.relayPort;

      # No secret: only what finds node, git, claude, hermes and container
      # (Apple's installer puts it in /usr/local/bin).
      agentEnvironment = {
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

      # Tailscale Serve route through the guard, which refuses a port that
      # already serves anything else.
      pageRoute = what: httpsPort: port: ''
        # === pi-durable ${what} via Tailscale Serve ===
        if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
          /bin/bash ${./pi-durable-page-route.sh} \
            ${config.services.tailscale.package}/bin/tailscale ${pkgs.jq}/bin/jq \
            ${toString httpsPort} http://127.0.0.1:${toString port}; then
          echo "Failed to route the Pi Durable ${what} through Tailscale Serve on port ${toString httpsPort}" >&2
          exit 1
        fi
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
            description = ''
              Addresses or CIDR ranges allowed to use the proxy: the internal
              network's subnet. pf refuses them everything else.
            '';
          };
          ipv6Subnet = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = ''
              The internal network's IPv6 prefix (`container network inspect`:
              `ipv6Subnet`). The proxy does not listen on it; pf refuses it
              everything. Required with `listenAddress`.
            '';
          };
          allowedDomains = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            # The application contract's allowlist. The last three
            # (durable-pi-workspace#174) are read-only GitHub content hosts:
            # raw files, Actions logs behind `gh run view --log`, and release
            # downloads. The live sandbox gate re-attests the list after any
            # change.
            default = [
              "github.com"
              "api.github.com"
              "codeload.github.com"
              "objects.githubusercontent.com"
              "registry.npmjs.org"
              "pypi.org"
              "files.pythonhosted.org"
              "raw.githubusercontent.com"
              "results-receiver.actions.githubusercontent.com"
              "release-assets.githubusercontent.com"
            ];
            description = "Exact lowercase hostnames the VM may reach through the proxy; every other destination is refused.";
          };
        };

        modelProxy = {
          enable = lib.mkEnableOption ''
            the model-provider proxy: the sandbox's VMs reach Claude and Jev
            through it with a per-case placeholder, and it alone reads the real
            credentials. It listens on the sandbox network's gateway
            (`proxy.listenAddress`), so it requires the sandbox proxy, and
            exits 1 until ~/.config/pi-durable/model-proxy.json exists; see the
            application's workspaces/proxies/OPERATIONS.md before enabling'';
          port = lib.mkOption {
            type = lib.types.port;
            default = 8892;
            description = ''
              Port the model proxy listens on at `proxy.listenAddress`, the one
              pf passes the network to besides tinyproxy's. Must equal
              `listen.port` in model-proxy.json.
            '';
          };
        };

        githubProxy = {
          enable = lib.mkEnableOption ''
            the GitHub write proxy: the workspaces' services write to GitHub
            only through it, on a Unix socket in its state directory, and it
            alone reads the agents' GitHub tokens and signing keys. Case VMs
            push and request commit signatures through its listener on the
            sandbox network's gateway (`proxy.listenAddress`, at `port`), so it
            requires the sandbox proxy. It exits 1 until
            ~/.config/pi-durable/github-proxy.json exists; see the application's
            workspaces/proxies/OPERATIONS.md §7 before enabling'';
          port = lib.mkOption {
            type = lib.types.port;
            default = 8893;
            description = ''
              Port the write proxy's push and signing listener uses at
              `proxy.listenAddress`, which pf passes the network to besides
              tinyproxy's and the model proxy's. Must equal `push.listen.port`
              in github-proxy.json (the application's
              workspaces/proxies/OPERATIONS.md §7). Without that `push` block
              the proxy opens no port, and pf passing it reaches nothing.
            '';
          };
        };

        replayProxy = {
          enable = lib.mkEnableOption ''
            the replay proxy: a second tinyproxy on the sandbox network's
            gateway (`proxy.listenAddress`) for the evaluation's replay VMs,
            whose allowlist omits GitHub so a replay's GitHub context comes
            only from its case's bundle. It requires the sandbox proxy, and
            its port must equal `sandbox.proxy`'s in
            ~/.config/pi-durable/replay.json'';
          port = lib.mkOption {
            type = lib.types.port;
            default = 8889;
            description = ''
              Port the replay proxy listens on at `proxy.listenAddress`, which
              pf passes the network to besides the other proxies'. The replay
              config's `sandbox.proxy` is http://<listenAddress>:<port>.
            '';
          };
          allowedDomains = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            # The sandbox allowlist's package hosts. No GitHub host may ever be
            # added: a replay's GitHub context comes only from its case's
            # bundle, and an assertion refuses one.
            default = [
              "registry.npmjs.org"
              "pypi.org"
              "files.pythonhosted.org"
            ];
            description = "Exact lowercase hostnames a replay VM may reach through the replay proxy, never a GitHub host; every other destination is refused.";
          };
        };

        forgeBroker = {
          enable = lib.mkEnableOption ''
            the forge broker: agent and service VMs fetch from and push to
            Forgejo only through it, with a grant's placeholder, and it alone
            reads the machine accounts' tokens and signing keys. It listens on
            the sandbox network's gateway (`proxy.listenAddress`) and, with
            `planning`, on the planning service network's, so it requires the
            sandbox proxy. It exits 1 until ~/.config/pi-durable/forge-broker.json
            exists; see the application's workspaces/proxies/OPERATIONS.md §8
            before enabling'';
          port = lib.mkOption {
            type = lib.types.port;
            default = 8894;
            description = ''
              Port the forge broker listens on at each gateway, which both
              networks' pf anchors pass their clients to. Must equal
              `listen.port` in forge-broker.json.
            '';
          };
        };

        vmBroker = {
          enable = lib.mkEnableOption ''
            the VM broker: the planning service starts, drives and stops agent
            VMs only through it, and it alone runs the `container` CLI for
            them and writes their grants. It listens on the planning service
            network's gateway alone (`planning.listenAddress`), so it requires
            `planning`, and its port is never passed by the sandbox network's
            anchor. It exits 1 until ~/.config/pi-durable/vm-broker.json
            exists; see the application's workspaces/proxies/OPERATIONS.md §9
            before enabling'';
          port = lib.mkOption {
            type = lib.types.port;
            default = 8895;
            description = ''
              Port the VM broker listens on at `planning.listenAddress`, which
              only the planning anchor passes. Must equal `listen.port` in
              vm-broker.json.
            '';
          };
        };

        planning = {
          enable = lib.mkEnableOption ''
            the planning service in its VM: the launcher that runs it on the
            planning service network, the relay that carries its page from
            loopback to the VM's published socket, and the pf anchor that
            confines that network to the forge broker and the VM broker. It
            requires both brokers; see the application's
            workspaces/planning/OPERATIONS.md §4.1 before enabling'';
          listenAddress = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = ''
              Gateway IPv4 address of the planning service network
              (`container network create --internal`), where the brokers
              listen for the service VM. Required with `enable`; never the
              sandbox network's.
            '';
          };
          allowedClients = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ ];
            description = ''
              Addresses or CIDR ranges of the planning service network: its
              subnet. pf passes them to the brokers and refuses them
              everything else.
            '';
          };
          ipv6Subnet = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = ''
              The planning service network's IPv6 prefix (`container network
              inspect`: `ipv6Subnet`); pf refuses it everything. Required with
              `enable`.
            '';
          };
          relayPort = lib.mkOption {
            type = lib.types.port;
            default = 8792;
            description = "Loopback port of the relay to the service's page. Must equal `listen.port` in planning-relay.json (its default, 8792).";
          };
          tailscaleHttpsPort = lib.mkOption {
            type = lib.types.nullOr lib.types.port;
            default = null;
            description = ''
              Tailnet-only HTTPS port that Tailscale Serve forwards to the
              relay. Null: no route. Activation refuses a port that already
              serves another route.
            '';
          };
        };

        sampler = {
          enable = lib.mkEnableOption ''
            the shared Claude usage sampler: it reads each subscription's usage
            on a schedule, serves the shared usage page and sends the weekly
            message. It refuses to start without
            ~/.config/pi-durable/sampler.json; see the application's
            workspaces/sampler/OPERATIONS.md before enabling'';
          port = lib.mkOption {
            type = lib.types.port;
            default = 8791;
            description = "Loopback port of the sampler's page. Must equal `page.port` in sampler.json.";
          };
          tailscaleHttpsPort = lib.mkOption {
            type = lib.types.nullOr lib.types.port;
            default = null;
            description = ''
              Tailnet-only HTTPS port that Tailscale Serve forwards to the
              sampler's page. Null: no route. Activation refuses a port that
              already serves another route.
            '';
          };
        };
      };

      config = lib.mkMerge [
        (lib.mkIf cfg.enable (lib.mkMerge [
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
              EnvironmentVariables = agentEnvironment;
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
              {
                assertion = cfg.proxy.ipv6Subnet != null && isIpv6Subnet cfg.proxy.ipv6Subnet;
                message = "services.pi-durable.proxy.ipv6Subnet must name the internal network's IPv6 prefix (`container network inspect`: ipv6Subnet) in lowercase hex with a /1 to /128 prefix, so pf can confine the network";
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
              # === pi-durable sandbox isolation (pf) ===
              if ! ${lib.escapeShellArgs pfLoader}; then
                echo "Failed to load the Pi Durable sandbox's pf anchor ${pfAnchor}" >&2
                exit 1
              fi
            '';

            launchd.daemons.pi-durable-pf.serviceConfig = pfBootDaemon pfLoader "/var/log/pi-durable-pf.log";
          })

          (lib.mkIf (cfg.page.tailscaleHttpsPort != null) {
            system.activationScripts.extraActivation.text =
              lib.mkAfter (pageRoute "standing page" cfg.page.tailscaleHttpsPort cfg.page.port);
          })
        ]))

        # The model proxy rides on the sandbox proxy: its gateway address and the
        # pf anchor that confines the network both come from there.
        (lib.mkIf cfg.modelProxy.enable {
          assertions = [
            {
              assertion = cfg.enable && cfg.proxy.listenAddress != null;
              message = "services.pi-durable.modelProxy requires services.pi-durable.enable and proxy.listenAddress: it listens on the sandbox network's gateway, and only the sandbox proxy's pf anchor confines that network";
            }
            {
              assertion = cfg.modelProxy.port != cfg.proxy.port;
              message = "services.pi-durable.modelProxy.port must differ from services.pi-durable.proxy.port (tinyproxy)";
            }
            {
              assertion = cfg.modelProxy.port != cfg.page.port && (!cfg.sampler.enable || cfg.modelProxy.port != cfg.sampler.port);
              message = "services.pi-durable.modelProxy.port must differ from the pages' ports (page.port and sampler.port)";
            }
          ];

          # Until the internal network's gateway exists the bind fails; the
          # proxy retries in-process and KeepAlive covers an exit. Without
          # model-proxy.json, `run` exits 1 and KeepAlive retries it at
          # launchd's throttle.
          launchd.user.agents.pi-durable-model-proxy.serviceConfig = {
            ProgramArguments = [
              node
              "${checkout}/dist/workspaces/proxies/src/model-main.js"
              "run"
              "--config"
              "${home}/.config/pi-durable/model-proxy.json"
            ];
            # HOME only: the proxy finds its config, grants and token files
            # under it and runs no other program, so it needs no PATH. launchd's
            # default (/usr/bin:/bin:/usr/sbin:/sbin) holds no node, and Node is
            # named by store path above. No secret: it reads its 0600 files.
            EnvironmentVariables.HOME = home;
            RunAtLoad = true;
            KeepAlive = true;
            StandardOutPath = "/tmp/pi-durable-model-proxy.log";
            StandardErrorPath = "/tmp/pi-durable-model-proxy.error.log";
          };
        })

        # Both proxies listen on the gateway from the same Node binary: one
        # exception, while either is enabled.
        (lib.mkIf nodeListens {
          system.activationScripts.extraActivation.text = lib.mkAfter ''
            # === pi-durable proxies' Node firewall ===
            /usr/libexec/ApplicationFirewall/socketfilterfw --add ${node} >/dev/null 2>&1 || true
            /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${node} >/dev/null 2>&1 || true
          '';
        })

        # The GitHub write proxy, from the same deployment checkout: its API
        # route is a Unix socket (0600, in a 0700 state directory); with the
        # `push` block in github-proxy.json it also listens on the gateway at
        # `githubProxy.port` for case VMs' pushes and `/sign`, retrying the
        # bind until vmnet brings the gateway up, as the model proxy does.
        # Like the model proxy, it rides on the sandbox proxy's address and pf
        # anchor. Without github-proxy.json, `run` exits 1 and KeepAlive
        # retries it at launchd's throttle.
        (lib.mkIf cfg.githubProxy.enable {
          assertions = [
            {
              assertion = cfg.enable && cfg.proxy.listenAddress != null;
              message = "services.pi-durable.githubProxy requires services.pi-durable.enable and proxy.listenAddress: its push listener is on the sandbox network's gateway, and only the sandbox proxy's pf anchor confines that network";
            }
            {
              assertion = cfg.githubProxy.port != cfg.proxy.port && (!cfg.modelProxy.enable || cfg.githubProxy.port != cfg.modelProxy.port);
              message = "services.pi-durable.githubProxy.port must differ from services.pi-durable.proxy.port (tinyproxy) and modelProxy.port";
            }
            {
              assertion = cfg.githubProxy.port != cfg.page.port && (!cfg.sampler.enable || cfg.githubProxy.port != cfg.sampler.port);
              message = "services.pi-durable.githubProxy.port must differ from the pages' ports (page.port and sampler.port)";
            }
          ];

          launchd.user.agents.pi-durable-github-proxy.serviceConfig = {
            ProgramArguments = [
              node
              "${checkout}/dist/workspaces/proxies/src/github-main.js"
              "run"
              "--config"
              "${home}/.config/pi-durable/github-proxy.json"
            ];
            # HOME: it finds its config, the policy plugin's files and the
            # agents' 0600 token and signing-key files under it. PATH: only
            # Nix's git (git, `git http-backend`, `git verify-commit`) and
            # openssh (`ssh-keygen -Y sign`, and verify-commit's ssh-keygen),
            # at store paths, so nothing finds Homebrew's node or any other
            # tool. No system directory: on Studio (2026-11-03), with only these
            # two, git signed and verified a commit, served receive-pack, ran a
            # `#!/bin/sh` pre-receive hook and reached GitHub over HTTPS without
            # a certificate variable. No secret.
            EnvironmentVariables = {
              HOME = home;
              PATH = lib.makeBinPath [ pkgs.git pkgs.openssh ];
            };
            RunAtLoad = true;
            KeepAlive = true;
            StandardOutPath = "/tmp/pi-durable-github-proxy.log";
            StandardErrorPath = "/tmp/pi-durable-github-proxy.error.log";
          };
        })

        # The replay proxy rides on the sandbox proxy: its listen address,
        # clients and pf anchor come from there, and so does its firewall
        # exception (the same tinyproxy binary, added while listenAddress is
        # set, which this requires).
        (lib.mkIf cfg.replayProxy.enable {
          assertions = [
            {
              assertion = cfg.enable && cfg.proxy.listenAddress != null;
              message = "services.pi-durable.replayProxy requires services.pi-durable.enable and proxy.listenAddress: it listens on the sandbox network's gateway, and only the sandbox proxy's pf anchor confines that network";
            }
            {
              assertion = lib.all (domain: builtins.match "[a-z0-9.-]+" domain != null) cfg.replayProxy.allowedDomains;
              message = "services.pi-durable.replayProxy.allowedDomains takes exact lowercase hostnames, no wildcards";
            }
            {
              assertion = !lib.any isGithubHost cfg.replayProxy.allowedDomains;
              message = "services.pi-durable.replayProxy.allowedDomains must hold no GitHub host (github.com, *.github.com, *githubusercontent.com): a replay's GitHub context comes only from its case's bundle";
            }
            {
              assertion = cfg.replayProxy.port != cfg.proxy.port && cfg.replayProxy.port != cfg.modelProxy.port && cfg.replayProxy.port != cfg.githubProxy.port;
              message = "services.pi-durable.replayProxy.port must differ from services.pi-durable.proxy.port (tinyproxy), modelProxy.port and githubProxy.port";
            }
          ];

          launchd.user.agents.pi-durable-replay-proxy.serviceConfig = {
            ProgramArguments = [ tinyproxy "-d" "-c" "${replayProxyConfig}" ];
            RunAtLoad = true;
            # As the sandbox proxy: until a VM brings the gateway up the bind
            # fails, tinyproxy exits and launchd retries.
            KeepAlive = true;
            StandardOutPath = "/tmp/pi-durable-replay-proxy.log";
            StandardErrorPath = "/tmp/pi-durable-replay-proxy.error.log";
          };
        })

        # The forge broker, from the same deployment checkout on the same
        # Node: it listens on the sandbox network's gateway (agent VMs) and,
        # with `planning`, the planning service network's (the service VM),
        # retrying each bind until vmnet brings that gateway up. Like the
        # proxies, it rides on the sandbox proxy's address and pf anchor; its
        # port joins that anchor's and the planning one's. Without
        # forge-broker.json, `run` exits 1 and KeepAlive retries it at
        # launchd's throttle.
        (lib.mkIf cfg.forgeBroker.enable {
          assertions = [
            {
              assertion = cfg.enable && cfg.proxy.listenAddress != null;
              message = "services.pi-durable.forgeBroker requires services.pi-durable.enable and proxy.listenAddress: it listens on the sandbox network's gateway, and only the sandbox proxy's pf anchor confines that network";
            }
            {
              assertion = !lib.elem cfg.forgeBroker.port ([ cfg.proxy.port ]
                ++ lib.optional cfg.modelProxy.enable cfg.modelProxy.port
                ++ lib.optional cfg.githubProxy.enable cfg.githubProxy.port
                ++ lib.optional cfg.replayProxy.enable cfg.replayProxy.port);
              message = "services.pi-durable.forgeBroker.port must differ from services.pi-durable.proxy.port (tinyproxy), modelProxy.port, githubProxy.port and replayProxy.port";
            }
            {
              assertion = !lib.elem cfg.forgeBroker.port pagePorts;
              message = "services.pi-durable.forgeBroker.port must differ from the pages' ports (page.port, sampler.port and planning.relayPort)";
            }
          ];

          launchd.user.agents.pi-durable-forge-broker.serviceConfig = {
            ProgramArguments = [
              node
              "${checkout}/dist/workspaces/proxies/src/forge-main.js"
              "run"
              "--config"
              "${home}/.config/pi-durable/forge-broker.json"
            ];
            # As the write proxy's: HOME for its config, the grants and the
            # machine accounts' 0600 token and key files; PATH only Nix's git
            # (`git http-backend`, fetches and pushes to Forgejo) and openssh
            # (`ssh-keygen -Y sign` and verify). No secret.
            EnvironmentVariables = {
              HOME = home;
              PATH = lib.makeBinPath [ pkgs.git pkgs.openssh ];
            };
            RunAtLoad = true;
            KeepAlive = true;
            StandardOutPath = "/tmp/pi-durable-forge-broker.log";
            StandardErrorPath = "/tmp/pi-durable-forge-broker.error.log";
          };
        })

        # The VM broker: the planning service's one way to start agent VMs.
        # It listens on the planning service network's gateway alone; the
        # sandbox network's anchor never passes its port, so an agent VM
        # cannot start another. Without vm-broker.json, `run` exits 1 and
        # KeepAlive retries it at launchd's throttle.
        (lib.mkIf cfg.vmBroker.enable {
          assertions = [
            {
              assertion = cfg.planning.enable;
              message = "services.pi-durable.vmBroker requires services.pi-durable.planning: it listens on the planning service network's gateway, and only the planning pf anchor confines that network";
            }
            {
              assertion = !lib.elem cfg.vmBroker.port gatewayPorts;
              message = "services.pi-durable.vmBroker.port must never be passed by the sandbox network's pf anchor (proxy.port, modelProxy.port, githubProxy.port, replayProxy.port and forgeBroker.port): an agent VM must not reach the VM broker";
            }
            {
              assertion = !lib.elem cfg.vmBroker.port pagePorts;
              message = "services.pi-durable.vmBroker.port must differ from the pages' ports (page.port, sampler.port and planning.relayPort)";
            }
          ];

          launchd.user.agents.pi-durable-vm-broker.serviceConfig = {
            ProgramArguments = [
              node
              "${checkout}/dist/workspaces/proxies/src/vm-broker-main.js"
              "run"
              "--config"
              "${home}/.config/pi-durable/vm-broker.json"
            ];
            # HOME for its config, grants and records. PATH: Nix's git first
            # (a builder's commit-msg hook, the clone on a volume), then the
            # system directories for Apple's `container` CLI, which its config
            # names as `container` (/usr/local/bin) and is not in Nix. No
            # secret: it writes grants, never a token.
            EnvironmentVariables = {
              HOME = home;
              PATH = "${lib.makeBinPath [ pkgs.git ]}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
            };
            RunAtLoad = true;
            KeepAlive = true;
            # SIGTERM removes its VMs (within 10 s) before it exits.
            ExitTimeOut = 30;
            StandardOutPath = "/tmp/pi-durable-vm-broker.log";
            StandardErrorPath = "/tmp/pi-durable-vm-broker.error.log";
          };
        })

        # The planning service in its VM: the launcher (the only host code
        # that starts the service VM), the relay from loopback to the VM's
        # published socket, the planning network's pf anchor, and the relay's
        # Serve route. It needs both brokers: the service fetches through the
        # forge broker and starts agents through the VM broker.
        (lib.mkIf cfg.planning.enable (lib.mkMerge [
          {
            assertions = [
              {
                assertion = cfg.enable && cfg.forgeBroker.enable && cfg.vmBroker.enable;
                message = "services.pi-durable.planning requires services.pi-durable.enable, forgeBroker and vmBroker: the service VM fetches through the forge broker and starts agent VMs through the VM broker";
              }
              {
                assertion = cfg.planning.listenAddress != null && isAddress cfg.planning.listenAddress;
                message = "services.pi-durable.planning.listenAddress must be the planning service network's gateway as one canonical IPv4 address, not a wildcard, name or anything else";
              }
              {
                assertion = cfg.planning.allowedClients != [ ] && lib.all isClient cfg.planning.allowedClients;
                message = "services.pi-durable.planning.allowedClients must name the planning service network's subnet as canonical IPv4 addresses or CIDR ranges with a /1 to /32 prefix";
              }
              {
                assertion = cfg.planning.ipv6Subnet != null && isIpv6Subnet cfg.planning.ipv6Subnet;
                message = "services.pi-durable.planning.ipv6Subnet must name the planning service network's IPv6 prefix (`container network inspect`: ipv6Subnet) in lowercase hex with a /1 to /128 prefix, so pf can confine the network";
              }
              {
                assertion = cfg.planning.listenAddress != cfg.proxy.listenAddress
                  && !lib.any (client: lib.elem client cfg.proxy.allowedClients) cfg.planning.allowedClients
                  && cfg.planning.ipv6Subnet != cfg.proxy.ipv6Subnet;
                message = "services.pi-durable.planning's listenAddress, allowedClients and ipv6Subnet must never equal the sandbox network's (proxy.*): the planning service network is its own";
              }
              {
                assertion = cfg.planning.relayPort != cfg.page.port && (!cfg.sampler.enable || cfg.planning.relayPort != cfg.sampler.port);
                message = "services.pi-durable.planning.relayPort must differ from the pages' ports (page.port and sampler.port)";
              }
              {
                assertion = cfg.planning.tailscaleHttpsPort == null || config.services.tailscale.enable;
                message = "services.pi-durable.planning.tailscaleHttpsPort requires services.tailscale";
              }
              {
                assertion = cfg.planning.tailscaleHttpsPort == null
                  || (cfg.planning.tailscaleHttpsPort != cfg.page.tailscaleHttpsPort
                  && (!cfg.sampler.enable || cfg.planning.tailscaleHttpsPort != cfg.sampler.tailscaleHttpsPort));
                message = "services.pi-durable.planning.tailscaleHttpsPort must differ from the other pages' Serve ports (page.tailscaleHttpsPort and sampler.tailscaleHttpsPort)";
              }
            ];

            # Attached to the service VM: it ends when the VM does (exit 1,
            # for launchd to start a fresh one) and reconciles any VM and
            # grant left before starting anew. Without
            # planning-service-vm.json, `run` exits 1 and KeepAlive retries it.
            launchd.user.agents.pi-durable-planning-service.serviceConfig = {
              ProgramArguments = [
                node
                "${checkout}/dist/workspaces/planning/src/service-vm.js"
                "run"
                "--config"
                "${home}/.config/pi-durable/planning-service-vm.json"
              ];
              # HOME for its config, grant store and state. PATH: the system
              # directories only, for Apple's `container` CLI (/usr/local/bin),
              # the one program it runs. No secret: the grant's placeholder
              # goes to the CLI by environment, never into this plist.
              EnvironmentVariables = {
                HOME = home;
                PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
              };
              RunAtLoad = true;
              KeepAlive = true;
              # SIGTERM stops the attached CLI (SIGKILL after 10 s), then
              # removes the VM, confirmed, and deletes its grant.
              ExitTimeOut = 60;
              StandardOutPath = "/tmp/pi-durable-planning-service.log";
              StandardErrorPath = "/tmp/pi-durable-planning-service.error.log";
            };

            # Bytes only, from 127.0.0.1:relayPort to the service VM's
            # published socket; while the socket is absent it closes each
            # connection. A port it can't bind exits 1, for launchd to retry.
            launchd.user.agents.pi-durable-planning-relay.serviceConfig = {
              ProgramArguments = [
                node
                "${checkout}/dist/workspaces/proxies/src/relay-main.js"
                "run"
                "--config"
                "${home}/.config/pi-durable/planning-relay.json"
              ];
              # HOME only: it finds its config and state under it and runs no
              # other program. No secret.
              EnvironmentVariables.HOME = home;
              RunAtLoad = true;
              KeepAlive = true;
              StandardOutPath = "/tmp/pi-durable-planning-relay.log";
              StandardErrorPath = "/tmp/pi-durable-planning-relay.error.log";
            };

            system.activationScripts.extraActivation.text = lib.mkAfter ''
              # === pi-durable planning service network isolation (pf) ===
              if ! ${lib.escapeShellArgs planningPfLoader}; then
                echo "Failed to load the Pi Durable planning service network's pf anchor ${planningPfAnchor}" >&2
                exit 1
              fi
            '';

            launchd.daemons.pi-durable-planning-pf.serviceConfig =
              pfBootDaemon planningPfLoader "/var/log/pi-durable-planning-pf.log";
          }

          # After every mkAfter block (1500), so the planning network's pf
          # anchor is loaded before its page is routed.
          (lib.mkIf (cfg.planning.tailscaleHttpsPort != null) {
            system.activationScripts.extraActivation.text =
              lib.mkOrder 1600 (pageRoute "planning page" cfg.planning.tailscaleHttpsPort cfg.planning.relayPort);
          })
        ]))

        # The sampler runs on its own: it reads the workspaces' state
        # directories and its own config, and needs nothing above enabled.
        (lib.mkIf cfg.sampler.enable (lib.mkMerge [
          {
            assertions = [
              {
                assertion = cfg.sampler.tailscaleHttpsPort == null || config.services.tailscale.enable;
                message = "services.pi-durable.sampler.tailscaleHttpsPort requires services.tailscale";
              }
              {
                assertion = !cfg.enable || cfg.sampler.port != cfg.page.port;
                message = "services.pi-durable.sampler.port must differ from services.pi-durable.page.port";
              }
              {
                assertion = !cfg.enable || cfg.sampler.tailscaleHttpsPort == null || cfg.sampler.tailscaleHttpsPort != cfg.page.tailscaleHttpsPort;
                message = "services.pi-durable.sampler.tailscaleHttpsPort must differ from services.pi-durable.page.tailscaleHttpsPort";
              }
            ];

            # Without sampler.json, `run` prints "<file>: does not exist" and
            # exits 1; KeepAlive then retries it at launchd's throttle.
            launchd.user.agents.pi-durable-sampler.serviceConfig = {
              ProgramArguments = [
                "${config.homebrew.prefix}/bin/node"
                "${checkout}/dist/workspaces/sampler/src/main.js"
                "run"
                "--config"
                "${home}/.config/pi-durable/sampler.json"
              ];
              EnvironmentVariables = agentEnvironment;
              RunAtLoad = true;
              KeepAlive = true;
              StandardOutPath = "/tmp/pi-durable-sampler.log";
              StandardErrorPath = "/tmp/pi-durable-sampler.error.log";
            };
          }

          (lib.mkIf (cfg.sampler.tailscaleHttpsPort != null) {
            system.activationScripts.extraActivation.text =
              lib.mkAfter (pageRoute "usage sampler's page" cfg.sampler.tailscaleHttpsPort cfg.sampler.port);
          })
        ]))
      ];
    };

  # NixOS stub per repository convention: the pilot runs on Studio only.
  flake.modules.nixos.pi-durable = { };
}
