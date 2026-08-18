# Hindsight self-hosted agent memory (API + Control Plane + PostgreSQL 17).
#
# Named per-user instances. Each instance owns a dedicated PostgreSQL 17
# cluster with pgvector, a uv-locked hindsight-api venv, an npm-locked
# Control Plane, loopback-only listeners, Tailscale Serve HTTPS exposure,
# and age-encrypted six-hourly logical backups with tiered retention plus a
# monthly disposable restore test.
#
# Version pinning: the API env is locked by modules/services/hindsight-env/
# {pyproject.toml,uv.lock} and the Control Plane by hindsight-env/
# control-plane/{package.json,package-lock.json}. Service start applies the
# committed locks exactly (uv sync --locked / npm ci); updates are deliberate
# reviewed lock changes followed by update-studio, never discovery of newer
# versions at runtime. PostgreSQL comes from nixpkgs (postgresql_17 +
# pgvector) rather than Homebrew so a major upgrade is an explicit reviewed
# migration, not a side effect of `brew upgrade`.
#
# Secrets live outside Git and the Nix store in <secretsDir> (mode 0600):
#   api-bearer     shared API/MCP bearer checked by the built-in tenant ext
#   cp-access-key  Control Plane UI login key
#   db-password    PostgreSQL password for the hindsight role
#   age-identity.txt  private age identity for backup decryption/restore
# Only the public age recipient is declared here. Wrapper scripts read
# secret values at exec time; nothing secret enters derivations or plists.
{ inputs, ... }:
{
  flake.modules.darwin.hindsight = { config, lib, pkgs, ... }:
    let
      cfg = config.services.hindsight;
      envDir = ./hindsight-env;
      postgresPkg = pkgs.postgresql_17.withPackages (ps: [ ps.pgvector ]);
      agePkg = pkgs.age;

      instanceModule = { name, config, ... }: {
        options = {
          user = lib.mkOption {
            type = lib.types.str;
            default = name;
            description = "macOS user that owns and runs this Hindsight instance.";
          };
          homeDirectory = lib.mkOption {
            type = lib.types.str;
            default = "/Users/${config.user}";
            description = "Home directory for the instance user.";
          };
          stateDir = lib.mkOption {
            type = lib.types.str;
            default = "${config.homeDirectory}/.local/state/hindsight-${name}";
            description = "Instance state root (PostgreSQL data, venv, Control Plane env, backups).";
          };
          secretsDir = lib.mkOption {
            type = lib.types.str;
            default = "${config.homeDirectory}/.secrets/hindsight-${name}";
            description = "Directory holding this instance's mode-0600 secret files.";
          };
          apiPort = lib.mkOption {
            type = lib.types.port;
            default = 8888;
            description = "Loopback port for the Hindsight API.";
          };
          controlPlanePort = lib.mkOption {
            type = lib.types.port;
            default = 9999;
            description = "Loopback port for the Control Plane UI.";
          };
          dbPort = lib.mkOption {
            type = lib.types.port;
            default = 5433;
            description = "Loopback port for this instance's dedicated PostgreSQL cluster.";
          };
          llm = {
            model = lib.mkOption {
              type = lib.types.str;
              description = "Ollama model tag used for extraction/consolidation/reflection.";
            };
            baseUrl = lib.mkOption {
              type = lib.types.str;
              default = "http://127.0.0.1:11434";
              description = "Ollama base URL.";
            };
          };
          defaultBankTemplate = lib.mkOption {
            type = lib.types.str;
            default = "";
            description = ''
              JSON BankTemplateManifest applied to every newly created bank
              (HINDSIGHT_API_DEFAULT_BANK_TEMPLATE). Empty string leaves the
              variable unset. Used to enable Memory Defense on all banks.
            '';
          };
          tailscale = {
            enable = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Expose the loopback API/Control Plane through Tailscale Serve HTTPS.";
            };
            apiHttpsPort = lib.mkOption {
              type = lib.types.port;
              default = 9443;
              description = "Tailnet-only HTTPS port for the API.";
            };
            controlPlaneHttpsPort = lib.mkOption {
              type = lib.types.port;
              default = 9444;
              description = "Tailnet-only HTTPS port for the Control Plane.";
            };
          };
          backups = {
            ageRecipient = lib.mkOption {
              type = lib.types.str;
              description = "Public age recipient every backup archive is encrypted to.";
            };
          };
        };
      };

      enabledInstances = cfg.instances;

      instancePieces = name: instance:
        let
          state = instance.stateDir;
          secrets = instance.secretsDir;
          pgData = "${state}/postgres";
          pgSocketDir = "${state}/run";
          venv = "${state}/venv";
          cpEnv = "${state}/control-plane";
          backups = "${state}/backups";
          uvBin = "${config.homebrew.prefix}/bin/uv";
          npmBin = "${config.homebrew.prefix}/bin/npm";
          logPrefix = "/tmp/hindsight-${name}";

          baseEnvironment = {
            HOME = instance.homeDirectory;
            PATH = lib.concatStringsSep ":" [
              "${instance.homeDirectory}/.local/bin"
              "/run/current-system/sw/bin"
              "${config.homebrew.prefix}/bin"
              "/usr/bin"
              "/bin"
              "/usr/sbin"
              "/sbin"
            ];
          };

          postgresStart = pkgs.writeShellScript "hindsight-${name}-postgres-start" ''
            set -euo pipefail
            if [ ! -s "${secrets}/db-password" ]; then
              echo "hindsight-${name}: missing ${secrets}/db-password" >&2
              exit 1
            fi
            /bin/mkdir -p ${lib.escapeShellArg pgSocketDir}
            if [ ! -f "${pgData}/PG_VERSION" ]; then
              ${postgresPkg}/bin/initdb \
                --pgdata=${lib.escapeShellArg pgData} \
                --username=hindsight \
                --pwfile=${lib.escapeShellArg "${secrets}/db-password"} \
                --auth-host=scram-sha-256 \
                --auth-local=trust \
                --encoding=UTF8
            fi
            exec ${postgresPkg}/bin/postgres \
              -D ${lib.escapeShellArg pgData} \
              -p ${toString instance.dbPort} \
              -c listen_addresses=127.0.0.1 \
              -c unix_socket_directories=${lib.escapeShellArg pgSocketDir}
          '';

          apiStart = pkgs.writeShellScript "hindsight-${name}-api-start" ''
            set -euo pipefail
            for f in api-bearer db-password; do
              if [ ! -s "${secrets}/$f" ]; then
                echo "hindsight-${name}: missing ${secrets}/$f" >&2
                exit 1
              fi
            done
            # Apply the committed lock exactly; fails if lock and pyproject drift.
            export UV_PROJECT_ENVIRONMENT=${lib.escapeShellArg venv}
            "${uvBin}" sync --locked --no-dev --project ${envDir}
            # Wait for the dedicated cluster, then ensure database + pgvector.
            for _ in $(seq 60); do
              ${postgresPkg}/bin/pg_isready -q -h ${lib.escapeShellArg pgSocketDir} -p ${toString instance.dbPort} && break
              /bin/sleep 1
            done
            ${postgresPkg}/bin/psql -h ${lib.escapeShellArg pgSocketDir} -p ${toString instance.dbPort} \
              -U hindsight -d postgres -v ON_ERROR_STOP=1 -qAt \
              -c "SELECT 1 FROM pg_database WHERE datname = 'hindsight'" | grep -q 1 || \
              ${postgresPkg}/bin/createdb -h ${lib.escapeShellArg pgSocketDir} -p ${toString instance.dbPort} \
                -U hindsight hindsight
            ${postgresPkg}/bin/psql -h ${lib.escapeShellArg pgSocketDir} -p ${toString instance.dbPort} \
              -U hindsight -d hindsight -v ON_ERROR_STOP=1 -q \
              -c "CREATE EXTENSION IF NOT EXISTS vector"
            DB_PASSWORD=$(/bin/cat "${secrets}/db-password")
            API_BEARER=$(/bin/cat "${secrets}/api-bearer")
            export HINDSIGHT_API_DATABASE_URL="postgresql://hindsight:''${DB_PASSWORD}@127.0.0.1:${toString instance.dbPort}/hindsight"
            export HINDSIGHT_API_HOST=127.0.0.1
            export HINDSIGHT_API_PORT=${toString instance.apiPort}
            export HINDSIGHT_API_LLM_PROVIDER=ollama
            export HINDSIGHT_API_LLM_MODEL=${lib.escapeShellArg instance.llm.model}
            export HINDSIGHT_API_LLM_BASE_URL=${lib.escapeShellArg instance.llm.baseUrl}
            export HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
            export HINDSIGHT_API_TENANT_API_KEY="''${API_BEARER}"
            export HINDSIGHT_API_MCP_AUTH_TOKEN="''${API_BEARER}"
            export HINDSIGHT_API_AUDIT_LOG_ENABLED=true
            # The 'retain' audit action stores the raw request body, which
            # would preserve a secret even after Memory Defense redacted it
            # from the stored memory (verified 2026-08-18). Allowlist only
            # non-content audit evidence.
            export HINDSIGHT_API_AUDIT_LOG_ACTIONS=memory_defense,create_bank,delete_bank,update_bank_config
            ${lib.optionalString (instance.defaultBankTemplate != "") ''
              export HINDSIGHT_API_DEFAULT_BANK_TEMPLATE=${lib.escapeShellArg instance.defaultBankTemplate}
            ''}
            exec "${venv}/bin/hindsight-api"
          '';

          controlPlaneStart = pkgs.writeShellScript "hindsight-${name}-control-plane-start" ''
            set -euo pipefail
            for f in api-bearer cp-access-key; do
              if [ ! -s "${secrets}/$f" ]; then
                echo "hindsight-${name}: missing ${secrets}/$f" >&2
                exit 1
              fi
            done
            # Apply the committed npm lock exactly into a writable env dir.
            /bin/mkdir -p ${lib.escapeShellArg cpEnv}
            /bin/cp -f ${envDir}/control-plane/package.json ${envDir}/control-plane/package-lock.json ${lib.escapeShellArg cpEnv}/
            LOCK_HASH=$(/usr/bin/shasum -a 256 ${envDir}/control-plane/package-lock.json | /usr/bin/awk '{print $1}')
            STAMP="${cpEnv}/.lock-hash"
            if [ ! -f "$STAMP" ] || [ "$(/bin/cat "$STAMP")" != "$LOCK_HASH" ]; then
              (cd ${lib.escapeShellArg cpEnv} && "${npmBin}" ci --ignore-scripts --no-audit --no-fund)
              printf '%s' "$LOCK_HASH" > "$STAMP"
            fi
            export HOSTNAME=127.0.0.1
            export PORT=${toString instance.controlPlanePort}
            export HINDSIGHT_CP_DATAPLANE_API_URL="http://127.0.0.1:${toString instance.apiPort}"
            HINDSIGHT_CP_DATAPLANE_API_KEY=$(/bin/cat "${secrets}/api-bearer")
            export HINDSIGHT_CP_DATAPLANE_API_KEY
            HINDSIGHT_CP_ACCESS_KEY=$(/bin/cat "${secrets}/cp-access-key")
            export HINDSIGHT_CP_ACCESS_KEY
            exec "${cpEnv}/node_modules/.bin/hindsight-control-plane" \
              --hostname 127.0.0.1 --port ${toString instance.controlPlanePort}
          '';

          # Six-hourly age-encrypted logical backups with tiered retention:
          # six-hourly 48h, daily 14d, weekly 4w, pre-upgrade until released.
          # Existing points are promoted (hardlinked) into higher tiers rather
          # than re-dumped.
          backupScript = pkgs.writeShellScript "hindsight-${name}-backup" ''
            set -euo pipefail
            MODE="''${1:-scheduled}"
            TS=$(/bin/date -u +%Y%m%dT%H%M%SZ)
            DAY=$(/bin/date -u +%Y%m%d)
            /bin/mkdir -p ${lib.escapeShellArg backups}/six-hourly ${lib.escapeShellArg backups}/daily \
              ${lib.escapeShellArg backups}/weekly ${lib.escapeShellArg backups}/pre-upgrade
            if [ "$MODE" = "pre-upgrade" ]; then
              DEST="${backups}/pre-upgrade/hindsight-${name}-$TS.dump.age"
            else
              DEST="${backups}/six-hourly/hindsight-${name}-$TS.dump.age"
            fi
            ${postgresPkg}/bin/pg_dump -h ${lib.escapeShellArg pgSocketDir} -p ${toString instance.dbPort} \
              -U hindsight -Fc hindsight \
              | ${agePkg}/bin/age -r ${lib.escapeShellArg instance.backups.ageRecipient} -o "$DEST"
            if [ ! -s "$DEST" ]; then
              echo "hindsight-${name}: backup produced empty archive $DEST" >&2
              exit 1
            fi
            SIZE=$(/usr/bin/stat -f %z "$DEST")
            echo "$(/bin/date -u +%FT%TZ) backup $MODE $DEST ($SIZE bytes)"
            if [ "$MODE" = "pre-upgrade" ]; then
              exit 0
            fi
            # Promote today's newest six-hourly point into the daily tier.
            if [ ! -e "${backups}/daily/hindsight-${name}-$DAY.dump.age" ]; then
              /bin/ln "$DEST" "${backups}/daily/hindsight-${name}-$DAY.dump.age"
              echo "promoted $DEST -> daily/$DAY"
            fi
            # Promote into the weekly tier when the newest weekly point is >6 days old.
            NEWEST_WEEKLY=$(/usr/bin/find ${lib.escapeShellArg backups}/weekly -name '*.dump.age' -mtime -7 | /usr/bin/head -1)
            if [ -z "$NEWEST_WEEKLY" ]; then
              /bin/ln "${backups}/daily/hindsight-${name}-$DAY.dump.age" \
                "${backups}/weekly/hindsight-${name}-$DAY.dump.age"
              echo "promoted daily/$DAY -> weekly"
            fi
            # Prune per the declared retention policy (hardlinks keep promoted data alive).
            /usr/bin/find ${lib.escapeShellArg backups}/six-hourly -name '*.dump.age' -mtime +2 -delete
            /usr/bin/find ${lib.escapeShellArg backups}/daily -name '*.dump.age' -mtime +14 -delete
            /usr/bin/find ${lib.escapeShellArg backups}/weekly -name '*.dump.age' -mtime +28 -delete
            # Report unexpected growth (>50% over the previous scheduled backup).
            PREV_FILE="${state}/.last-backup-size"
            if [ -f "$PREV_FILE" ]; then
              PREV=$(/bin/cat "$PREV_FILE")
              if [ "$PREV" -gt 0 ] && [ "$SIZE" -gt $((PREV * 3 / 2)) ]; then
                echo "WARNING hindsight-${name}: backup size grew ''${PREV} -> ''${SIZE} bytes (>50%)" >&2
              fi
            fi
            printf '%s' "$SIZE" > "$PREV_FILE"
            /usr/bin/du -sk ${lib.escapeShellArg backups} | /usr/bin/awk '{print "backup tree total: " $1 " KiB"}'
          '';

          # Monthly restore drill into a disposable database on the same
          # cluster; never touches the live 'hindsight' database.
          restoreTestScript = pkgs.writeShellScript "hindsight-${name}-restore-test" ''
            set -euo pipefail
            LATEST=$(/bin/ls -t ${lib.escapeShellArg backups}/six-hourly/*.dump.age 2>/dev/null | /usr/bin/head -1)
            if [ -z "$LATEST" ]; then
              echo "hindsight-${name}: no backup archive found for restore test" >&2
              exit 1
            fi
            TESTDB="hindsight_restore_test"
            PSQL="${postgresPkg}/bin/psql -h ${pgSocketDir} -p ${toString instance.dbPort} -U hindsight"
            $PSQL -d postgres -qAt -c "DROP DATABASE IF EXISTS $TESTDB"
            ${postgresPkg}/bin/createdb -h ${lib.escapeShellArg pgSocketDir} -p ${toString instance.dbPort} -U hindsight "$TESTDB"
            $PSQL -d "$TESTDB" -q -c "CREATE EXTENSION IF NOT EXISTS vector"
            ${agePkg}/bin/age -d -i "${secrets}/age-identity.txt" "$LATEST" \
              | ${postgresPkg}/bin/pg_restore -h ${lib.escapeShellArg pgSocketDir} -p ${toString instance.dbPort} \
                  -U hindsight -d "$TESTDB" --no-owner
            TABLES=$($PSQL -d "$TESTDB" -qAt -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
            echo "$(/bin/date -u +%FT%TZ) restore test from $LATEST: $TABLES tables"
            if [ "$TABLES" -lt 5 ]; then
              echo "hindsight-${name}: restore test found too few tables ($TABLES)" >&2
              exit 1
            fi
            $PSQL -d "$TESTDB" -qAt -c "SELECT 'banks: ' || count(*) FROM banks" || true
            $PSQL -d postgres -q -c "DROP DATABASE $TESTDB"
            echo "restore test OK"
          '';

          backupNow = pkgs.writeShellScriptBin "hindsight-${name}-backup-now" ''
            exec ${backupScript} "''${1:-scheduled}"
          '';
        in
        {
          agents = {
            "hindsight-${name}-postgres" = {
              serviceConfig = {
                Label = "org.nixos.hindsight-${name}-postgres";
                ProgramArguments = [ "${postgresStart}" ];
                RunAtLoad = true;
                KeepAlive = true;
                WorkingDirectory = instance.homeDirectory;
                EnvironmentVariables = baseEnvironment;
                StandardOutPath = "${logPrefix}-postgres.log";
                StandardErrorPath = "${logPrefix}-postgres.error.log";
                ProcessType = "Background";
                ThrottleInterval = 10;
              };
            };
            "hindsight-${name}-api" = {
              serviceConfig = {
                Label = "org.nixos.hindsight-${name}-api";
                ProgramArguments = [ "${apiStart}" ];
                RunAtLoad = true;
                KeepAlive = true;
                WorkingDirectory = instance.homeDirectory;
                EnvironmentVariables = baseEnvironment;
                StandardOutPath = "${logPrefix}-api.log";
                StandardErrorPath = "${logPrefix}-api.error.log";
                SoftResourceLimits.NumberOfFiles = 65536;
                HardResourceLimits.NumberOfFiles = 65536;
                ProcessType = "Background";
                ThrottleInterval = 10;
              };
            };
            "hindsight-${name}-control-plane" = {
              serviceConfig = {
                Label = "org.nixos.hindsight-${name}-control-plane";
                ProgramArguments = [ "${controlPlaneStart}" ];
                RunAtLoad = true;
                KeepAlive = true;
                WorkingDirectory = instance.homeDirectory;
                EnvironmentVariables = baseEnvironment;
                StandardOutPath = "${logPrefix}-control-plane.log";
                StandardErrorPath = "${logPrefix}-control-plane.error.log";
                ProcessType = "Background";
                ThrottleInterval = 10;
              };
            };
            "hindsight-${name}-backup" = {
              serviceConfig = {
                Label = "org.nixos.hindsight-${name}-backup";
                ProgramArguments = [ "${backupScript}" ];
                StartCalendarInterval = map (h: { Hour = h; Minute = 45; }) [ 0 6 12 18 ];
                EnvironmentVariables = baseEnvironment;
                StandardOutPath = "${logPrefix}-backup.log";
                StandardErrorPath = "${logPrefix}-backup.error.log";
                ProcessType = "Background";
              };
            };
            "hindsight-${name}-restore-test" = {
              serviceConfig = {
                Label = "org.nixos.hindsight-${name}-restore-test";
                ProgramArguments = [ "${restoreTestScript}" ];
                StartCalendarInterval = [{ Day = 1; Hour = 4; Minute = 30; }];
                EnvironmentVariables = baseEnvironment;
                StandardOutPath = "${logPrefix}-restore-test.log";
                StandardErrorPath = "${logPrefix}-restore-test.error.log";
                ProcessType = "Background";
              };
            };
          };
          packages = [ backupNow ];
          activation = ''
            # === Hindsight instance: ${name} ===
            /bin/mkdir -p \
              ${lib.escapeShellArg state} \
              ${lib.escapeShellArg pgSocketDir} \
              ${lib.escapeShellArg backups} \
              ${lib.escapeShellArg secrets}
            /usr/sbin/chown ${lib.escapeShellArg instance.user}:staff \
              ${lib.escapeShellArg state} ${lib.escapeShellArg secrets}
            /bin/chmod 0700 ${lib.escapeShellArg state} ${lib.escapeShellArg secrets}
            ${lib.optionalString instance.tailscale.enable ''
              if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
                ${config.services.tailscale.package}/bin/tailscale serve \
                  --bg --yes \
                  --https=${toString instance.tailscale.apiHttpsPort} \
                  http://127.0.0.1:${toString instance.apiPort}; then
                echo "Failed to configure Tailscale Serve for Hindsight API on port ${toString instance.tailscale.apiHttpsPort}" >&2
                exit 1
              fi
              if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
                ${config.services.tailscale.package}/bin/tailscale serve \
                  --bg --yes \
                  --https=${toString instance.tailscale.controlPlaneHttpsPort} \
                  http://127.0.0.1:${toString instance.controlPlanePort}; then
                echo "Failed to configure Tailscale Serve for Hindsight Control Plane on port ${toString instance.tailscale.controlPlaneHttpsPort}" >&2
                exit 1
              fi
            ''}
          '';
        };

      pieces = lib.mapAttrsToList instancePieces enabledInstances;
      allPorts = lib.flatten (lib.mapAttrsToList
        (_: i: [ i.apiPort i.controlPlanePort i.dbPort ])
        enabledInstances);
    in
    {
      options.services.hindsight = {
        enable = lib.mkEnableOption "Hindsight self-hosted agent memory";

        instances = lib.mkOption {
          type = lib.types.attrsOf (lib.types.submodule instanceModule);
          default = { };
          description = "Named per-user Hindsight instances.";
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [
          {
            assertion = builtins.length allPorts == builtins.length (lib.unique allPorts);
            message = "services.hindsight instance ports must be unique";
          }
        ] ++ lib.mapAttrsToList
          (name: instance: {
            # Instances currently run as launchd user agents of the primary
            # user. A future instance for another account (e.g. traci) needs
            # LaunchDaemons plus its own state/secrets/ports/backups; extend
            # this module deliberately before enabling one.
            assertion = instance.user == config.system.primaryUser;
            message = "services.hindsight.instances.${name} must run as system.primaryUser";
          })
          enabledInstances
        ++ lib.mapAttrsToList
          (name: instance: {
            assertion = !instance.tailscale.enable || config.services.tailscale.enable;
            message = "services.hindsight.instances.${name}.tailscale requires services.tailscale";
          })
          enabledInstances;

        environment.systemPackages = [ agePkg ] ++ lib.flatten (map (p: p.packages) pieces);

        launchd.user.agents = lib.mkMerge (map (p: p.agents) pieces);

        system.activationScripts.extraActivation.text =
          lib.mkAfter (lib.concatMapStrings (p: p.activation) pieces);
      };
    };

  flake.modules.nixos.hindsight = { ... }: {
    # TODO: Implement NixOS equivalent (LaunchDaemons -> systemd units)
  };
}
