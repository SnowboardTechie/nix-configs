# Open WebUI service (uv-managed tool installation)
#
# Provides a web interface for LLM interaction. Installed via `uv tool install`
# into an isolated venv with a uv-managed lockfile, which eliminates the
# dependency-tree-rot issue that hit us before (missing greenlet caused a 65-hour
# crash loop invisible behind the Cloudflare tunnel).
#
# Requires `uv` via Homebrew — added to studio host config.
#
# Includes:
# - Main service runner (auto-installs if missing)
# - Daily update checker with an IMPORT PROBE — refuses to kickstart a
#   broken install so a bad dep resolution can't take the service down.
# - Manual upgrade script in PATH
{ inputs, ... }:
{
  # Darwin aspect - full configuration from modules/darwin/services/open-webui.nix
  flake.modules.darwin.open-webui = { pkgs, config, lib, ... }:
  let
    cfg = config.services.open-webui;
    homeDir = "/Users/${config.system.primaryUser}";
    # uv installs tools into ~/.local/share/uv/tools/<name>/bin/<name>
    # and symlinks entrypoints into ~/.local/bin/.
    uvPath = "/opt/homebrew/bin/uv";
    toolBin = "${homeDir}/.local/bin/open-webui";
    toolVenvPython = "${homeDir}/.local/share/uv/tools/open-webui/bin/python";

    # Shared preflight — used by runner, updater, upgrade script.
    # Verifies uv is available; fails loudly if not.
    preflight = ''
      if [ ! -x "${uvPath}" ]; then
        echo "ERROR: uv not found at ${uvPath}"
        echo "Install with: brew install uv"
        exit 1
      fi
    '';

    # Import probe — runs the actual import chain that was failing during
    # the Apr 21-24 outage. If this fails, the tool install is broken and
    # we must NOT kickstart the service (which would just crash-loop).
    # Returns 0 on success, non-zero on broken install.
    importProbe = ''
      if [ ! -x "${toolVenvPython}" ]; then
        echo "IMPORT PROBE: python missing at ${toolVenvPython}"
        return 1
      fi
      if ! "${toolVenvPython}" -c "
      import open_webui.main
      import greenlet
      import sqlalchemy
      from open_webui.utils.payload import apply_model_params_to_body_ollama
      " 2>&1; then
        echo "IMPORT PROBE: open-webui install cannot load core modules"
        return 1
      fi
      return 0
    '';

    # Manual upgrade script for Open WebUI
    upgrade-open-webui = pkgs.writeShellScriptBin "upgrade-open-webui" ''
      #!/bin/bash
      set -e

      echo "=== Open WebUI Manual Upgrade ==="
      echo ""

      ${preflight}

      # Get current version (may be empty on fresh install)
      CURRENT_VERSION=$("${uvPath}" tool list 2>/dev/null | awk '/^open-webui/{print $2}')
      echo "Current version: ''${CURRENT_VERSION:-not installed}"
      echo ""

      if [ -z "$CURRENT_VERSION" ]; then
        echo "Installing Open WebUI..."
        "${uvPath}" tool install --python 3.11 open-webui
      else
        echo "Upgrading Open WebUI..."
        "${uvPath}" tool upgrade open-webui
      fi
      echo ""

      NEW_VERSION=$("${uvPath}" tool list 2>/dev/null | awk '/^open-webui/{print $2}')
      echo "New version: ''${NEW_VERSION:-unknown}"
      echo ""

      import_probe() {
        ${importProbe}
      }

      if ! import_probe; then
        echo ""
        echo "ERROR: New install failed import probe. NOT restarting service."
        echo "Recovery: uv tool uninstall open-webui && uv tool install --python 3.11 open-webui"
        exit 1
      fi

      if [ "$CURRENT_VERSION" != "$NEW_VERSION" ]; then
        echo "✓ Upgraded from ''${CURRENT_VERSION:-nothing} to $NEW_VERSION"
        echo "Restarting Open WebUI service..."
        launchctl kickstart -k "gui/$(id -u)/org.nixos.open-webui"
        echo "✓ Service restarted"
      else
        echo "Already at latest version ($NEW_VERSION) — no restart needed"
      fi

      echo ""
      echo "=== Upgrade Complete ==="
      echo "Access at: http://localhost:${toString cfg.port}"
    '';

    # Runner script that auto-installs Open WebUI if missing, then execs it.
    # Does NOT run the updater — that's a separate launchd agent.
    openWebUIRunner = pkgs.writeShellScriptBin "run-open-webui" ''
      #!/bin/bash
      set -euo pipefail

      export HOME="${homeDir}"
      export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

      ${preflight}

      if [ ! -x "${toolBin}" ]; then
        echo "Installing Open WebUI into uv tool venv..."
        "${uvPath}" tool install --python 3.11 open-webui
      fi

      mkdir -p "${cfg.dataDir}"

      exec "${toolBin}" serve
    '';

    # Updater with import probe.
    #
    # Behavior:
    #   1. Capture current version.
    #   2. `uv tool upgrade open-webui` (idempotent; no-op if already latest).
    #   3. Run import probe against the tool venv python.
    #   4. ONLY IF probe passes AND version changed, kickstart the service.
    #
    # If the probe fails after an upgrade, we leave the existing (running) service
    # alone and emit a loud error. Service stays up on the old install bits that
    # were running before the upgrade rearranged site-packages. On next run the
    # upgrade re-resolves deps, which usually self-heals.
    openWebUIUpdater = pkgs.writeShellScriptBin "update-open-webui" ''
      #!/bin/bash
      set -euo pipefail

      export HOME="${homeDir}"
      export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

      ${preflight}

      OLD_VERSION=$("${uvPath}" tool list 2>/dev/null | awk '/^open-webui/{print $2}')

      if [ -z "$OLD_VERSION" ]; then
        # Fresh install is the runner's job (avoids a race with `uv tool install`
        # when both agents load at the same time).
        echo "Open WebUI not installed yet — runner will handle first install. Skipping."
        exit 0
      fi

      echo "Checking for Open WebUI updates (current: $OLD_VERSION)..."
      "${uvPath}" tool upgrade open-webui || {
        echo "uv tool upgrade failed; leaving existing install alone."
        exit 0
      }

      NEW_VERSION=$("${uvPath}" tool list 2>/dev/null | awk '/^open-webui/{print $2}')

      import_probe() {
        ${importProbe}
      }

      if ! import_probe; then
        echo "=== IMPORT PROBE FAILED ==="
        echo "New install (''${NEW_VERSION:-unknown}) cannot load required modules."
        echo "Service will NOT be kickstarted. Existing process continues running."
        echo "Recovery: uv tool uninstall open-webui && uv tool install --python 3.11 open-webui"
        exit 1
      fi

      if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
        echo "Already at latest ($NEW_VERSION) — no restart needed."
        exit 0
      fi

      echo "Import probe passed: $OLD_VERSION -> $NEW_VERSION. Restarting."
      launchctl kickstart -k "gui/$(id -u)/org.nixos.open-webui" >/dev/null 2>&1 || true
    '';
  in
  {
    options.services.open-webui = {
      enable = lib.mkEnableOption "Open WebUI LLM interface";

      port = lib.mkOption {
        type = lib.types.port;
        default = 8080;
        description = "Port for Open WebUI to listen on";
      };

      ollamaUrl = lib.mkOption {
        type = lib.types.str;
        default = "http://127.0.0.1:11434";
        description = "URL of the Ollama server";
      };

      dataDir = lib.mkOption {
        type = lib.types.str;
        default = "${homeDir}/.open-webui/data";
        description = "Directory for Open WebUI data storage";
      };

      autoUpdate = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Enable daily automatic updates (with import probe safety net)";
      };

      updateInterval = lib.mkOption {
        type = lib.types.int;
        default = 86400;
        description = "Update check interval in seconds (default: 24 hours)";
      };
    };

    config = lib.mkIf cfg.enable {
      # Add upgrade script to system packages
      environment.systemPackages = [ upgrade-open-webui ];

      # Main Open WebUI service
      launchd.user.agents.open-webui = {
        serviceConfig = {
          ProgramArguments = [ "${openWebUIRunner}/bin/run-open-webui" ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/open-webui.log";
          StandardErrorPath = "/tmp/open-webui.error.log";
          WorkingDirectory = "${homeDir}/.open-webui";
          EnvironmentVariables = {
            PORT = toString cfg.port;
            OLLAMA_BASE_URL = cfg.ollamaUrl;
            WEBUI_AUTH = "true";
            DATA_DIR = cfg.dataDir;
            HOME = homeDir;
          };
        };
      };

      # Auto-updater service (runs daily by default)
      launchd.user.agents.open-webui-updater = lib.mkIf cfg.autoUpdate {
        serviceConfig = {
          ProgramArguments = [ "${openWebUIUpdater}/bin/update-open-webui" ];
          RunAtLoad = true;
          StartInterval = cfg.updateInterval;
          StandardOutPath = "/tmp/open-webui.updater.log";
          StandardErrorPath = "/tmp/open-webui.updater.error.log";
        };
      };

      # Firewall rules — folded into extraActivation because nix-darwin's
      # system.activationScripts only composes a fixed set of named phases into
      # the activate script. See services/AGENTS.md for the full explanation.
      # Firewall target is now uv's tool venv python, not the system python3.11.
      system.activationScripts.extraActivation.text = lib.mkAfter ''
        # === open-webui firewall ===
        if [ -x "${toolVenvPython}" ]; then
          /usr/libexec/ApplicationFirewall/socketfilterfw --add "${toolVenvPython}" >/dev/null 2>&1 || true
          /usr/libexec/ApplicationFirewall/socketfilterfw --unblock "${toolVenvPython}" >/dev/null 2>&1 || true
        fi
      '';
    };
  };

  # NixOS aspect - stub for future implementation
  flake.modules.nixos.open-webui = { config, lib, ... }: {
    # TODO: Implement NixOS equivalent using systemd service
    # Consider using nixpkgs open-webui package when available
  };
}
