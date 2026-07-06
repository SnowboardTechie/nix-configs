# Development module: Editor configuration
# Provides vim and neovim for both darwin and nixos systems
# NixOS additionally includes the Zed editor
{ inputs, ... }:
{
  flake.modules.darwin.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      neovim
      vim
    ];
    homebrew.casks = [
      "sublime-text"
    ];
  };

  flake.modules.nixos.editors = { pkgs, ... }: {
    environment.systemPackages = with pkgs; [
      neovim
      zed-editor
      vim
    ];
  };
}
