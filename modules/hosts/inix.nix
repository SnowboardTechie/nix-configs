# Host configuration: inix (Garage/shop iMac Pro on macOS)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, activation
# Services: syncthing, Tailscale
# Hardware: 2017 iMac Pro (Intel Xeon W) running macOS with nix-darwin
{ inputs, ... }:
{
  flake.modules.darwin.inix = { pkgs, ... }:
    let
      installHermesIntelDesktop = pkgs.writeShellApplication {
        name = "install-hermes-intel-desktop";
        runtimeInputs = with pkgs; [
          coreutils
          curl
          git
          nodejs_22
          python312
        ];
        text = builtins.readFile ./scripts/install-hermes-intel-desktop.sh;
      };
    in
    {
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

      # The resulting app remains user-managed because npm/Electron packaging
      # and ad-hoc signing are mutable host-local operations.
      environment.systemPackages = [ installHermesIntelDesktop ];

      # === Enable Services ===

      services.syncthing.enable = true;
      services.tailscale.enable = true;
      # Official Hermes Desktop artifacts do not yet include Intel support.
      # The pinned local builder connects the user-managed app to Studio over Tailscale.

      # === Host-specific Homebrew Configuration ===

      homebrew = {
        brews = [
          "pinentry-mac" # GPG pinentry for macOS
          "syncthing"
        ];

        casks = [
          "opencode-desktop" # OpenCode AI coding agent desktop app
          "rectangle-pro"
          "slack"
          "superwhisper" # Voice dictation and transcription
          "vlc"
          "zen" # Zen Browser (host-level since not in base homebrew)
        ];
      };
    };
}
