# Gaming setup
#
# Cross-platform gaming configuration. On macOS, installs Steam via Homebrew cask.
# On NixOS, configures the full gaming stack: Steam with Proton GE, gamescope,
# gamemode, 32-bit graphics support, and XIVLauncher for FFXIV.
{ inputs, ... }:
{
  # Darwin aspect - Steam Homebrew cask
  flake.modules.darwin.gaming = { ... }: {
    homebrew.casks = [
      "steam"
    ];
  };

  # NixOS aspect - full gaming stack
  flake.modules.nixos.gaming = { pkgs, lib, ... }: {
    # Enable graphics for gaming (Steam)
    hardware.graphics = {
      enable = true;
      enable32Bit = true;
    };

    # Enable gamemode
    programs.gamemode.enable = true;

    # Steam setup
    programs.steam = {
      enable = true;
      extraCompatPackages = [
        pkgs.proton-ge-bin
      ];
      gamescopeSession = {
        enable = true;
      };
      remotePlay.openFirewall = true;
      dedicatedServer.openFirewall = true;
    };

    # FFXIV launcher (XIVLauncher.Core) - manages its own Wine/DXVK
    environment.systemPackages = [
      pkgs.xivlauncher
    ];
  };
}
