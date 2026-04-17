# Base module: Homebrew configuration
# Provides Homebrew infrastructure and darwin-only packages.
# Feature modules (zsh.nix, git.nix, editors.nix, cli-tools.nix) contribute
# additional brews/casks via their darwin aspects.
# Note: Homebrew is darwin-only, no nixos aspect needed
{ ... }:
{
  flake.modules.darwin.homebrew = { ... }: {
    homebrew = {
      enable = true;

      onActivation = {
        autoUpdate = true;
        cleanup = "zap";
        upgrade = true;
      };

      # Common Homebrew taps
      taps = [
      ];

      # Darwin-only packages (no NixOS equivalent)
      brews = [
        "ca-certificates"
      ];

      # Common Homebrew casks (GUI applications)
      casks = [
        "font-meslo-lg-nerd-font"
        "obsidian"
        "sol"
        "vivaldi"
        "yaak"
      ];
    };
  };
}
