# Host configuration: mbp (Personal MacBook Pro)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, personal desktop
# Host-specific: Personal apps (Bambu Studio, Steam, etc.)
{ inputs, ... }:
{
  flake.modules.darwin.mbp = { ... }: {
    imports = with inputs.self.modules.darwin; [
      fonts
      nix-settings
      zsh
      homebrew
      editors
      git
      cli-tools
      rust
      activation
      openchamber
      personal-desktop
      # Desktop features
      gaming
      # Service modules
      syncthing
      hermes
      obsidian-headless
    ];

    # === Core System Settings ===

    # Set primary user for homebrew and other user-specific options
    system.primaryUser = "bryan";

    # Platform
    nixpkgs.hostPlatform = "aarch64-darwin";

    # System state version
    system.stateVersion = 4;

    # Allow unfree packages
    nixpkgs.config.allowUnfree = true;

    # Add Homebrew to system PATH
    environment.systemPath = [ "/opt/homebrew/bin" ];

    # === Enable Services ===

    services.syncthing.enable = true;
    services.obsidian-headless = {
      enable = true;
      vaultPath = "/Users/bryan/second-brain";
    };
    services.tailscale.enable = true;
    services.hermes = {
      enable = true;
      clientOnly = true;
      desktop.enable = true;
    };

    # === Host-specific Homebrew Configuration ===

    homebrew = {
      # Additional brews
      brews = [
        "exercism"
        "pandoc" # For converting documents
        "texlive" # For converting MD to PDF
        "pinentry-mac" # GPG pinentry for macOS
        "podman" # Container runtime
        "podman-compose" # Compose for podman
        "syncthing"
      ];

      # Additional casks for this host
      casks = [
        "bambu-studio"
        "claude"
        "opencode-desktop" # OpenCode AI coding agent desktop app
        "qobuz"
        "zen"
      ];
    };
  };
}
