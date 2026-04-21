# Development module: CLI tools
# Provides common CLI utilities for both darwin and nixos systems
# Darwin: shared tools via Homebrew + nix-only packages via nixpkgs
# NixOS: all packages via nixpkgs
{ inputs, ... }:
{
  # Darwin aspect - hybrid delivery
  flake.modules.darwin.cli-tools = { pkgs, ... }: {
    # Homebrew brews for macOS compatibility
    homebrew.brews = [
      "bat"
      "biome"
      "bind"
      "direnv"
      "eza"
      "ffmpeg"
      "fzf"
      "git-lfs"
      "gnupg"
      "jq"
      "just"
      "lazydocker"
      "libpq"
      "marksman"
      "ncurses"
      "opencode"
      "pinentry-mac"
      "pipx"
      "poetry"
      "python"
      "redis"
      "shellcheck"
      "stow"
      "tlrc"
      "tmux"
      "worktrunk"
      "zoxide"
    ];

    # Nix-only packages (no homebrew equivalent)
    environment.systemPackages = with pkgs; [
      alacritty
      bun
      fd
      htop
      nix-direnv
      nixd
      ripgrep
      tea
      tree
      ttyper
      wget
      # Language servers
      lua-language-server
      graphql-language-service-cli
      pyright
      solargraph
      typescript
      typescript-language-server
      yaml-language-server
      vacuum-go
    ];
  };

  # NixOS aspect - all packages via nixpkgs
  flake.modules.nixos.cli-tools = { pkgs, ... }: {
    # All CLI tools as nix packages
    environment.systemPackages = (with pkgs; [
      alacritty
      bat
      biome
      bun
      direnv
      dnsutils # dig, nslookup (brew: bind)
      eza
      fd
      ffmpeg
      fzf
      gh-dash
      gnupg
      htop
      jq
      just
      lazydocker
      libpq
      marksman
      ncurses
      nix-direnv
      nixd
      opencode
      pipx
      poetry
      python3 # (brew: python)
      redis
      ripgrep
      shellcheck
      stow
      tea
      tlrc
      tmux
      tree
      ttyper
      vacuum-go
      wget
      zoxide
      # Language servers
      lua-language-server
      graphql-language-service-cli
      pyright
      solargraph
      typescript
      typescript-language-server
      yaml-language-server
    ]) ++ [
      # External flake inputs (not in nixpkgs)
      inputs.worktrunk.packages.${pkgs.stdenv.hostPlatform.system}.default
    ];
  };
}
