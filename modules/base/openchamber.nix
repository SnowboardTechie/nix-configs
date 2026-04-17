# Base module: OpenChamber (desktop UI for OpenCode)
#
# Upstream ships no official Homebrew cask, so we vendor a minimal cask in a
# self-owned local tap at /opt/homebrew/Library/Taps/bryan/homebrew-local.
# The cask uses `version :latest` + `sha256 :no_check` + the GitHub
# `/releases/latest/download/` redirect so `brew install` / `brew reinstall`
# always fetch the newest release with no manual version bumps here.
#
# Darwin-only. Import from hosts that should have OpenChamber (mbp, a6mbp).
{ ... }:
{
  flake.modules.darwin.openchamber = { config, pkgs, lib, ... }:
    let
      caskFile = pkgs.writeText "openchamber.rb" ''
        cask "openchamber" do
          arch arm: "aarch64", intel: "x86_64"

          version :latest
          sha256 :no_check

          url "https://github.com/openchamber/openchamber/releases/latest/download/OpenChamber.app-darwin-#{arch}.tar.gz",
              verified: "github.com/openchamber/openchamber/"
          name "OpenChamber"
          desc "Desktop and web interface for OpenCode AI agent"
          homepage "https://github.com/openchamber/openchamber"

          app "OpenChamber.app"
        end
      '';
    in
    {
      # preActivation runs before the homebrew activation phase, so the tap
      # directory is in place by the time `brew bundle` resolves the cask.
      # Runs as root; `su -` drops to the Homebrew-owning user to write under
      # /opt/homebrew (user-owned on Apple Silicon).
      system.activationScripts.preActivation.text = lib.mkAfter ''
        echo >&2 "Seeding bryan/local homebrew tap (openchamber)..."
        /usr/bin/su - ${config.system.primaryUser} -c '
          set -e
          TAP_DIR=/opt/homebrew/Library/Taps/bryan/homebrew-local
          mkdir -p "$TAP_DIR/Casks"
          install -m 0644 ${caskFile} "$TAP_DIR/Casks/openchamber.rb"
          # brew expects a tap to be a git repo; init an empty local one once.
          if [ ! -d "$TAP_DIR/.git" ]; then
            cd "$TAP_DIR"
            /usr/bin/git init -q
            /usr/bin/git add -A
            /usr/bin/git -c user.email=nix-darwin@local -c user.name=nix-darwin \
              commit -q -m seed >/dev/null 2>&1 || true
          fi
        '
      '';

      # Declare tap + cask so nix-darwin tracks them in the generated Brewfile
      # and `cleanup = "zap"` does not rip them out.
      homebrew.taps = [ "bryan/local" ];
      homebrew.casks = [ "bryan/local/openchamber" ];
    };
}
