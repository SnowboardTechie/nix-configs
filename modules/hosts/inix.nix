# Host configuration: inix (Garage/shop iMac Pro on macOS)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, activation
# Services: syncthing, Tailscale, Hermes Desktop client
# Hardware: 2017 iMac Pro (Intel Xeon W) running macOS with nix-darwin
{ inputs, ... }:
{
  flake.modules.darwin.inix = { pkgs, ... }: {
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
      hermes
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
    services.tailscale.enable = true;
    services.hermes =
      let
        # The Hermes flake omits x86_64-darwin outputs. Instantiate its external
        # overlay with Hermes's own pinned nixpkgs so its Electron hashes align.
        hermesPkgs = import inputs.hermes-agent.inputs.nixpkgs {
          system = pkgs.stdenv.hostPlatform.system;
          overlays = [ inputs.hermes-agent.overlays.default ];
        };
      in
      {
        enable = true;
        clientOnly = true;
        desktop.enable = true;
        package = hermesPkgs.hermes-agent;
      };

    # === Host-specific Homebrew Configuration ===

    homebrew = {
      brews = [
        "pinentry-mac" # GPG pinentry for macOS
        "syncthing"
      ];

      casks = [
        "finicky" # Browser/URL router — github.com/johnste/finicky
        "opencode-desktop" # OpenCode AI coding agent desktop app
        "rectangle-pro"
        "slack"
        "vlc"
        "zen" # Zen Browser (host-level since not in base homebrew)
      ];
    };
  };
}
