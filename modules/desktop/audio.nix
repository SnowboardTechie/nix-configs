{ inputs, ... }:
{
  flake.modules.nixos.audio = { pkgs, lib, ... }: {
    # Enable sound with pipewire
    security.rtkit.enable = true;
    services.pipewire = {
      enable = true;
      alsa.enable = true;
      alsa.support32Bit = true;
      pulse.enable = true;
    };
  };
}
