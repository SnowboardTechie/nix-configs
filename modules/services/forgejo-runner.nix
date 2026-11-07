# forgejo-runner: a Forgejo Actions runner in host mode, for one repository's
# canonical gate (durable-pi-workspace#357; that repository's docs/ci.md).
#
# Security review: durable-pi-workspace vault,
# explorations/2026-11-07-forgejo-runner-security-review.md. In host mode a job
# runs as the runner's user with its network and environment and no other
# isolation, so the boundary is that user: a hidden service account that is not
# an administrator and cannot read Bryan's 0700 home (~/.secrets, Pi Durable's
# state). It does not close loopback: any local process reaches the services
# listening on 127.0.0.1, as every gate run as Bryan does today (finding 3).
#
# The daemon is a system LaunchDaemon running as that account. Its launcher
# (forgejo-runner-launcher.sh) waits, sleeping, until Bryan has written the
# registration's UUID and token into the account's home, then writes the
# effective config (the store template below with the UUID filled in) at 0600
# and execs `forgejo-runner daemon`. The token never enters the store, the
# plist, the config or a log: the runner reads it through `token_url`.
#
# The cache is off: its server and proxy listen on all interfaces when on.
#
# Not here: the registration (Bryan, in the repository's settings), the
# approved browser copy `browserApproval` names (the coordinator places it), and
# Forgejo's own Actions settings (the YunoHost VPS).
{ inputs, ... }:
{
  flake.modules.darwin.forgejo-runner =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.forgejo-runner;
      user = "_forgejo-runner";
      group = "_forgejo-runner";
      stateDir = "/var/lib/forgejo-runner";
      logDir = "/var/log/forgejo-runner";
      uuidPlaceholder = "@FORGEJO_RUNNER_UUID@";
      runner = "${cfg.package}/bin/forgejo-runner";
      worktrunk = inputs.worktrunk.packages.${pkgs.stdenv.hostPlatform.system}.default;

      # libolm is marked insecure in nixpkgs (CVE-2024-45191 to 45193, timing
      # side channels; deprecated upstream). This Python only runs the Matrix
      # tests against their synthetic homeserver, so it is permitted here, for
      # this one environment, by dropping the mark from the olm it is built
      # with. The host-wide `nixpkgs.config.permittedInsecurePackages` stays
      # untouched, so nothing else on the host can pull libolm in.
      olm = pkgs.olm.overrideAttrs (old: {
        meta = old.meta // { knownVulnerabilities = [ ]; };
      });
      python = pkgs.python3.override {
        self = python;
        packageOverrides = _: super: {
          python-olm = super.python-olm.override { inherit olm; };
        };
      };
      # mautrix (the plugin's tests require exactly 0.21.1: test_forgejo_runner_module.py
      # checks it), its encryption extra (python-olm, pycryptodome,
      # unpaddedbase64, base58: `mautrix.crypto` imports them) and aiohttp.
      matrixPython = python.withPackages (ps:
        [ ps.mautrix ps.aiohttp ] ++ ps.mautrix.optional-dependencies.encryption);

      # What every job sees beyond the daemon's own environment (HOME, PATH).
      # Nothing secret: a job can read all of it, and its log may print it.
      jobEnvironment = {
        PLANNING_MATRIX_PYTHON = "${matrixPython}/bin/python3";
        npm_config_cache = "${stateDir}/npm-cache";
      }
      // lib.optionalAttrs (cfg.hermesPython != null) {
        SGG_TEST_HERMES_PYTHON = cfg.hermesPython;
      }
      // lib.optionalAttrs (cfg.browserApproval != null) {
        PLANNING_WEB_BROWSER_APPROVAL = cfg.browserApproval;
      };

      settings = {
        log = {
          level = "info";
          job_level = "info";
        };
        runner = {
          # Absolute, so nothing depends on the working directory; it never
          # exists (registration is the connection below), and `env_file` is
          # left unset so no file adds to the jobs' environment.
          file = "${stateDir}/.runner";
          capacity = 1;
          envs = jobEnvironment;
          timeout = "${toString cfg.timeoutMinutes}m";
          # A clean stop (update-system, shutdown) lets a running gate finish.
          shutdown_timeout = "${toString cfg.shutdownTimeoutMinutes}m";
        };
        cache.enabled = false;
        host.workdir_parent = "${stateDir}/work";
        server.connections.forgejo = {
          inherit (cfg) url labels;
          uuid = uuidPlaceholder;
          token_url = "file:${stateDir}/token";
        };
      };
      # JSON is YAML: the runner parses it with its YAML loader. A plain
      # string keeps the template readable at evaluation (formats.yaml would
      # need a build to show it).
      configText = builtins.toJSON settings;
      configTemplate = pkgs.writeText "forgejo-runner-config-template.yaml" configText;
      launcher = ./forgejo-runner-launcher.sh;
    in
    {
      options.services.forgejo-runner = {
        enable = lib.mkEnableOption "a Forgejo Actions runner (host mode) as its own hidden account";

        package = lib.mkOption {
          type = lib.types.package;
          default = pkgs.forgejo-runner;
          defaultText = lib.literalExpression "pkgs.forgejo-runner";
          description = "The forgejo-runner package (nixpkgs; it updates only with the flake's nixpkgs input).";
        };

        url = lib.mkOption {
          type = lib.types.str;
          default = "https://git.snowboardtechie.com/";
          description = "The Forgejo instance the runner polls (outbound HTTPS only).";
        };

        labels = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [ ];
          example = [ "studio-gate:host" ];
          description = "The runner's labels; `<name>:host` runs jobs on this host as the runner's account.";
        };

        uid = lib.mkOption {
          type = lib.types.int;
          default = 450;
          description = "The runner account's uid (must be free on the host).";
        };

        gid = lib.mkOption {
          type = lib.types.int;
          default = 450;
          description = "The runner group's gid (must be free on the host).";
        };

        timeoutMinutes = lib.mkOption {
          type = lib.types.ints.positive;
          default = 90;
          description = "The longest a job may run.";
        };

        shutdownTimeoutMinutes = lib.mkOption {
          type = lib.types.ints.positive;
          default = 30;
          description = "How long a stopping runner waits for a running job before cancelling it.";
        };

        hermesPython = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "The Hermes interpreter jobs get as SGG_TEST_HERMES_PYTHON (derive it from the Hermes package; never a copied path).";
        };

        browserApproval = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "The approved browser's record, jobs get as PLANNING_WEB_BROWSER_APPROVAL. Not Nix-managed.";
        };

        matrixPython = lib.mkOption {
          type = lib.types.package;
          readOnly = true;
          default = matrixPython;
          description = "The Python jobs get as PLANNING_MATRIX_PYTHON: mautrix with its encryption extra, and aiohttp.";
        };

        configText = lib.mkOption {
          type = lib.types.str;
          readOnly = true;
          default = configText;
          description = "The config template (no secrets; the UUID is a placeholder the launcher fills in).";
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [
          {
            assertion = cfg.labels != [ ];
            message = "services.forgejo-runner.labels must name at least one label";
          }
          {
            assertion = lib.hasPrefix "https://" cfg.url;
            message = "services.forgejo-runner.url must be https://";
          }
        ];

        users.knownGroups = [ group ];
        users.groups.${group} = {
          inherit (cfg) gid;
          description = "Forgejo Actions runner";
        };
        users.knownUsers = [ user ];
        users.users.${user} = {
          inherit (cfg) uid gid;
          description = "Forgejo Actions runner";
          home = stateDir;
          # Created below with its mode; createhomedir would make a user home.
          createHome = false;
          shell = "/usr/bin/false";
          isHidden = true;
        };

        # In the `users` phase, right after the account exists and before the
        # `launchd` phase loads the daemon (whose working directory and log
        # files these are). `extraActivation` runs before `users`, so on the
        # first activation the account would not exist yet. Logs: a root-owned
        # directory nobody else can write; the files belong to the runner
        # (launchd opens them as the job's user) and Bryan's admin group reads
        # them. They never hold the token.
        system.activationScripts.users.text = lib.mkAfter ''
          # === forgejo-runner state and logs ===
          /bin/mkdir -p /var/lib ${stateDir} ${logDir}
          /usr/sbin/chown ${user}:${group} ${stateDir}
          /bin/chmod 0700 ${stateDir}
          /usr/sbin/chown root:wheel ${logDir}
          /bin/chmod 0755 ${logDir}
          /usr/bin/touch ${logDir}/runner.log ${logDir}/runner.error.log
          /usr/sbin/chown ${user}:admin ${logDir}/runner.log ${logDir}/runner.error.log
          /bin/chmod 0640 ${logDir}/runner.log ${logDir}/runner.error.log
        '';

        launchd.daemons.forgejo-runner.serviceConfig = {
          UserName = user;
          GroupName = group;
          # Through /bin/sh: at boot the Nix volume may not be mounted yet.
          ProgramArguments = [
            "/bin/sh"
            "-c"
            ''
              while [ ! -r "$1" ] || [ ! -x "$2" ]; do
                /bin/sleep 1
              done
              exec /bin/sh "$@"
            ''
            "wait-for-nix-store"
            "${launcher}"
            runner
            stateDir
            "${configTemplate}"
            "30"
          ];
          # Jobs inherit this. Nix's Node 26 (the same as Homebrew's node the
          # services run on), git and Worktrunk (the flake input cli-tools
          # installs, the same `wt` as Homebrew's on Studio), then the system;
          # no Homebrew. No secret.
          EnvironmentVariables = {
            HOME = stateDir;
            PATH = lib.concatStringsSep ":" [
              (lib.makeBinPath [ pkgs.nodejs_26 pkgs.git worktrunk ])
              "/usr/bin:/bin:/usr/sbin:/sbin"
            ];
          };
          WorkingDirectory = stateDir;
          RunAtLoad = true;
          KeepAlive = true;
          # launchd's default (20 s) would kill a gate the runner is letting
          # finish on SIGTERM.
          ExitTimeOut = cfg.shutdownTimeoutMinutes * 60 + 60;
          StandardOutPath = "${logDir}/runner.log";
          StandardErrorPath = "${logDir}/runner.error.log";
        };
      };
    };

  # NixOS stub per repository convention: the runner runs on Studio only.
  flake.modules.nixos.forgejo-runner = { };
}
