{ inputs, ... }:

{
  perSystem = { pkgs, ... }: {
    devShells.simpler-grants = let
      # Node.js 22 LTS for simpler-grants-protocol
      nodejs = pkgs.nodejs_22;
      # Python 3.11 — Poetry builds the in-project .venv from this interpreter
      python = pkgs.python311;

    in
    pkgs.mkShell {
      buildInputs = [
        nodejs
        # pnpm is managed via corepack (reads packageManager from package.json)
        # Do NOT add pkgs.nodePackages.pnpm — it shadows the corepack shim
        python
        # poetry is NOT included due to aarch64-darwin build issues with rapidfuzz
        # Install poetry via Homebrew: brew install poetry
        # Or via pipx: pipx install poetry
        pkgs.ruff
        pkgs.black
        pkgs.mypy
        pkgs.git
        # Native module build tools (subset of nodeLib.commonBuildTools)
        pkgs.gcc
        pkgs.gnumake
        pkgs.pkg-config
      ];

      shellHook = ''
        echo "🚀 simpler-grants-protocol development environment"
        echo ""
        echo "Node:   $(node --version)"
        echo "Python: $(python3 --version 2>&1 | cut -d' ' -f2)"
        if command -v poetry &> /dev/null; then
          echo "Poetry: $(poetry --version 2>&1 | grep -oP '[\d.]+')"
        else
          echo "⚠️  Poetry not found — install via: brew install poetry"
        fi
        echo ""
        if [[ ! -d node_modules ]]; then
          echo "📦 Run: pnpm install"
        fi
        if [[ -f lib/python-sdk/pyproject.toml && ! -x lib/python-sdk/.venv/bin/mypy ]]; then
          echo "📦 Run: cd lib/python-sdk && poetry install"
        fi
        echo ""

        # ─── Node.js environment ─────────────────────────────────
        export NODE_OPTIONS="--max-old-space-size=4096"
        export PATH="$PWD/node_modules/.bin:$PATH"

        # This project uses ESLint 9 flat config
        unset ESLINT_USE_FLAT_CONFIG

        # npm supply chain hardening
        export npm_config_ignore_scripts=true

        # ─── Corepack / pnpm ─────────────────────────────────────
        export COREPACK_HOME="''${HOME}/.cache/corepack"
        # Install corepack shims to a writable location (nix store is read-only)
        corepack enable --install-directory "''${HOME}/.local/bin" 2>/dev/null || true
        export PATH="''${HOME}/.local/bin:$PATH"

        # ─── Poetry / Python ─────────────────────────────────────
        # Homebrew Poetry (2.x) misdiscovers its own interpreter (Python 3.14)
        # and tries to install into Homebrew's externally-managed site-packages,
        # which pip blocks under PEP 668. (POETRY_VIRTUALENVS_PREFER_ACTIVE_PYTHON
        # was removed in Poetry 2.0, so it no longer steers selection.) Pre-creating
        # the in-project .venv from the flake's Python 3.11 sidesteps discovery
        # entirely: with in-project=true Poetry adopts an existing .venv as-is.
        export POETRY_VIRTUALENVS_CREATE=true
        export POETRY_VIRTUALENVS_IN_PROJECT=true
        if [[ -f lib/python-sdk/pyproject.toml && ! -e lib/python-sdk/.venv ]]; then
          ${python}/bin/python3 -m venv lib/python-sdk/.venv
        fi
      '';
    };
  };
}
