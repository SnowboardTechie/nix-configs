# Host configuration: mbp (Personal MacBook Pro)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools
# Host-specific: Personal apps (Bambu Studio, Discord, Steam, etc.)
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
      # Desktop features
      gaming
      reaper
      # Service modules
      syncthing
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

    # === Host-specific Homebrew Configuration ===

    homebrew = {
      # Additional brews
      brews = [
        "exercism"
        "libfido2" # FIDO2/U2F tools
        "node"     # Includes npm and npx for MCP extensions
        "pandoc"   # For converting documents
        "texlive"  # For converting MD to PDF
        "pinentry-mac" # GPG pinentry for macOS
        "podman"   # Container runtime
        "syncthing"
        "ykman"    # YubiKey Manager CLI
      ];

      # Additional casks for this host
      casks = [
        "bambu-studio"
        "claude"
        "monal"
        "qobuz"
        "rectangle-pro"
        "slack"
        "yubico-authenticator"
        "zen"
        "zoom"
      ];
    };
  };
}
