# Shared Matrix desktop client. No Homebrew cask is available for Cinny.
{ ... }:
{
  flake.modules.darwin.cinny = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.cinny-desktop ];
  };

  flake.modules.nixos.cinny = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.cinny-desktop ];
  };
}
