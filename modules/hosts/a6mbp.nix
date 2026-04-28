# Host configuration: a6mbp (Work MacBook Pro)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools
# Host-specific: Work tools (AWS, Docker, DDEV, Slack, Zoom, etc.)
{ inputs, ... }:
{
  flake.modules.darwin.a6mbp = { pkgs, ... }: {
    imports = with inputs.self.modules.darwin; [
      fonts
      nix-settings
      zsh
      homebrew
      editors
      git
      cli-tools
      activation
      openchamber
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

    # === Host-specific System Packages ===

    environment.systemPackages = with pkgs; [
      # Cloud & DevOps
      awscli2
      docker-compose
    ];

    # === Host-specific Homebrew Configuration ===

    homebrew = {
      # Additional taps for this host
      taps = [
        "ddev/ddev"
      ];

      # Additional brews for this host
      brews = [
        # DDEV for va.gov-cms development
        "ddev/ddev/ddev"
        "libfido2"     # FIDO2/U2F tools
        "pinentry-mac" # GPG pinentry for macOS
        "podman"       # Container runtime
        "syncthing"
        "ykman"    # YubiKey Manager CLI
      ];

      # Additional casks for this host
      casks = [
        "claude"
        "docker-desktop"
        "notion"
        "rectangle-pro"
        "slack"
        "superwhisper"
        "yubico-authenticator"
        "zoom"
      ];
    };
  };
}
