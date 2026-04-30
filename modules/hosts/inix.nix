# Host configuration: inix (Garage/shop iMac Pro on macOS)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, activation
# Services: syncthing
# Hardware: 2017 iMac Pro (Intel Xeon W) running macOS with nix-darwin
{ inputs, ... }:
{
  flake.modules.darwin.inix = { ... }: {
    imports = with inputs.self.modules.darwin; [
      fonts
      nix-settings
      zsh
      homebrew
      editors
      git
      cli-tools
      activation
      # Service modules
      syncthing
    ];

    # === Core System Settings ===

    # Set primary user for homebrew and other user-specific options
    system.primaryUser = "bryan";

    # Platform — Intel Mac (iMac Pro 2017)
    nixpkgs.hostPlatform = "x86_64-darwin";

    # System state version
    system.stateVersion = 4;

    # Allow unfree packages
    nixpkgs.config.allowUnfree = true;

    # Add Homebrew to system PATH (Intel prefix)
    environment.systemPath = [ "/usr/local/bin" ];

    # === Enable Services ===

    services.syncthing.enable = true;

    # === Host-specific Homebrew Configuration ===

    homebrew = {
      brews = [
        "pinentry-mac" # GPG pinentry for macOS
        "syncthing"
      ];

      casks = [
        "slack"
        "vlc"
      ];
    };
  };
}
