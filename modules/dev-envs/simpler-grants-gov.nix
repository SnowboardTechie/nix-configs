{ ... }:

{
  perSystem = { pkgs, ... }: {
    devShells.simpler-grants-gov = let
      # Python 3.14 to match api/pyproject.toml requires-python ">=3.14,<3.15".
      # uv builds the in-project .venv from this interpreter (PY_RUN_APPROACH=local).
      python = pkgs.python314;
    in
    pkgs.mkShell {
      buildInputs = [
        pkgs.uv
        python
        pkgs.postgresql_17   # server + client; runs natively, no Docker
        pkgs.opensearch      # 3.5.0 — app's docker pins opensearch:2; validate search tests
        pkgs.jq
        pkgs.git
        pkgs.gnumake
        pkgs.coreutils
      ];

      shellHook = ''
        # --- simpler-grants-gov (HHS Flask API) native dev services, no Docker ---
        # The repo's documented env is docker-compose; this shell runs the two services
        # the test suite actually needs (Postgres + OpenSearch) natively. AWS/OAuth are
        # mocked in-process via moto, so the other compose containers are not needed.
        # Data lives outside the repo so it survives worktree switches.
        export SGG_SERVICES_DIR="$HOME/.local/share/simpler-grants-gov"
        export PGDATA="$SGG_SERVICES_DIR/pg"
        export PGHOST="localhost"
        export PGPORT="5432"

        # Mirror api/local.env, but point the docker-network hostnames at localhost.
        export PY_RUN_APPROACH="local"
        export DB_HOST="localhost"
        export DB_PORT="5432"
        export DB_NAME="app"
        export DB_USER="app"
        export DB_PASSWORD="secret123"
        export SEARCH_ENDPOINT="localhost"
        export SEARCH_PORT="9200"
        export SEARCH_USE_SSL="FALSE"
        export SEARCH_VERIFY_CERTS="FALSE"

        export OPENSEARCH_PKG="${pkgs.opensearch}"

        sgg-db-init() {
          if [ ! -f "$PGDATA/PG_VERSION" ]; then
            mkdir -p "$SGG_SERVICES_DIR"
            initdb -D "$PGDATA" -U postgres --auth=trust --no-locale --encoding=UTF8
          fi
        }
        sgg-db-start() {
          pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" \
            -o "-p $PGPORT -c listen_addresses=localhost -k /tmp" -w start || return 1
          createuser -h localhost -p "$PGPORT" -U postgres -s "$DB_USER" 2>/dev/null || true
          createdb   -h localhost -p "$PGPORT" -U postgres -O "$DB_USER" "$DB_NAME" 2>/dev/null || true
          echo "Postgres up: postgresql://$DB_USER@localhost:$PGPORT/$DB_NAME"
        }
        sgg-db-stop() { pg_ctl -D "$PGDATA" -w stop 2>/dev/null || true; }

        sgg-search-conf() {
          local conf="$SGG_SERVICES_DIR/opensearch/config"
          mkdir -p "$conf" "$SGG_SERVICES_DIR/opensearch/data" "$SGG_SERVICES_DIR/opensearch/logs"
          # Regenerate config each run. Files copied from the read-only nix store come
          # back mode 0444, so remove stale ones first or the rewrite hits EACCES.
          rm -f "$conf/jvm.options" "$conf/log4j2.properties"
          # jvm.options uses cwd-relative diagnostic paths (logs/gc.log, heap-dump,
          # error-file) that break the JVM ergonomics probe (it runs from a different
          # cwd). The lines carry JDK version prefixes (e.g. "9-:-Xlog:gc*...",
          # "8:-Xloggc:..."), so match by content, not line-start. Local dev needs none.
          grep -vE 'gc\.log|HeapDumpPath|ErrorFile' \
            "$OPENSEARCH_PKG/config/jvm.options" > "$conf/jvm.options"
          install -m 644 "$OPENSEARCH_PKG/config/log4j2.properties" "$conf/log4j2.properties"
          cat > "$conf/opensearch.yml" <<EOF
cluster.name: sgg-local
node.name: sgg-node
discovery.type: single-node
network.host: localhost
http.port: 9200
path.data: $SGG_SERVICES_DIR/opensearch/data
path.logs: $SGG_SERVICES_DIR/opensearch/logs
bootstrap.memory_lock: false
cluster.routing.allocation.disk.threshold_enabled: false
# nixpkgs bundles the opensearch-security plugin, which aborts startup with
# "No SSL configuration found" unless disabled. Turn it off for local dev — no
# TLS/auth — matching the app's SEARCH_USE_SSL=FALSE / docker DISABLE_SECURITY_PLUGIN=true.
plugins.security.disabled: true
EOF
        }
        sgg-search-start() {
          sgg-search-conf
          local os="$SGG_SERVICES_DIR/opensearch"
          echo "Starting OpenSearch on localhost:9200 (foreground; Ctrl-C to stop)..."
          # Run from the service dir so jvm.options' relative paths (logs/gc.log,
          # heap-dump) resolve; nixpkgs opensearch ships no auth/security plugin, so
          # it serves plain http with no TLS/auth (matches SEARCH_USE_SSL=FALSE).
          ( cd "$os" && \
            OPENSEARCH_PATH_CONF="$os/config" \
            OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
            exec opensearch )
        }

        sgg-up() {
          sgg-db-init && sgg-db-start
          echo "Now start OpenSearch in a second shell: sgg-search-start"
        }
        sgg-down() { sgg-db-stop; }

        echo "🚀 simpler-grants-gov native dev environment (no Docker)"
        echo "  uv $(uv --version 2>&1 | grep -oE '[0-9.]+' | head -1) · python $(python3 --version 2>&1 | cut -d' ' -f2) · postgres 17 · opensearch ${pkgs.opensearch.version}"
        echo ""
        echo "📦 Quick start (testing):"
        echo "  sgg-up                          # init + start Postgres (app/app/secret123)"
        echo "  sgg-search-start                # start OpenSearch (use a 2nd shell)"
        echo "  cd api && uv sync               # install Python deps"
        echo "  cd api && make test args=\"-x\"   # PY_RUN_APPROACH=local -> uv run pytest"
        echo "  sgg-down                        # stop Postgres"
        echo ""
        echo "  Tests build their own isolated schema (create_all) + index — no migration"
        echo "  needed. Running the app server natively additionally needs local.env sourced"
        echo "  (set -a; . api/local.env; set +a) so vars like ALL_DB_SCHEMAS are present."
        echo ""
      '';
    };
  };
}
